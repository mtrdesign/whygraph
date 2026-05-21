"""The ``whygraph_evidence_for`` MCP tool and its evidence collector.

The collector walks ``git blame`` for a code chunk, then joins the owning
commits to the pull requests that contain them and the issues those PRs
close — producing the :class:`~whygraph.analyze.CommitEvidence` bundle the
rationale generator consumes. :mod:`whygraph.mcp.rationale` reuses
:func:`collect_evidence`; the tool itself serializes the bundle to JSON.
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, col, select

from whygraph.analyze import CommitEvidence
from whygraph.db import get_session
from whygraph.db.models import Commit, Issue, PRIssueLink, PullRequest
from whygraph.services.git import GitError, Repository

from ._shared import Target, WhyGraphError, repo_root, resolve_target, target_dict

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

    Blames the chunk to find its owning commits, then joins each to its
    pull requests and closing issues. SHAs that ``git blame`` reports but
    that are absent from the WhyGraph DB (uncommitted lines, or a DB that
    predates the commit) are skipped — only scanned commits become
    evidence.

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
        hunks = repo.blame(target.path, target.line_start, target.line_end)
    except GitError as exc:
        raise WhyGraphError(f"git blame failed: {exc}") from exc

    items: list[CommitEvidence] = []
    try:
        with get_session() as session:
            for hunk in hunks:
                commit = session.get(Commit, hunk.sha)
                if commit is None:
                    continue
                prs = _linked_prs(session, hunk.sha)
                issues = _linked_issues(session, prs)
                items.append(CommitEvidence(commit, tuple(prs), tuple(issues)))
            # Detach the rows before the session commits + closes, so the
            # caller (and the rationale generator) can still read their
            # already-loaded columns. Expunged rows escape commit-time
            # expiry.
            session.expunge_all()
    except OperationalError as exc:
        raise WhyGraphError(
            "WhyGraph DB is missing or unscanned — run `whygraph scan` first"
        ) from exc

    items.sort(key=lambda item: item.commit.committed_at or "", reverse=True)
    return items[:limit]


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
    """Serialize a pull request to a JSON-ready dict."""
    return {
        "number": pr.number,
        "title": pr.title,
        "body": pr.body,
        "state": pr.state,
        "merged_at": pr.merged_at,
        "author": pr.author,
        "html_url": pr.html_url,
        "labels": _json_list(pr.labels),
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
    if limit < 1:
        raise WhyGraphError("limit must be >= 1")
    target = resolve_target(
        path=path,
        line_start=line_start,
        line_end=line_end,
        qualified_name=qualified_name,
    )
    evidence = collect_evidence(target, limit=limit)
    return {
        "target": target_dict(target),
        "evidence": [_evidence_dict(item) for item in evidence],
    }


def register(mcp: FastMCP) -> None:
    """Attach the evidence tool to an MCP server."""
    mcp.tool(name="whygraph_evidence_for", description=_TOOL_DESCRIPTION)(
        whygraph_evidence_for
    )
