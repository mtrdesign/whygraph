"""Project transfer export (plan 42 section 5.7, step 7).

:func:`build_export` reads one project's WhyGraph DB in a single read
transaction and writes a bundle (:mod:`whygraph.portal.transfer_bundle`) to a
0600 temp file under ``<data>/tmp``. The route streams the file and unlinks it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlmodel import Session, col, select

from whygraph.core.safe_paths import UnsafePathError
from whygraph.db.engine import get_session as project_session
from whygraph.db.models import (
    ChatMessage,
    ChatSession,
    Commit,
    CommitFileChange,
    ImportedActor,
    ImportedChat,
    ProjectNote,
    ProjectPurposeRun,
    RationaleCache,
)
from whygraph.mcp.notes import AGENT_SOURCES, IMPORTED_UID_PREFIX
from whygraph.mcp.rationale_cache import decode_items
from whygraph.serve.chat import native_origin_key
from whygraph.serve.errors import ServeError
from whygraph.services.git.credentials import (
    PROVIDER_KEY_PATTERN,
    TOKEN_PATTERN,
    strip_userinfo,
)
from whygraph.services.github import remote_identity

from . import transfer_bundle as tb
from .config_layers import ConfigPolicyError, find_secret_paths, load_layer
from .context import resolve_root
from .db import get_session as portal_session
from .models import Project, User

_log = logging.getLogger(__name__)

TMP_PREFIX = "transfer-"
"""Name prefix of every transfer temp file in ``<data>/tmp`` (exports and uploads)."""

SWEEP_AGE_SEC = 3600.0
"""The start-up sweep removes transfer temp files older than this."""

FORMER_MEMBER = "Former member"
"""The name of an exported uid no table can name."""

_STREAM_BATCH = 500


@dataclass
class ExportFile:
    """A finished export.

    Attributes
    ----------
    path : Path
        The temp file (0600). The caller streams it and unlinks it.
    filename : str
        The suggested download name, ``whygraph-<slug>-<date>.zip``.
    size : int
        Bytes on disk.
    sha256 : str
        Hex digest of the file (for the audit event).
    counts : dict
        Rows written per member, plus ``skipped`` (rows that failed wire
        validation and were left out).
    manifest : Manifest
        The manifest as written.
    """

    path: Path
    filename: str
    size: int
    sha256: str
    counts: dict[str, int]
    manifest: tb.Manifest
    skipped: int = field(default=0)


def origin_of(project: Project) -> str | None:
    """The advisory ``host/owner/name`` of a project's repository.

    Used by the export (manifest) and the import (the wrong-repo message).

    Parameters
    ----------
    project : Project
        A project row.

    Returns
    -------
    str or None
        From ``remote_url`` when set, else (local projects) from the
        checkout's ``origin``; ``None`` when neither names a repository.
    """
    identity = None
    if project.remote_url:
        identity = remote_identity(strip_userinfo(project.remote_url))
    elif project.source == "local":
        from .platform_routes import origin_identity

        identity = origin_identity(resolve_root(project))
    return "/".join(identity) if identity else None


def _strip(uid: str | None) -> str | None:
    """``uid`` without the ``imported:`` prefix."""
    return None if uid is None else uid.removeprefix(IMPORTED_UID_PREFIX)


def _user_names(uids: set[str]) -> dict[str, str]:
    """Display names of plain uids from the portal's ``users`` table."""
    if not uids:
        return {}
    with portal_session() as db:
        return dict(
            db.exec(
                select(User.uid, User.display_name).where(col(User.uid).in_(uids))
            ).all()
        )


def _project_layer(project: Project, mode: str | None) -> dict[str, Any]:
    """The project's config layer, filtered to what a transfer may carry (D15)."""
    from whygraph.core.config import normalize_v2

    from .transfer_import import transfer_config_layer

    with portal_session() as db:
        raw = load_layer(db, project.id, org_id=project.org_id)
    layer, _ = normalize_v2(raw, resolve_root(project))
    return transfer_config_layer(layer, mode)


def _secret_free(layer: Mapping[str, Any]) -> None:
    """Raise when anything in ``layer`` could be a secret; never strip."""
    if find_secret_paths(layer):
        raise ConfigPolicyError("the exported settings hold a secret key")
    stack: list[Any] = [layer]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str) and (
            TOKEN_PATTERN.search(item) or PROVIDER_KEY_PATTERN.search(item)
        ):
            raise ConfigPolicyError("the exported settings hold a token")


def _tmp_dir(data_dir: Path) -> Path:
    """``<data>/tmp`` (0700), refusing a symlink."""
    tmp = data_dir / "tmp"
    if tmp.is_symlink():
        raise UnsafePathError(tmp, "is a symlink")
    tmp.mkdir(mode=0o700, exist_ok=True)
    if tmp.is_symlink() or not stat.S_ISDIR(os.lstat(tmp).st_mode):
        raise UnsafePathError(tmp, "is not a directory")
    os.chmod(tmp, 0o700)
    return tmp


def sweep_transfer_tmp(data_dir: Path, *, max_age: float = SWEEP_AGE_SEC) -> int:
    """Delete leftover transfer temp files in ``<data_dir>/tmp`` (plan 42 section 5.8).

    Called once at start-up (:meth:`whygraph.portal.runner.ScanRunner.start`).
    Each export and import unlinks its own file; this catches the ones a
    crash left behind. Only regular files named ``transfer-*.zip`` older
    than ``max_age`` seconds are removed; a symlinked ``tmp`` is refused
    (nothing is removed) and a symlink inside it is left alone. Never raises.

    Parameters
    ----------
    data_dir : Path
        The portal's data directory.
    max_age : float, optional
        Minimum age in seconds (by modification time) of a removed file.

    Returns
    -------
    int
        How many files were removed.
    """
    tmp = data_dir / "tmp"
    try:
        if tmp.is_symlink():
            _log.warning("transfer sweep: refusing the symlinked %s", tmp)
            return 0
        if not tmp.is_dir():
            return 0
        entries = list(tmp.glob(f"{TMP_PREFIX}*.zip"))
    except OSError:
        _log.exception("transfer sweep: could not list %s", tmp)
        return 0
    cutoff = time.time() - max_age
    removed = 0
    for path in entries:
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_mtime > cutoff:
                continue
            os.unlink(path)
            removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            _log.exception("transfer sweep: could not remove %s", path)
    return removed


def _json_list(text: str | None) -> list | None:
    """A stored JSON list, or ``None`` when absent or malformed."""
    if text is None:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, list) else None


class _Skips:
    """Counts the records that failed wire validation."""

    def __init__(self) -> None:
        self.count = 0

    def build(self, cls: type[BaseModel], /, **values: Any) -> BaseModel | None:
        try:
            return cls(**values)
        except (ValidationError, ValueError):
            self.count += 1
            return None


def _note_rows(session: Session) -> list[ProjectNote]:
    """The notes a bundle carries (D13)."""
    agent_pending = col(ProjectNote.source).in_(AGENT_SOURCES) & (
        ProjectNote.status == "proposed"
    )
    rejected = (ProjectNote.status == "retired") & (
        ProjectNote.retire_reason == "rejected"
    )
    stmt = (
        select(ProjectNote)
        .where(~agent_pending, ~rejected)
        .order_by(col(ProjectNote.id))
    )
    return list(session.exec(stmt).all())


def _note_record(
    row: ProjectNote, kept: set[int], skips: _Skips
) -> tb.NoteRecord | None:
    supersedes = row.supersedes_id
    if supersedes is not None and (supersedes not in kept or supersedes >= row.id):
        supersedes = None
    return skips.build(  # type: ignore[return-value]
        tb.NoteRecord,
        id=row.id,
        kind=row.kind,
        body=row.body,
        edited_on_confirm=row.proposed_body is not None,
        status=row.status,
        anchor_kind=row.anchor_kind,
        anchor_path=row.anchor_path,
        line_start=row.line_start,
        line_end=row.line_end,
        qualified_name=row.qualified_name,
        area=row.area,
        content_hash=row.content_hash,
        source=row.source,
        origin=row.origin,
        refs=_json_list(row.refs),
        supersedes_id=supersedes,
        proposed_by=_strip(row.proposed_by) or "",
        proposed_at=row.proposed_at,
        confirmed_by=_strip(row.confirmed_by),
        confirmed_at=row.confirmed_at,
        reconfirmed_by=_strip(row.reconfirmed_by),
        reconfirmed_at=row.reconfirmed_at,
        retired_by=_strip(row.retired_by),
        retired_at=row.retired_at,
        retire_reason=row.retire_reason,
        check_kind=row.check_kind,
        check_text=row.check_text,
        check_glob=row.check_glob,
    )


def _stream(session: Session, stmt: Any) -> Iterator[Any]:
    """Rows of ``stmt`` in batches, never all at once."""
    result = session.exec(stmt.execution_options(yield_per=_STREAM_BATCH))
    yield from result


def _actors(
    session: Session, uids: set[str], prefixed: set[str]
) -> list[tb.ActorRecord]:
    """One actor per uid (prefix stripped); names per D12."""
    names: dict[str, str] = {}
    if prefixed:
        names.update(
            {
                _strip(uid) or "": name
                for uid, name in session.exec(
                    select(ImportedActor.uid, ImportedActor.display_name).where(
                        col(ImportedActor.uid).in_(
                            {IMPORTED_UID_PREFIX + u for u in prefixed}
                        )
                    )
                ).all()
            }
        )
    plain = uids - prefixed
    names.update(_user_names(plain))
    out = []
    for uid in sorted(uids):
        name = _one_line(names.get(uid) or "").strip() or FORMER_MEMBER
        record = _build_actor(uid, name)
        if record is not None:
            out.append(record)
    return out


def _build_actor(uid: str, name: str) -> tb.ActorRecord | None:
    try:
        return tb.ActorRecord(uid=uid, display_name=name)
    except ValidationError:
        return None


def _root_commits(session: Session) -> list[str]:
    stmt = (
        select(Commit.sha)
        .where(Commit.parent_shas == "")
        .order_by(col(Commit.committed_at), col(Commit.sha))
        .limit(tb.MAX_ROOT_COMMITS)
    )
    return [s for s in session.exec(stmt).all() if s and len(s) == 40]


def _schema_revision(session: Session) -> str | None:
    try:
        row = (
            session.connection()
            .exec_driver_sql("SELECT version_num FROM alembic_version")
            .first()
        )
    except Exception:  # noqa: BLE001 - informational only
        return None
    return str(row[0])[:64] if row else None


def build_export(
    project: Project,
    *,
    org_slug: str,
    uid: str | None,
    include_chats: bool,
    mode: str,
    data_dir: Path,
) -> ExportFile:
    """Write a project's bundle to a temp file (plan 42 section 5.7).

    Call under the project's bound context (``use_project``) and inside the
    route's ``claim_project`` and worker thread; the project's WhyGraph DB is
    read in one ``BEGIN`` transaction.

    Parameters
    ----------
    project : Project
        The project row (not linked; the route refuses those first).
    org_slug : str
        The project's org slug, for the manifest label.
    uid : str or None
        The exporter. Their own chat sessions travel with ``include_chats``;
        in local mode so do sessions with no owner.
    include_chats : bool
        Whether to carry the exporter's chats.
    mode : {"local", "production"}
        This portal's mode.
    data_dir : Path
        The portal data directory; the file goes to ``<data_dir>/tmp``.

    Returns
    -------
    ExportFile
        The temp file and its facts. The caller unlinks the file.

    Raises
    ------
    ServeError
        ``409 transfer_needs_scan`` before a first scan;
        ``413 bundle_too_large`` past a bundle cap (the file is removed).
    ConfigPolicyError
        When the settings to export hold anything secret-looking.
    UnsafePathError
        When ``<data_dir>/tmp`` is a symlink.
    """
    if project.last_scanned_head is None:
        raise ServeError(
            409, "Scan the project before exporting.", code="transfer_needs_scan"
        )
    layer = _project_layer(project, mode)
    _secret_free(layer)
    tmp = _tmp_dir(data_dir)
    path = tmp / f"{TMP_PREFIX}{uuid.uuid4()}.zip"
    skips = _Skips()
    counts: dict[str, int] = {}

    with project_session() as session:
        session.connection().exec_driver_sql("BEGIN")
        roots = _root_commits(session)
        if not roots:
            raise ServeError(
                409, "Scan the project before exporting.", code="transfer_needs_scan"
            )

        note_rows = _note_rows(session)
        kept = {int(r.id) for r in note_rows}
        notes = [
            rec for r in note_rows if (rec := _note_record(r, kept, skips)) is not None
        ]
        note_ids = {n.id for n in notes}
        # A note dropped for a bad field must not stay named as a predecessor.
        notes = [
            n.model_copy(update={"supersedes_id": None})
            if n.supersedes_id is not None and n.supersedes_id not in note_ids
            else n
            for n in notes
        ]

        run_stmt = (
            select(ProjectPurposeRun)
            .where(ProjectPurposeRun.status == "ok")
            .order_by(col(ProjectPurposeRun.id))
        )
        runs = []
        for r in session.exec(run_stmt).all():
            if r.note_id not in note_ids:
                continue
            rec = skips.build(
                tb.PurposeRunRecord,
                requested_by=_strip(r.requested_by) or "",
                started_at=r.started_at,
                finished_at=r.finished_at,
                status=r.status,
                input_hash=r.input_hash,
                input_bytes=r.input_bytes,
                sources=_json_list(r.sources),
                provider=r.provider,
                model=r.model,
                tokens_in=r.tokens_in,
                tokens_out=r.tokens_out,
                note_id=r.note_id,
                error_code=r.error_code,
            )
            if rec is not None:
                runs.append(rec)

        sessions: list[tb.ChatSessionRecord] = []
        chat_ids: set[int] = set()
        if include_chats and uid:
            owned = ChatSession.owner_uid == uid
            if mode == "local":
                owned = owned | col(ChatSession.owner_uid).is_(None)
            imported = dict(
                session.exec(
                    select(ImportedChat.session_id, ImportedChat.origin_key)
                ).all()
            )
            for r in session.exec(
                select(ChatSession).where(owned).order_by(col(ChatSession.id))
            ).all():
                rec = skips.build(
                    tb.ChatSessionRecord,
                    id=r.id,
                    title=r.title,
                    provider=r.provider,
                    model=r.model,
                    created_at=r.created_at,
                    updated_at=r.updated_at,
                    active_leaf_id=r.active_leaf_id,
                    effort=r.effort,
                    mode=r.mode,
                    origin_key=imported.get(r.id) or native_origin_key(r),
                )
                if rec is not None:
                    sessions.append(rec)
                    chat_ids.add(r.id)

        # Actors: every uid the exported records name, plus the exporter.
        referenced: set[str] = set()
        for r in note_rows:
            referenced.update(
                filter(
                    None,
                    (
                        r.proposed_by,
                        r.confirmed_by,
                        r.reconfirmed_by,
                        r.retired_by,
                    ),
                )
            )
        for run in runs:
            if run.requested_by:
                referenced.add(run.requested_by)
        prefixed = {
            u.removeprefix(IMPORTED_UID_PREFIX)
            for u in referenced
            if u.startswith(IMPORTED_UID_PREFIX)
        }
        uids = {u.removeprefix(IMPORTED_UID_PREFIX) for u in referenced}
        if uid:
            uids.add(uid)
        uids.discard("")
        actors = _actors(session, uids, prefixed)

        def rationale() -> Iterator[tb.RationaleRecord]:
            stmt = select(RationaleCache).order_by(
                col(RationaleCache.path),
                col(RationaleCache.line_start),
                col(RationaleCache.line_end),
                col(RationaleCache.provider),
                col(RationaleCache.model),
            )
            for r in _stream(session, stmt):
                rec = skips.build(
                    tb.RationaleRecord,
                    path=r.path,
                    line_start=r.line_start,
                    line_end=r.line_end,
                    provider=r.provider,
                    model=r.model,
                    evidence_fingerprint=r.evidence_fingerprint,
                    cached_at=r.cached_at,
                    purpose=r.purpose,
                    why=r.why,
                    constraints=[i.as_dict() for i in decode_items(r.constraints)],
                    tradeoffs=_json_list(r.tradeoffs) or [],
                    risks=[i.as_dict() for i in decode_items(r.risks)],
                    input_tokens=r.input_tokens,
                    output_tokens=r.output_tokens,
                    actual_provider=r.actual_provider,
                    actual_model=r.actual_model,
                    qualified_name=r.qualified_name,
                    deferred_shas=r.deferred_shas,
                )
                if rec is not None:
                    yield rec

        def commit_descriptions() -> Iterator[tb.CommitDescriptionRecord]:
            stmt = (
                select(Commit.sha, Commit.llm_description, Commit.llm_description_model)
                .where(
                    col(Commit.llm_description).is_not(None),
                    col(Commit.llm_description_model).is_not(None),
                )
                .order_by(col(Commit.sha))
            )
            for sha, text, model in _stream(session, stmt):
                rec = skips.build(
                    tb.CommitDescriptionRecord,
                    sha=sha,
                    llm_description=text,
                    llm_description_model=model,
                )
                if rec is not None:
                    yield rec

        def file_descriptions() -> Iterator[tb.FileDescriptionRecord]:
            stmt = (
                select(
                    CommitFileChange.commit_sha,
                    CommitFileChange.path,
                    CommitFileChange.llm_description,
                    CommitFileChange.llm_description_model,
                )
                .where(
                    col(CommitFileChange.llm_description).is_not(None),
                    col(CommitFileChange.llm_description_model).is_not(None),
                )
                .order_by(col(CommitFileChange.id))
            )
            for sha, file_path, text, model in _stream(session, stmt):
                rec = skips.build(
                    tb.FileDescriptionRecord,
                    commit_sha=sha,
                    path=file_path,
                    llm_description=text,
                    llm_description_model=model,
                )
                if rec is not None:
                    yield rec

        def message_record(
            r: ChatMessage, skips: _Skips
        ) -> tb.ChatMessageRecord | None:
            raw = _json_replay(r.replay)
            return skips.build(  # type: ignore[return-value]
                tb.ChatMessageRecord,
                id=r.id,
                session_id=r.session_id,
                parent_id=r.parent_id,
                role=r.role,
                content=r.content,
                tool_calls=_json_list(r.tool_calls) or [],
                tool_call_id=r.tool_call_id,
                is_error=r.is_error,
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
                provider=r.provider,
                model=r.model,
                error=r.error,
                replay=None if raw is None else {"reasoning_content": raw},
                finish_reason=r.finish_reason,
                effort=r.effort,
                mode=r.mode,
                grounding=r.grounding,
                reasoning=r.reasoning,
                created_at=r.created_at,
            )

        def message_stmt() -> Any:
            return (
                select(ChatMessage)
                .where(col(ChatMessage.session_id).in_(chat_ids))
                .order_by(col(ChatMessage.id))
            )

        if chat_ids:
            # A message that fails the wire model would leave its children
            # without a parent, so its whole session stays home.
            probe = _Skips()
            bad = {
                r.session_id
                for r in _stream(session, message_stmt())
                if message_record(r, probe) is None
            }
            if bad:
                skips.count += len(bad)
                sessions = [s for s in sessions if s.id not in bad]
                chat_ids -= bad

        def messages() -> Iterator[tb.ChatMessageRecord]:
            if not chat_ids:
                return
            for r in _stream(session, message_stmt()):
                rec = message_record(r, skips)
                if rec is not None:
                    yield rec

        source = tb.ManifestSource(
            mode=mode,  # type: ignore[arg-type]
            label=_one_line(f"{org_slug}/{project.slug}"),
            name=_one_line(project.name),
        )
        manifest = tb.Manifest(
            format=tb.FORMAT,
            format_version=tb.FORMAT_VERSION,
            whygraph_version=tb._whygraph_version(),
            schema_revision=_schema_revision(session),
            exported_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            source=source,
            repo=tb.ManifestRepo(
                root_commits=roots,
                origin=_advisory_origin(origin_of(project)),
                default_branch=_clean_line(project.default_branch),
            ),
        )
        members: dict[str, Iterable[BaseModel] | Mapping[str, Any]] = {
            tb.ACTORS: actors,
            tb.NOTES: notes,
            tb.PURPOSE_RUNS: runs,
            tb.RATIONALE: rationale(),
            tb.COMMIT_DESCRIPTIONS: commit_descriptions(),
            tb.FILE_DESCRIPTIONS: file_descriptions(),
            tb.CONFIG: layer,
        }
        if include_chats:
            members[tb.CHAT_SESSIONS] = sessions
            members[tb.CHAT_MESSAGES] = messages()
        written = tb.write_bundle(path, manifest, members)

    try:
        size = os.stat(path).st_size
        if size > tb.MAX_BUNDLE_BYTES:
            raise tb.BundleError(
                413,
                "Bundle too large: over 100 MiB; export without chats.",
                code="bundle_too_large",
            )
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    counts = {name: info.rows for name, info in written.members.items()}
    return ExportFile(
        path=path,
        filename=f"whygraph-{project.slug}-{datetime.now(timezone.utc):%Y-%m-%d}.zip",
        size=size,
        sha256=digest.hexdigest(),
        counts=counts,
        manifest=written,
        skipped=skips.count,
    )


def _json_replay(text: str | None) -> str | None:
    """``reasoning_content`` of a stored replay payload, if it is a string."""
    if not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    if isinstance(value, dict) and isinstance(value.get("reasoning_content"), str):
        return value["reasoning_content"]
    return None


def _one_line(text: str, max_chars: int = 200) -> str:
    """``text`` cut to what a wire single-line field accepts.

    Every character the wire rule (:func:`transfer_bundle._single_line`)
    refuses is dropped, so the check lives in one place; the result is cut
    to ``max_chars``.
    """
    ok = "".join(c for c in text if tb._printable(c, 1) is not None)
    return ok[:max_chars]


def _advisory_origin(origin: str | None) -> str | None:
    """The origin, or ``None`` when it is over the wire cap (never a cut identity)."""
    if origin is None or len(origin) > _ORIGIN_MAX:
        return None
    return _clean_line(origin, _ORIGIN_MAX)


_ORIGIN_MAX = 300


def _clean_line(text: str | None, max_chars: int = 255) -> str | None:
    """``text`` as a manifest advisory field, or ``None`` when unusable."""
    if text is None:
        return None
    cleaned = _one_line(text, max_chars)
    return cleaned or None
