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

from mcp.server.fastmcp import FastMCP
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, col, select

from whygraph.analyze import CommitEvidence
from whygraph.db import get_session
from whygraph.db.models import Commit, CommitFileChange, Issue, PRIssueLink, PullRequest
from whygraph.scan.refactor_score import BORING_THRESHOLD
from whygraph.services.git import BlameHunk, GitError, Repository

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

_TOOL_DESCRIPTION = (
    "Find the historical evidence behind a chunk of code: the commits that "
    "own its lines (via git blame), plus the pull requests containing those "
    "commits and the issues those PRs close. Pass either (path, line_start, "
    "line_end) or a qualified_name (resolved to a file/line range via "
    "CodeGraph). Returns {target, evidence}. Run `whygraph scan` first to "
    "populate the WhyGraph database."
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
        initial = repo.blame(target.path, target.line_start, target.line_end)
    except GitError as exc:
        raise WhyGraphError.wrap("git blame failed", exc)

    try:
        return _collect_evidence_against_db(repo, target, initial, limit)
    except OperationalError as exc:
        raise WhyGraphError(
            "WhyGraph DB is missing or unscanned — run `whygraph scan` first"
        ) from exc


def _collect_evidence_against_db(
    repo: Repository,
    target: Target,
    initial: tuple[BlameHunk, ...],
    limit: int,
) -> list[CommitEvidence]:
    """The DB-dependent half of :func:`collect_evidence`.

    Split out so the caller can wrap every DB touch — walk-past,
    predecessor-blame, the main commit/PR/issue join, and the
    area-history fill — in a single ``OperationalError`` translation.
    """
    walked_hunks, _boring = _walk_past_boring(repo, target, initial_hunks=initial)
    predecessor_hunks = _predecessor_blame(repo, target)

    base_labeled: list[tuple[BlameHunk, str]] = (
        [(h, "blame") for h in initial]
        + [(h, "blame-walked") for h in walked_hunks]
        + [(h, "predecessor-blame") for h in predecessor_hunks]
    )
    # SHAs the queried lines actually blamed to — the candidates for being
    # an enriched squash's merge_commit_sha.
    blame_shas = {h.sha for h, _ in base_labeled if not h.is_uncommitted}

    by_sha: dict[str, CommitEvidence] = {}
    with get_session() as session:
        # Per-line squash attribution: re-blame at each enriched squash PR's
        # head_sha so origin commits surface with the existing dedupe /
        # priority / cap machinery (no special-casing past the label).
        origin_hunks = _attribute_squash_origins(
            repo, target, blame_shas=blame_shas, session=session
        )
        labeled_hunks = base_labeled + [(h, "pr-origin") for h in origin_hunks]
        for hunk, source in labeled_hunks:
            if hunk.is_uncommitted:
                continue
            commit = session.get(Commit, hunk.sha)
            if commit is None:
                continue
            if not _should_replace(by_sha.get(hunk.sha), source):
                continue
            prs = _linked_prs(session, hunk.sha)
            issues = _linked_issues(session, prs)
            by_sha[hunk.sha] = CommitEvidence(
                commit, tuple(prs), tuple(issues), source=source
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
    return items[:limit]


def _should_replace(existing: CommitEvidence | None, new_source: str) -> bool:
    """Whether a newly-discovered source supersedes a SHA we've already kept."""
    if existing is None:
        return True
    return _SOURCE_PRIORITY.get(new_source, 99) < _SOURCE_PRIORITY.get(
        existing.source, 99
    )


def _walk_past_boring(
    repo: Repository, target: Target, *, initial_hunks: tuple[BlameHunk, ...]
) -> tuple[list[BlameHunk], set[str]]:
    """Re-run blame with refactor-heavy commits ignored, up to a cap.

    Returns the set of hunks that *appeared only after* a boring commit
    was ignored, along with the set of boring SHAs we ended up walking
    past. Used by :func:`collect_evidence` to tag those hunks
    ``source="blame-walked"``.
    """
    seen_shas = {h.sha for h in initial_hunks if not h.is_uncommitted}
    boring_shas = _boring_shas_in(seen_shas)
    if not boring_shas:
        return [], set()

    walked: list[BlameHunk] = []
    ignored = set(boring_shas)
    for _ in range(_MAX_BORING_HOPS):
        try:
            hunks = repo.blame(
                target.path,
                target.line_start,
                target.line_end,
                ignore_revs=tuple(sorted(ignored)),
            )
        except GitError:
            # Walk-past is best-effort: if git refuses the call (e.g.
            # an ignored SHA can't be resolved), bail out cleanly and
            # keep whatever we already have.
            break
        new_walked = [
            h for h in hunks if not h.is_uncommitted and h.sha not in seen_shas
        ]
        walked.extend(new_walked)
        seen_shas.update(h.sha for h in new_walked)
        new_boring = _boring_shas_in({h.sha for h in new_walked}) - ignored
        if not new_boring:
            break
        ignored.update(new_boring)
    return walked, ignored


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


def _predecessor_blame(repo: Repository, target: Target) -> list[BlameHunk]:
    """Blame ``target``'s line range inside every rename predecessor.

    For each rename event in the target's lineage, this re-runs blame
    against the predecessor file as it existed at the rename commit's
    parent. The line range is reused as-is — when the predecessor file
    was too short for the range, git errors out and the event is
    skipped (predecessor-blame is best-effort signal, not a strict
    guarantee).
    """
    out: list[BlameHunk] = []
    for rename_commit_sha, predecessor_path in _rename_events_for(target.path):
        parent_sha = _first_parent_of(rename_commit_sha)
        if parent_sha is None:
            continue
        try:
            hunks = repo.blame(
                predecessor_path,
                target.line_start,
                target.line_end,
                rev=parent_sha,
            )
        except GitError:
            continue
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
) -> list[BlameHunk]:
    """Re-blame the target range at each enriched squash PR's ``head_sha``.

    When a blamed line lands on a squash ``merge_commit_sha``, the squash
    tree equals the PR's ``head_sha`` tree for that file, so blaming the
    same range at ``head_sha`` maps each line back to the original
    feature-branch commit that authored it — the same mechanism
    predecessor-blame uses with ``rev=parent_sha``. Best-effort: a
    squash-vs-head mismatch or absent object skips that PR (mirrors
    :func:`_predecessor_blame`'s per-event ``GitError`` swallow), leaving
    the PR-level Stage 1 evidence untouched.
    """
    out: list[BlameHunk] = []
    for pr in _enriched_squash_prs_for(session, blame_shas):
        try:
            hunks = repo.blame(
                target.path,
                target.line_start,
                target.line_end,
                rev=pr.head_sha,
            )
        except GitError:
            continue
        out.extend(h for h in hunks if not h.is_uncommitted)
    return out


def _rename_events_for(path: str) -> list[tuple[str, str]]:
    """Return ``(rename_commit_sha, predecessor_path)`` for every rename in path's lineage."""
    # Lazy import: path_history reuses _linked_prs / _linked_issues from
    # this module, so eager imports would create a cycle.
    from .path_history import resolve_path_aliases

    out: list[tuple[str, str]] = []
    with get_session() as session:
        aliases = resolve_path_aliases(session, path)
        if not aliases:
            return []
        rows = session.exec(
            select(CommitFileChange.commit_sha, CommitFileChange.renamed_from)
            .where(col(CommitFileChange.path).in_(aliases))
            .where(col(CommitFileChange.change_type).in_(("R", "C")))
            .where(col(CommitFileChange.renamed_from).is_not(None))
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
    items: list[CommitEvidence], *, target_path: str
) -> None:
    """Lazily backfill ``llm_description`` for any commit in ``items``.

    The MCP tools that consume the evidence call this once they're sure
    they actually need the description text — :func:`whygraph_evidence_for`
    before serializing, :func:`whygraph.mcp.rationale.whygraph_rationale_brief`
    only on a rationale-cache miss. Putting the call site in the tools (not
    in :func:`collect_evidence`) keeps the rationale-cache path free of LLM
    cost when it hits.

    Two regimes, split on the commit's file count
    (``analyze.large_commit_file_count``):

    * **Normal commits** — described whole-diff via
      :func:`~whygraph.analyze.backfill_all`, exactly as before, when
      their ``llm_description`` is still ``NULL``.
    * **Bulk commits** (imports / squash merges) — never described
      whole-diff. Instead the slice for ``target_path`` is described via
      :func:`~whygraph.analyze.backfill_file_description` (cached on the
      ``commit_file_change`` row) and written onto the in-memory commit's
      ``llm_description`` *for this request only*, so both the evidence
      serializer and the rationale prompt see the file-specific text
      while the DB ``commit`` row keeps its stub. If the bulk commit did
      not touch ``target_path`` (e.g. a rename predecessor), the stub is
      left in place.

    Silently degrades when the configured analyze provider is unavailable —
    the commit keeps whatever ``llm_description`` it already had (a stub for
    bulk commits, ``None`` otherwise) and downstream consumers already gate
    on truthiness.

    Parameters
    ----------
    items : list[CommitEvidence]
        The evidence bundle whose descriptions to fill in.
    target_path : str
        The path the caller resolved the target to — the file used to
        slice bulk commits' diffs. For blame / blame-walked / area
        evidence this is the file the queried lines live in.
    """
    from whygraph.core import get_config

    threshold = get_config().analyze.large_commit_file_count
    bulk = [it.commit for it in items if it.commit.files_changed > threshold]
    normal = [
        it.commit
        for it in items
        if it.commit.files_changed <= threshold and it.commit.llm_description is None
    ]
    if not bulk and not normal:
        return
    # Lazy imports mirror the pattern in `whygraph.cli.commands.scan.scan_cmd`
    # and keep the module's import-time surface free of analyze/LLM deps.
    from whygraph.analyze import (
        LlmDescriptor,
        backfill_all,
        backfill_file_description,
    )
    from whygraph.services.llm import LlmError

    try:
        descriptor = LlmDescriptor.from_config(get_config().analyze)
    except LlmError as exc:
        _log.debug("skipping lazy LLM description backfill: %s", exc)
        return

    repository = Repository(repo_root())
    for commit in bulk:
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
    backfill_all(normal, repository=repository, descriptor=descriptor)


def _commit_dict(commit: Commit) -> dict:
    """Serialize a scanned commit to a JSON-ready dict."""
    return {
        "sha": commit.sha,
        "subject": commit.subject,
        "body": commit.body,
        "llm_description": commit.llm_description,
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


def _evidence_dict(item: CommitEvidence) -> dict:
    """Serialize one :class:`CommitEvidence` to a JSON-ready dict."""
    return {
        "commit": _commit_dict(item.commit),
        "pull_requests": [_pr_dict(pr) for pr in item.pull_requests],
        "issues": [_issue_dict(issue) for issue in item.issues],
        "source": item.source,
    }


def whygraph_evidence_for(
    path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    qualified_name: str | None = None,
    limit: int = 20,
) -> dict:
    """MCP tool — historical evidence for a chunk of code.

    See :data:`_TOOL_DESCRIPTION` for the agent-facing summary.
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
    evidence = collect_evidence(target, limit=limit)
    backfill_evidence_descriptions(evidence, target_path=target.path)
    return {
        "target": target_dict(target),
        "evidence": [_evidence_dict(item) for item in evidence],
    }


def register(mcp: FastMCP) -> None:
    """Attach the evidence tool to an MCP server."""
    mcp.tool(name="whygraph_evidence_for", description=_TOOL_DESCRIPTION)(
        log_tool_errors(whygraph_evidence_for)
    )
