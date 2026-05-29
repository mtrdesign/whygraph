"""Lazy, per-commit ``llm_description`` backfill for the MCP request path.

The :class:`~whygraph.scan.analyze_crawler.AnalyzeCrawler` describes
every undescribed commit in bulk at scan time. When a scan is run with
``whygraph scan --no-llm-descriptions`` (or when a commit was added to
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
from collections.abc import Iterable

from sqlmodel import select

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

    description = descriptor.describe(diff)
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

    description = descriptor.describe(diff)
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

    Returns
    -------
    int
        The number of commits that were successfully backfilled.
    """
    succeeded = 0
    for commit in commits:
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
