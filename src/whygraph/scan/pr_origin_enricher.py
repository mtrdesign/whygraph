"""PROriginEnricher — recover a squash-merged PR's original commits.

A squash merge collapses a feature branch into one commit on the default
branch, discarding the per-commit history WhyGraph would otherwise index.
GitHub still retains the originals under ``refs/pull/<N>/head``, so this
crawler — run after :class:`~whygraph.scan.github_crawler.GitHubCrawler`,
once PR rows exist — fetches them once during the remote scan and persists
them as ``commit`` rows flagged ``on_default_branch=0`` so they enrich
evidence without leaking into the main-walk-only queries (area-history,
refactor-walk).

Only the squashes that actually lost history are enriched (the *balanced
gate*, see :func:`_select_candidates`), and the fetch is one targeted
batched ``git fetch`` carrying only the gated candidates' refspecs — never
the ``refs/pull/*`` wildcard. No PR↔commit link row is written: the
association is already carried by the PR's ``commit_titles`` and resolved
at query time by ``mcp/evidence.py:_linked_prs``.

The recovered commits' diffs and LLM descriptions stay lazy — this crawler
writes only the ``commit`` row (full message + stats from ``git log``),
leaving ``llm_description`` ``NULL`` for the on-read backfill to fill.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from rich.progress import Progress
from sqlmodel import col, select

from whygraph.db import get_session
from whygraph.db.models.commit import Commit as CommitRow
from whygraph.db.models.pull_request import PullRequest
from whygraph.services.git import GitError, Repository
from whygraph.services.git.commit import Commit as CommitDC

from .crawler import Crawler

_log = logging.getLogger(__name__)

# Pin each candidate PR's head under our own ref namespace so the fetched
# objects survive local GC and later blame/diff stay offline. The source
# ``refs/pull/<N>/head`` is GitHub's server-side immutable PR ref.
_PULL_REFSPEC = "refs/pull/{number}/head:refs/whygraph/pull/{number}"


def _json_list(raw: str | None) -> list:
    """Decode a JSON-encoded list column; empty list on anything malformed.

    Mirrors ``mcp/evidence.py:_json_list`` — duplicated rather than
    imported to keep the scan layer free of an upward dependency on the
    MCP server layer.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A gated squash PR and the subset of its oids not yet in ``commit``.

    Attributes
    ----------
    number : int
        The PR number, used to build its ``refs/pull/<N>/head`` refspec.
    new_oids : list[str]
        The PR's ``commit_titles`` oids that are absent from the ``commit``
        table — exactly the rows this run will materialize.
    """

    number: int
    new_oids: list[str]


def _select_candidates(
    session, *, min_commits: int, large_commit_file_count: int
) -> list[_Candidate]:
    """Pick the squash-merged PRs whose original commits should be recovered.

    Applies the *balanced gate* (plan §3.3 / §3.5): a merged PR qualifies
    when its ``commit_titles`` oids are **absent** from the ``commit``
    table (squash detection — the originals are not on the main walk)
    **and** either loss signal fires:

    - **file-bulk** — its ``merge_commit_sha`` touched more than
      ``large_commit_file_count`` files (the squash that lost the most
      *description* fidelity); **or**
    - **commit-rich** — it collapsed at least ``min_commits`` commits (lost
      *narrative*, even if few files changed).

    A PR whose oids are already on the main walk (a plain merge / rebase)
    is skipped — the normal path already indexed it.

    Parameters
    ----------
    session : sqlmodel.Session
        Open session to read PR / commit rows through.
    min_commits : int
        Commit-rich threshold (``analyze.pr_origin_min_commits``).
    large_commit_file_count : int
        File-bulk threshold (``analyze.large_commit_file_count``).

    Returns
    -------
    list[_Candidate]
        One entry per gated PR, carrying the oids still to insert.
    """
    existing: set[str] = set(session.exec(select(CommitRow.sha)).all())
    candidates: list[_Candidate] = []
    merged = session.exec(
        select(PullRequest).where(col(PullRequest.merged_at).is_not(None))
    ).all()
    for pr in merged:
        oids = [
            c["oid"]
            for c in _json_list(pr.commit_titles)
            if isinstance(c, dict) and c.get("oid")
        ]
        if not oids or all(o in existing for o in oids):
            continue  # not a squash — originals already on the main walk
        squash = (
            session.get(CommitRow, pr.merge_commit_sha) if pr.merge_commit_sha else None
        )
        file_bulk = bool(squash and squash.files_changed > large_commit_file_count)
        if not (file_bulk or len(oids) >= min_commits):
            continue  # below both halves of the balanced gate
        candidates.append(
            _Candidate(
                number=pr.number, new_oids=[o for o in oids if o not in existing]
            )
        )
    return candidates


def _to_origin_row(dc: CommitDC, *, scanned_at: str) -> CommitRow:
    """Build an ``on_default_branch=0`` commit row from a git value object.

    Mirrors ``git_crawler._to_row`` but flags the row as a recovered
    PR-origin commit and leaves ``refactor_score`` at its default — origin
    commits carry no ``commit_file_change`` rows, so the refactor-walk
    never reaches them regardless.
    """
    return CommitRow(
        sha=dc.sha,
        parent_shas=" ".join(dc.parent_shas),
        author_name=dc.author_name,
        author_email=dc.author_email,
        authored_at=dc.authored_at,
        committed_at=dc.committed_at,
        subject=dc.subject,
        body=dc.body,
        files_changed=dc.stats.files_changed,
        insertions=dc.stats.insertions,
        deletions=dc.stats.deletions,
        scanned_at=scanned_at,
        on_default_branch=0,
    )


class PROriginEnricher(Crawler):
    """Recover and persist squash-merged PRs' original feature-branch commits.

    Runs after the GitHub crawler (so PR rows exist) and the git crawler
    (so the squash commits and the main walk exist). Selects the gated
    squash PRs (:func:`_select_candidates`), fetches their PR-head refs in
    one batched ``git fetch``, then inserts one ``commit`` row per recovered
    oid with ``on_default_branch=0``. Re-scans are idempotent: oids already
    in ``commit`` are excluded at selection time, so a second run with no
    new squashes does nothing.

    Best-effort by design (plan §6.6): a failed fetch (GC'd ref, no
    network) or an unreadable single oid is logged and skipped rather than
    failing the scan — the PR keeps its existing PR-level evidence.

    Parameters
    ----------
    progress : rich.progress.Progress
        Shared Progress instance owned by the orchestrator.
    repository : Repository
        The git repository to fetch and read commit metadata through.
    min_commits : int
        Commit-rich half of the gate (``analyze.pr_origin_min_commits``).
    large_commit_file_count : int
        File-bulk half of the gate (``analyze.large_commit_file_count``).
    """

    def __init__(
        self,
        progress: Progress,
        *,
        repository: Repository,
        min_commits: int,
        large_commit_file_count: int,
    ) -> None:
        super().__init__("pr-origins", progress, total=None)
        self._repository = repository
        self._min_commits = min_commits
        self._large_commit_file_count = large_commit_file_count

    def work(self) -> None:
        with get_session() as session:
            candidates = _select_candidates(
                session,
                min_commits=self._min_commits,
                large_commit_file_count=self._large_commit_file_count,
            )
            self.set_total(len(candidates))
            if not candidates:
                self.summary = "no squash candidates"
                return

            # ONE batched fetch — only the gated candidates' refs, never the
            # refs/pull/* wildcard. A failure here degrades the whole phase
            # gracefully rather than aborting the scan.
            refspecs = [_PULL_REFSPEC.format(number=c.number) for c in candidates]
            try:
                self._repository.fetch_refs(refspecs)
            except GitError as exc:
                _log.warning(
                    "pr-origin fetch failed; skipping enrichment for %d PR(s): %s",
                    len(candidates),
                    exc,
                )
                self.summary = "fetch skipped"
                return

            scanned_at = datetime.now(timezone.utc).isoformat()
            inserted: set[str] = set()
            for cand in candidates:
                for oid in cand.new_oids:
                    # A commit shared across stacked / backport PRs appears
                    # in two candidates' new_oids; insert it once.
                    if oid in inserted:
                        continue
                    try:
                        dc = self._repository.commit_metadata(oid)
                    except GitError as exc:
                        _log.warning("skipping origin commit %s: %s", oid[:9], exc)
                        continue
                    session.add(_to_origin_row(dc, scanned_at=scanned_at))
                    inserted.add(oid)
                self.advance(1)

        self.summary = f"{len(inserted)} commits recovered"
