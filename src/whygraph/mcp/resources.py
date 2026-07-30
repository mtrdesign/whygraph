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

import json
import logging

from mcp.server.fastmcp import FastMCP
from sqlalchemy import func
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, col, select

from whygraph.core.utils import LIKE_ESCAPE_CHAR, like_escape
from whygraph.db import get_session
from whygraph.db.models import (
    Commit,
    CommitFileChange,
    Issue,
    PRIssueLink,
    PullRequest,
)

from .errors import WhyGraphError
from .evidence import _json_list, _linked_prs
from .path_history import resolve_path_aliases

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

    ``llm_description`` leads for the same reason it does in
    :func:`whygraph.mcp.evidence._commit_dict`: it is diff-derived and
    therefore the reliable account of what changed, and an agent anchors on
    whichever field it reads first. Key order only — the key set is unchanged.
    """
    return {
        "sha": commit.sha,
        "llm_description": commit.llm_description,
        "subject": commit.subject,
        "body": commit.body,
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


_RECENT_DESCRIPTION_CHARS = 240
"""``llm_description`` is truncated in the recent-activity listing: the point
is a cheap index the model can scan in one round, and a dozen full paragraphs
would cost more context than the drill-down calls it replaces."""


def _recent_activity_resource(limit: int = 10) -> dict:
    """Most recent commits, pull requests, and issues — a scannable index.

    Exists because every other history read needs an identifier the caller
    must already know (a SHA, a PR number, a file path). That left
    "what shipped lately?" with no entry point: the assistant had to walk
    the tree and read files, which burns tool rounds and still misses the
    history sitting in the DB.

    Deliberately **compact** — subject / title / author / timestamp and a
    truncated description, no bodies or comment threads. It is an index:
    the model picks what matters and drills in with
    :func:`_commit_resource` or :func:`_pr_resource`.

    Commits are restricted to the first-parent main walk
    (``on_default_branch == 1``), matching area-history and the
    refactor-walk — "what shipped" is a default-branch question, and
    PR-origin commits recovered from squash merges would double-count.

    Parameters
    ----------
    limit : int, optional
        Maximum rows per category (not in total). Default 10.

    Returns
    -------
    dict
        ``{"limit", "commits", "pull_requests", "issues"}``. A category
        with nothing scanned comes back as an empty list rather than
        being omitted, so the model can tell "none" from "not asked".

    Raises
    ------
    WhyGraphError
        The DB is missing or unscanned.

    Notes
    -----
    Lives beside the resource bodies to reuse their session handling and
    ``OperationalError`` contract, but is **not** registered in
    :func:`register` — the chat tool is its only caller, and WhyGraph's MCP
    surface deliberately stays narrow.
    """
    _log.debug("recent activity resource read: limit=%r", limit)
    limit = max(1, limit)
    try:
        with get_session() as session:
            commits = session.exec(
                select(Commit)
                .where(Commit.on_default_branch == 1)
                .order_by(col(Commit.authored_at).desc())
                .limit(limit)
            ).all()
            prs = session.exec(
                select(PullRequest)
                .order_by(col(PullRequest.updated_at).desc())
                .limit(limit)
            ).all()
            issues = session.exec(
                select(Issue).order_by(col(Issue.updated_at).desc()).limit(limit)
            ).all()

            return {
                "limit": limit,
                "commits": [
                    {
                        "sha": c.sha,
                        "subject": c.subject,
                        "description": _truncate(c.llm_description),
                        "author_name": c.author_name,
                        "authored_at": c.authored_at,
                        "files_changed": c.files_changed,
                        "insertions": c.insertions,
                        "deletions": c.deletions,
                    }
                    for c in commits
                ],
                "pull_requests": [
                    {
                        "number": p.number,
                        "title": p.title,
                        "state": p.state,
                        "draft": p.draft,
                        "merged_at": p.merged_at,
                        "updated_at": p.updated_at,
                        "author": p.author,
                        "labels": _json_list(p.labels),
                    }
                    for p in prs
                ],
                "issues": [
                    {
                        "number": i.number,
                        "title": i.title,
                        "state": i.state,
                        "closed_at": i.closed_at,
                        "updated_at": i.updated_at,
                        "author": i.author,
                    }
                    for i in issues
                ],
            }
    except OperationalError as exc:
        raise WhyGraphError(_DB_UNSCANNED_MESSAGE) from exc


def _truncate(text: str | None) -> str | None:
    """Clip a commit description to :data:`_RECENT_DESCRIPTION_CHARS`."""
    if text is None:
        return None
    if len(text) <= _RECENT_DESCRIPTION_CHARS:
        return text
    return text[:_RECENT_DESCRIPTION_CHARS] + "…"


_FIND_CHANGES_MAX_LIMIT = 50
"""Hard cap on the number of ``find_changes`` rows a caller may ask for.

A row-count cap alone does **not** bound the payload, because descriptions are
emitted in full and vary from a sentence to several paragraphs — that is what
:data:`_FIND_CHANGES_PAYLOAD_BUDGET` is for.
"""

_FIND_CHANGES_PAYLOAD_BUDGET = 26_000
"""Char budget for the ``commits`` array, the real bound on result size.

Sits under the chat registry's 30,000-char result cap
(:data:`whygraph.chat.tools.MAX_RESULT_CHARS`) with room for the envelope.
Duplicated as a literal rather than imported because ``mcp`` must not depend on
``chat`` — ``chat`` is an adapter over this layer, not the reverse.

**Measured, after getting this wrong once:** descriptions average ~1,280 chars,
so ``limit=30`` on this repo produced 30,014 chars — over the cap, truncated
mid-string, and therefore **invalid JSON the model could not parse at all**.
Exactly the failure §2.5 of the plan documents and this tool exists to avoid.
Row assembly stops at this budget and says how many rows it dropped, because a
short honest answer beats a long broken one.
"""

_FIND_CHANGES_MATCHED_PATHS = 5
"""How many matching paths to echo back per commit, when a path filter is used."""


def _pr_commit_shas(pr: PullRequest) -> set[str]:
    """Every commit SHA a pull request contains.

    The inverse of :func:`whygraph.mcp.evidence._linked_prs`: that function
    asks "which PRs contain this commit", this one asks "which commits does
    this PR contain". Same three sources — the merge commit, the head commit,
    and the ``commit_titles`` blob — and the same discipline of comparing
    ``oid`` exactly rather than scanning the JSON as text.
    """
    shas = {pr.merge_commit_sha, pr.head_sha}
    shas.update(
        entry["oid"]
        for entry in _json_list(pr.commit_titles)
        if isinstance(entry, dict) and entry.get("oid")
    )
    shas.discard(None)
    return shas


def _find_changes_resource(
    query: str | None = None,
    path: str | None = None,
    limit: int = 15,
) -> dict:
    """Commits found by *what they changed* — keyword and/or path.

    The one content-keyed history read. Every other one needs an identifier the
    caller must already know: :func:`whygraph.mcp.evidence.whygraph_evidence_for`
    needs a symbol, :func:`area_history_commits` needs an exact file path,
    :func:`_recent_activity_resource` knows only recency. That left defect
    investigation with no entry point, because a defect is reported in the
    vocabulary of *behaviour* ("sessions vanish after a refresh") and the only
    text written in that vocabulary is the diff descriptions.

    Parameters
    ----------
    query : str, optional
        Whitespace-separated terms, **AND**-ed. Each is matched
        case-insensitively as a substring of the commit's own text
        (``llm_description`` / ``subject`` / ``body``) or of a linked pull
        request's **title**. PR *bodies* are deliberately not searched — they
        are 39x the size of the titles and are the unreliable prose this tool
        exists to route around.
    path : str, optional
        A repo-relative file or directory prefix, expanded through
        :func:`resolve_path_aliases` so a renamed file's earlier history is
        included rather than silently dropped.
    limit : int, optional
        Maximum commits, newest first. Default 15, clamped to
        ``[1, _FIND_CHANGES_MAX_LIMIT]``.

    Returns
    -------
    dict
        ``{"query", "path", "count", "commits"}``. Each commit carries its
        **full** ``llm_description`` — that is the payload, so it is never
        clipped — plus stats and ``linked_prs`` as number + title. PR bodies,
        comments, commit titles, and the commit ``body`` are omitted: together
        they are 73% of the area-history payload that truncates today.

    Raises
    ------
    WhyGraphError
        The DB is missing or unscanned.

    Notes
    -----
    Not registered in :func:`register` — the chat tool is its only caller, and
    WhyGraph's MCP surface deliberately stays narrow. It is a **sibling** of
    ``whygraph_area_history`` rather than a fix to it: that tool's uncapped
    blobs are documented and deliberate, and the planner subagents consume it.
    """
    _log.debug("find changes: query=%r path=%r limit=%r", query, path, limit)
    terms = (query or "").split()
    path = (path or "").strip().removeprefix("./").rstrip("/")
    if not terms and not path:
        return {
            "error": (
                "pass `query` (keywords matched against commit diff "
                "descriptions and PR titles) and/or `path` (a file or "
                "directory prefix). Without a filter this would return the "
                "whole history."
            )
        }
    limit = max(1, min(int(limit), _FIND_CHANGES_MAX_LIMIT))

    try:
        with get_session() as session:
            aliases = resolve_path_aliases(session, path) if path else set()
            shas = _find_changes_shas(session, terms, path, aliases)
            if not shas:
                return {"query": query, "path": path or None, "count": 0, "commits": []}

            commits = session.exec(
                select(Commit)
                .where(col(Commit.sha).in_(shas))
                .where(col(Commit.on_default_branch) == 1)
                .order_by(col(Commit.authored_at).desc())
                .limit(limit)
            ).all()
            # Assemble under a char budget, not just a row count: descriptions
            # are emitted in full and vary wildly, so N rows is not a bound on
            # payload size. Stopping early keeps the JSON valid and parseable.
            rows: list[dict] = []
            spent = 0
            for commit in commits:
                row = _find_changes_row(session, commit, path, aliases)
                cost = len(json.dumps(row, default=str))
                if rows and spent + cost > _FIND_CHANGES_PAYLOAD_BUDGET:
                    break
                rows.append(row)
                spent += cost
            dropped = len(commits) - len(rows)
    except OperationalError as exc:
        raise WhyGraphError(_DB_UNSCANNED_MESSAGE) from exc

    result = {
        "query": query,
        "path": path or None,
        "count": len(rows),
        "commits": rows,
    }
    if dropped:
        # Say what was withheld and how to get it. Silence here would read as
        # "these are all the matches", which is the one wrong answer that
        # cannot be spotted downstream.
        result["omitted"] = dropped
        result["note"] = (
            f"{dropped} more matching commit(s) were withheld to keep this "
            "result parseable — the descriptions are long. Narrow the query, "
            "add a `path`, or fetch specific SHAs with get_commit."
        )
    return result


def _find_changes_shas(
    session: Session, terms: list[str], path: str, aliases: set[str]
) -> set[str]:
    """The candidate SHA set for :func:`_find_changes_resource`.

    Keyword matching runs in two passes because the two haystacks live in
    different tables. The commit's own text is one ``LIKE`` per term, AND-ed in
    SQL. A linked PR's **title** is resolved separately — matching PR titles
    first, then expanding each to the commits it contains via
    :func:`_pr_commit_shas` — because the commit-to-PR linkage needs the exact
    ``oid`` comparison that a SQL join over the JSON blob cannot express.
    """
    if terms:
        stmt = select(Commit.sha)
        for term in terms:
            pattern = f"%{like_escape(term)}%"
            stmt = stmt.where(
                func.coalesce(col(Commit.llm_description), "").like(
                    pattern, escape=LIKE_ESCAPE_CHAR
                )
                | func.coalesce(col(Commit.subject), "").like(
                    pattern, escape=LIKE_ESCAPE_CHAR
                )
                | func.coalesce(col(Commit.body), "").like(
                    pattern, escape=LIKE_ESCAPE_CHAR
                )
            )
        shas = set(session.exec(stmt).all())

        # Second haystack: PR titles. A commit whose own text says nothing
        # useful — the common case for a squash merge — is still findable
        # through the title of the PR that carried it.
        pr_stmt = select(PullRequest)
        for term in terms:
            pr_stmt = pr_stmt.where(
                col(PullRequest.title).like(
                    f"%{like_escape(term)}%", escape=LIKE_ESCAPE_CHAR
                )
            )
        for pr in session.exec(pr_stmt).all():
            shas.update(_pr_commit_shas(pr))
    else:
        shas = None  # path-only search: every commit touching the path

    if not path:
        return shas or set()

    path_stmt = select(CommitFileChange.commit_sha).where(
        col(CommitFileChange.path).in_(aliases)
        | col(CommitFileChange.path).like(
            f"{like_escape(path)}/%", escape=LIKE_ESCAPE_CHAR
        )
    )
    touching = set(session.exec(path_stmt).all())
    return touching if shas is None else shas & touching


def _find_changes_row(
    session: Session, commit: Commit, path: str, aliases: set[str]
) -> dict:
    """One :func:`_find_changes_resource` result row — description first."""
    row = {
        "sha": commit.sha,
        # Never clipped: this is the payload, not a preview of it.
        "llm_description": commit.llm_description,
        "subject": _truncate(commit.subject),
        "authored_at": commit.authored_at,
        "author_name": commit.author_name,
        "files_changed": commit.files_changed,
        "insertions": commit.insertions,
        "deletions": commit.deletions,
    }
    if path:
        # Which of the commit's files the filter actually hit — so a directory
        # search shows *where*, and an alias hit shows the pre-rename name.
        touched = session.exec(
            select(CommitFileChange.path).where(
                col(CommitFileChange.commit_sha) == commit.sha
            )
        ).all()
        matched = sorted(
            {p for p in touched if p in aliases or p.startswith(f"{path}/")}
        )
        row["matched_paths"] = matched[:_FIND_CHANGES_MATCHED_PATHS]
    row["linked_prs"] = [
        # Number plus title only. The title is in the search haystack, so the
        # model can see *why* a PR-title-only match came back.
        {"number": pr.number, "title": pr.title}
        for pr in _linked_prs(session, commit.sha)
    ]
    return row


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
            commit_count = session.exec(select(func.count()).select_from(Commit)).one()
            pr_count = session.exec(select(func.count()).select_from(PullRequest)).one()
            issue_count = session.exec(select(func.count()).select_from(Issue)).one()
            link_count = session.exec(
                select(func.count()).select_from(PRIssueLink)
            ).one()

            earliest, latest = session.exec(
                select(
                    func.min(Commit.authored_at),
                    func.max(Commit.authored_at),
                )
            ).one()
            latest_scanned_at = session.exec(select(func.max(Commit.scanned_at))).one()
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
