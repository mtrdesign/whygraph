"""Read-only MCP resources backed by the WhyGraph SQLite database.

Exposes four URI-addressed resources that any MCP client can fetch and
pin into context without paying the per-call latency a tool roundtrip
costs:

* ``whygraph://commit/{sha}`` — a scanned commit and the pull requests
  that contain it.
* ``whygraph://pr/{number}`` — a pull request and the issues it closes.
* ``whygraph://issue/{number}`` — an issue and the pull requests that
  close it.
* ``whygraph://repo/overview`` — counts, scan-freshness timestamps,
  LLM-description coverage, and the top-10 commit authors.

Three behaviour rules are worth surfacing here because they diverge
from the existing tool modules:

1. **One-hop linking only.** A ``whygraph://commit/{sha}`` payload
   inlines its linked PRs but does **not** transitively inline those
   PRs' closing issues. The pre-III-migration phase-1 surface inlined
   both hops; this revival deliberately stops at one. Clients that need
   the issues should read ``whygraph://pr/{number}`` for each PR.

2. **Not-found is content, not an exception.** A missing row returns
   ``{"error": "not_found", "sha"|"number": <id>}`` as the resource
   payload. FastMCP double-wraps thrown exceptions ("Error reading
   resource ...: WhyGraphError(...)"), so a content-shaped 404 is
   easier for an agent to consume than a wrapped traceback. Hard
   errors (e.g. the DB hasn't been scanned yet) still raise
   ``WhyGraphError`` — those are setup failures the user must act on.

3. **Resource discoverability is split.** Templated URIs (the three
   with ``{...}`` segments) show up in :meth:`FastMCP.list_resource_templates`
   while the concrete ``whygraph://repo/overview`` shows up in
   :meth:`FastMCP.list_resources`. Clients must check both to find
   the full surface.
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP
from sqlalchemy import func
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, col, select

from whygraph.db import get_session
from whygraph.db.models import Commit, Issue, PRIssueLink, PullRequest

from .errors import WhyGraphError
from .evidence import _json_list, _linked_prs

_log = logging.getLogger(__name__)

_DB_UNSCANNED_MESSAGE = (
    "WhyGraph DB is missing or unscanned — run `whygraph scan` first"
)
_TOP_CONTRIBUTORS_LIMIT = 10


# ---- hydration -----------------------------------------------------------


def _hydrate_commit(commit: Commit) -> dict:
    """Serialize a :class:`Commit` row to a JSON-ready dict.

    ``parent_shas`` is returned as the raw space-delimited string the
    column stores — phase-1 hydrated it as JSON, but the current schema
    stores parents space-delimited (see ``evidence.py``'s
    ``_first_parent_of`` which calls ``.split()``). Callers that want a
    list should ``.split()`` themselves.
    """
    return {
        "sha": commit.sha,
        "subject": commit.subject,
        "body": commit.body,
        "llm_description": commit.llm_description,
        "author_name": commit.author_name,
        "author_email": commit.author_email,
        "authored_at": commit.authored_at,
        "committed_at": commit.committed_at,
        "parent_shas": commit.parent_shas,
        "files_changed": commit.files_changed,
        "insertions": commit.insertions,
        "deletions": commit.deletions,
        "refactor_score": commit.refactor_score,
    }


def _hydrate_pr(pr: PullRequest, *, nested: bool = False) -> dict:
    """Serialize a :class:`PullRequest` row to a JSON-ready dict.

    Parameters
    ----------
    pr : PullRequest
        The pull request row to hydrate.
    nested : bool, optional
        When ``True``, drops the two heavy JSON blob columns
        (``commit_titles`` and ``comments``) so the row stays compact
        inside another resource's payload. Direct reads of
        ``whygraph://pr/{number}`` use ``nested=False`` and include
        both blobs decoded as lists.
    """
    payload: dict = {
        "number": pr.number,
        "title": pr.title,
        "body": pr.body,
        "state": pr.state,
        "draft": pr.draft,
        "created_at": pr.created_at,
        "updated_at": pr.updated_at,
        "closed_at": pr.closed_at,
        "merged_at": pr.merged_at,
        "merge_commit_sha": pr.merge_commit_sha,
        "head_sha": pr.head_sha,
        "head_ref": pr.head_ref,
        "base_ref": pr.base_ref,
        "author": pr.author,
        "html_url": pr.html_url,
        "labels": _json_list(pr.labels),
    }
    if not nested:
        payload["commit_titles"] = _json_list(pr.commit_titles)
        payload["comments"] = _json_list(pr.comments)
    return payload


def _hydrate_issue(issue: Issue) -> dict:
    """Serialize an :class:`Issue` row to a JSON-ready dict."""
    return {
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "state": issue.state,
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
        "closed_at": issue.closed_at,
        "author": issue.author,
        "html_url": issue.html_url,
        "labels": _json_list(issue.labels),
    }


# ---- linkage helpers -----------------------------------------------------


def _closing_issues_for_pr(session: Session, pr_number: int) -> list[Issue]:
    """Issues closed by a single PR (via ``pr_issue_link``)."""
    issue_numbers = sorted(
        session.exec(
            select(PRIssueLink.issue_number)
            .where(PRIssueLink.pr_number == pr_number)
            .where(PRIssueLink.link_kind == "closes")
        ).all()
    )
    if not issue_numbers:
        return []
    issues = session.exec(
        select(Issue).where(col(Issue.number).in_(issue_numbers))
    ).all()
    return sorted(issues, key=lambda issue: issue.number)


def _closing_prs_for_issue(session: Session, issue_number: int) -> list[PullRequest]:
    """Pull requests that close a single issue (via ``pr_issue_link``)."""
    pr_numbers = sorted(
        session.exec(
            select(PRIssueLink.pr_number)
            .where(PRIssueLink.issue_number == issue_number)
            .where(PRIssueLink.link_kind == "closes")
        ).all()
    )
    if not pr_numbers:
        return []
    prs = session.exec(
        select(PullRequest).where(col(PullRequest.number).in_(pr_numbers))
    ).all()
    return sorted(prs, key=lambda pr: pr.number)


# ---- resource bodies -----------------------------------------------------


def _commit_resource(sha: str) -> dict:
    """Read the resource backing ``whygraph://commit/{sha}``."""
    _log.debug("commit resource read: sha=%r", sha)
    try:
        with get_session() as session:
            commit = session.get(Commit, sha)
            if commit is None:
                return {"error": "not_found", "sha": sha}
            prs = _linked_prs(session, sha)
            return {
                "commit": _hydrate_commit(commit),
                "linked_prs": [_hydrate_pr(pr, nested=True) for pr in prs],
            }
    except OperationalError as exc:
        raise WhyGraphError(_DB_UNSCANNED_MESSAGE) from exc


def _pr_resource(number: int) -> dict:
    """Read the resource backing ``whygraph://pr/{number}``."""
    _log.debug("pr resource read: number=%r", number)
    try:
        with get_session() as session:
            pr = session.get(PullRequest, number)
            if pr is None:
                return {"error": "not_found", "number": number}
            issues = _closing_issues_for_pr(session, number)
            return {
                "pull_request": _hydrate_pr(pr),
                "closing_issues": [_hydrate_issue(issue) for issue in issues],
            }
    except OperationalError as exc:
        raise WhyGraphError(_DB_UNSCANNED_MESSAGE) from exc


def _issue_resource(number: int) -> dict:
    """Read the resource backing ``whygraph://issue/{number}``."""
    _log.debug("issue resource read: number=%r", number)
    try:
        with get_session() as session:
            issue = session.get(Issue, number)
            if issue is None:
                return {"error": "not_found", "number": number}
            prs = _closing_prs_for_issue(session, number)
            return {
                "issue": _hydrate_issue(issue),
                "closing_prs": [_hydrate_pr(pr, nested=True) for pr in prs],
            }
    except OperationalError as exc:
        raise WhyGraphError(_DB_UNSCANNED_MESSAGE) from exc


def _repo_overview_resource() -> dict:
    """Read the resource backing ``whygraph://repo/overview``.

    Aggregates counts, scan-freshness timestamps, LLM-description
    coverage, and the top-10 commit authors. Aggregations come from the
    ``commit`` / ``pull_request`` / ``issue`` / ``pr_issue_link`` tables
    directly — ``top_contributors`` is computed from ``Commit`` (not the
    ``Author`` table) because authors-resolution is a separate scan step
    that may not have run.
    """
    _log.debug("repo overview resource read")
    try:
        with get_session() as session:
            commit_count = session.exec(
                select(func.count()).select_from(Commit)
            ).one()
            pr_count = session.exec(
                select(func.count()).select_from(PullRequest)
            ).one()
            issue_count = session.exec(
                select(func.count()).select_from(Issue)
            ).one()
            link_count = session.exec(
                select(func.count()).select_from(PRIssueLink)
            ).one()

            earliest, latest = session.exec(
                select(
                    func.min(Commit.authored_at),
                    func.max(Commit.authored_at),
                )
            ).one()
            latest_scanned_at = session.exec(
                select(func.max(Commit.scanned_at))
            ).one()
            latest_pr_fetched_at = session.exec(
                select(func.max(PullRequest.fetched_at))
            ).one()
            latest_issue_fetched_at = session.exec(
                select(func.max(Issue.fetched_at))
            ).one()

            described_count = session.exec(
                select(func.count())
                .select_from(Commit)
                .where(col(Commit.llm_description).is_not(None))
            ).one()

            contributor_rows = session.exec(
                select(
                    Commit.author_name,
                    Commit.author_email,
                    func.count().label("commit_count"),
                )
                .group_by(Commit.author_name, Commit.author_email)
                .order_by(func.count().desc(), Commit.author_name.asc())
                .limit(_TOP_CONTRIBUTORS_LIMIT)
            ).all()
    except OperationalError as exc:
        raise WhyGraphError(_DB_UNSCANNED_MESSAGE) from exc

    fraction = described_count / commit_count if commit_count else 0.0
    return {
        "counts": {
            "commits": commit_count,
            "pull_requests": pr_count,
            "issues": issue_count,
            "pr_issue_links": link_count,
        },
        "commit_date_range": {
            "earliest_authored_at": earliest,
            "latest_authored_at": latest,
        },
        "scan_freshness": {
            "latest_scanned_at": latest_scanned_at,
            "latest_pr_fetched_at": latest_pr_fetched_at,
            "latest_issue_fetched_at": latest_issue_fetched_at,
        },
        "llm_description_coverage": {
            "total_commits": commit_count,
            "described": described_count,
            "fraction": fraction,
        },
        "top_contributors": [
            {
                "author_name": row[0],
                "author_email": row[1],
                "commit_count": row[2],
            }
            for row in contributor_rows
        ],
    }


# ---- registration --------------------------------------------------------


def register(mcp: FastMCP) -> None:
    """Attach the four read-only resources to an MCP server."""
    mcp.resource(
        "whygraph://commit/{sha}",
        name="whygraph_commit",
        description=(
            "A scanned commit and the pull requests that contain it "
            "(one hop; closing issues not inlined)."
        ),
        mime_type="application/json",
    )(_commit_resource)
    mcp.resource(
        "whygraph://pr/{number}",
        name="whygraph_pull_request",
        description=(
            "A pull request and the issues it closes. Includes full "
            "`commit_titles` and `comments` blobs."
        ),
        mime_type="application/json",
    )(_pr_resource)
    mcp.resource(
        "whygraph://issue/{number}",
        name="whygraph_issue",
        description="An issue and the pull requests that close it.",
        mime_type="application/json",
    )(_issue_resource)
    mcp.resource(
        "whygraph://repo/overview",
        name="whygraph_repo_overview",
        description=(
            "Repository-level summary: row counts, commit date range, "
            "scan freshness, LLM-description coverage, top contributors."
        ),
        mime_type="application/json",
    )(_repo_overview_resource)
