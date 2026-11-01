"""Lazy, per-commit ``llm_description`` backfill for the MCP request path.

The :class:`~whygraph.scan.analyze_crawler.AnalyzeCrawler` describes
every undescribed commit in bulk at scan time. When a scan is run with
a ``--skip-analyze`` scan (or when a commit was added to
the database before its description could be generated), the
``commit.llm_description`` column is left ``NULL``. The MCP tools
:func:`whygraph.mcp.evidence.whygraph_evidence_for` and
:func:`whygraph.mcp.rationale.whygraph_rationale_brief` need that text
on read, so they call into this module to generate and persist it on
demand, one commit at a time.

This module mirrors the crawler's per-commit recipe
(:meth:`AnalyzeCrawler._describe_commit`) deliberately rather than
sharing code with it: the crawler runs in a thread pool and aggregates
failures, while these helpers run inline inside a synchronous MCP
handler against a handful of detached SQLModel rows. The two ergonomics
diverge enough that one shared implementation would have to grow knobs
for both. The shared knowledge is the small "describe + write both
columns" recipe, which lives here.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, col, select

from whygraph.core.import_checks import ImportFormatError, clean_text
from whygraph.core.usage import usage_blocked
from whygraph.db import get_session
from whygraph.db.models.commit import Commit as CommitRow
from whygraph.db.models.commit_file_change import CommitFileChange
from whygraph.services.git import Repository
from whygraph.services.git.commit import Commit as CommitDC
from whygraph.services.git.commit import DiffStats

from .llm_descriptor import LlmDescriptor

_log = logging.getLogger(__name__)


def bulk_commit_stub(files_changed: int) -> str:
    """The placeholder ``llm_description`` for a bulk commit.

    Bulk commits (more files than ``analyze.large_commit_file_count``)
    are not described whole-diff — that would be one expensive LLM pass
    producing a vague repo-wide summary. Instead the commit row carries
    this cheap, deterministic stub so evidence payloads are never blank,
    and the real per-file descriptions are filled in lazily on read by
    :func:`backfill_file_description`.

    Parameters
    ----------
    files_changed : int
        The commit's file count, surfaced in the message so a reader
        understands *why* there is no whole-diff summary.

    Returns
    -------
    str
        A one-line human-readable stub.
    """
    return (
        f"Bulk commit touching {files_changed} files — too broad for a single "
        "whole-diff summary; per-file descriptions are generated on demand."
    )


def _row_to_diff_dc(commit: CommitRow) -> CommitDC:
    """Build a minimal :class:`CommitDC` for :meth:`Repository.diff`.

    :meth:`whygraph.services.git.Repository.diff` reads only ``.sha`` and
    ``.parent_shas`` on its argument — every other field of the frozen
    dataclass is unused. The DB stores ``parent_shas`` as a
    space-joined string (see :func:`whygraph.scan.git_crawler._to_row`),
    so split it back into a tuple and fill the unused fields with inert
    defaults.
    """
    return CommitDC(
        sha=commit.sha,
        parent_shas=tuple(p for p in commit.parent_shas.split() if p),
        author_name="",
        author_email="",
        authored_at="",
        committed_at="",
        subject="",
        body="",
        stats=DiffStats(files_changed=0, insertions=0, deletions=0),
    )


def backfill_commit_description(
    commit: CommitRow,
    *,
    repository: Repository,
    descriptor: LlmDescriptor,
) -> bool:
    """Generate, persist, and in-place set ``commit.llm_description``.

    Parameters
    ----------
    commit : Commit
        A (typically detached) DB row. When backfill succeeds, both
        ``llm_description`` and ``llm_description_model`` are written
        to the database **and** assigned on the passed-in instance so
        the caller — which keeps reading from the detached object —
        sees the new values without a re-fetch.
    repository : Repository
        Used to compute the commit's first-parent diff.
    descriptor : LlmDescriptor
        Pre-built descriptor. Its :meth:`LlmDescriptor.describe` runs
        synchronously on the calling thread — no pool is spawned here.

    Returns
    -------
    bool
        ``True`` when a new description was generated and persisted;
        ``False`` when the commit already had a description, or its
        diff was empty/whitespace-only.

    Raises
    ------
    whygraph.services.git.GitError
        If the diff computation fails (unknown sha, broken repo).
    AnalyzeError
        If :meth:`LlmDescriptor.describe` fails for any reason.
    """
    if commit.llm_description is not None:
        return False

    dc = _row_to_diff_dc(commit)
    diff = repository.diff(dc)
    if not diff.strip():
        return False

    description = descriptor.describe(diff, subject=commit.sha)
    model_label = f"{description.provider}:{description.model}"
    with get_session() as session:
        row = session.get(CommitRow, commit.sha)
        if row is not None:
            row.llm_description = description.text
            row.llm_description_model = model_label

    # Mirror the persisted values onto the caller's detached row so the
    # subsequent serializer / formatter sees the description without a
    # re-fetch from the DB.
    commit.llm_description = description.text
    commit.llm_description_model = model_label
    return True


def _file_change_row(session, commit_sha: str, path: str) -> CommitFileChange | None:
    """The ``commit_file_change`` row for ``(commit_sha, path)``, if any.

    There is at most one row per ``(commit, path)`` (see
    :class:`CommitFileChange`), so ``first()`` is exact rather than a
    sampled pick.
    """
    return session.exec(
        select(CommitFileChange)
        .where(CommitFileChange.commit_sha == commit_sha)
        .where(CommitFileChange.path == path)
    ).first()


def backfill_file_description(
    commit: CommitRow,
    path: str,
    *,
    repository: Repository,
    descriptor: LlmDescriptor,
) -> str | None:
    """Generate (or fetch the cached) per-file description for one path.

    For *bulk* commits the whole-commit diff is never described; instead
    the slice for a single ``path`` is described lazily on read and
    cached on that file's :class:`CommitFileChange` row, keyed by
    ``(commit.sha, path)``. A second call for the same file returns the
    cached text without an LLM round-trip.

    Parameters
    ----------
    commit : Commit
        The (typically detached) bulk-commit row. Only its ``sha`` and
        ``parent_shas`` drive the diff.
    path : str
        Repository-relative path to describe — the file the caller is
        actually asking about.
    repository : Repository
        Used to compute the path-scoped diff (``git diff … -- path``).
    descriptor : LlmDescriptor
        Pre-built descriptor; :meth:`LlmDescriptor.describe` runs
        synchronously on the calling thread.

    Returns
    -------
    str or None
        The description text (freshly generated or from cache), or
        ``None`` when the commit did not touch ``path`` (empty diff) so
        there is nothing to describe — callers then fall back to the
        commit-level stub.

    Raises
    ------
    whygraph.services.git.GitError
        If the diff computation fails (unknown sha, broken repo).
    AnalyzeError
        If :meth:`LlmDescriptor.describe` fails for any reason.
    """
    with get_session() as session:
        row = _file_change_row(session, commit.sha, path)
        if row is not None and row.llm_description is not None:
            return row.llm_description

    dc = _row_to_diff_dc(commit)
    diff = repository.diff(dc, pathspec=path)
    if not diff.strip():
        return None

    description = descriptor.describe(diff, subject=path)
    model_label = f"{description.provider}:{description.model}"
    with get_session() as session:
        row = _file_change_row(session, commit.sha, path)
        if row is not None:
            row.llm_description = description.text
            row.llm_description_model = model_label
    return description.text


def backfill_all(
    commits: Iterable[CommitRow],
    *,
    repository: Repository,
    descriptor: LlmDescriptor,
) -> int:
    """Apply :func:`backfill_commit_description` to each commit.

    Per-commit failures are swallowed and logged at ``WARNING``: one bad
    commit must not poison the whole MCP response. The aggregate-raise
    behaviour of :class:`~whygraph.scan.analyze_crawler.AnalyzeCrawler`
    is intentionally not mirrored — the crawler can afford to surface
    failures because it owns the whole scan; an MCP handler cannot.

    Before each commit the bound usage sink is asked whether an exhausted
    hard-stopped budget now covers the caller
    (:func:`~whygraph.core.usage.usage_blocked`); the loop stops when one
    does (M2f-2 plan section 4.7), leaving the rest undescribed.

    Returns
    -------
    int
        The number of commits that were successfully backfilled.
    """
    succeeded = 0
    for commit in commits:
        if usage_blocked() is not None:
            _log.info("lazy LLM description backfill stopped: the budget is spent")
            break
        try:
            if backfill_commit_description(
                commit, repository=repository, descriptor=descriptor
            ):
                succeeded += 1
        except Exception as exc:  # noqa: BLE001 — one bad commit must not poison the batch
            _log.warning(
                "lazy LLM description backfill failed for %s: %s",
                commit.sha[:12],
                exc,
            )
    return succeeded


# ---------------------------------------------------------------------------
# Import from a project-transfer bundle (plan 42 section 5.5)
# ---------------------------------------------------------------------------

_IMPORT_DESCRIPTION_MAX = 64_000
_IMPORT_MODEL_MAX = 200
_IMPORT_PATH_MAX = 4_096
_MODEL_LABEL_RE = re.compile(r"[^:]+:.+")
_IN_CHUNK = 500


@dataclass
class DescriptionImportResult:
    """What :func:`import_descriptions` did (plan 42 section 7.2 ``descriptions``).

    Attributes
    ----------
    filled : int
        Commit and file descriptions written (rows that were ``NULL``).
    kept_target : int
        Records whose target row already had a description (a target stub
        included), or that repeat an earlier record of the bundle.
    missing_commit : int
        Records whose commit (or, for a file record, commit and path) is not
        a row on the target.
    skipped_oversize : int
        Records skipped for a description over 64,000 characters.
    """

    filled: int = 0
    kept_target: int = 0
    missing_commit: int = 0
    skipped_oversize: int = 0

    def as_dict(self) -> dict[str, int]:
        """The counts block as a plain dict."""
        return {
            "filled": self.filled,
            "kept_target": self.kept_target,
            "missing_commit": self.missing_commit,
            "skipped_oversize": self.skipped_oversize,
        }


def _check_description(rec: Mapping[str, Any], *, path: bool) -> tuple[str, str] | None:
    """The ``(text, model)`` of a record; ``None`` when the text is too long."""
    if path:
        value = rec.get("path")
        if not isinstance(value, str) or not value:
            raise ImportFormatError("path", "must be a repository path")
        if (
            clean_text(value, multiline=False, max_chars=_IMPORT_PATH_MAX, field="path")
            is None
        ):
            raise ImportFormatError("path", "is too long")
    model = clean_text(
        rec.get("llm_description_model"),
        multiline=False,
        max_chars=_IMPORT_MODEL_MAX,
        field="llm_description_model",
    )
    if model is None or not _MODEL_LABEL_RE.fullmatch(model):
        raise ImportFormatError(
            "llm_description_model", "must look like <provider>:<model>"
        )
    text = clean_text(
        rec.get("llm_description"),
        multiline=True,
        max_chars=_IMPORT_DESCRIPTION_MAX,
        field="llm_description",
    )
    if text is None:
        return None
    return text, model


CommitFill = tuple[str, str, str]
"""A checked commit description: ``(sha, text, model)``."""

FileFill = tuple[str, str, str, str]
"""A checked file description: ``(commit_sha, path, text, model)``."""


def _checked_commit(rec: Mapping[str, Any]) -> CommitFill | None:
    """A commit record as ``(sha, text, model)``; ``None`` when the text is too long."""
    sha = _sha(rec.get("sha"), "sha")
    checked = _check_description(rec, path=False)
    return None if checked is None else (sha, *checked)


def _checked_file(rec: Mapping[str, Any]) -> FileFill | None:
    """A file record as ``(sha, path, text, model)``; ``None`` when too long."""
    try:
        sha = _sha(rec.get("commit_sha"), "commit_sha")
        checked = _check_description(rec, path=True)
    except ImportFormatError as exc:
        exc.field = f"file.{exc.field}"
        raise
    return None if checked is None else (sha, rec["path"], *checked)


def _each(
    records: Iterable[Any], check: Callable[[Mapping[str, Any]], Any]
) -> Iterator[tuple[int, Any]]:
    """Each record's position and ``check`` outcome; errors carry the position."""
    for index, raw in enumerate(records):
        rec = raw if isinstance(raw, Mapping) else raw.model_dump()
        try:
            yield index, check(rec)
        except ImportFormatError as exc:
            exc.index = index
            raise


class DescriptionImport:
    """Check, then fill, a bundle's LLM descriptions in batches (plan 42 Q11).

    Two passes over each member, so the caller can stream it instead of
    holding it: :meth:`check_commits` / :meth:`check_files` format-check
    **every** record before anything is written and count the over-long
    records and the in-bundle repeats (the first record of a key wins);
    :meth:`commit_rows` / :meth:`file_rows` re-read the records and yield
    the checked ones; :meth:`fill_commits` / :meth:`fill_files` write one
    batch. Repeats are found over the whole member, so no two batches touch
    the same key and a rolled-back dry run counts exactly what the apply
    writes.

    Only ``NULL`` descriptions are written (plan 42 D3), so a target stub
    (``llm_description_model`` ``NULL``) and any description the target made
    itself stay. Commits match by SHA; file descriptions by
    ``(commit_sha, path)``. Nothing is created: a record without a target
    row only counts.

    Attributes
    ----------
    result : DescriptionImportResult
        The counts so far; commit and file records share them.
    """

    def __init__(self) -> None:
        self.result = DescriptionImportResult()

    def _check(
        self, records: Iterable[Any], check: Callable[[Mapping[str, Any]], Any], key
    ) -> set[int]:
        """Pass one over one member; the positions :meth:`_rows` leaves out."""
        drop: set[int] = set()
        seen: set = set()
        for index, checked in _each(records, check):
            if checked is None:
                self.result.skipped_oversize += 1
                drop.add(index)
                continue
            k = key(checked)
            if k in seen:
                self.result.kept_target += 1
                drop.add(index)
            else:
                seen.add(k)
        return drop

    def check_commits(self, records: Iterable[Any]) -> set[int]:
        """Pass one over ``commit_descriptions.jsonl``.

        Parameters
        ----------
        records : Iterable
            ``CommitDescriptionRecord`` models or equivalent mappings, in
            line order.

        Returns
        -------
        set of int
            The 0-based positions :meth:`commit_rows` leaves out (over-long,
            or a repeat of an earlier SHA).

        Raises
        ------
        ImportFormatError
            A wrong type, a malformed SHA or model label, or a control
            character in a single-line field; ``index`` is the position.
        """
        return self._check(records, _checked_commit, lambda c: c[0])

    def check_files(self, records: Iterable[Any]) -> set[int]:
        """Pass one over ``file_descriptions.jsonl``.

        Parameters
        ----------
        records : Iterable
            ``FileDescriptionRecord`` models or equivalent mappings, in line
            order.

        Returns
        -------
        set of int
            The 0-based positions :meth:`file_rows` leaves out (over-long,
            or a repeat of an earlier ``(commit_sha, path)``).

        Raises
        ------
        ImportFormatError
            As :meth:`check_commits`, plus a malformed path; the field name
            is prefixed ``file.``.
        """
        return self._check(records, _checked_file, lambda f: (f[0], f[1]))

    @staticmethod
    def commit_rows(records: Iterable[Any], drop: set[int]) -> Iterator[CommitFill]:
        """Pass two: the checked commit descriptions :meth:`check_commits` kept.

        Parameters
        ----------
        records : Iterable
            The same records, in the same order, as pass one saw.
        drop : set of int
            What :meth:`check_commits` returned.

        Yields
        ------
        tuple of (str, str, str)
            ``(sha, text, model)``.
        """
        for index, checked in _each(records, _checked_commit):
            if checked is not None and index not in drop:
                yield checked

    @staticmethod
    def file_rows(records: Iterable[Any], drop: set[int]) -> Iterator[FileFill]:
        """Pass two: the checked file descriptions :meth:`check_files` kept.

        Parameters
        ----------
        records : Iterable
            The same records, in the same order, as pass one saw.
        drop : set of int
            What :meth:`check_files` returned.

        Yields
        ------
        tuple of (str, str, str, str)
            ``(commit_sha, path, text, model)``.
        """
        for index, checked in _each(records, _checked_file):
            if checked is not None and index not in drop:
                yield checked

    def fill_commits(self, session: Session, batch: Sequence[CommitFill]) -> None:
        """Fill the target's ``NULL`` commit descriptions for one batch.

        Runs in the caller's session (inside its write transaction) and never
        commits.

        Parameters
        ----------
        session : Session
            A session on the project's WhyGraph DB.
        batch : sequence of tuple
            Rows from :meth:`commit_rows`, no SHA twice.
        """
        result = self.result
        fills: list[tuple[str, str, str]] = []
        for start in range(0, len(batch), _IN_CHUNK):
            chunk = batch[start : start + _IN_CHUNK]
            state = dict(
                session.exec(  # type: ignore[call-overload]
                    select(
                        CommitRow.sha, col(CommitRow.llm_description).is_(None)
                    ).where(col(CommitRow.sha).in_([c[0] for c in chunk]))
                ).all()
            )
            for sha, text, model in chunk:
                if sha not in state:
                    result.missing_commit += 1
                elif not state[sha]:
                    result.kept_target += 1
                else:
                    fills.append((text, model, sha))
        if fills:
            _run_fill(session, _COMMIT_FILL, fills)
            result.filled += len(fills)

    def fill_files(self, session: Session, batch: Sequence[FileFill]) -> None:
        """Fill the target's ``NULL`` file descriptions for one batch.

        Every ``commit_file_change`` row of the ``(commit_sha, path)`` with a
        ``NULL`` description is filled; the record counts once. Runs in the
        caller's session and never commits.

        Parameters
        ----------
        session : Session
            A session on the project's WhyGraph DB.
        batch : sequence of tuple
            Rows from :meth:`file_rows`, no ``(commit_sha, path)`` twice.
        """
        result = self.result
        by_sha: dict[str, list[tuple[str, str, str]]] = {}
        for sha, path, text, model in batch:
            by_sha.setdefault(sha, []).append((path, text, model))
        shas = sorted(by_sha)
        file_fills: list[tuple[str, str, int]] = []
        for start in range(0, len(shas), _IN_CHUNK):
            chunk = shas[start : start + _IN_CHUNK]
            rows: dict[tuple[str, str], list[tuple[int, bool]]] = {}
            for rid, sha, path, empty in session.exec(  # type: ignore[call-overload]
                select(
                    CommitFileChange.id,
                    CommitFileChange.commit_sha,
                    CommitFileChange.path,
                    col(CommitFileChange.llm_description).is_(None),
                ).where(col(CommitFileChange.commit_sha).in_(chunk))
            ).all():
                rows.setdefault((sha, path), []).append((rid, bool(empty)))
            for sha in chunk:
                for path, text, model in by_sha[sha]:
                    found = rows.get((sha, path))
                    if not found:
                        result.missing_commit += 1
                        continue
                    empties = [rid for rid, empty in found if empty]
                    if not empties:
                        result.kept_target += 1
                        continue
                    file_fills.extend((text, model, rid) for rid in empties)
                    result.filled += 1
        if file_fills:
            _run_fill(session, _FILE_FILL, file_fills)


def import_descriptions(
    session: Session,
    commits: Iterable[Any],
    files: Iterable[Any],
) -> DescriptionImportResult:
    """Fill the target's missing LLM descriptions from a transfer bundle, in one call.

    Only ``NULL`` descriptions are written (plan 42 D3), so a target stub
    (``llm_description_model`` ``NULL``) and any description the target made
    itself stay. Commits match by SHA; file descriptions by
    ``(commit_sha, path)``. Nothing is created: a record without a target row
    only counts. Runs in the caller's session and never commits. The
    transfer import runs the same steps in batches through
    :class:`DescriptionImport`.

    Parameters
    ----------
    session : Session
        A session on the project's WhyGraph DB.
    commits : Iterable
        ``CommitDescriptionRecord`` models or equivalent mappings.
    files : Iterable
        ``FileDescriptionRecord`` models or equivalent mappings.

    Returns
    -------
    DescriptionImportResult
        The counts; commit and file records share them.

    Raises
    ------
    ImportFormatError
        A wrong type, a malformed SHA, path or model label, or a control
        character in a single-line field. ``index`` is the position inside
        ``commits`` or ``files``; the field name is prefixed ``file.`` for a
        file record. Both members are checked before anything is written.
    """
    commits, files = list(commits), list(files)
    job = DescriptionImport()
    commit_drop = job.check_commits(commits)
    file_drop = job.check_files(files)
    job.fill_commits(session, list(job.commit_rows(commits, commit_drop)))
    job.fill_files(session, list(job.file_rows(files, file_drop)))
    return job.result


_COMMIT_FILL = (
    'UPDATE "commit" SET llm_description = ?, llm_description_model = ? '
    "WHERE sha = ? AND llm_description IS NULL"
)
_FILE_FILL = (
    "UPDATE commit_file_change SET llm_description = ?, llm_description_model = ? "
    "WHERE id = ? AND llm_description IS NULL"
)


def _run_fill(session: Session, statement: str, rows: list[tuple]) -> None:
    """``executemany`` on the DBAPI cursor: the project DB is always SQLite.

    Going through SQLAlchemy's compiled ``update()`` costs more per row than
    the update itself at the bundle's row caps.
    """
    session.connection().exec_driver_sql(statement, rows)  # type: ignore[arg-type]


def _sha(value: object, name: str) -> str:
    """A full lowercase 40-hex commit SHA, else a format error."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ImportFormatError(name, "must be a 40-character lowercase SHA")
    return value
