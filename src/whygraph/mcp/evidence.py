"""The ``whygraph_evidence_for`` MCP tool and its evidence collector.

The collector combines five signals: line-blame at HEAD, line-blame
after walking past refactor-heavy commits, line-blame against a rename
predecessor at its pre-rename location, per-line attribution back to a
squash-merged PR's original commits, and area-history from the
``commit_file_change`` index. Each commit is tagged with a ``source``
label so the rationale generator can weight precision vs coverage.
:mod:`whygraph.mcp.rationale` reuses :func:`collect_evidence`; the tool
itself serializes the bundle to JSON.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, col, select

from whygraph.analyze import CommitEvidence
from whygraph.core.config import RationaleConfig
from whygraph.core.context import current_project
from whygraph.core.remote import RemoteError, RemoteProject, platform_block
from whygraph.core.usage import usage_blocked
from whygraph.db import get_session
from whygraph.db.models import Commit, CommitFileChange, Issue, PRIssueLink, PullRequest
from whygraph.scan.refactor_score import BORING_THRESHOLD
from whygraph.services.git import BlameHunk, GitError, Repository

from .descriptions import (
    EVIDENCE_FOR_ANNOTATIONS,
    EVIDENCE_FOR_DESCRIPTION,
)
from .structured import structured
from .errors import WhyGraphError, log_tool_errors
from .targets import Target, repo_root, resolve_target, target_dict

# Cap on how many rounds of "blame returned a boring commit; ignore it
# and try again" the collector will run. Each round is one extra git
# blame invocation, so the bound keeps query latency predictable on
# pathological refactor chains.
_MAX_BORING_HOPS = 3

# Source ordering for dedupe — a SHA that surfaces from multiple paths
# is kept with the strongest source label only. ``blame`` beats every
# other label; ``area`` is the weakest. ``pr-origin`` sits just below
# ``blame`` (0.5): a real authoring commit reached through the squash is
# high-precision, but a direct HEAD blame hit is still preferred.
_SOURCE_PRIORITY = {
    "blame": 0,
    "pr-origin": 0.5,
    "blame-walked": 1,
    "predecessor-blame": 2,
    "area": 3,
}

_log = logging.getLogger(__name__)

BACKFILL_WALL_SEC = 20
"""Wall-clock seconds one call may spend describing commits (plan section 5.4).

Checked before each description, so a call starts no new LLM request past it;
the commits it leaves are counted in the result's ``describing``.
"""

MAX_BACKFILL_PER_CALL = 5
"""Commits one call may describe with an LLM, newest first (plan section 5.4)."""

Detail = Literal["compact", "full"]
"""How much of each evidence item an MCP tool returns (plan section 5.4, U8)."""

PROSE_BODY_MAX_CHARS = 4000
"""Characters of a PR or issue body a ``detail="full"`` result keeps.

The rest is cut (marked with ``...``) and counted in that entry's
``omitted.body_chars``. Commit bodies are not capped.
"""


class _EvidenceToolRequired(TypedDict):
    target: dict[str, Any]
    evidence: list[dict[str, Any]]
    describing: int


class EvidenceToolResult(_EvidenceToolRequired, total=False):
    """The ``whygraph_evidence_for`` result, for FastMCP's output schema.

    ``evidence`` items are the ``compact`` or ``full`` shape of
    :func:`shape_evidence_item`; ``pull_requests`` / ``issues`` are the
    ``detail="full"`` prose maps of :func:`shape_evidence`; ``platform``
    is set for a linked project. (Optional keys use ``total=False``:
    ``NotRequired`` is lost to the string annotations
    ``from __future__ import annotations`` makes.)
    """

    pull_requests: dict[str, dict[str, Any]]
    issues: dict[str, dict[str, Any]]
    platform: dict[str, Any]


@dataclass
class EvidenceResult:
    """What :func:`evidence_from_hunks` found for a set of blamed hunks.

    Attributes
    ----------
    evidence : list[CommitEvidence]
        The evidence bundle, newest first and capped - what
        :func:`collect_evidence` returns.
    unknown : list[BlameHunk]
        The given (``"blame"``-label) hunks whose SHA is not in the
        WhyGraph DB, once per SHA, in input order. Uncommitted hunks are
        never listed; walked, predecessor and PR-origin SHAs missing from
        the DB are dropped silently.
    """

    evidence: list[CommitEvidence] = field(default_factory=list)
    unknown: list[BlameHunk] = field(default_factory=list)


class _Blamer:
    """The blame calls of one evidence run: one ignore-revs file, one budget.

    ``.git-blame-ignore-revs`` is resolved (with ``check_inside``) once,
    here, and passed to every blame. ``budget`` caps the number of blame
    calls; :meth:`blame` returns ``None`` once it is spent.
    """

    def __init__(self, repo: Repository, *, budget: int | None = None) -> None:
        self.repo = repo
        self.ignore_revs_file: Path | None = repo.blame_ignore_revs_file()
        self.remaining = budget

    def blame(self, path: str, **kwargs: object) -> tuple[BlameHunk, ...] | None:
        """Run one blame, or return ``None`` when the budget is spent."""
        if self.remaining is not None:
            if self.remaining <= 0:
                return None
            self.remaining -= 1
        return self.repo.blame(
            path,
            ignore_revs_file=self.ignore_revs_file,
            **kwargs,  # type: ignore[arg-type]
        )


def _json_list(raw: str | None) -> list:
    """Decode a JSON-encoded list column; empty list on anything malformed."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _commit_titles_contain_oid(raw: str, sha: str) -> bool:
    """Return whether ``raw`` (a PR's ``commit_titles`` JSON) lists ``sha``.

    Compares the ``oid`` of each entry exactly — a substring scan of the
    JSON blob could false-match a SHA appearing in another field.
    """
    for entry in _json_list(raw):
        if isinstance(entry, dict) and entry.get("oid") == sha:
            return True
    return False


def _linked_prs(session: Session, sha: str) -> list[PullRequest]:
    """Pull requests that contain commit ``sha``.

    A PR contains the commit when its ``merge_commit_sha`` or ``head_sha``
    matches, or when the commit appears in the PR's ``commit_titles``. The
    ``LIKE`` only narrows candidates in SQL; ``_commit_titles_contain_oid``
    confirms the match by parsing the JSON.
    """
    stmt = select(PullRequest).where(
        (col(PullRequest.merge_commit_sha) == sha)
        | (col(PullRequest.head_sha) == sha)
        | (col(PullRequest.commit_titles).like(f"%{sha}%"))
    )
    matched = [
        pr
        for pr in session.exec(stmt).all()
        if pr.merge_commit_sha == sha
        or pr.head_sha == sha
        or _commit_titles_contain_oid(pr.commit_titles, sha)
    ]
    return sorted(matched, key=lambda pr: pr.number)


def _linked_issues(session: Session, prs: list[PullRequest]) -> list[Issue]:
    """Issues closed by any PR in ``prs`` (via ``pr_issue_link``)."""
    if not prs:
        return []
    numbers = [pr.number for pr in prs]
    link_stmt = select(PRIssueLink.issue_number).where(
        col(PRIssueLink.pr_number).in_(numbers),
        PRIssueLink.link_kind == "closes",
    )
    issue_numbers = sorted(set(session.exec(link_stmt).all()))
    if not issue_numbers:
        return []
    issues = session.exec(
        select(Issue).where(col(Issue.number).in_(issue_numbers))
    ).all()
    return sorted(issues, key=lambda issue: issue.number)


def collect_evidence(target: Target, *, limit: int = 20) -> list[CommitEvidence]:
    """Gather the historical evidence bundle for a code chunk.

    The collector combines four signals, each tagged with the
    :attr:`CommitEvidence.source` label that describes how it was
    discovered:

    1. ``"blame"`` — line-level attribution from the target's current
       range. The primary signal.
    2. ``"pr-origin"`` — when a blamed line lands on a squash-merged PR's
       commit that Stage 1 enriched, the same range is re-blamed at the
       PR's ``head_sha`` so each line maps back to the original
       feature-branch commit that authored it.
    3. ``"blame-walked"`` — line-level attribution surfaced by walking
       past refactor-heavy commits (scored at scan time). Each round
       runs blame again with ``--ignore-rev`` for every boring SHA seen
       so far; bounded by :data:`_MAX_BORING_HOPS`.
    4. ``"predecessor-blame"`` — for every rename event in the target
       path's lineage (``commit_file_change`` rows with ``change_type``
       ``"R"``), blame the predecessor file at the rename commit's
       parent so authorship for code that has been moved across files
       still surfaces.
    5. ``"area"`` — drawn from the ``commit_file_change`` index for the
       target's path and every rename ancestor. Used to fill the cap
       when the line-blame signals are thin.

    SHAs that appear under multiple labels are kept once at the strongest
    label (see :data:`_SOURCE_PRIORITY`). The final list is sorted newest
    first and capped at ``limit``.

    Parameters
    ----------
    target : Target
        The resolved code chunk.
    limit : int, optional
        Cap on the number of commits returned, newest first (default 20).

    Returns
    -------
    list[CommitEvidence]
        One entry per owning, scanned commit, newest first.

    Raises
    ------
    WhyGraphError
        If ``git blame`` fails, or the WhyGraph DB is missing/unscanned.
    """
    repo = Repository(repo_root())
    try:
        try:
            initial = repo.blame(target.path, target.line_start, target.line_end)
        except GitError as exc:
            raise WhyGraphError.wrap("git blame failed", exc, code="blame_failed")
        return evidence_from_hunks(repo, target, initial, limit=limit).evidence
    except OperationalError as exc:
        raise WhyGraphError(
            "WhyGraph DB is missing or unscanned — scan from the WhyGraph portal first"
        ) from exc


def evidence_from_hunks(
    repo: Repository,
    target: Target,
    hunks: Sequence[BlameHunk],
    *,
    limit: int = 20,
    git_budget: int | None = None,
) -> EvidenceResult:
    """The evidence bundle for already-blamed hunks of ``target``.

    The DB-dependent half of :func:`collect_evidence`: walk-past,
    predecessor-blame, squash-PR attribution, the commit / PR / issue join
    and the area-history fill, without blaming the working tree. Every
    git call runs against commit objects (each hunk's
    :attr:`BlameHunk.origins` at its own SHA), so a server clone answers
    for a developer's checkout.

    Parameters
    ----------
    repo : Repository
        The repository holding the hunks' commits.
    target : Target
        The code chunk the hunks were blamed from. Its path and range are
        reused by predecessor-blame and squash-PR attribution, and by the
        walk-past of a hunk without origins.
    hunks : Sequence[BlameHunk]
        The blame of ``target`` (uncommitted hunks are skipped).
    limit : int, optional
        Cap on the number of commits returned, newest first (default 20).
    git_budget : int or None, optional
        Cap on the number of blame calls this run may make; ``None``
        (default) means no cap. Calls past it are skipped: hunks stay
        unwalked, and further predecessor / squash blames do not run.

    Returns
    -------
    EvidenceResult
        The evidence, plus the given hunks whose SHA the DB does not know.

    Raises
    ------
    sqlalchemy.exc.OperationalError
        If the WhyGraph DB is missing or unscanned (:func:`collect_evidence`
        translates it).
    """
    initial = tuple(hunks)
    blamer = _Blamer(repo, budget=git_budget)
    walked_hunks, _boring = _walk_past_boring(
        repo, target, initial_hunks=initial, blamer=blamer
    )
    predecessor_hunks = _predecessor_blame(repo, target, blamer=blamer)

    base_labeled: list[tuple[BlameHunk, str]] = (
        [(h, "blame") for h in initial]
        + [(h, "blame-walked") for h in walked_hunks]
        + [(h, "predecessor-blame") for h in predecessor_hunks]
    )
    # SHAs the queried lines actually blamed to — the candidates for being
    # an enriched squash's merge_commit_sha.
    blame_shas = {h.sha for h, _ in base_labeled if not h.is_uncommitted}

    by_sha: dict[str, CommitEvidence] = {}
    unknown: dict[str, BlameHunk] = {}
    with get_session() as session:
        # Per-line squash attribution: re-blame at each enriched squash PR's
        # head_sha so origin commits surface with the existing dedupe /
        # priority / cap machinery (no special-casing past the label).
        origin_hunks = _attribute_squash_origins(
            repo, target, blame_shas=blame_shas, session=session, blamer=blamer
        )
        labeled_hunks = base_labeled + [(h, "pr-origin") for h in origin_hunks]
        for hunk, source in labeled_hunks:
            if hunk.is_uncommitted:
                continue
            commit = session.get(Commit, hunk.sha)
            if commit is None:
                if source == "blame":
                    unknown.setdefault(hunk.sha, hunk)
                continue
            if not _should_replace(by_sha.get(hunk.sha), source):
                continue
            prs = _linked_prs(session, hunk.sha)
            issues = _linked_issues(session, prs)
            by_sha[hunk.sha] = CommitEvidence(
                commit,
                tuple(prs),
                tuple(issues),
                source=source,
                lines=hunk_lines(hunk) if source == "blame" else None,
            )
        # Detach so the loaded columns remain readable after the
        # session closes.
        session.expunge_all()

    items = list(by_sha.values())
    remaining = limit - len(items)
    if remaining > 0:
        from .path_history import area_history_commits

        extras = area_history_commits(
            target.path,
            limit=remaining,
            exclude_shas=set(by_sha),
        )
        items.extend(extras)

    items.sort(key=lambda item: item.commit.committed_at or "", reverse=True)
    return EvidenceResult(evidence=items[:limit], unknown=list(unknown.values()))


def _should_replace(existing: CommitEvidence | None, new_source: str) -> bool:
    """Whether a newly-discovered source supersedes a SHA we've already kept."""
    if existing is None:
        return True
    return _SOURCE_PRIORITY.get(new_source, 99) < _SOURCE_PRIORITY.get(
        existing.source, 99
    )


def _walk_past_boring(
    repo: Repository,
    target: Target,
    *,
    initial_hunks: tuple[BlameHunk, ...],
    blamer: _Blamer | None = None,
) -> tuple[list[BlameHunk], set[str]]:
    """Blame refactor-heavy hunks past their own commit, up to a cap.

    Returns the set of hunks that *appeared only after* a boring commit
    was ignored, along with the set of boring SHAs we ended up walking
    past. Used by :func:`evidence_from_hunks` to tag those hunks
    ``source="blame-walked"``.

    Each hop blames every boring hunk at its own SHA - per origin path,
    the hunk's line ranges on that path - with every boring SHA seen so
    far ignored, so no working tree is needed. A hunk git refuses to
    blame keeps its lines (the walk goes on with the others); a spent
    ``blamer`` budget leaves the remaining hunks unwalked. New hunks are
    merged by SHA before the next hop, and each inherits the first
    ``final_start`` of the boring hunk(s) it came from, which keeps the
    first-appearance order of a working-tree re-blame.
    """
    if blamer is None:
        blamer = _Blamer(repo)
    seen_shas = {h.sha for h in initial_hunks if not h.is_uncommitted}
    boring_shas = _boring_shas_in(seen_shas)
    if not boring_shas:
        return [], set()

    walked: list[BlameHunk] = []
    ignored = set(boring_shas)
    frontier = [
        (h, _first_final_start(h))
        for h in initial_hunks
        if not h.is_uncommitted and h.sha in boring_shas
    ]
    for _ in range(_MAX_BORING_HOPS):
        found: dict[str, tuple[BlameHunk, int]] = {}
        spent = False
        for hunk, order_key in frontier:
            hunks = _blame_hunk_at_its_sha(blamer, target, hunk, ignored)
            if hunks is None:
                spent = True
                break
            for new in hunks:
                if new.is_uncommitted or new.sha in seen_shas:
                    continue
                prior = found.get(new.sha)
                if prior is None:
                    found[new.sha] = (new, order_key)
                else:
                    found[new.sha] = (
                        _merge_hunks(prior[0], new),
                        min(prior[1], order_key),
                    )
        # sorted() is stable: ties keep first-appearance order.
        new_walked = sorted(found.values(), key=lambda pair: pair[1])
        walked.extend(h for h, _ in new_walked)
        seen_shas.update(found)
        if spent:
            break
        new_boring = _boring_shas_in(set(found)) - ignored
        if not new_boring:
            break
        ignored.update(new_boring)
        frontier = [(h, key) for h, key in new_walked if h.sha in new_boring]
    return walked, ignored


def _first_final_start(hunk: BlameHunk) -> int:
    """The first blamed-file line ``hunk`` owns (``0`` without origins)."""
    return min((o.final_start for o in hunk.origins), default=0)


def _blame_hunk_at_its_sha(
    blamer: _Blamer, target: Target, hunk: BlameHunk, ignored: set[str]
) -> list[BlameHunk] | None:
    """Blame ``hunk``'s lines at ``hunk.sha`` with ``ignored`` walked past.

    One blame per origin path, with that path's ranges. A hunk without
    origins falls back to ``target``'s path and range. Returns ``[]`` when
    git refuses any of the calls (the hunk stays unwalked), ``None`` when
    the budget runs out.
    """
    ignore_revs = tuple(sorted(ignored))
    by_path: dict[str, list[tuple[int, int]]] = {}
    for origin in hunk.origins:
        by_path.setdefault(origin.path, []).append((origin.start, origin.end))
    if not by_path:
        by_path[target.path] = [(target.line_start, target.line_end)]
    out: list[BlameHunk] = []
    for path, ranges in by_path.items():
        try:
            hunks = blamer.blame(
                path,
                ranges=_normalize_ranges(ranges),
                rev=hunk.sha,
                ignore_revs=ignore_revs,
            )
        except GitError:
            # Walk-past is best-effort: if git refuses the call (e.g. an
            # ignored SHA can't be resolved), this hunk keeps its lines
            # and the walk goes on with the others.
            return []
        if hunks is None:
            return None
        out.extend(hunks)
    return out


def _normalize_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort ``ranges`` and merge overlapping or adjacent ones."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _merge_hunks(first: BlameHunk, other: BlameHunk) -> BlameHunk:
    """One hunk for a SHA two blames returned: lines summed, origins unioned."""
    origins = first.origins + tuple(o for o in other.origins if o not in first.origins)
    return BlameHunk(
        sha=first.sha,
        lines_owned=first.lines_owned + other.lines_owned,
        author_name=first.author_name or other.author_name,
        author_email=first.author_email or other.author_email,
        summary=first.summary or other.summary,
        committed_at=first.committed_at or other.committed_at,
        origins=origins,
    )


def _boring_shas_in(shas: set[str]) -> set[str]:
    """Return the subset of ``shas`` whose ``refactor_score`` is boring.

    Restricted to default-branch commits (``on_default_branch == 1``):
    refactor-walk is a main-walk-only notion, and recovered PR-origin
    commits (``0``) must never drive a walk-past — they carry no
    ``commit_file_change`` rows and default ``refactor_score=0``, so the
    guard is belt-and-suspenders against a future broad consumer.
    """
    if not shas:
        return set()
    with get_session() as session:
        rows = session.exec(
            select(Commit.sha)
            .where(col(Commit.sha).in_(shas))
            .where(col(Commit.refactor_score) >= BORING_THRESHOLD)
            .where(col(Commit.on_default_branch) == 1)
        ).all()
    return set(rows)


def _predecessor_blame(
    repo: Repository, target: Target, *, blamer: _Blamer | None = None
) -> list[BlameHunk]:
    """Blame ``target``'s line range inside every rename predecessor.

    For each rename event in the target's lineage, this re-runs blame
    against the predecessor file as it existed at the rename commit's
    parent. The line range is reused as-is — when the predecessor file
    was too short for the range, git errors out and the event is
    skipped (predecessor-blame is best-effort signal, not a strict
    guarantee). A spent ``blamer`` budget stops it.
    """
    if blamer is None:
        blamer = _Blamer(repo)
    out: list[BlameHunk] = []
    for rename_commit_sha, predecessor_path in _rename_events_for(target.path):
        parent_sha = _first_parent_of(rename_commit_sha)
        if parent_sha is None:
            continue
        try:
            hunks = blamer.blame(
                predecessor_path,
                line_start=target.line_start,
                line_end=target.line_end,
                rev=parent_sha,
            )
        except GitError:
            continue
        if hunks is None:
            break
        out.extend(h for h in hunks if not h.is_uncommitted)
    return out


def _enriched_squash_prs_for(
    session: Session, blame_shas: set[str]
) -> list[PullRequest]:
    """Squash PRs in ``blame_shas`` that have recovered origin commits.

    A PR qualifies when its ``merge_commit_sha`` is one of the blamed SHAs
    (so the queried lines land on its squash commit) **and** at least one
    of its ``commit_titles`` oids exists in ``commit`` as an origin row
    (``on_default_branch == 0``) — i.e. Stage 1 actually enriched it, so
    ``head_sha``'s objects are local and a re-blame there will resolve.
    This gate-agnostic predicate covers exactly the PRs the enricher
    recovered, whether they were gated as file-bulk or commit-rich.
    """
    if not blame_shas:
        return []
    prs = session.exec(
        select(PullRequest).where(col(PullRequest.merge_commit_sha).in_(blame_shas))
    ).all()
    enriched: list[PullRequest] = []
    for pr in prs:
        oids = [
            entry["oid"]
            for entry in _json_list(pr.commit_titles)
            if isinstance(entry, dict) and entry.get("oid")
        ]
        if not oids:
            continue
        has_origin = session.exec(
            select(Commit.sha)
            .where(col(Commit.sha).in_(oids))
            .where(col(Commit.on_default_branch) == 0)
            .limit(1)
        ).first()
        if has_origin is not None:
            enriched.append(pr)
    return enriched


def _attribute_squash_origins(
    repo: Repository,
    target: Target,
    *,
    blame_shas: set[str],
    session: Session,
    blamer: _Blamer | None = None,
) -> list[BlameHunk]:
    """Re-blame the target range at each enriched squash PR's ``head_sha``.

    When a blamed line lands on a squash ``merge_commit_sha``, the squash
    tree equals the PR's ``head_sha`` tree for that file, so blaming the
    same range at ``head_sha`` maps each line back to the original
    feature-branch commit that authored it — the same mechanism
    predecessor-blame uses with ``rev=parent_sha``. Best-effort: a
    squash-vs-head mismatch or absent object skips that PR (mirrors
    :func:`_predecessor_blame`'s per-event ``GitError`` swallow), leaving
    the PR-level Stage 1 evidence untouched. A spent ``blamer`` budget
    stops it.
    """
    if blamer is None:
        blamer = _Blamer(repo)
    out: list[BlameHunk] = []
    for pr in _enriched_squash_prs_for(session, blame_shas):
        try:
            hunks = blamer.blame(
                target.path,
                line_start=target.line_start,
                line_end=target.line_end,
                rev=pr.head_sha,
            )
        except GitError:
            continue
        if hunks is None:
            break
        out.extend(h for h in hunks if not h.is_uncommitted)
    return out


def _rename_events_for(path: str) -> list[tuple[str, str]]:
    """Return ``(rename_commit_sha, predecessor_path)`` for every rename in path's lineage."""
    # Lazy import: path_history reuses _linked_prs / _linked_issues from
    # this module, so eager imports would create a cycle.
    from .path_history import branch_scope, current_branch_scope, resolve_path_aliases

    current_branch = current_branch_scope()
    out: list[tuple[str, str]] = []
    with get_session() as session:
        aliases = resolve_path_aliases(session, path, current_branch=current_branch)
        if not aliases:
            return []
        rows = session.exec(
            select(CommitFileChange.commit_sha, CommitFileChange.renamed_from)
            .join(Commit, col(Commit.sha) == col(CommitFileChange.commit_sha))
            .where(col(CommitFileChange.path).in_(aliases))
            .where(col(CommitFileChange.change_type).in_(("R", "C")))
            .where(col(CommitFileChange.renamed_from).is_not(None))
            .where(branch_scope(current_branch))
        ).all()
    for row in rows:
        sha = row[0]
        predecessor = row[1]
        if sha and predecessor:
            out.append((sha, predecessor))
    return out


def _first_parent_of(sha: str) -> str | None:
    """First parent SHA of a scanned commit, or ``None`` if it's a root."""
    with get_session() as session:
        commit = session.get(Commit, sha)
        if commit is None:
            return None
        parents = commit.parent_shas.split() if commit.parent_shas else []
    return parents[0] if parents else None


def backfill_evidence_descriptions(
    items: list[CommitEvidence],
    *,
    target_path: str,
    allow: Callable[[], bool] | None = None,
    max_commits: int = MAX_BACKFILL_PER_CALL,
    wall_sec: float = BACKFILL_WALL_SEC,
    on_start: Callable[[int], None] | None = None,
) -> int:
    """Lazily backfill ``llm_description`` for the commits in ``items``, bounded.

    The MCP tools that consume the evidence call this once they're sure
    they actually need the description text — :func:`whygraph_evidence_for`
    before serializing, :func:`whygraph.mcp.rationale.whygraph_rationale_brief`
    only on a rationale-cache miss. Putting the call site in the tools (not
    in :func:`collect_evidence`) keeps the rationale-cache path free of LLM
    cost when it hits.

    Two regimes, split on the commit's file count
    (``analyze.large_commit_file_count``):

    * **Normal commits** — described whole-diff via
      :func:`~whygraph.analyze.backfill_all` when their
      ``llm_description`` is still ``NULL``.
    * **Bulk commits** (imports / squash merges) — never described
      whole-diff. Instead the slice for ``target_path`` is described via
      :func:`~whygraph.analyze.backfill_file_description` (cached on the
      ``commit_file_change`` row) and written onto the in-memory commit's
      ``llm_description`` *for this request only*, so both the evidence
      serializer and the rationale prompt see the file-specific text
      while the DB ``commit`` row keeps its stub. If the bulk commit did
      not touch ``target_path`` (e.g. a rename predecessor), the stub is
      left in place. An already cached per-file description is applied
      without an LLM call and does not count against the bound.

    The commits that need an LLM call are described in ``items`` order
    (newest first), at most ``max_commits`` of them and none started after
    ``wall_sec`` seconds; the rest are left for a later call and counted
    in the return value.

    Silently degrades when the configured analyze provider is unavailable —
    the commit keeps whatever ``llm_description`` it already had (a stub for
    bulk commits, ``None`` otherwise) and downstream consumers already gate
    on truthiness.

    Parameters
    ----------
    items : list[CommitEvidence]
        The evidence bundle whose descriptions to fill in, newest first.
    target_path : str
        The path the caller resolved the target to — the file used to
        slice bulk commits' diffs. For blame / blame-walked / area
        evidence this is the file the queried lines live in.
    allow : callable, optional
        A budget (the platform's org limit for agent requests): called
        once per commit just before its LLM call, and that commit is
        skipped when it returns ``False``. ``None`` (default) describes
        everything the bound lets through.
    max_commits : int, optional
        How many commits this call may describe with an LLM. Default
        :data:`MAX_BACKFILL_PER_CALL`; ``0`` describes none and only
        counts them (a tool's ``describe=False``).
    wall_sec : float, optional
        No description starts once this many seconds have passed since
        the first one. Default :data:`BACKFILL_WALL_SEC`. A description
        already running finishes (its own bound is the provider timeout).
    on_start : callable, optional
        Called once with the number of commits this call is about to
        describe, before the first LLM call, only when that number is
        positive (the rationale tool's progress step).

    Returns
    -------
    int
        The commits that still need a description because the bound
        stopped this call: what an MCP result reports as ``describing``.
        ``0`` when nothing was left, when the caller may not cause LLM
        spend, or when no provider is configured.

    Notes
    -----
    Does nothing when the bound project context has ``llm_allowed`` false
    (a project viewer's portal request, M2f-1 plan section 4.6): a viewer
    never causes LLM spend, so commits keep whatever description they have.
    Stops before the next describe once the bound usage sink reports an
    exhausted hard-stopped budget (M2f-2 plan section 4.7).
    """
    ctx = current_project()
    if ctx is not None and not ctx.llm_allowed:
        return 0
    from whygraph.core import get_config

    threshold = get_config().analyze.large_commit_file_count
    pending: list[tuple[Commit, bool]] = []
    for item in items:
        commit = item.commit
        if commit.files_changed > threshold:
            cached = _cached_file_description(commit.sha, target_path)
            if cached is not None:
                # In-memory override for this request (see the docstring).
                commit.llm_description = cached
            else:
                pending.append((commit, True))
        elif commit.llm_description is None:
            pending.append((commit, False))
    if not pending:
        return 0
    if max_commits <= 0:
        return len(pending)
    # Lazy imports mirror the pattern in `whygraph.scan.child`
    # and keep the module's import-time surface free of analyze/LLM deps.
    from whygraph.analyze import (
        LlmDescriptor,
        backfill_all,
        backfill_file_description,
    )
    from whygraph.services.llm import LlmError

    try:
        descriptor = LlmDescriptor.from_config(get_config())
    except LlmError as exc:
        _log.debug("skipping lazy LLM description backfill: %s", exc)
        return 0

    repository = Repository(repo_root())
    if on_start is not None:
        on_start(min(len(pending), max_commits))
    deadline = time.monotonic() + wall_sec
    spent = 0
    for index, (commit, bulk) in enumerate(pending):
        if spent >= max_commits or time.monotonic() >= deadline:
            left = len(pending) - index
            _log.info("lazy LLM description backfill bounded: %d left", left)
            return left
        if usage_blocked() is not None:
            # An exhausted hard-stopped budget (M2f-2 plan section 4.7):
            # stop before the next describe; backfill_all checks the same.
            break
        if allow is not None and not allow():
            continue
        spent += 1
        if not bulk:
            backfill_all([commit], repository=repository, descriptor=descriptor)
            continue
        try:
            text = backfill_file_description(
                commit, target_path, repository=repository, descriptor=descriptor
            )
        except Exception as exc:  # noqa: BLE001 — one bad file must not poison the batch
            _log.warning(
                "lazy per-file description backfill failed for %s @ %s: %s",
                commit.sha[:12],
                target_path,
                exc,
            )
            continue
        if text is not None:
            # In-memory override for this request: the serializer and the
            # rationale prompt read commit.llm_description; the DB row
            # keeps its bulk-commit stub.
            commit.llm_description = text
    return 0


def _cached_file_description(sha: str, path: str) -> str | None:
    """The stored per-file description of ``(sha, path)``, if any."""
    with get_session() as session:
        return session.exec(
            select(CommitFileChange.llm_description).where(
                CommitFileChange.commit_sha == sha,
                CommitFileChange.path == path,
            )
        ).first()


def _commit_dict(commit: Commit) -> dict:
    """Serialize a scanned commit to a JSON-ready dict.

    Notes
    -----
    ``llm_description`` leads deliberately. It is generated from the diff
    alone — the developer's commit message is never shown to that generator
    (see :meth:`whygraph.analyze.LlmDescriptor.describe`) — so it is the
    reliable account of what changed, while ``subject`` / ``body`` are human
    prose that may be terse, stale, or wrong. An agent anchors on the field it
    reads first, and for years that was ``subject``. The key **set** is
    unchanged, so this is not a contract change for the MCP tools.
    """
    return {
        "sha": commit.sha,
        "llm_description": commit.llm_description,
        "subject": commit.subject,
        "body": commit.body,
        "author_name": commit.author_name,
        "author_email": commit.author_email,
        "committed_at": commit.committed_at,
    }


def _pr_dict(pr: PullRequest) -> dict:
    """Serialize a pull request to a JSON-ready dict.

    Notes
    -----
    ``commit_titles`` and ``comments`` are emitted **uncapped**: the
    consumer of ``whygraph_evidence_for`` is an agent that can handle the
    full lists. The size caps in :class:`RationaleConfig` apply only to the
    LLM *rationale prompt* (see ``analyze.rationale_generator._format_pr``),
    not to this raw tool output.
    """
    return {
        "number": pr.number,
        "title": pr.title,
        "body": pr.body,
        "state": pr.state,
        "merged_at": pr.merged_at,
        "author": pr.author,
        "html_url": pr.html_url,
        "labels": _json_list(pr.labels),
        "commit_titles": _json_list(pr.commit_titles),
        "comments": _json_list(pr.comments),
    }


def _issue_dict(issue: Issue) -> dict:
    """Serialize an issue to a JSON-ready dict."""
    return {
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "state": issue.state,
        "author": issue.author,
        "html_url": issue.html_url,
        "labels": _json_list(issue.labels),
    }


def hunk_lines(hunk: BlameHunk) -> tuple[tuple[int, int], ...] | None:
    """The working-tree line ranges ``hunk`` owns, merged and sorted.

    Read off ``hunk.origins`` (``final_start`` plus the range length);
    ``None`` for a hunk without origins.
    """
    spans = sorted(
        (o.final_start, o.final_start + (o.end - o.start)) for o in hunk.origins
    )
    if not spans:
        return None
    merged = [spans[0]]
    for start, end in spans[1:]:
        if start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _evidence_dict(item: CommitEvidence) -> dict:
    """Serialize one :class:`CommitEvidence` to a JSON-ready dict."""
    return {
        "commit": _commit_dict(item.commit),
        "pull_requests": [_pr_dict(pr) for pr in item.pull_requests],
        "issues": [_issue_dict(issue) for issue in item.issues],
        "source": item.source,
    }


# ---------------------------------------------------------------------------
# The MCP item shapes (plan section 5.4): compact by default, full on request
# ---------------------------------------------------------------------------


def _link_ref(kind: str, entry: dict) -> dict:
    """A PR or issue as a compact link: number, title, URL and its resource."""
    number = entry.get("number")
    return {
        "number": number,
        "title": entry.get("title"),
        "html_url": entry.get("html_url"),
        "resource": f"whygraph://{kind}/{number}",
    }


def _clip(text: object, max_chars: int) -> object:
    """``text`` cut to ``max_chars`` (marked with ``...``); non-strings unchanged."""
    if isinstance(text, str) and len(text) > max_chars:
        return text[:max_chars] + "..."
    return text


def _body_overflow(body: object) -> int:
    """Characters of ``body`` past :data:`PROSE_BODY_MAX_CHARS` (0 for a non-string)."""
    if isinstance(body, str):
        return max(0, len(body) - PROSE_BODY_MAX_CHARS)
    return 0


def _pr_prose(pr: dict, caps: RationaleConfig) -> dict:
    """One PR's ``full`` entry: capped body and comments, the roster, ``omitted``."""
    comments = [c for c in pr.get("comments") or [] if isinstance(c, dict)]
    titles = list(pr.get("commit_titles") or [])
    kept = comments[: caps.pr_discussion_max_comments]
    body = pr.get("body")
    out: dict = {
        "text": {
            "body": _clip(body, PROSE_BODY_MAX_CHARS),
            "comments": [
                {**c, "body": _clip(c.get("body"), caps.pr_comment_max_chars)}
                for c in kept
            ],
        },
        "commit_titles": titles[: caps.pr_roster_max_commits],
    }
    omitted = {
        "comments": len(comments) - len(kept),
        "commit_titles": len(titles) - len(out["commit_titles"]),
        "body_chars": _body_overflow(body),
    }
    if any(omitted.values()):
        out["omitted"] = omitted
    return out


def _issue_prose(issue: dict) -> dict:
    """One issue's ``full`` entry: its capped body and ``omitted`` when cut."""
    body = issue.get("body")
    out: dict = {"text": {"body": _clip(body, PROSE_BODY_MAX_CHARS)}}
    overflow = _body_overflow(body)
    if overflow:
        out["omitted"] = {"body_chars": overflow}
    return out


def shape_evidence_item(item: dict, detail: Detail = "compact") -> dict:
    """One evidence item in the shape the MCP tools return.

    Parameters
    ----------
    item : dict
        An item in the nested shape of :func:`_evidence_dict` (the
        ``/api/v1`` wire shape, also what the Explorer reads), optionally
        with ``lines`` and ``push_status``. Missing keys read as ``None``,
        so an older platform's answer still shapes.
    detail : {"compact", "full"}, optional
        ``"compact"`` (default): ``sha``, ``llm_description``,
        ``subject``, ``author``, ``date``, ``lines``, ``source`` and the
        linked PRs and issues as ``{number, title, html_url, resource}``.
        ``"full"`` adds the commit ``body`` under ``text``; the PR and
        issue prose is not on the item but in the top-level maps
        :func:`shape_evidence` builds, once per number.

    Returns
    -------
    dict
        The shaped item; ``push_status`` is kept when the input has one.
    """
    commit = item.get("commit") or {}
    out = {
        "sha": commit.get("sha"),
        "llm_description": commit.get("llm_description"),
        "subject": commit.get("subject"),
        "author": commit.get("author_name"),
        "date": commit.get("committed_at"),
        "lines": item.get("lines"),
        "source": item.get("source"),
    }
    if "push_status" in item:
        out["push_status"] = item["push_status"]
    out["pull_requests"] = [
        _link_ref("pr", pr) for pr in item.get("pull_requests") or []
    ]
    out["issues"] = [_link_ref("issue", issue) for issue in item.get("issues") or []]
    if detail == "full":
        out["text"] = {"body": commit.get("body")}
    return out


def shape_evidence(items: list[dict], detail: Detail = "compact") -> dict:
    """Evidence items in the shape the MCP tools return, PR / issue prose once.

    Parameters
    ----------
    items : list of dict
        Items in the nested shape :func:`shape_evidence_item` takes.
    detail : {"compact", "full"}, optional
        ``"compact"`` (default) returns only ``evidence``. ``"full"`` also
        returns ``pull_requests`` and ``issues``: maps keyed by the number
        as a string, one entry per PR or issue however many items link it
        (the first item's copy wins). A PR entry holds the untrusted prose
        under ``text`` (its ``body`` clipped to
        :data:`PROSE_BODY_MAX_CHARS`, its first ``pr_discussion_max_comments``
        comments, each clipped to ``pr_comment_max_chars``), its first
        ``pr_roster_max_commits`` ``commit_titles`` and, when anything was
        cut, ``omitted`` (``comments``, ``commit_titles``, ``body_chars``);
        an issue entry holds ``text.body`` (clipped the same way) and
        ``omitted.body_chars`` when cut.

    Returns
    -------
    dict
        ``{"evidence": [...]}``, plus ``pull_requests`` and ``issues`` for
        ``"full"``.
    """
    out: dict = {"evidence": [shape_evidence_item(i, detail) for i in items]}
    if detail != "full":
        return out
    from whygraph.core import get_config

    caps = get_config().rationale
    prs: dict[str, dict] = {}
    issues: dict[str, dict] = {}
    for item in items:
        for pr in item.get("pull_requests") or []:
            key = str(pr.get("number"))
            if key not in prs:
                prs[key] = _pr_prose(pr, caps)
        for issue in item.get("issues") or []:
            key = str(issue.get("number"))
            if key not in issues:
                issues[key] = _issue_prose(issue)
    out["pull_requests"] = prs
    out["issues"] = issues
    return out


# ---------------------------------------------------------------------------
# Linked projects: the platform answers for the pushed SHAs
# ---------------------------------------------------------------------------

PUSH_UNCOMMITTED = "uncommitted"
"""The lines are not committed yet, so no SHA exists to ask about."""

PUSH_NOT_PUSHED = "not_pushed"
"""The owning commit is in this checkout only - no ``origin/*`` ref holds it."""

PUSH_NOT_ON_DEFAULT = "not_on_default_branch"
"""The owning commit is pushed, but not on the branch the platform scans."""

PUSH_PENDING_SCAN = "pending_scan"
"""Pushed and on the default branch, but the platform has not scanned it yet."""

LOCAL_SOURCE = "local"
"""The ``source`` of an evidence item built from blame alone (no platform row)."""

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class PushSplit:
    """Blame hunks divided by what a platform can be told about them.

    Nothing a platform is not already holding may leave the machine (M2e
    plan section 0.2 #8), so only :attr:`pushed` is ever sent.

    Attributes
    ----------
    pushed : tuple[BlameHunk, ...]
        Committed hunks some ``origin/*`` ref contains, in blame order -
        the ones the platform may be asked about.
    on_default : frozenset[str]
        Which of those SHAs the default-branch remote ref contains. A
        pushed SHA the platform does not know is ``pending_scan`` when it
        is on the default branch and ``not_on_default_branch`` otherwise.
    local : tuple[tuple[BlameHunk, str], ...]
        The hunks that stay here, each with its ``push_status``
        (:data:`PUSH_UNCOMMITTED` or :data:`PUSH_NOT_PUSHED`), in blame
        order.
    """

    pushed: tuple[BlameHunk, ...] = ()
    on_default: frozenset[str] = frozenset()
    local: tuple[tuple[BlameHunk, str], ...] = ()


def default_remote_refs(repo: Repository) -> tuple[str, ...]:
    """The full remote-tracking refs of ``repo``'s default branch.

    :attr:`Repository.default_branch_refs` answers in full refnames, local
    branches (``refs/heads/*``) included. Only the remote-tracking entries
    are kept: a local branch says nothing about what has been pushed.

    Returns
    -------
    tuple[str, ...]
        ``("refs/remotes/origin/main",)``, or empty when the default
        branch does not resolve (the caller must read that as "cannot
        judge", never as "off the default branch").
    """
    return tuple(r for r in repo.default_branch_refs if r.startswith("refs/remotes/"))


def split_by_push_status(repo: Repository, hunks: Sequence[BlameHunk]) -> PushSplit:
    """Classify blamed ``hunks`` by whether the platform can see them.

    Purely local: the ``origin/*`` refs as the last fetch left them, never
    a network call (M2e plan section 0.1 #2 - no fetch on demand).

    Parameters
    ----------
    repo : Repository
        The developer's checkout.
    hunks : Sequence[BlameHunk]
        A working-tree blame of the target.

    Returns
    -------
    PushSplit
        The sendable hunks, which of them are on the default branch, and
        the ones that stay local with their labels.

    Notes
    -----
    When the default branch does not resolve (:func:`default_remote_refs`
    is empty) every pushed hunk counts as on-default, so an unknown SHA
    reads as ``pending_scan`` rather than being mislabelled as living on
    another branch.
    """
    default = set(default_remote_refs(repo))
    pushed: list[BlameHunk] = []
    on_default: set[str] = set()
    local: list[tuple[BlameHunk, str]] = []
    for hunk in hunks:
        if hunk.is_uncommitted:
            local.append((hunk, PUSH_UNCOMMITTED))
            continue
        refs = repo.remote_refs_containing(hunk.sha)
        if not refs:
            local.append((hunk, PUSH_NOT_PUSHED))
            continue
        pushed.append(hunk)
        if not default or default & set(refs):
            on_default.add(hunk.sha)
    return PushSplit(tuple(pushed), frozenset(on_default), tuple(local))


def path_tracked_at(repo: Repository, path: str, revs: Sequence[str]) -> bool:
    """Whether ``path`` is tracked at any of ``revs``.

    Parameters
    ----------
    repo : Repository
        The checkout.
    path : str
        A repository-relative path.
    revs : Sequence[str]
        Revisions known to be pushed (a remote-tracking ref, or a pushed
        commit SHA).

    Returns
    -------
    bool
        ``True`` as soon as one of them tracks it; ``False`` for none, for
        no revisions at all, or when ``git`` fails.
    """
    for rev in revs:
        try:
            if repo.tracked_paths(rev, path):
                return True
        except GitError:
            return False
    return False


def pushed_target(repo: Repository, target: Target, split: PushSplit) -> Target | None:
    """``target`` reduced to what may be sent to the platform.

    Two fields are privileged. The **path** goes only when it is tracked
    at a pushed revision; when it is not (a file renamed or added locally
    and not pushed) the path of a pushed hunk's own origin takes its place,
    because that path exists in a commit the platform already holds. The
    **qualified name** goes only when the target's first line - the symbol's
    declaration - blames to a pushed SHA.

    Parameters
    ----------
    repo : Repository
        The checkout.
    target : Target
        The locally resolved target.
    split : PushSplit
        Its classified hunks.

    Returns
    -------
    Target or None
        The target to send, or ``None`` when nothing may be sent (no
        pushed hunk, or no pushed path to name it by) - in which case no
        request is made at all.
    """
    if not split.pushed:
        return None
    path = _pushed_path(repo, target.path, split)
    if path is None:
        return None
    return Target(
        path=path,
        line_start=target.line_start,
        line_end=target.line_end,
        qualified_name=(
            target.qualified_name if _declaration_pushed(target, split) else None
        ),
    )


def _pushed_path(repo: Repository, path: str, split: PushSplit) -> str | None:
    """``path`` when a pushed rev tracks it, else a pushed hunk's origin path."""
    revs = [*default_remote_refs(repo)[:1], split.pushed[0].sha]
    if path_tracked_at(repo, path, revs):
        return path
    for hunk in split.pushed:
        for origin in hunk.origins:
            return origin.path
    return None


def _declaration_pushed(target: Target, split: PushSplit) -> bool:
    """Whether ``target``'s first line is owned by a pushed commit.

    Read off the blame that produced ``split``: an origin range starting at
    working-tree line ``final_start`` covers as many lines as it spans at
    its own commit. A target with no name, or a line no pushed origin
    claims, answers ``False`` - the conservative direction.
    """
    if target.qualified_name is None:
        return False
    line = target.line_start
    for hunk in split.pushed:
        for origin in hunk.origins:
            if (
                origin.final_start
                <= line
                <= origin.final_start + (origin.end - origin.start)
            ):
                return True
    return False


def local_push_status(repo: Repository, sha: str) -> str | None:
    """Why the platform could not answer for ``sha``, judged locally.

    Parameters
    ----------
    repo : Repository
        The checkout.
    sha : str
        A full commit SHA.

    Returns
    -------
    str or None
        One of :data:`PUSH_NOT_PUSHED`, :data:`PUSH_PENDING_SCAN`,
        :data:`PUSH_NOT_ON_DEFAULT`, or ``None`` when the commit is not in
        this checkout either.
    """
    if not repo.has_commit(sha):
        return None
    refs = repo.remote_refs_containing(sha)
    if not refs:
        return PUSH_NOT_PUSHED
    default = set(default_remote_refs(repo))
    if not default or default & set(refs):
        return PUSH_PENDING_SCAN
    return PUSH_NOT_ON_DEFAULT


def local_evidence_dict(hunk: BlameHunk, push_status: str) -> dict:
    """An evidence item built from blame alone, for a SHA the platform lacks.

    Parameters
    ----------
    hunk : BlameHunk
        The blamed hunk (its porcelain metadata is all there is).
    push_status : str
        Why the platform has nothing for it.

    Returns
    -------
    dict
        The shape of :func:`_evidence_dict` with ``source: "local"``, a
        null ``llm_description`` (nothing generated it) and no PRs or
        issues (those live in the platform's database), plus
        ``push_status``.
    """
    return {
        "commit": {
            "sha": hunk.sha,
            "llm_description": None,
            "subject": hunk.summary or "",
            "body": "",
            "author_name": hunk.author_name,
            "author_email": hunk.author_email,
            "committed_at": hunk.committed_at,
        },
        "pull_requests": [],
        "issues": [],
        "source": LOCAL_SOURCE,
        "lines": [list(r) for r in hunk_lines(hunk) or ()] or None,
        "push_status": push_status,
    }


def _parsed_time(value: object) -> datetime:
    """``value`` as an aware datetime; the epoch floor for anything unusable.

    The two sides time-stamp differently - the platform's rows carry
    ``git``'s ``%cI`` with the committer's offset, a blame hunk carries UTC -
    so the merged list must sort on parsed instants, not on strings.
    """
    if not isinstance(value, str) or not value:
        return _EPOCH
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return _EPOCH
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def merge_linked_evidence(
    platform_items: Sequence[dict],
    unknown_shas: Sequence[str],
    split: PushSplit,
    *,
    limit: int,
) -> list[dict]:
    """Merge a platform's evidence with the locally labelled hunks.

    Parameters
    ----------
    platform_items : Sequence[dict]
        What the platform answered (empty when it was not asked, or could
        not be reached).
    unknown_shas : Sequence[str]
        The sent SHAs it has no row for.
    split : PushSplit
        The classification the request was built from.
    limit : int
        Cap on the merged list.

    Returns
    -------
    list[dict]
        Newest first by parsed timestamp, one entry per SHA - a platform
        item always beats a locally built one - capped at ``limit``.
    """
    by_sha: dict[str, dict] = {}
    for item in platform_items:
        sha = (item.get("commit") or {}).get("sha")
        if isinstance(sha, str) and sha not in by_sha:
            by_sha[sha] = item
    pushed = {hunk.sha: hunk for hunk in split.pushed}
    for sha in unknown_shas:
        hunk = pushed.get(sha)
        if hunk is None or sha in by_sha:
            continue
        by_sha[sha] = local_evidence_dict(
            hunk,
            PUSH_PENDING_SCAN if sha in split.on_default else PUSH_NOT_ON_DEFAULT,
        )
    for hunk, push_status in split.local:
        by_sha.setdefault(hunk.sha, local_evidence_dict(hunk, push_status))
    for hunk in (*split.pushed, *(h for h, _ in split.local)):
        item = by_sha.get(hunk.sha)
        if item is not None and "lines" not in item:
            item["lines"] = [list(r) for r in hunk_lines(hunk) or ()] or None
    items = sorted(
        by_sha.values(),
        key=lambda item: _parsed_time((item.get("commit") or {}).get("committed_at")),
        reverse=True,
    )
    return items[:limit]


def blame_target(repo: Repository, target: Target) -> tuple[BlameHunk, ...]:
    """Blame ``target``'s range in the working tree, as a tool error on failure."""
    try:
        return repo.blame(target.path, target.line_start, target.line_end)
    except GitError as exc:
        raise WhyGraphError.wrap("git blame failed", exc, code="blame_failed")


def linked_evidence(remote: RemoteProject, target: Target, *, limit: int) -> dict:
    """``whygraph_evidence_for``'s answer for a project linked to a platform.

    Blames the working tree here, sends only the pushed hunks, and labels
    everything the platform could not account for with its
    ``push_status``. Never opens a local WhyGraph database, never
    backfills a description and never touches the rationale cache - a
    linked project has none of the three.

    Parameters
    ----------
    remote : RemoteProject
        The project's platform.
    target : Target
        The resolved target.
    limit : int
        Cap on the evidence items.

    Returns
    -------
    dict
        ``{"target", "evidence", "describing", "platform"}`` with items in
        the nested shape; ``describing`` is always ``0`` (descriptions
        are the platform's). ``platform.answered`` is ``False`` when the
        platform did not account for the pushed hunks (nothing pushed to
        ask about, or the call failed offline / revoked / removed), so
        the ``pending_scan`` / ``not_on_default_branch`` labels are local
        guesses.
    """
    repo = Repository(repo_root())
    split = split_by_push_status(repo, blame_target(repo, target))
    sendable = pushed_target(repo, target, split)
    items: list[dict] = []
    unknown: list[str] = [hunk.sha for hunk in split.pushed]
    answered = False
    if sendable is not None:
        try:
            items, unknown = remote.evidence(sendable, split.pushed, limit)
            answered = True
        except RemoteError:
            # Offline, revoked or removed: the agent still gets blame, with
            # every pushed SHA labelled from the local refs.
            items, unknown = [], [hunk.sha for hunk in split.pushed]
    return {
        "target": target_dict(target),
        "evidence": merge_linked_evidence(items, unknown, split, limit=limit),
        "describing": 0,
        "platform": {**platform_block(remote), "answered": answered},
    }


def evidence_payload(
    path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    qualified_name: str | None = None,
    limit: int = 20,
    describe: bool = True,
) -> dict:
    """Historical evidence for a chunk of code, items in the nested shape.

    The body of :func:`whygraph_evidence_for` before its items are shaped,
    and what the Explorer's Evidence tab reads (``item.commit.sha``).

    Parameters
    ----------
    path, line_start, line_end, qualified_name
        The target (see :func:`~whygraph.mcp.targets.resolve_target`).
    limit : int, optional
        Cap on the evidence items. Default 20.
    describe : bool, optional
        Backfill missing commit descriptions, bounded by
        :data:`MAX_BACKFILL_PER_CALL` / :data:`BACKFILL_WALL_SEC`
        (default). ``False`` never calls an LLM.

    Returns
    -------
    dict
        ``{"target", "evidence", "describing"}``: items in the
        :func:`_evidence_dict` shape plus ``lines``; ``describing`` counts
        the commits left undescribed by the bound (or by
        ``describe=False``). A project linked to a WhyGraph platform is
        answered by :func:`linked_evidence`, which adds a ``platform``
        block.

    Raises
    ------
    WhyGraphError
        A bad ``limit`` or target, or a failing blame.
    """
    _log.debug(
        "whygraph_evidence_for called: path=%r line_start=%r line_end=%r "
        "qualified_name=%r limit=%d",
        path,
        line_start,
        line_end,
        qualified_name,
        limit,
    )
    if limit < 1:
        raise WhyGraphError("limit must be >= 1")
    target = resolve_target(
        path=path,
        line_start=line_start,
        line_end=line_end,
        qualified_name=qualified_name,
    )
    ctx = current_project()
    if ctx is not None and ctx.remote is not None:
        return linked_evidence(ctx.remote, target, limit=limit)
    evidence = collect_evidence(target, limit=limit)
    describing = backfill_evidence_descriptions(
        evidence,
        target_path=target.path,
        max_commits=MAX_BACKFILL_PER_CALL if describe else 0,
    )
    return {
        "target": target_dict(target),
        "evidence": [
            {
                **_evidence_dict(item),
                "lines": [list(r) for r in item.lines or ()] or None,
            }
            for item in evidence
        ],
        "describing": describing,
    }


def whygraph_evidence_for(
    path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    qualified_name: str | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 10,
    detail: Detail = "compact",
    describe: bool = True,
) -> EvidenceToolResult:
    """MCP tool — historical evidence for a chunk of code.

    See :data:`~whygraph.mcp.descriptions.EVIDENCE_FOR_DESCRIPTION` for the agent-facing summary.

    :func:`evidence_payload` with its items passed through
    :func:`shape_evidence` at ``detail``. For a project linked to a
    WhyGraph platform the answer comes from :func:`linked_evidence`
    instead, and carries a ``platform`` block.
    """
    result = evidence_payload(
        path=path,
        line_start=line_start,
        line_end=line_end,
        qualified_name=qualified_name,
        limit=limit,
        describe=describe,
    )
    return {**result, **shape_evidence(result["evidence"], detail)}


def register(mcp: FastMCP) -> None:
    """Attach the evidence tool to an MCP server."""
    mcp.tool(
        name="whygraph_evidence_for",
        title=EVIDENCE_FOR_ANNOTATIONS.title,
        description=EVIDENCE_FOR_DESCRIPTION,
        annotations=EVIDENCE_FOR_ANNOTATIONS,
    )(log_tool_errors(structured(EvidenceToolResult)(whygraph_evidence_for)))
