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

from whygraph.db import get_session
from whygraph.db.models.commit import Commit as CommitRow
from whygraph.services.git import Repository
from whygraph.services.git.commit import Commit as CommitDC
from whygraph.services.git.commit import DiffStats

from .llm_descriptor import LlmDescriptor

_log = logging.getLogger(__name__)


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
