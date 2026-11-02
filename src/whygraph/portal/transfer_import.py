"""Project transfer import: apply a bundle to a scanned project (plan 42 section 5.7).

:func:`apply_import` is the one entry point, for the dry run and the apply
alike (D4): the dry run is the same work ending in an explicit
``session.rollback()`` of every transaction, so the preview of a request is
what that request would do.

Three stages (plan 42 Q11):

1. **Check.** Every member is format-checked before anything is written -
   the backup included: the small members from parsed records, the cache
   members (rationale cards, commit and file descriptions) streamed from the
   bundle so they are never all in memory. A format violation refuses the
   whole bundle (``400 bundle_invalid`` naming the member and line).
2. **Core.** One ``BEGIN IMMEDIATE`` transaction writes the import log row,
   the actors, chats (:func:`whygraph.serve.chat.import_sessions`), notes
   (:func:`whygraph.mcp.notes.import_notes`) and purpose runs
   (:func:`whygraph.portal.purpose_routes.import_runs`): all or nothing.
3. **Caches.** Rationale cards (:class:`whygraph.mcp.rationale_cache.RationaleImport`)
   and LLM descriptions (:class:`whygraph.analyze.backfill.DescriptionImport`)
   are re-read from the bundle and written in batches of
   :data:`CACHE_BATCH_ROWS`, each its own short ``BEGIN IMMEDIATE``
   transaction, so no single transaction holds the write lock for long
   (Q10: under 2 s). They are re-creatable caches where the target wins, so a
   batch that fails after the core commit (a lock wait past
   ``busy_timeout``, a full disk) stops the import without undoing what
   landed: the result marks the member ``partial`` and the log row's counts
   say what was written. Importing the same bundle again adds the rest.

Settings travel separately, in the portal's own Postgres database: they are
checked before the SQLite transaction (a save that is rolled back,
:func:`_preflight`) and saved after the SQLite commit when the importer
ticked "Apply these settings" - after the cache stage, whether or not it
finished (the caches do not depend on the settings). The two databases are
not one transaction, so a failure of that save answers with
``config.applied = false`` and never undoes the data import (R6).

The routes, slots, throttles, ``claim_project`` / ``is_busy`` and the audit
are :mod:`whygraph.portal.transfer_routes`' (step 9); this module is the work
the route runs in its worker thread.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import logging
import os
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, col, select, update

from whygraph.analyze.backfill import DescriptionImport, DescriptionImportResult
from whygraph.core.config import Config, merge_v2, normalize_v2
from whygraph.core.context import ProjectContext, use_project
from whygraph.core.import_checks import SINGLE_LINE_BAD, ImportFormatError, clean_text
from whygraph.core.safe_paths import UnsafePathError, check_inside
from whygraph.db.engine import get_session as project_session
from whygraph.db.models import Commit, ImportedActor, ProjectImport, ProjectNote
from whygraph.mcp import notes
from whygraph.mcp.errors import WhyGraphError
from whygraph.mcp.rationale_cache import (
    RationaleImport,
    RationaleImportResult,
    _model_key,
)
from whygraph.serve.chat import check_import_sessions, import_sessions

from . import db as portal_db
from . import transfer_bundle as tb
from .config_layers import load_layer
from .context import build_project_context
from .deps import ApiError, unsafe_path_error
from .models import Project
from .paths import check_project_db
from .policy import IMPORT_ALLOWLIST, filter_layer, invalid_values, put_allowlist
from .purpose_routes import check_import_runs, import_runs
from .routes import checked_project_layer, finish_project_layer
from .transfer_bundle import BundleError
from .transfer_export import origin_of

_log = logging.getLogger(__name__)

TRANSFER_CONFIG_ALLOWLIST = {k: v for k, v in IMPORT_ALLOWLIST.items() if k != "scan"}
"""D15: model and knowledge settings only. ``llm.model``, ``llm.<provider>.model``,
``[analyze]``, ``[rationale]``, ``[chat]`` (all minus the org-only keys, which
:data:`~whygraph.portal.policy.IMPORT_ALLOWLIST` already leaves out) and
``areas``. Never ``scan.*``, never a secret or a connection key."""

BACKUPS_KEPT = 3
"""Pre-import backups kept per project (plan 42 D9); older ones are deleted."""

BACKUP_PREFIX = "whygraph-import."
"""Name prefix of a pre-import backup: ``whygraph-import.<UTC stamp>.db``."""

ACTOR_NAME_MAX_CHARS = 100
"""An imported display name is cut to this many characters (plan 42 section 5.1)."""

CACHE_BATCH_ROWS = 10_000
"""Cache records written per transaction (plan 42 Q11).

Sized from ``plans/42-eval/lock_time.py``: a batch of rationale cards or
descriptions holds the write lock for well under 0.5 s on the dev machine,
so a scan or a chat turn waiting on ``busy_timeout`` waits briefly."""

PARTIAL_ERROR = (
    "The import stopped before this part finished: the project's database was "
    "busy or could not be written. What was imported stays; import the bundle "
    "again to add the rest."
)
"""The ``error`` of a cache block marked ``partial`` (generic: never a value)."""

_ACTOR_UID_MAX = 200
_IN_CHUNK = 500
_T = TypeVar("_T")


def transfer_config_layer(layer: Mapping[str, Any], mode: str | None) -> dict:
    """Keep the part of a bundle's config a transfer may apply (D15).

    Parameters
    ----------
    layer : Mapping
        The (normalized) project layer from a bundle's ``config.json``.
    mode : str or None
        The target portal's mode; the result is also limited to that mode's
        project ``PUT`` allowlist.

    Returns
    -------
    dict
        A new layer with only the allowed keys. Dropped keys are not reported.
    """
    kept, _ = filter_layer(layer, TRANSFER_CONFIG_ALLOWLIST)
    kept, _ = filter_layer(kept, put_allowlist(mode))
    return _drop_null_tables(kept, TRANSFER_CONFIG_ALLOWLIST)


def _drop_null_tables(layer: Mapping[str, Any], spec: Mapping[str, Any]) -> dict:
    """``layer`` without a ``null`` where the allowlist has a table.

    The policy filter keeps a ``null`` table ("reset it"), which on a PUT
    is the user's own choice. In a bundle it would reset the keys D15 never
    lets travel too (``llm.provider``, an endpoint) and, through the
    endpoint change, delete the project's stored API key. A bundle may only
    set values, so a ``null`` table is dropped; a ``null`` leaf stays.
    """
    out: dict = {}
    for key, value in layer.items():
        rule = spec.get(key)
        if isinstance(rule, Mapping):
            if value is None:
                continue
            value = _drop_null_tables(value, rule)
            if not value:
                continue
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _needs_scan() -> BundleError:
    """``409 transfer_needs_scan``: the target has no scanned history yet."""
    return BundleError(
        409,
        "This project has not been scanned yet: run its first scan, then import "
        "the bundle.",
        code="transfer_needs_scan",
    )


def _wrong_repo(source_origin: str | None, target_origin: str | None) -> BundleError:
    """``409 bundle_wrong_repo`` naming both origins and the shallow-clone hint."""
    source = source_origin or "an unnamed repository"
    target = target_origin or "an unnamed repository"
    return BundleError(
        409,
        f"This bundle is from another repository: it states {source}, and this "
        f"project is {target}. None of the root commits the bundle states is a "
        "commit of this project. If this project is a shallow clone, fetch its "
        "full history and scan it again.",
        code="bundle_wrong_repo",
    )


def _format_error(member: str, exc: ImportFormatError) -> BundleError:
    """``400 bundle_invalid`` for a domain format error, naming member and line only."""
    where = member if exc.index is None else f"{member} line {exc.index + 1}"
    return BundleError(
        400,
        f"Invalid bundle: {where}: {exc.field}: {exc.reason}",
        code="bundle_invalid",
    )


@contextmanager
def _member(member: str | Callable[[ImportFormatError], str]) -> Iterator[None]:
    """Turn an :class:`ImportFormatError` into ``bundle_invalid`` for ``member``.

    ``index`` on the error is the 0-based position in the list the helper was
    given, which is the member's records in line order, so line = index + 1.
    """
    try:
        yield
    except ImportFormatError as exc:
        name = member(exc) if callable(member) else member
        raise _format_error(name, exc) from None


def _chat_member(exc: ImportFormatError) -> str:
    """The chat member a chat import error came from (``session.`` / ``message.``)."""
    return tb.CHAT_MESSAGES if exc.field.startswith("message.") else tb.CHAT_SESSIONS


# ---------------------------------------------------------------------------
# Reading the bundle (outside the write lock)
# ---------------------------------------------------------------------------


@dataclass
class _Records:
    """The core members as plain dicts, in line order (line = index + 1).

    The cache members are not here: they are streamed from the bundle
    (:func:`_stream`), once to check and once to write.
    """

    actors: list[dict[str, Any]]
    notes: list[dict[str, Any]]
    runs: list[dict[str, Any]]
    sessions: list[dict[str, Any]]
    messages: list[dict[str, Any]]


def _records(bundle: tb.Bundle) -> _Records:
    """Parse the core members into dicts, so the domain helpers only check and write.

    :meth:`~whygraph.portal.transfer_bundle.Bundle.iter_records` yields one
    record per line (the verification pass refused blank lines), so a
    record's list position plus one is its line number.
    """

    def rows(name: str) -> list[dict[str, Any]]:
        return [record.model_dump() for _, record in bundle.iter_records(name)]

    return _Records(
        actors=rows(tb.ACTORS),
        notes=rows(tb.NOTES),
        runs=rows(tb.PURPOSE_RUNS),
        sessions=rows(tb.CHAT_SESSIONS),
        messages=rows(tb.CHAT_MESSAGES),
    )


def _stream(bundle: tb.Bundle, name: str) -> Iterator[BaseModel]:
    """A member's records, parsed one line at a time (position + 1 = line)."""
    for _, record in bundle.iter_records(name):
        yield record


@dataclass
class _Caches:
    """The cache members' import jobs and what their check pass left out."""

    rationale: RationaleImport
    descriptions: DescriptionImport
    rationale_drop: set[int] = field(default_factory=set)
    commit_drop: set[int] = field(default_factory=set)
    file_drop: set[int] = field(default_factory=set)


def _check_all(
    bundle: tb.Bundle,
    records: _Records,
    *,
    keep_status: bool,
    now: datetime,
    cache_key: tuple[str, str],
) -> _Caches:
    """Format-check every member before anything is written (plan 42 Q11).

    The members are checked in the order the import writes them, so the
    first violation named is the one the old single transaction met first.

    Raises
    ------
    BundleError
        ``400 bundle_invalid`` naming the member and line.
    """
    with _member(tb.ACTORS):
        _actor_rows(records.actors, 0)
    with _member(_chat_member):
        check_import_sessions(records.sessions, records.messages)
    with _member(tb.NOTES):
        notes.check_import_notes(records.notes, keep_status=keep_status)
    with _member(tb.PURPOSE_RUNS):
        check_import_runs(records.runs, now=now)
    caches = _Caches(
        rationale=RationaleImport(now=now, cache_key=cache_key),
        descriptions=DescriptionImport(),
    )
    with _member(tb.RATIONALE), closing(_stream(bundle, tb.RATIONALE)) as stream:
        caches.rationale_drop = caches.rationale.check(stream)
    with (
        _member(tb.COMMIT_DESCRIPTIONS),
        closing(_stream(bundle, tb.COMMIT_DESCRIPTIONS)) as stream,
    ):
        caches.commit_drop = caches.descriptions.check_commits(stream)
    with (
        _member(tb.FILE_DESCRIPTIONS),
        closing(_stream(bundle, tb.FILE_DESCRIPTIONS)) as stream,
    ):
        caches.file_drop = caches.descriptions.check_files(stream)
    return caches


def _actor_rows(records: Sequence[Mapping[str, Any]], import_id: int) -> list[dict]:
    """``imported_actor`` rows for ``actors.jsonl`` (first name of a uid wins).

    Raises
    ------
    ImportFormatError
        An empty, prefixed, over-long or unprintable uid, or an empty or
        unprintable display name; ``index`` is the record position.
    """
    rows: dict[str, dict[str, Any]] = {}
    for index, rec in enumerate(records):
        try:
            uid = rec.get("uid")
            if not isinstance(uid, str) or not uid:
                raise ImportFormatError("uid", "must be a non-empty uid")
            if uid.startswith(notes.IMPORTED_UID_PREFIX):
                raise ImportFormatError("uid", "must not carry the imported: prefix")
            if (
                clean_text(uid, multiline=False, max_chars=_ACTOR_UID_MAX, field="uid")
                is None
            ):
                raise ImportFormatError(
                    "uid", f"must be at most {_ACTOR_UID_MAX} characters"
                )
            raw_name = rec.get("display_name")
            if not isinstance(raw_name, str):
                raise ImportFormatError("display_name", "must be text")
            # A name is display-only: characters a single-line field refuses
            # (controls, bidi marks such as the RLM in a real Hebrew or Arabic
            # name, separators, lone surrogates) are dropped, not refused.
            name = SINGLE_LINE_BAD.sub("", raw_name)
            name = name.strip()[:ACTOR_NAME_MAX_CHARS].strip()
            if not name:
                raise ImportFormatError("display_name", "must not be empty")
        except ImportFormatError as exc:
            exc.index = index
            raise
        stored = notes.IMPORTED_UID_PREFIX + uid
        rows.setdefault(
            stored, {"uid": stored, "display_name": name, "import_id": import_id}
        )
    return list(rows.values())


# ---------------------------------------------------------------------------
# Settings (the portal's Postgres database)
# ---------------------------------------------------------------------------


@dataclass
class _Settings:
    """The outcome of the settings preflight."""

    present: bool = False
    layer: dict[str, Any] = field(default_factory=dict)
    invalid: list[dict[str, str]] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    config: Config | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the settings may be applied."""
        return (
            self.present
            and not self.invalid
            and self.error is None
            and self.config is not None
        )


def _leaves(layer: Mapping[str, Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Every non-table value of ``layer`` with its dotted key, in key order."""
    for key in sorted(layer):
        value = layer[key]
        path = f"{prefix}{key}"
        if isinstance(value, Mapping) and value:
            yield from _leaves(value, f"{path}.")
        else:
            yield path, value


def _lookup(layer: Mapping[str, Any], path: str) -> Any:
    """The value at dotted ``path`` in ``layer``, or ``None``."""
    node: Any = layer
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _changes(
    current: Mapping[str, Any], merged: Mapping[str, Any], bundle: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """The keys the bundle's layer changes in the project layer, with both values."""
    out = []
    for path, _ in _leaves(bundle):
        before, after = _lookup(current, path), _lookup(merged, path)
        if before != after:
            out.append({"key": path, "from": before, "to": after})
    return out


def _error_text(exc: BaseException) -> str:
    """The message of a refused settings save (an ``ApiError`` or a config error)."""
    if isinstance(exc, ApiError):
        return exc.error
    return str(exc) or type(exc).__name__


def _preflight(
    state: Any, project: Any, raw: Mapping[str, Any] | None, mode: str | None
) -> _Settings:
    """Check the bundle's settings by saving them and rolling back (plan 42 section 5.6).

    The bundle's layer is filtered to D15, value-checked, and merged over the
    target's current project layer (the bundle wins, Q9). The merged layer
    goes through the ``PUT`` path's own check and save
    (:func:`~whygraph.portal.routes.checked_project_layer`) and a context
    build, then the portal session is rolled back. Never raises for bad
    settings: they land in ``invalid`` / ``error``.
    """
    if raw is None:
        return _Settings()
    settings = _Settings(present=True)
    try:
        normalized, _ = normalize_v2(raw, project.root)
    except Exception as exc:  # noqa: BLE001 - untrusted input; reported, not raised
        settings.error = _error_text(exc)
        return settings
    settings.layer = transfer_config_layer(normalized, mode)
    settings.invalid = [
        {"key": key, "reason": reason}
        for key, reason in sorted(invalid_values(settings.layer).items())
    ]
    with portal_db.get_session() as session:
        try:
            current = load_layer(session, project.id, org_id=project.org_id)
            merged = merge_v2(current, settings.layer)
            settings.changes = _changes(current, merged, settings.layer)
            if settings.invalid:
                return settings
            row = session.get(Project, project.id)
            if row is None:
                raise ApiError(404, f"project {project.slug!r} not found")
            checked_project_layer(session, state, project, merged, linked=False)
            settings.config = build_project_context(session, row).config
        except Exception as exc:  # noqa: BLE001 - reported in the preview
            settings.error = _error_text(exc)
        finally:
            session.rollback()
    return settings


def _apply_settings(
    state: Any, project: Any, settings: _Settings
) -> tuple[bool, str | None]:
    """Save the bundle's settings after the data commit; never raises (R6).

    The target's layer is re-read, so a change made since the preflight is
    kept wherever the bundle does not set the key.

    Returns
    -------
    tuple of (bool, str or None)
        Whether the layer was saved, and the error when it was not.
    """
    old_hooks = project.ctx.config.scan_hooks
    try:
        with portal_db.get_session() as session:
            row = session.get(Project, project.id)
            if row is None:
                raise ApiError(404, f"project {project.slug!r} not found")
            current = load_layer(session, project.id, org_id=project.org_id)
            merged = merge_v2(current, settings.layer)
            checked_project_layer(session, state, project, merged, linked=False)
            new_ctx = build_project_context(session, row)
            session.commit()
    except Exception as exc:  # noqa: BLE001 - R6: the data import stands
        _log.warning(
            "transfer import into project %s: settings not applied: %s",
            project.id,
            type(exc).__name__,
        )
        return False, _error_text(exc)
    try:
        finish_project_layer(state, project, new_ctx, old_hooks)
    except Exception:  # noqa: BLE001 - saved; the cache invalidation is best effort
        _log.exception("transfer import into project %s: settings saved", project.id)
    return True, None


# ---------------------------------------------------------------------------
# The pre-import backup (plan 42 D9)
# ---------------------------------------------------------------------------


@dataclass
class BackupResult:
    """What :func:`backup_project_db` did.

    Attributes
    ----------
    written : bool
        A backup file was written.
    skipped_low_disk : bool
        No backup: the free space is under twice the database's size.
    path : Path or None
        The backup file, when written.
    """

    written: bool = False
    skipped_low_disk: bool = False
    path: Path | None = None


def _backup_stamp(now: datetime) -> str:
    """A sortable UTC stamp to the microsecond: ``20261101T100000.123456Z``."""
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _prune_backups(backups: Path, keep: int) -> None:
    """Delete all but the newest ``keep`` pre-import backups (regular files only)."""
    found = sorted(
        entry
        for entry in backups.iterdir()
        if entry.name.startswith(BACKUP_PREFIX)
        and entry.name.endswith(".db")
        and entry.is_file()
        and not entry.is_symlink()
    )
    for entry in found[: max(len(found) - keep, 0)]:
        try:
            entry.unlink()
        except OSError:
            _log.warning("could not delete the old import backup %s", entry.name)


def backup_project_db(
    db_path: Path,
    data_dir: Path,
    *,
    write: bool = True,
    now: datetime | None = None,
) -> BackupResult:
    """Copy a project's WhyGraph DB aside before an import (plan 42 D9).

    The copy is ``<project data dir>/backups/whygraph-import.<UTC stamp to
    the microsecond>.db``, created ``0600`` and never through a symlink,
    with SQLite's online backup API (a consistent copy, WAL included). The
    newest :data:`BACKUPS_KEPT` are kept. It mirrors
    :func:`whygraph.portal.migrate._backup`, which keeps every backup under
    another name.

    Parameters
    ----------
    db_path : Path
        The project's WhyGraph DB (in the data directory).
    data_dir : Path
        The portal's data directory, spelled as the DB path was built.
    write : bool, optional
        ``False`` only decides whether a backup would be skipped for low
        disk space (the dry run's preview), writing nothing.
    now : datetime, optional
        The stamp's time (default: now).

    Returns
    -------
    BackupResult
        Whether it was written, or skipped for low disk space.

    Raises
    ------
    whygraph.core.safe_paths.UnsafePathError
        When the DB or the backup path leaves the data directory or passes
        a symlink (a symlinked ``backups/`` included).
    OSError
        When the copy cannot be written.
    """
    check_project_db(data_dir, db_path)
    backups = db_path.parent / "backups"
    check_inside(data_dir, backups)
    size = sum(
        p.stat().st_size
        for p in (db_path, db_path.with_name(db_path.name + "-wal"))
        if p.is_file()
    )
    probe = backups if backups.is_dir() else db_path.parent
    if shutil.disk_usage(probe).free < 2 * size:
        return BackupResult(skipped_low_disk=True)
    if not write:
        return BackupResult()
    backups.mkdir(mode=0o700, exist_ok=True)
    dest = (
        backups
        / f"{BACKUP_PREFIX}{_backup_stamp(now or datetime.now(timezone.utc))}.db"
    )
    check_inside(data_dir, dest)
    fd = os.open(
        dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    os.close(fd)
    try:
        src = sqlite3.connect(db_path)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        os.chmod(dest, 0o600)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    _prune_backups(backups, BACKUPS_KEPT)
    return BackupResult(written=True, path=dest)


# ---------------------------------------------------------------------------
# The import
# ---------------------------------------------------------------------------


def _is_scanned(project_row: Any) -> bool:
    """Whether the target has a finished scan and at least one commit row."""
    if project_row.last_scanned_head is None:
        return False
    with project_session() as session:
        return session.exec(select(Commit.sha).limit(1)).first() is not None


def _matched_roots(roots: Sequence[str]) -> int:
    """How many of the bundle's stated root commits are commit rows here (F8)."""
    with project_session() as session:
        return len(
            session.exec(select(Commit.sha).where(col(Commit.sha).in_(roots))).all()
        )


def _has_live_purpose(session: Session) -> bool:
    """Whether the target holds a proposed or confirmed purpose note (D11)."""
    return (
        session.exec(
            select(ProjectNote.id)
            .where(
                col(ProjectNote.kind) == notes.PURPOSE_KIND,
                col(ProjectNote.status).in_(("proposed", "confirmed")),
            )
            .limit(1)
        ).first()
        is not None
    )


def _area_resolves(name: str | None) -> bool:
    """Whether area ``name`` resolves under the bound project's config."""
    # Function-local: chat/__init__ -> chat.tools -> mcp.rationale -> mcp.notes.
    from whygraph.chat.areas import resolve_area

    try:
        resolve_area(name or "")
    except WhyGraphError:
        return False
    return True


def _wide_notes(
    session: Session, note_ids: Sequence[int], contexts: Sequence[ProjectContext]
) -> list[dict[str, Any]]:
    """The inserted confirmed notes that reach (nearly) every agent session (Q5).

    A note is listed when :func:`~whygraph.mcp.notes.is_wide_anchor` holds
    under **any** of ``contexts`` (the current config, and the post-merge one
    when the bundle's settings passed the preflight, so the preview covers
    both answers to "Apply these settings"), or when it is an ``area`` note
    whose area resolves under none of them (narrow today, wide as soon as
    someone defines the area broadly). Full text, verbatim.
    """
    rows: list[ProjectNote] = []
    for start in range(0, len(note_ids), _IN_CHUNK):
        chunk = list(note_ids[start : start + _IN_CHUNK])
        rows.extend(
            session.exec(
                select(ProjectNote).where(col(ProjectNote.id).in_(chunk))
            ).all()
        )
    rows.sort(key=lambda r: r.id or 0)
    out: list[dict[str, Any]] = []
    for row in rows:
        wide = False
        resolved = row.anchor_kind != "area"
        for ctx in contexts:
            with use_project(ctx):
                wide = wide or notes.is_wide_anchor(row)
                if not resolved:
                    resolved = _area_resolves(row.area)
        if not wide and resolved:
            continue
        out.append(
            {
                "kind": row.kind,
                "anchor": row.anchor_kind,
                "target": row.area if row.anchor_kind == "area" else row.anchor_path,
                "body": row.body,
                "unresolved_area": not resolved,
            }
        )
    return out


def _settings_block(
    settings: _Settings, *, applied: bool, error: str | None
) -> dict[str, Any]:
    """The ``config`` block of the result (plan 42 section 7.2)."""
    return {
        "present": settings.present,
        "invalid": settings.invalid,
        "changes": settings.changes,
        "applied": applied,
        "error": error if error is not None else settings.error,
    }


def _batched(rows: Iterable[_T], size: int) -> Iterator[list[_T]]:
    """``rows`` in lists of at most ``size``."""
    it = iter(rows)
    while batch := list(itertools.islice(it, size)):
        yield batch


def _write_batch(write: Callable[[Session], None], *, dry_run: bool) -> float:
    """Run ``write`` in its own ``BEGIN IMMEDIATE`` transaction; the seconds the lock was held.

    Commits, or rolls back on a dry run. Raises whatever the database
    raises (``database is locked`` past ``busy_timeout``, a full disk), with
    the transaction rolled back.
    """
    with project_session() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        locked_at = time.perf_counter()
        write(session)
        if dry_run:
            session.rollback()
        else:
            session.commit()
        return time.perf_counter() - locked_at


@dataclass
class _CacheOutcome:
    """What the cache stage did: the blocks it did not finish, and its lock times."""

    partial: set[str] = field(default_factory=set)
    batches: int = 0
    longest: float = 0.0


def _apply_caches(
    bundle: tb.Bundle,
    caches: _Caches,
    *,
    import_id: int,
    dry_run: bool,
    project_id: int,
) -> _CacheOutcome:
    """Write the cache members in batches after the core commit (plan 42 Q11).

    Rationale cards, then commit descriptions, then file descriptions, each
    re-read from the bundle and written :data:`CACHE_BATCH_ROWS` records per
    transaction. A batch that raises is rolled back and stops the stage:
    earlier batches stay, its counts are restored to what landed, and the
    block it was writing and every later one are reported ``partial``.
    Never raises for a database error.
    """
    rationale, descriptions = caches.rationale, caches.descriptions
    steps: list[tuple[str, Callable[[], Iterator[Any]], Callable[..., None], Any]] = [
        (
            "rationale",
            lambda: rationale.rows(
                _stream(bundle, tb.RATIONALE), caches.rationale_drop
            ),
            lambda session, batch: rationale.insert(
                session, batch, import_id=import_id
            ),
            rationale,
        ),
        (
            "descriptions",
            lambda: descriptions.commit_rows(
                _stream(bundle, tb.COMMIT_DESCRIPTIONS), caches.commit_drop
            ),
            descriptions.fill_commits,
            descriptions,
        ),
        (
            "descriptions",
            lambda: descriptions.file_rows(
                _stream(bundle, tb.FILE_DESCRIPTIONS), caches.file_drop
            ),
            descriptions.fill_files,
            descriptions,
        ),
    ]
    outcome = _CacheOutcome()
    for block, rows, write, job in steps:
        if outcome.partial:
            outcome.partial.add(block)
            continue
        with closing(rows()) as stream:
            for batch in _batched(stream, CACHE_BATCH_ROWS):
                landed = dataclasses.replace(job.result)
                try:
                    held = _write_batch(
                        lambda session, batch=batch: write(session, batch),
                        dry_run=dry_run,
                    )
                except Exception as exc:  # noqa: BLE001 - Q11: reported, never raised
                    job.result = landed
                    outcome.partial.add(block)
                    _log.warning(
                        "transfer import into project %s: a %s batch failed (%s); "
                        "the earlier batches stay",
                        project_id,
                        block,
                        type(exc).__name__,
                    )
                    break
                outcome.batches += 1
                outcome.longest = max(outcome.longest, held)
    return outcome


def _cache_block(
    result: RationaleImportResult | DescriptionImportResult, *, partial: bool
) -> dict[str, Any]:
    """A cache member's counts block; ``partial`` and ``error`` only when it did not finish."""
    block: dict[str, Any] = result.as_dict()
    if partial:
        block.update(partial=True, error=PARTIAL_ERROR)
    return block


def _record_counts(import_id: int, counts: Mapping[str, Any], project_id: int) -> None:
    """Write the final counts to the import's log row; never raises (logged)."""
    try:
        with project_session() as session:
            session.exec(  # type: ignore[call-overload]
                update(ProjectImport)
                .where(col(ProjectImport.id) == import_id)
                .values(counts=json.dumps(counts, sort_keys=True))
            )
    except Exception as exc:  # noqa: BLE001 - the data stands; the log row lags
        _log.warning(
            "transfer import into project %s: could not record the final counts (%s)",
            project_id,
            type(exc).__name__,
        )


def apply_import(
    project_row: Project,
    bound_project: Any,
    bundle: tb.Bundle,
    *,
    uid: str,
    keep_status: bool,
    apply_config: bool,
    dry_run: bool,
    mode: str | None,
    state: Any,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Import a verified bundle into a scanned project, or preview it (plan 42 section 5.7).

    In order: (1) refuse an unscanned target; (2) the repository gate - one
    of the bundle's stated root commits must be a commit here; (3) the
    settings preflight (a rolled-back save); format-check **every** member
    (Q11); (5) the pre-import backup, unless ``dry_run``; (6) the core
    ``BEGIN IMMEDIATE`` transaction: (4) the purpose decision (D11, once,
    before any insert), the ``project_import`` log row, ``imported_actor``,
    chats, notes, purpose runs and the wide confirmed notes; (7) roll it back
    on ``dry_run``, else commit; then the cache batches (rationale cards,
    commit and file descriptions, each batch its own transaction, rolled
    back on ``dry_run``) and, on an apply, the final counts on the log row;
    (8) save the settings when asked and the preflight passed, even when a
    cache member is ``partial``; (9) the result.

    Synchronous and blocking: the route runs it in a worker thread, inside
    ``claim_project``, its slots and after ``is_busy`` (step 9), all of which
    it holds across every batch. It binds ``bound_project.ctx`` itself; the
    domain helpers write through the session of the transaction they run in,
    and none opens its own.

    Parameters
    ----------
    project_row : Project
        The target's portal ``projects`` row (``last_scanned_head``,
        ``remote_url``, ``source``, ``root``).
    bound_project : BoundProject
        The target as the request bound it (``id``, ``org_id``, ``slug``,
        ``root``, ``ctx``, ``db_path``, ``source``).
    bundle : Bundle
        The verified bundle (:func:`~whygraph.portal.transfer_bundle.read_bundle`).
    uid : str
        The importer's ``users.uid``: owner of the imported chats, the
        ``project_import`` log's ``imported_by``.
    keep_status : bool
        Keep confirmed notes confirmed (the importer may configure the
        project, Q2); ``False`` lands them proposed.
    apply_config : bool
        Save the bundle's settings after the data commit (Q9).
    dry_run : bool
        Do everything, then roll back: the preview (D4).
    mode : str or None
        The portal's mode (``"local"`` / ``"production"``): the settings
        allowlist, and whether the backup's path is returned.
    state : PortalState
        The portal state (the settings save and the context cache).
    data_dir : Path, optional
        The portal data directory (default :func:`whygraph.portal.db.data_dir`).

    Returns
    -------
    dict
        The ``ImportResult`` of plan 42 section 7.2, the same shape for the
        dry run and the apply. When a cache batch failed after the core
        commit, the ``rationale`` and/or ``descriptions`` block also holds
        ``"partial": true`` and a generic ``error``, and its counts are what
        landed (Q11); the call does not raise for it.

    Raises
    ------
    BundleError
        ``409 transfer_needs_scan``, ``409 bundle_wrong_repo``, or
        ``400 bundle_invalid`` naming the member and line of a format
        violation (nothing is written, no backup either).
    ApiError
        ``409 unsafe_path`` when the backup path passes a symlink;
        ``403 managed_on_platform`` for a linked project.
    """
    if bound_project.source == "platform":
        raise ApiError(
            403,
            "Project transfer is managed on the platform for a linked project.",
            code="managed_on_platform",
        )
    manifest = bundle.manifest
    ctx: ProjectContext = bound_project.ctx
    with use_project(ctx):
        # 1-2. A scanned target of the same repository.
        if not _is_scanned(project_row):
            raise _needs_scan()
        matched = _matched_roots(manifest.repo.root_commits)
        target_origin = origin_of(project_row)
        if matched == 0:
            raise _wrong_repo(manifest.repo.origin, target_origin)

        # 3. Settings preflight; the post-merge Config feeds the card key and Q5.
        settings = _preflight(state, bound_project, bundle.config, mode)
        will_apply = apply_config and settings.ok
        key_config = settings.config if will_apply and settings.config else ctx.config
        provider, pinned = key_config.cache_identity("rationale")
        cache_key = (provider, _model_key(pinned))
        contexts = [ctx]
        if settings.ok and settings.config is not None:
            contexts.append(dataclasses.replace(ctx, config=settings.config))

        # Parse the core members; check every member before anything is written.
        records = _records(bundle)
        now = datetime.now(timezone.utc)
        caches = _check_all(
            bundle, records, keep_status=keep_status, now=now, cache_key=cache_key
        )

        # 5. The backup (D9); the dry run only says whether it would be skipped.
        try:
            backup = backup_project_db(
                bound_project.db_path,
                data_dir if data_dir is not None else portal_db.data_dir(),
                write=not dry_run,
            )
        except UnsafePathError as exc:
            raise unsafe_path_error(exc) from None

        # 6-7. The core transaction: all or nothing.
        with project_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            locked_at = time.perf_counter()
            # 4. D11: decided once, under the lock, before the first insert.
            purpose_allowed = not _has_live_purpose(session)
            log_row = ProjectImport(
                bundle_sha256=bundle.sha256,
                imported_by=uid,
                imported_at=now.isoformat(),
                source_mode=manifest.source.mode,
                source_label=manifest.source.label,
                whygraph_version=manifest.whygraph_version,
                counts="{}",
            )
            session.add(log_row)
            session.flush()
            import_id = int(log_row.id or 0)
            with _member(tb.ACTORS):
                actors = _actor_rows(records.actors, import_id)
            if actors:
                session.connection().execute(
                    sqlite_insert(ImportedActor).on_conflict_do_nothing(), actors
                )
            with _member(_chat_member):
                chats = import_sessions(
                    session,
                    records.sessions,
                    records.messages,
                    owner_uid=uid,
                    import_id=import_id,
                )
            with _member(tb.NOTES):
                note_result = notes.import_notes(
                    session,
                    records.notes,
                    keep_status=keep_status,
                    actor=uid,
                    chat_ids=chats.chat_ids,
                    purpose_allowed=purpose_allowed,
                )
            with _member(tb.PURPOSE_RUNS):
                runs = import_runs(
                    session, records.runs, note_ids=note_result.inserted_ids, now=now
                )
            del records
            core = {
                "notes": note_result.counts(),
                "purpose_runs": {"inserted": runs},
                "chats": chats.counts(),
            }
            # Until the cache stage records what landed, the log row says
            # the caches are not done (a crash in between leaves it so).
            log_row.counts = json.dumps(
                {
                    **core,
                    "rationale": _cache_block(caches.rationale.result, partial=True),
                    "descriptions": _cache_block(
                        caches.descriptions.result, partial=True
                    ),
                },
                sort_keys=True,
            )
            session.add(log_row)
            session.flush()
            wide = _wide_notes(session, note_result.confirmed_ids, contexts)
            if dry_run:
                session.rollback()
            else:
                session.commit()
            held = time.perf_counter() - locked_at

        # The caches, in batches of their own (Q11).
        started = time.perf_counter()
        outcome = _apply_caches(
            bundle,
            caches,
            import_id=import_id,
            dry_run=dry_run,
            project_id=bound_project.id,
        )
        _log.info(
            "transfer import into project %s (%s): write lock held %.3f s; "
            "%d cache batches, longest lock %.3f s, %.3f s in all",
            bound_project.id,
            "dry run" if dry_run else "apply",
            held,
            outcome.batches,
            outcome.longest,
            time.perf_counter() - started,
        )
        counts = {
            "notes": core["notes"],
            "purpose_runs": core["purpose_runs"],
            "rationale": _cache_block(
                caches.rationale.result, partial="rationale" in outcome.partial
            ),
            "descriptions": _cache_block(
                caches.descriptions.result,
                partial="descriptions" in outcome.partial,
            ),
            "chats": core["chats"],
        }
        if not dry_run:
            _record_counts(import_id, counts, bound_project.id)

    # 8. Settings, after the data (a partial cache stage does not stop them).
    applied, error = False, None
    if will_apply and not dry_run and settings.changes:
        applied, error = _apply_settings(state, bound_project, settings)

    # 9. The result.
    return {
        "applied": not dry_run,
        "source": {
            "mode": manifest.source.mode,
            "label": manifest.source.label,
            "whygraph_version": manifest.whygraph_version,
            "exported_at": manifest.exported_at,
        },
        "repo": {
            "matched_roots": matched,
            "source_origin": manifest.repo.origin,
            "target_origin": target_origin,
        },
        **counts,
        "wide_confirmed": wide,
        "config": _settings_block(settings, applied=applied, error=error),
        "backup": {
            "written": backup.written,
            "skipped_low_disk": backup.skipped_low_disk,
            "path": (
                str(backup.path)
                if backup.path is not None and mode != "production"
                else None
            ),
        },
    }


__all__ = [
    "ACTOR_NAME_MAX_CHARS",
    "BACKUPS_KEPT",
    "BACKUP_PREFIX",
    "CACHE_BATCH_ROWS",
    "PARTIAL_ERROR",
    "TRANSFER_CONFIG_ALLOWLIST",
    "BackupResult",
    "apply_import",
    "backup_project_db",
    "transfer_config_layer",
]
