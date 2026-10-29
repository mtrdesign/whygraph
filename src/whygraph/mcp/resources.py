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

A project *linked* to a WhyGraph platform (M2e) has no local database:
all four resources are read from the platform instead, and each payload
carries a ``platform`` block. Only ``whygraph://commit/{sha}`` has a
local half - a commit the platform does not hold is still in the
checkout, so its raw git message is returned with the ``push_status``
that explains the gap.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from mcp.server.fastmcp import FastMCP
from sqlalchemy import func, or_
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, col, select

from whygraph.core import get_config
from whygraph.core.context import current_project
from whygraph.core.remote import (
    RemoteError,
    RemoteNotFound,
    RemoteProject,
    platform_block,
)
from whygraph.core.utils import LIKE_ESCAPE_CHAR, like_escape
from whygraph.db import get_session
from whygraph.db.models import (
    Author,
    Commit,
    CommitFileChange,
    ForgeCrawlState,
    Issue,
    Milestone,
    PRIssueLink,
    PullRequest,
    Tag,
)
from whygraph.scan.authors import is_bot
from whygraph.scan.refactor_score import BORING_THRESHOLD
from whygraph.services.codegraph.paths import CODEGRAPH_DB_RELPATH
from whygraph.services.git import GitError, Repository, git_env

from .errors import WhyGraphError
from .evidence import _json_list, _linked_prs, local_push_status
from .path_history import current_branch_scope, resolve_path_aliases
from .targets import repo_root

_log = logging.getLogger(__name__)

_DB_UNSCANNED_MESSAGE = (
    "WhyGraph DB is missing or unscanned — scan from the WhyGraph portal first"
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


# ---- linked projects -----------------------------------------------------


def _remote() -> RemoteProject | None:
    """The bound project's platform, or ``None`` for an ordinary project."""
    ctx = current_project()
    return None if ctx is None else ctx.remote


def _linked_read(
    remote: RemoteProject, read: Callable[[], dict], missing: dict
) -> dict:
    """One platform resource read, with the module's not-found-is-content rule.

    Parameters
    ----------
    remote : RemoteProject
        The project's platform.
    read : callable
        The call to make on it.
    missing : dict
        The payload for an object the platform does not know (rule 2 of
        the module docstring).

    Returns
    -------
    dict
        The platform's body plus a ``platform`` block.

    Raises
    ------
    WhyGraphError
        The platform could not be reached, or refused - a setup failure
        the user must act on, like an unscanned database.
    """
    try:
        body = read()
    except RemoteNotFound:
        body = missing
    except RemoteError as exc:
        raise WhyGraphError(str(exc)) from exc
    return {**body, "platform": platform_block(remote)}


def _linked_commit(remote: RemoteProject, sha: str) -> dict:
    """``whygraph://commit/{sha}`` for a linked project, with a local fallback.

    A commit the platform has not scanned (or has never seen - an
    unpushed one) is still in the developer's checkout, so the raw git
    message is returned with the ``push_status`` that explains the gap,
    rather than a bare not-found.
    """
    try:
        body = remote.commit(sha)
    except RemoteNotFound:
        body = _local_commit(sha)
    except RemoteError as exc:
        raise WhyGraphError(str(exc)) from exc
    return {**body, "platform": platform_block(remote)}


def _local_commit(sha: str) -> dict:
    """The checkout's own view of ``sha``: raw message, no PRs, a push status."""
    repo = Repository(repo_root())
    push_status = local_push_status(repo, sha)
    if push_status is None:
        return {"error": "not_found", "sha": sha}
    try:
        commit = repo.commit_metadata(sha)
    except GitError:
        return {"error": "not_found", "sha": sha}
    return {
        "commit": {
            "sha": commit.sha,
            "llm_description": None,
            "subject": commit.subject,
            "body": commit.body,
            "author_name": commit.author_name,
            "author_email": commit.author_email,
            "authored_at": commit.authored_at,
            "committed_at": commit.committed_at,
            "parent_shas": " ".join(commit.parent_shas),
        },
        "linked_prs": [],
        "source": "local",
        "push_status": push_status,
    }


# ---- resource bodies -----------------------------------------------------


def _commit_resource(sha: str) -> dict:
    """Read the resource backing ``whygraph://commit/{sha}``.

    For a project linked to a WhyGraph platform the commit comes from
    there (:func:`_linked_commit`), falling back to local git.
    """
    _log.debug("commit resource read: sha=%r", sha)
    remote = _remote()
    if remote is not None:
        return _linked_commit(remote, sha)
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
    """Read the resource backing ``whygraph://pr/{number}``.

    A linked project's pull requests live only on the platform - there is
    no local fallback for one.
    """
    _log.debug("pr resource read: number=%r", number)
    remote = _remote()
    if remote is not None:
        return _linked_read(
            remote,
            lambda: remote.pr(number),
            {"error": "not_found", "number": number},
        )
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
    """Read the resource backing ``whygraph://issue/{number}``.

    A linked project's issues live only on the platform - there is no
    local fallback for one.
    """
    _log.debug("issue resource read: number=%r", number)
    remote = _remote()
    if remote is not None:
        return _linked_read(
            remote,
            lambda: remote.issue(number),
            {"error": "not_found", "number": number},
        )
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


_MAX_SINCE_DAYS = 3650
"""Upper clamp on a ``since_days`` window (ten years)."""


def _clamp_since_days(since_days: int | None) -> int | None:
    """Clamp a ``since_days`` window to ``[1, _MAX_SINCE_DAYS]``; ``None`` stays."""
    if since_days is None:
        return None
    return max(1, min(int(since_days), _MAX_SINCE_DAYS))


def _since_cutoff(since_days: int | None) -> str | None:
    """ISO-8601 UTC lower bound for a ``since_days`` window, or ``None``.

    Timestamps are stored as ISO strings, so the comparison is lexical; a
    commit authored under a non-UTC offset can land a few hours either side.
    """
    if since_days is None:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()


_RECENT_DESCRIPTION_CHARS = 240
"""``llm_description`` is truncated in the recent-activity listing: the point
is a cheap index the model can scan in one round, and a dozen full paragraphs
would cost more context than the drill-down calls it replaces."""


def _recent_activity_resource(limit: int = 10, since_days: int | None = None) -> dict:
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
    since_days : int, optional
        Only rows authored (commits) or updated (pull requests, issues) in
        the last this-many days, clamped to ``[1, _MAX_SINCE_DAYS]``.

    Returns
    -------
    dict
        ``{"limit", "commits", "pull_requests", "issues",
        "forge_data_as_of"}``, plus ``"since_days"`` when a window was
        applied. A category with nothing scanned comes back as an empty
        list rather than being omitted, so the model can tell "none" from
        "not asked". ``forge_data_as_of`` dates the pull request and issue
        lists (see :func:`_forge_data_as_of`).

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
    since_days = _clamp_since_days(since_days)
    cutoff = _since_cutoff(since_days)
    try:
        with get_session() as session:
            commit_stmt = select(Commit).where(Commit.on_default_branch == 1)
            pr_stmt = select(PullRequest)
            issue_stmt = select(Issue)
            if cutoff is not None:
                commit_stmt = commit_stmt.where(col(Commit.authored_at) >= cutoff)
                pr_stmt = pr_stmt.where(col(PullRequest.updated_at) >= cutoff)
                issue_stmt = issue_stmt.where(col(Issue.updated_at) >= cutoff)
            commits = session.exec(
                commit_stmt.order_by(col(Commit.authored_at).desc()).limit(limit)
            ).all()
            prs = session.exec(
                pr_stmt.order_by(col(PullRequest.updated_at).desc()).limit(limit)
            ).all()
            issues = session.exec(
                issue_stmt.order_by(col(Issue.updated_at).desc()).limit(limit)
            ).all()
            forge_as_of = _forge_data_as_of(session)

            return {
                "limit": limit,
                **({"since_days": since_days} if since_days is not None else {}),
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
                "forge_data_as_of": forge_as_of,
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


_FULL_SHA = re.compile(r"[0-9a-f]{40}")
"""A full SHA-1 commit id: the only revision text that may reach ``git`` argv."""

_LINK_CHUNK = 100
"""SHAs per query in :func:`_linked_prs_many` (three bound values each)."""


def _linked_prs_many(session: Session, shas: set[str]) -> dict[str, list[PullRequest]]:
    """Pull requests per commit, for many commits in a few queries.

    The batched form of :func:`whygraph.mcp.evidence._linked_prs`, with the
    same three sources (merge commit, head commit, ``commit_titles``): one
    query per :data:`_LINK_CHUNK` SHAs instead of one per commit, each match
    confirmed by :func:`_pr_commit_shas` rather than by the ``LIKE`` alone.
    Only full hex SHAs are looked up, so none needs ``LIKE`` escaping.

    Returns
    -------
    dict[str, list[PullRequest]]
        For each SHA some PR contains, those PRs by number; SHAs no PR
        contains are absent.
    """
    wanted = sorted(sha for sha in shas if _FULL_SHA.fullmatch(sha))
    found: dict[int, PullRequest] = {}
    for start in range(0, len(wanted), _LINK_CHUNK):
        chunk = wanted[start : start + _LINK_CHUNK]
        stmt = select(PullRequest).where(
            or_(
                col(PullRequest.merge_commit_sha).in_(chunk),
                col(PullRequest.head_sha).in_(chunk),
                *(col(PullRequest.commit_titles).like(f"%{sha}%") for sha in chunk),
            )
        )
        for pr in session.exec(stmt).all():
            found[pr.number] = pr
    wanted_set = set(wanted)
    linked: dict[str, list[PullRequest]] = {}
    for number in sorted(found):
        pr = found[number]
        for sha in _pr_commit_shas(pr) & wanted_set:
            linked.setdefault(sha, []).append(pr)
    return linked


def _find_changes_resource(
    query: str | None = None,
    path: str | None = None,
    limit: int = 15,
    area: str | None = None,
    area_predicates: list[tuple[str, str | None]] | None = None,
    since_days: int | None = None,
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
    area : str, optional
        An already-resolved area name, echoed in the result and counted as a
        filter. Mutually exclusive with ``path``.
    area_predicates : list of tuple, optional
        The area's ``(escaped LIKE pattern, exact)`` pairs from
        :func:`whygraph.chat.areas.path_predicates`. Passed in rather than
        imported because ``mcp`` must not depend on ``chat``. Rename aliases
        are not applied to them.
    since_days : int, optional
        Only commits authored in the last this-many days, clamped to
        ``[1, _MAX_SINCE_DAYS]``. A window alone is not a filter.

    Returns
    -------
    dict
        ``{"query", "path", "count", "commits"}`` (plus ``"area"`` and
        ``"since_days"`` when given). Each commit carries its
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
    area = (area or "").strip() or None
    if area and path:
        return {
            "error": (
                "pass `area` or `path`, not both: `area` already stands for "
                "a configured set of paths."
            )
        }
    if not terms and not path and not area:
        return {
            "error": (
                "pass `query` (keywords matched against commit diff "
                "descriptions and PR titles), `path` (a file or "
                "directory prefix) and/or `area` (a configured area). "
                "Without a filter this would return the whole history."
            )
        }
    limit = max(1, min(int(limit), _FIND_CHANGES_MAX_LIMIT))
    since_days = _clamp_since_days(since_days)
    cutoff = _since_cutoff(since_days)
    predicates = list(area_predicates or []) if area else []
    echo = {
        **({"area": area} if area else {}),
        **({"since_days": since_days} if since_days is not None else {}),
    }

    try:
        with get_session() as session:
            aliases = (
                resolve_path_aliases(
                    session, path, current_branch=current_branch_scope()
                )
                if path
                else set()
            )
            shas = _find_changes_shas(session, terms, path, aliases, predicates, cutoff)
            if not shas:
                return {
                    "query": query,
                    "path": path or None,
                    **echo,
                    "count": 0,
                    "commits": [],
                }

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
                row = _find_changes_row(session, commit, path, aliases, predicates)
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
        **echo,
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


def _area_clause(predicates: list[tuple[str, str | None]]):
    """OR of an area's escaped path predicates over ``commit_file_change.path``."""
    clauses = []
    for pattern, exact in predicates:
        clauses.append(
            col(CommitFileChange.path).like(pattern, escape=LIKE_ESCAPE_CHAR)
        )
        if exact is not None:
            clauses.append(col(CommitFileChange.path) == exact)
    return or_(*clauses)


def _find_changes_shas(
    session: Session,
    terms: list[str],
    path: str,
    aliases: set[str],
    predicates: list[tuple[str, str | None]] | None = None,
    cutoff: str | None = None,
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

    if path:
        path_stmt = select(CommitFileChange.commit_sha).where(
            col(CommitFileChange.path).in_(aliases)
            | col(CommitFileChange.path).like(
                f"{like_escape(path)}/%", escape=LIKE_ESCAPE_CHAR
            )
        )
        touching = set(session.exec(path_stmt).all())
        shas = touching if shas is None else shas & touching
    if predicates:
        area_stmt = select(CommitFileChange.commit_sha).where(_area_clause(predicates))
        touching = set(session.exec(area_stmt).all())
        shas = touching if shas is None else shas & touching
    if shas and cutoff is not None:
        shas = set(
            session.exec(
                select(Commit.sha)
                .where(col(Commit.sha).in_(shas))
                .where(col(Commit.authored_at) >= cutoff)
            ).all()
        )
    return shas or set()


def _matches_predicates(path: str, predicates: list[tuple[str, str | None]]) -> bool:
    """Whether ``path`` satisfies any ``(escaped LIKE pattern, exact)`` predicate.

    The Python twin of :func:`_area_clause`: the pattern ends in ``%`` and its
    literal head is recovered by undoing the escaping.
    """
    for pattern, exact in predicates:
        if exact is not None and path == exact:
            return True
        head = pattern.removesuffix("%")
        literal = re.sub(re.escape(LIKE_ESCAPE_CHAR) + r"(.)", r"\1", head)
        if path.startswith(literal):
            return True
    return False


def _find_changes_row(
    session: Session,
    commit: Commit,
    path: str,
    aliases: set[str],
    predicates: list[tuple[str, str | None]] | None = None,
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
    if path or predicates:
        # Which of the commit's files the filter actually hit — so a directory
        # search shows *where*, and an alias hit shows the pre-rename name.
        touched = session.exec(
            select(CommitFileChange.path).where(
                col(CommitFileChange.commit_sha) == commit.sha
            )
        ).all()
        if predicates:
            matched = sorted({p for p in touched if _matches_predicates(p, predicates)})
        else:
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


# ---- work in flight and area owners (chat-only reads) ---------------------


def _utcnow() -> datetime:
    """The current UTC time. A seam: tests freeze the clock here."""
    return datetime.now(timezone.utc)


def _parse_ts(value: str | None) -> datetime | None:
    """Parse a stored ISO-8601 timestamp (``Z`` or an offset) to aware UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _days_between(earlier: str | None, now: datetime) -> int | None:
    """Whole days from ``earlier`` to ``now``; ``None`` when unparseable."""
    parsed = _parse_ts(earlier)
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds() // 86_400))


def _forge_data_as_of(session: Session) -> str | None:
    """When pull requests and issues were last crawled from GitHub.

    The **older** :attr:`ForgeCrawlState.crawled_at` of the
    ``"pull_request"`` and ``"issue"`` kinds, so a date it reports covers
    both lists. ``None`` when neither kind has a row: GitHub access is off,
    or the repository has no GitHub remote.
    """
    stamps = session.exec(
        select(ForgeCrawlState.crawled_at).where(
            col(ForgeCrawlState.kind).in_(("pull_request", "issue"))
        )
    ).all()
    dated = [(parsed, raw) for raw in stamps if (parsed := _parse_ts(raw))]
    return min(dated)[1] if dated else None


_FORGE_STALE_HOURS = 24
"""Past this age ``get_work_in_flight`` carries a ``freshness_warning``."""


def _freshness_warning(forge_as_of: str | None, now: datetime) -> str | None:
    """The warning that dates the forge data, or ``None`` when it is fresh."""
    parsed = _parse_ts(forge_as_of)
    if parsed is None:
        return (
            "No GitHub crawl is recorded for this project (GitHub access is "
            "off or there is no GitHub remote), so open pull requests and "
            "issues may be missing."
        )
    age = now - parsed
    if age <= timedelta(hours=_FORGE_STALE_HOURS):
        return None
    days = age.days
    unit = "day" if days == 1 else "days"
    return (
        f"GitHub data is {days} {unit} old; hook scans do not refresh PRs - "
        "run a full scan"
    )


_WIP_MAX_DAYS = 365
"""Upper clamp on ``stale_days`` / ``issue_stale_days``."""

_WIP_MAX_LIMIT = 50
"""Upper clamp on the per-category ``limit`` of ``get_work_in_flight``."""

_WIP_COMMENT_PAGE = 100
"""The crawl stores ``comments(first: 100)``, oldest first: a PR holding this
many may have newer comments the crawl never saw, so its last activity falls
back to ``updated_at``."""

_WIP_NOT_TRACKED = (
    "team_review_requests",
    "deployments",
    "incidents",
)
"""Fields a lead would ask about that the crawl does not store.

Team review requests are left out of the crawl on purpose: an installation
token cannot read teams, so only user and bot requests are stored."""

_SQL_IN_CHUNK = 500
"""Batch size for ``IN (...)`` lists, well under SQLite's variable limit."""


def _clamp(value: int | None, default: int, high: int) -> int:
    """Clamp ``value`` to ``[1, high]``; ``None`` means ``default``."""
    if value is None:
        return default
    return max(1, min(int(value), high))


def _pr_last_activity(pr: PullRequest) -> str:
    """A PR's last activity: its newest non-bot comment, else ``updated_at``.

    ``updated_at`` moves on bot comments and label edits, so a comment is the
    better signal when there is one. Stored comments carry only the author
    login, so a bot is recognized by its ``[bot]`` suffix alone.
    """
    comments = _json_list(pr.comments)
    if len(comments) >= _WIP_COMMENT_PAGE:
        return pr.updated_at
    human = [
        (parsed, c["created_at"])
        for c in comments
        if isinstance(c, dict)
        and not is_bot("", c.get("author"))
        and (parsed := _parse_ts(c.get("created_at")))
    ]
    return max(human)[1] if human else pr.updated_at


def _pr_areas(
    pr: PullRequest,
    labels: list[str],
    commit_paths: dict[str, set[str]],
    *,
    labels_to_areas: Callable[[list[str]], list[str]] | None,
    paths_to_areas: Callable[[list[str]], list[str]] | None,
) -> tuple[list[str], str | None]:
    """A pull request's areas and where they came from.

    Labels first (``"labels"``); else the PR's changed files as the crawl
    stored them, the first 100 (``"files"``); else the recorded file changes
    of its commits that are in ``commit_paths`` (``"commits"``); else no area
    (``None``). A source that yields paths decides, even when they map to no
    area.
    """
    areas = labels_to_areas(labels) if labels_to_areas is not None else []
    if areas:
        return areas, "labels"
    if paths_to_areas is None:
        return [], None
    files = sorted({str(p) for p in _json_list(pr.files) if isinstance(p, str) and p})
    if files:
        return paths_to_areas(files), "files"
    touched = sorted(
        {p for sha in _pr_commit_shas(pr) for p in commit_paths.get(sha, ())}
    )
    if touched:
        return paths_to_areas(touched), "commits"
    return [], None


def _str_list(raw: str | None) -> list[str]:
    """Decode a JSON list column of strings (logins), dropping anything else."""
    return [str(item) for item in _json_list(raw) if isinstance(item, str)]


def _milestone_ref(number: int | None, titles: dict[int, str]) -> dict | None:
    """``{"number", "title"}`` for a milestone number, ``None`` without one."""
    if number is None:
        return None
    return {"number": number, "title": titles.get(number)}


def _paths_by_sha(session: Session, shas: set[str]) -> dict[str, set[str]]:
    """Recorded file-change paths per commit, for the commits that have any."""
    found: dict[str, set[str]] = {}
    ordered = sorted(shas)
    for start in range(0, len(ordered), _SQL_IN_CHUNK):
        chunk = ordered[start : start + _SQL_IN_CHUNK]
        rows = session.exec(
            select(CommitFileChange.commit_sha, CommitFileChange.path).where(
                col(CommitFileChange.commit_sha).in_(chunk)
            )
        ).all()
        for sha, path in rows:
            found.setdefault(sha, set()).add(path)
    return found


def _work_in_flight_resource(
    area: str | None = None,
    stale_days: int | None = 7,
    issue_stale_days: int | None = 30,
    limit: int | None = 20,
    *,
    labels_to_areas: Callable[[list[str]], list[str]] | None = None,
    paths_to_areas: Callable[[list[str]], list[str]] | None = None,
) -> dict:
    """Open pull requests and issues, with idle time and stale flags.

    The one read for "what is open / in flight / stuck". History reads
    (:func:`_recent_activity_resource`, :func:`_find_changes_resource`) say
    what happened; this says what is still open and for how long it has sat.

    Parameters
    ----------
    area : str, optional
        An already-resolved area name. Keeps only items attributed to it.
    stale_days : int, optional
        A non-draft PR idle this many days is ``stale``. Default 7, clamped
        to ``[1, _WIP_MAX_DAYS]``.
    issue_stale_days : int, optional
        An issue idle this many days is ``stale``. Default 30, same clamp.
    limit : int, optional
        Maximum rows **per category**. Default 20, clamped to
        ``[1, _WIP_MAX_LIMIT]``; the rest is counted in ``omitted``.
    labels_to_areas, paths_to_areas : callable, optional
        Map labels / repo paths to configured area names
        (:func:`whygraph.chat.areas.areas_for_labels` /
        :func:`~whygraph.chat.areas.areas_for_paths`). Passed in because
        ``mcp`` must not depend on ``chat``; ``None`` (no areas configured)
        leaves every item without an area.

    Returns
    -------
    dict
        ``{"as_of", "forge_data_as_of", "area", "summary", "pull_requests",
        "issues", "not_tracked", "omitted"}``, plus ``"freshness_warning"``
        when the forge data is missing or older than
        :data:`_FORGE_STALE_HOURS`, and a ``"note"`` when rows were omitted.

    Raises
    ------
    WhyGraphError
        The DB is missing or unscanned.

    Notes
    -----
    **Area attribution is labels, then files, then commits** (see
    :func:`_pr_areas`): a PR's labels, else its changed files as crawled
    (``area_source="files"``), else the recorded file changes of its commits
    (open-PR commits are not crawled, so only commits already scanned locally
    qualify). Issues are attributed by labels only.

    Each PR carries its review decision, requested reviewers (users and bots;
    team requests are not crawled), first review time, assignees, milestone
    and diff size; each issue its assignees and milestone.

    A PR's ``last_activity_at`` is its newest non-bot comment, else
    ``updated_at`` (see :func:`_pr_last_activity`). Drafts are never
    ``stale``; an idle draft counts in ``summary.stale_drafts``. Both lists
    are sorted by ``idle_days``, longest first.

    Not registered in :func:`register` — the chat tool is its only caller.
    """
    stale_days = _clamp(stale_days, 7, _WIP_MAX_DAYS)
    issue_stale_days = _clamp(issue_stale_days, 30, _WIP_MAX_DAYS)
    limit = _clamp(limit, 20, _WIP_MAX_LIMIT)
    area = (area or "").strip() or None
    now = _utcnow()
    try:
        with get_session() as session:
            forge_as_of = _forge_data_as_of(session)
            prs = session.exec(
                select(PullRequest).where(col(PullRequest.state) == "open")
            ).all()
            issues = session.exec(select(Issue).where(col(Issue.state) == "open")).all()
            milestone_titles = dict(
                session.exec(select(Milestone.number, Milestone.title)).all()
            )
            open_numbers = {pr.number for pr in prs}
            closes: dict[int, list[int]] = {}
            closed_by: dict[int, list[int]] = {}
            if open_numbers:
                links = session.exec(
                    select(PRIssueLink.pr_number, PRIssueLink.issue_number).where(
                        col(PRIssueLink.pr_number).in_(sorted(open_numbers)),
                        col(PRIssueLink.link_kind) == "closes",
                    )
                ).all()
                for pr_number, issue_number in sorted(links):
                    closes.setdefault(pr_number, []).append(issue_number)
                    closed_by.setdefault(issue_number, []).append(pr_number)

            pr_shas = {pr.number: _pr_commit_shas(pr) for pr in prs}
            paths = (
                _paths_by_sha(session, set().union(*pr_shas.values()))
                if paths_to_areas is not None and pr_shas
                else {}
            )
            pr_rows: list[dict] = []
            for pr in prs:
                labels = [str(label) for label in _json_list(pr.labels)]
                areas, source = _pr_areas(
                    pr,
                    labels,
                    paths,
                    labels_to_areas=labels_to_areas,
                    paths_to_areas=paths_to_areas,
                )
                if area is not None and area not in areas:
                    continue
                last_activity = _pr_last_activity(pr)
                idle = _days_between(last_activity, now)
                draft = bool(pr.draft)
                pr_rows.append(
                    {
                        "number": pr.number,
                        "title": pr.title,
                        "author": pr.author,
                        "draft": draft,
                        "created_at": pr.created_at,
                        "last_activity_at": last_activity,
                        "age_days": _days_between(pr.created_at, now),
                        "idle_days": idle,
                        "stale": not draft and idle is not None and idle >= stale_days,
                        "base_ref": pr.base_ref,
                        "head_ref": pr.head_ref,
                        "labels": labels,
                        "areas": areas,
                        "area_source": source,
                        "review_decision": pr.review_decision,
                        "review_requests": _str_list(pr.review_requests),
                        "first_review_at": pr.first_review_at,
                        "assignees": _str_list(pr.assignees),
                        "milestone": _milestone_ref(pr.milestone, milestone_titles),
                        "additions": pr.additions,
                        "deletions": pr.deletions,
                        "changed_files": pr.changed_files,
                        "closes_issues": closes.get(pr.number, []),
                    }
                )

            issue_rows: list[dict] = []
            for issue in issues:
                labels = [str(label) for label in _json_list(issue.labels)]
                areas = labels_to_areas(labels) if labels_to_areas is not None else []
                if area is not None and area not in areas:
                    continue
                idle = _days_between(issue.updated_at, now)
                issue_rows.append(
                    {
                        "number": issue.number,
                        "title": issue.title,
                        "author": issue.author,
                        "created_at": issue.created_at,
                        "idle_days": idle,
                        "stale": idle is not None and idle >= issue_stale_days,
                        "labels": labels,
                        "areas": areas,
                        "assignees": _str_list(issue.assignees),
                        "milestone": _milestone_ref(issue.milestone, milestone_titles),
                        "linked_open_prs": closed_by.get(issue.number, []),
                    }
                )
    except OperationalError as exc:
        raise WhyGraphError(_DB_UNSCANNED_MESSAGE) from exc

    def _idle_first(row: dict) -> tuple[int, int]:
        return (-(row["idle_days"] or 0), row["number"])

    pr_rows.sort(key=_idle_first)
    issue_rows.sort(key=_idle_first)
    drafts = [r for r in pr_rows if r["draft"]]
    summary = {
        "open_prs": len(pr_rows),
        "drafts": len(drafts),
        "stale_prs": sum(1 for r in pr_rows if r["stale"]),
        "stale_drafts": sum(
            1
            for r in drafts
            if r["idle_days"] is not None and r["idle_days"] >= stale_days
        ),
        "open_issues": len(issue_rows),
        "stale_issues": sum(1 for r in issue_rows if r["stale"]),
    }
    omitted = max(0, len(pr_rows) - limit) + max(0, len(issue_rows) - limit)
    result: dict = {
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "forge_data_as_of": forge_as_of,
    }
    warning = _freshness_warning(forge_as_of, now)
    if warning:
        result["freshness_warning"] = warning
    result.update(
        {
            "area": area,
            "stale_days": stale_days,
            "issue_stale_days": issue_stale_days,
            "summary": summary,
            "pull_requests": pr_rows[:limit],
            "issues": issue_rows[:limit],
            "not_tracked": list(_WIP_NOT_TRACKED),
            "omitted": omitted,
        }
    )
    if omitted:
        result["note"] = (
            f"{omitted} more open item(s) are counted in summary but not "
            f"listed (limit {limit} per list). Narrow with `area` or raise "
            f"`limit` (max {_WIP_MAX_LIMIT})."
        )
    return result


# ---- compare refs and milestones (chat-only reads) ------------------------


_COMPARE_MAX_LIMIT = 50
"""Upper clamp on the per-list ``limit`` of ``compare_refs``."""

_SHA_PREFIX = re.compile(r"[0-9a-fA-F]{7,40}")
"""What ``compare_refs`` tries as a commit SHA (after a tag name)."""

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

_REF_HINT_TAGS = 10
"""How many of the newest tag names an unresolvable ref's error lists."""


def _date_cutoff(text: str) -> str | None:
    """An inclusive ``authored_at`` upper bound for an ISO date, else ``None``.

    A bare date (``YYYY-MM-DD``) means the end of that day; a datetime is
    taken as given (UTC when it carries no offset). The bound is compared
    lexically with stored timestamps, so a commit under a non-UTC offset can
    land a few hours either side, as with :func:`_since_cutoff`.
    """
    if _ISO_DATE.fullmatch(text):
        try:
            day = date.fromisoformat(text)
        except ValueError:
            return None
        return f"{day.isoformat()}T23:59:59Z"
    if "T" not in text:
        return None
    parsed = _parse_ts(text)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") if parsed else None


def _resolve_ref(session: Session, ref: str | None, *, role: str) -> dict:
    """Resolve one ``compare_refs`` endpoint to a scanned commit.

    Tried in order: a tag name (the ``tag`` table), a SHA prefix of 7-40 hex
    characters (``commit``, by key range), an ISO date (the newest
    default-branch commit authored on or before it). An empty ``to_ref``
    means the newest scanned default-branch commit. Every resolved ``sha`` is
    a full 40-hex id read from the DB; the caller's text never is one.

    Returns
    -------
    dict
        ``{"ref", "kind", "sha", "date"}``, ``kind`` one of ``"tag"``,
        ``"sha"``, ``"date"``, ``"latest"``.

    Raises
    ------
    WhyGraphError
        The ref resolves to nothing, or a SHA prefix to several commits.
    """
    text = (ref or "").strip()
    if not text:
        if role == "from_ref":
            raise WhyGraphError(
                "from_ref is required: a tag name, a commit SHA or an ISO date"
            )
        row = session.exec(
            select(Commit.sha, Commit.authored_at)
            .where(Commit.on_default_branch == 1)
            .order_by(col(Commit.authored_at).desc())
            .limit(1)
        ).first()
        if row is None:
            raise WhyGraphError(_DB_UNSCANNED_MESSAGE)
        return {"ref": None, "kind": "latest", "sha": row[0], "date": row[1]}

    tag = session.get(Tag, text)
    if tag is not None and _FULL_SHA.fullmatch(tag.sha or ""):
        return {"ref": text, "kind": "tag", "sha": tag.sha, "date": tag.tagged_at}

    if _SHA_PREFIX.fullmatch(text):
        prefix = text.lower()
        rows = session.exec(
            select(Commit.sha, Commit.authored_at)
            .where(
                col(Commit.sha) >= prefix,
                col(Commit.sha) <= prefix + "f" * (40 - len(prefix)),
            )
            .limit(2)
        ).all()
        if len(rows) > 1:
            raise WhyGraphError(
                f"{role} {text!r} matches more than one commit: give more characters"
            )
        if rows and _FULL_SHA.fullmatch(rows[0][0]):
            return {"ref": text, "kind": "sha", "sha": rows[0][0], "date": rows[0][1]}

    cutoff = _date_cutoff(text)
    if cutoff is not None:
        row = session.exec(
            select(Commit.sha, Commit.authored_at)
            .where(Commit.on_default_branch == 1, col(Commit.authored_at) <= cutoff)
            .order_by(col(Commit.authored_at).desc())
            .limit(1)
        ).first()
        if row is None:
            raise WhyGraphError(
                f"{role} {text!r}: no default-branch commit on or before that date"
            )
        return {"ref": text, "kind": "date", "sha": row[0], "date": row[1]}

    newest = session.exec(
        select(Tag.name)
        .order_by(col(Tag.tagged_at).desc(), col(Tag.name))
        .limit(_REF_HINT_TAGS)
    ).all()
    known = ", ".join(newest) if newest else "(no tags recorded)"
    raise WhyGraphError(
        f"{role} {text!r} is not a known tag, a scanned commit SHA (7-40 hex) "
        f"or an ISO date (YYYY-MM-DD). Newest tags: {known}."
    )


def _compare_refs_resource(
    from_ref: str | None,
    to_ref: str | None = None,
    limit: int | None = 25,
    *,
    labels_to_areas: Callable[[list[str]], list[str]] | None = None,
    paths_to_areas: Callable[[list[str]], list[str]] | None = None,
) -> dict:
    """What ``to_ref`` adds on top of ``from_ref``: commits, PRs, closed issues.

    The release question ("what shipped in v1.4", "what changed since
    <date>"): two fixed endpoints and everything between them, grouped by
    area. History reads without endpoints are
    :func:`_recent_activity_resource` and :func:`_find_changes_resource`.

    Parameters
    ----------
    from_ref : str
        The older endpoint; resolved by :func:`_resolve_ref`.
    to_ref : str, optional
        The newer endpoint; ``None`` or empty means the newest scanned
        default-branch commit.
    limit : int, optional
        Maximum rows per list (commits, pull requests, issues). Default 25,
        clamped to ``[1, _COMPARE_MAX_LIMIT]``; the rest is counted in
        ``omitted``.
    labels_to_areas, paths_to_areas : callable, optional
        Map labels / repo paths to configured area names, as for
        :func:`_work_in_flight_resource`; ``None`` groups nothing.

    Returns
    -------
    dict
        ``{"from", "to", "total_commits", "not_scanned", "by_area",
        "pull_requests", "issues_closed", "commits", "forge_data_as_of",
        "limit", "omitted"}``, plus a ``"note"`` when something needs saying.

    Raises
    ------
    WhyGraphError
        The DB is missing or unscanned, a ref does not resolve, or ``git``
        cannot list the range.

    Notes
    -----
    The range is read from the local repository (``git rev-list
    --end-of-options <to> ^<from>`` through
    :meth:`~whygraph.services.git.Repository.range_shas`), and only the two
    DB-resolved 40-hex SHAs reach its argv. Pull requests come from one
    batched lookup (:func:`_linked_prs_many`), and a commit or PR counts in
    every area it touches.

    Not registered in :func:`register` - the chat tool is its only caller.
    """
    limit = _clamp(limit, 25, _COMPARE_MAX_LIMIT)
    try:
        with get_session() as session:
            start = _resolve_ref(session, from_ref, role="from_ref")
            end = _resolve_ref(session, to_ref, role="to_ref")
            if not (
                _FULL_SHA.fullmatch(start["sha"]) and _FULL_SHA.fullmatch(end["sha"])
            ):
                raise WhyGraphError("a ref resolved to something other than a commit")
            try:
                shas = Repository(repo_root()).range_shas(start["sha"], end["sha"])
            except GitError as exc:
                raise WhyGraphError(
                    f"git could not list {start['sha'][:7]}..{end['sha'][:7]} "
                    "in the local repository - both commits must be in this "
                    "checkout"
                ) from exc

            commits: list[Commit] = []
            ordered = sorted(shas)
            for offset in range(0, len(ordered), _SQL_IN_CHUNK):
                chunk = ordered[offset : offset + _SQL_IN_CHUNK]
                commits.extend(
                    session.exec(select(Commit).where(col(Commit.sha).in_(chunk))).all()
                )
            scanned = {c.sha for c in commits}
            linked = _linked_prs_many(session, scanned)
            prs = {pr.number: pr for found in linked.values() for pr in found}
            mapped = paths_to_areas is not None
            commit_paths = _paths_by_sha(session, scanned) if mapped else {}
            if mapped and prs:
                pr_only = set().union(*(_pr_commit_shas(pr) for pr in prs.values()))
                commit_paths.update(_paths_by_sha(session, pr_only - scanned))

            closes: dict[int, list[int]] = {}
            issues: list[Issue] = []
            if prs:
                links = session.exec(
                    select(PRIssueLink.pr_number, PRIssueLink.issue_number).where(
                        col(PRIssueLink.pr_number).in_(sorted(prs)),
                        col(PRIssueLink.link_kind) == "closes",
                    )
                ).all()
                for pr_number, issue_number in sorted(links):
                    closes.setdefault(pr_number, []).append(issue_number)
                numbers = sorted({n for found in closes.values() for n in found})
                if numbers:
                    issues = list(
                        session.exec(
                            select(Issue).where(col(Issue.number).in_(numbers))
                        ).all()
                    )
            forge_as_of = _forge_data_as_of(session)
            session.expunge_all()  # rows are read after the session closes
    except OperationalError as exc:
        raise WhyGraphError(_DB_UNSCANNED_MESSAGE) from exc

    def _commit_areas(sha: str) -> list[str]:
        paths = commit_paths.get(sha)
        return paths_to_areas(sorted(paths)) if mapped and paths else []

    commits.sort(key=lambda c: (c.authored_at, c.sha), reverse=True)
    commit_rows = [
        {
            "sha": c.sha,
            "subject": c.subject,
            "author_name": c.author_name,
            "authored_at": c.authored_at,
            "areas": _commit_areas(c.sha),
            "pull_requests": [pr.number for pr in linked.get(c.sha, [])],
        }
        for c in commits
    ]

    pr_rows: list[dict] = []
    for pr in prs.values():
        labels = [str(label) for label in _json_list(pr.labels)]
        areas, source = _pr_areas(
            pr,
            labels,
            commit_paths,
            labels_to_areas=labels_to_areas,
            paths_to_areas=paths_to_areas,
        )
        pr_rows.append(
            {
                "number": pr.number,
                "title": pr.title,
                "author": pr.author,
                "state": pr.state,
                "merged_at": pr.merged_at,
                "labels": labels,
                "areas": areas,
                "area_source": source,
                "closes_issues": closes.get(pr.number, []),
            }
        )
    pr_rows.sort(key=lambda r: (r["merged_at"] or "", r["number"]), reverse=True)
    issue_rows = [
        {
            "number": i.number,
            "title": i.title,
            "state": i.state,
            "state_reason": i.state_reason,
            "closed_at": i.closed_at,
        }
        for i in sorted(issues, key=lambda i: i.number)
    ]

    by_area: list[dict] = []
    if mapped:
        groups: dict[str | None, dict] = {}
        for row in commit_rows:
            for name in row["areas"] or [None]:
                groups.setdefault(name, {"commits": 0, "prs": set()})["commits"] += 1
        for row in pr_rows:
            for name in row["areas"] or [None]:
                groups.setdefault(name, {"commits": 0, "prs": set()})["prs"].add(
                    row["number"]
                )
        for name, group in groups.items():
            numbers = sorted(group["prs"], reverse=True)
            by_area.append(
                {
                    "area": name,
                    "commits": group["commits"],
                    "pull_request_count": len(numbers),
                    "pull_requests": numbers[:limit],
                }
            )
        by_area.sort(key=lambda g: (g["area"] is None, -g["commits"], g["area"] or ""))

    omitted = (
        max(0, len(commit_rows) - limit)
        + max(0, len(pr_rows) - limit)
        + max(0, len(issue_rows) - limit)
    )
    result: dict = {
        "from": start,
        "to": end,
        "total_commits": len(shas),
        "not_scanned": len(shas) - len(scanned),
        "by_area": by_area,
        "pull_requests": pr_rows[:limit],
        "issues_closed": issue_rows[:limit],
        "commits": commit_rows[:limit],
        "forge_data_as_of": forge_as_of,
        "limit": limit,
        "omitted": omitted,
    }
    notes: list[str] = []
    if not shas and (start["date"] or "") > (end["date"] or ""):
        notes.append(
            "from_ref is newer than to_ref, so nothing is between them; swap them."
        )
    if not mapped:
        notes.append("No areas are configured, so by_area is empty.")
    if omitted:
        notes.append(
            f"{omitted} more row(s) are counted but not listed (limit {limit} "
            f"per list, max {_COMPARE_MAX_LIMIT}); by_area covers them all."
        )
    if notes:
        result["note"] = " ".join(notes)
    return result


_MILESTONE_STATES = ("open", "closed", "all")

_MILESTONE_MAX_LIMIT = 50
"""Upper clamp on the ``limit`` of ``get_milestones``."""

_BURNDOWN_POINTS = 60
"""At most about this many burndown rows: past it, each row spans more days."""

_MILESTONE_DESCRIPTION_CHARS = 240
"""Milestone descriptions are clipped like the recent-activity ones."""

_BURNDOWN_NOTE = (
    "Each item counts from its creation date, because when it joined the "
    "milestone is not tracked; items moved out of the milestone are not "
    "counted, and a reopened item counts as open throughout."
)


def _burndown_rows(
    items: list[tuple[datetime, datetime | None]], end: datetime
) -> tuple[list[list], int, str | None]:
    """Open / closed counts per day for one milestone's items.

    Parameters
    ----------
    items : list of (created, closed or None)
        Every issue and PR in the milestone, parsed.
    end : datetime
        The last day to plot (the milestone's close, else now).

    Returns
    -------
    tuple
        ``(rows, step_days, start_date)``: rows of ``[date, open, closed]``
        from the earliest creation to ``end``, one every ``step_days`` days
        and always ending on ``end``'s date; empty with no items.
    """
    if not items:
        return [], 1, None
    first = min(created for created, _ in items).date()
    last = max(end.date(), first)
    span = (last - first).days
    step = max(1, -(-span // _BURNDOWN_POINTS))
    days = [first + timedelta(days=n) for n in range(0, span + 1, step)]
    if days[-1] != last:
        days.append(last)
    rows: list[list] = []
    for day in days:
        cut = datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc)
        created = [(c, d) for c, d in items if c <= cut]
        closed = sum(1 for _, d in created if d is not None and d <= cut)
        rows.append([day.isoformat(), len(created) - closed, closed])
    return rows, step, first.isoformat()


def _milestones_resource(
    state: str | None = "open",
    number: int | None = None,
    limit: int | None = 20,
) -> dict:
    """Milestones with their progress, plus one milestone's burndown.

    Parameters
    ----------
    state : {"open", "closed", "all"}, optional
        Which milestones to list. Default ``"open"``.
    number : int, optional
        The milestone to draw the burndown for; it need not be in the listed
        state. Default: the first one listed.
    limit : int, optional
        Maximum milestones listed. Default 20, clamped to
        ``[1, _MILESTONE_MAX_LIMIT]``; the rest is counted in ``omitted``.

    Returns
    -------
    dict
        ``{"as_of", "forge_data_as_of", "state", "milestones", "burndown",
        "columns", "rows", "omitted"}``, plus ``"freshness_warning"`` and
        ``"note"`` when they apply. ``columns`` / ``rows`` are the burndown
        (``date``, ``open``, ``closed``) of ``burndown.number``, top level so
        the chat registry can mint a chart ref for them.

    Raises
    ------
    WhyGraphError
        The DB is missing or unscanned, ``state`` is unknown, or ``number``
        names no milestone.

    Notes
    -----
    Open milestones are listed by due date (none last), closed ones newest
    close first. Counts come from the crawled issues and PRs that carry the
    milestone; ``progress_pct`` is GitHub's own figure. Not registered in
    :func:`register` - the chat tool is its only caller.
    """
    state = (state or "open").strip().lower()
    if state not in _MILESTONE_STATES:
        raise WhyGraphError(
            f"state must be one of {', '.join(_MILESTONE_STATES)}; got {state!r}"
        )
    limit = _clamp(limit, 20, _MILESTONE_MAX_LIMIT)
    now = _utcnow()
    try:
        with get_session() as session:
            every = session.exec(select(Milestone)).all()
            issue_rows = session.exec(
                select(
                    Issue.milestone, Issue.state, Issue.created_at, Issue.closed_at
                ).where(col(Issue.milestone).is_not(None))
            ).all()
            pr_rows = session.exec(
                select(
                    PullRequest.milestone,
                    PullRequest.state,
                    PullRequest.created_at,
                    PullRequest.closed_at,
                    PullRequest.merged_at,
                ).where(col(PullRequest.milestone).is_not(None))
            ).all()
            stamps = session.exec(
                select(ForgeCrawlState.crawled_at).where(
                    col(ForgeCrawlState.kind).in_(
                        ("milestone", "pull_request", "issue")
                    )
                )
            ).all()
            session.expunge_all()  # rows are read after the session closes
    except OperationalError as exc:
        raise WhyGraphError(_DB_UNSCANNED_MESSAGE) from exc

    dated = [(parsed, raw) for raw in stamps if (parsed := _parse_ts(raw))]
    forge_as_of = min(dated)[1] if dated else None

    counts: dict[int, dict] = {}
    items: dict[int, list[tuple[datetime, datetime | None]]] = {}
    for kind, rows in (("issues", issue_rows), ("pull_requests", pr_rows)):
        for row in rows:
            ms, item_state, created_raw = row[0], (row[1] or "").lower(), row[2]
            closed_raw = row[3] or (row[4] if len(row) > 4 else None)
            tally = counts.setdefault(
                ms,
                {
                    "issues": {"open": 0, "closed": 0},
                    "pull_requests": {"open": 0, "merged": 0, "closed": 0},
                },
            )[kind]
            tally[item_state if item_state in tally else "closed"] += 1
            created = _parse_ts(created_raw)
            if created is not None:
                closed = _parse_ts(closed_raw) if item_state != "open" else None
                items.setdefault(ms, []).append((created, closed))

    def _due_order(m: Milestone) -> tuple:
        return (m.due_on is None, m.due_on or "", m.number)

    open_ms = sorted((m for m in every if m.state == "open"), key=_due_order)
    closed_ms = sorted(
        (m for m in every if m.state != "open"),
        key=lambda m: (m.closed_at or "", m.number),
        reverse=True,
    )
    listed = {"open": open_ms, "closed": closed_ms, "all": open_ms + closed_ms}[state]

    def _row(m: Milestone) -> dict:
        due = _parse_ts(m.due_on)
        days_to_due = None if due is None else (due.date() - now.date()).days
        description = m.description
        if description and len(description) > _MILESTONE_DESCRIPTION_CHARS:
            description = description[:_MILESTONE_DESCRIPTION_CHARS] + "…"
        tally = counts.get(m.number) or {
            "issues": {"open": 0, "closed": 0},
            "pull_requests": {"open": 0, "merged": 0, "closed": 0},
        }
        return {
            "number": m.number,
            "title": m.title,
            "state": m.state,
            "description": description,
            "due_on": m.due_on,
            "days_to_due": days_to_due,
            "overdue": m.state == "open"
            and days_to_due is not None
            and days_to_due < 0,
            "closed_at": m.closed_at,
            "progress_pct": m.progress_pct,
            **tally,
            "html_url": m.html_url,
        }

    target: Milestone | None
    if number is not None:
        target = next((m for m in every if m.number == int(number)), None)
        if target is None:
            raise WhyGraphError(
                f"no milestone #{int(number)}; known: "
                + (", ".join(f"#{m.number}" for m in open_ms + closed_ms) or "none")
            )
    else:
        target = listed[0] if listed else None

    burndown: dict | None = None
    columns: list[str] = ["date", "open", "closed"]
    rows: list[list] = []
    if target is not None:
        end = _parse_ts(target.closed_at) if target.state != "open" else None
        rows, step, start = _burndown_rows(items.get(target.number, []), end or now)
        burndown = {
            "number": target.number,
            "title": target.title,
            "start": start,
            "end": rows[-1][0] if rows else None,
            "step_days": step,
            "note": _BURNDOWN_NOTE,
        }

    omitted = max(0, len(listed) - limit)
    result: dict = {
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "forge_data_as_of": forge_as_of,
    }
    warning = _freshness_warning(forge_as_of, now)
    if warning:
        result["freshness_warning"] = warning
    result.update(
        {
            "state": state,
            "milestones": [_row(m) for m in listed[:limit]],
            "burndown": burndown,
            "columns": columns,
            "rows": rows,
            "omitted": omitted,
        }
    )
    if omitted:
        result["note"] = (
            f"{omitted} more milestone(s) not listed (limit {limit}, max "
            f"{_MILESTONE_MAX_LIMIT})."
        )
    elif not every:
        result["note"] = (
            "No milestones are recorded: the repository has none, or GitHub "
            "access is off."
        )
    return result


_OWNERS_DEFAULT_DAYS = 365
"""Default ``since_days`` window for ``get_area_owners``."""

_OWNERS_MAX_LIMIT = 50
"""Upper clamp on the number of owners listed."""

_OWNERS_ACTIVE_DAYS = 90
"""An owner with a default-branch commit this recent counts as active."""

_OWNERS_NOTE = (
    "from commit history on the default branch; merges, bots and formatting "
    "sweeps excluded; Co-authored-by and CODEOWNERS not read; ownership, not "
    "performance"
)


def _absence_factor(commit_counts: list[int]) -> int:
    """CHAOSS Contributor Absence Factor over per-person commit counts.

    The fewest people whose commits add up to at least half of the total;
    ``0`` when there are no commits.
    """
    total = sum(commit_counts)
    running = 0
    for people, count in enumerate(sorted(commit_counts, reverse=True), start=1):
        running += count
        if 2 * running >= total:
            return people
    return 0


def _area_owners_resource(
    scope: dict,
    predicates: list[tuple[str, str | None]],
    since_days: int | None = _OWNERS_DEFAULT_DAYS,
    limit: int | None = 10,
) -> dict:
    """Who holds the commits under a set of paths, and how concentrated that is.

    Parameters
    ----------
    scope : dict
        What the predicates stand for, echoed back: ``{"area", "paths"}`` for
        a configured area or ``{"path"}`` for a single path.
        ``"since_days"`` is added.
    predicates : list of tuple
        ``(escaped LIKE pattern, exact)`` pairs from
        :func:`whygraph.chat.areas.path_predicates`. Passed in rather than
        imported because ``mcp`` must not depend on ``chat``.
    since_days : int, optional
        Window over ``authored_at``. Default 365, clamped to
        ``[1, _MAX_SINCE_DAYS]``.
    limit : int, optional
        Owners listed. Default 10, clamped to ``[1, _OWNERS_MAX_LIMIT]``;
        the absence factors always cover every owner.

    Returns
    -------
    dict
        ``{"scope", "total_commits", "excluded", "owners",
        "contributor_absence_factor", "active_contributor_absence_factor",
        "note"}``, plus ``"omitted"`` when owners were cut by ``limit``.
        ``total_commits`` counts the commits left after the exclusions, and
        each owner's ``share`` is a fraction of it.

    Raises
    ------
    WhyGraphError
        The DB is missing or unscanned.

    Notes
    -----
    Counts default-branch commits (``on_default_branch = 1``) in the window
    that touched a matching ``commit_file_change`` path. Each commit is
    excluded at most once, in this order: a merge (more than one parent),
    a bot (:func:`whygraph.scan.authors.is_bot` on the commit's address and
    the resolved author's login), a formatting / refactor sweep
    (``refactor_score >= BORING_THRESHOLD``).

    Commits are grouped by resolved :class:`Author` through its ``emails``.
    An address that sits in several authors' clusters (a shared one such as
    ``noreply@github.com``), or in none, falls back to the commit's
    ``author_name``.

    ``contributor_absence_factor`` is the CHAOSS measure: the fewest owners
    whose shares sum to at least 0.5. ``active_contributor_absence_factor``
    is the same over owners with a default-branch commit anywhere in the
    last :data:`_OWNERS_ACTIVE_DAYS` days, with shares taken among them;
    ``0`` when nobody active holds any of these commits.

    Not registered in :func:`register` — the chat tool is its only caller.
    """
    since_days = _clamp(since_days, _OWNERS_DEFAULT_DAYS, _MAX_SINCE_DAYS)
    limit = _clamp(limit, 10, _OWNERS_MAX_LIMIT)
    now = _utcnow()
    cutoff = (now - timedelta(days=since_days)).isoformat()
    active_cutoff = now - timedelta(days=_OWNERS_ACTIVE_DAYS)
    try:
        with get_session() as session:
            rows = session.exec(
                select(
                    Commit.sha,
                    Commit.author_name,
                    Commit.author_email,
                    Commit.authored_at,
                    Commit.parent_shas,
                    Commit.refactor_score,
                    CommitFileChange.path,
                )
                .join(
                    CommitFileChange,
                    col(CommitFileChange.commit_sha) == col(Commit.sha),
                )
                .where(
                    col(Commit.on_default_branch) == 1,
                    col(Commit.authored_at) >= cutoff,
                    _area_clause(predicates),
                )
            ).all()
            authors = session.exec(select(Author)).all()

            by_id = {a.id: a for a in authors}
            clusters_of: dict[str, set[int]] = {}
            for a in authors:
                for email in _json_list(a.emails):
                    clusters_of.setdefault(str(email), set()).add(a.id)

            commits: dict[str, dict] = {}
            for sha, name, email, authored_at, parents, score, path in rows:
                entry = commits.get(sha)
                if entry is None:
                    entry = commits[sha] = {
                        "name": name,
                        "email": email,
                        "authored_at": authored_at,
                        "parents": parents,
                        "score": score,
                        "paths": set(),
                    }
                entry["paths"].add(path)

            excluded = {"merges": 0, "bots": 0, "refactors": 0}
            owners: dict[tuple, dict] = {}
            for entry in commits.values():
                ids = clusters_of.get(entry["email"], set())
                author = by_id[next(iter(ids))] if len(ids) == 1 else None
                login = author.primary_login if author is not None else None
                if len((entry["parents"] or "").split()) > 1:
                    excluded["merges"] += 1
                    continue
                if is_bot(entry["email"] or "", login):
                    excluded["bots"] += 1
                    continue
                if (entry["score"] or 0) >= BORING_THRESHOLD:
                    excluded["refactors"] += 1
                    continue
                if author is not None:
                    key: tuple = ("author", author.id)
                    display = (
                        author.primary_name
                        or author.primary_login
                        or author.primary_email
                        or entry["name"]
                    )
                    last_active = author.last_seen
                else:
                    key = ("name", entry["name"])
                    display, last_active = entry["name"], None
                owner = owners.get(key)
                if owner is None:
                    owner = owners[key] = {
                        "name": display,
                        "login": login,
                        "commits": 0,
                        "paths": set(),
                        "last_touched_at": None,
                        "last_active_at": last_active,
                    }
                owner["commits"] += 1
                owner["paths"] |= entry["paths"]
                touched = _parse_ts(entry["authored_at"])
                newest = _parse_ts(owner["last_touched_at"])
                if touched is not None and (newest is None or touched > newest):
                    owner["last_touched_at"] = entry["authored_at"]

            fallback = sorted(k[1] for k in owners if k[0] == "name")
            if fallback:
                latest = dict(
                    session.exec(
                        select(Commit.author_name, func.max(Commit.authored_at))
                        .where(
                            col(Commit.on_default_branch) == 1,
                            col(Commit.author_name).in_(fallback),
                        )
                        .group_by(Commit.author_name)
                    ).all()
                )
                for key, owner in owners.items():
                    if key[0] == "name":
                        owner["last_active_at"] = latest.get(key[1])
    except OperationalError as exc:
        raise WhyGraphError(_DB_UNSCANNED_MESSAGE) from exc

    total = sum(o["commits"] for o in owners.values())
    ordered = sorted(owners.values(), key=lambda o: (-o["commits"], o["name"] or ""))

    def _active(owner: dict) -> bool:
        seen = _parse_ts(owner["last_active_at"])
        return seen is not None and seen >= active_cutoff

    result: dict = {
        "scope": {**scope, "since_days": since_days},
        "total_commits": total,
        "excluded": excluded,
        "owners": [
            {
                "name": o["name"],
                "login": o["login"],
                "commits": o["commits"],
                "share": round(o["commits"] / total, 2) if total else 0.0,
                "files_touched": len(o["paths"]),
                "last_touched_at": o["last_touched_at"],
                "last_active_at": o["last_active_at"],
            }
            for o in ordered[:limit]
        ],
        "contributor_absence_factor": _absence_factor([o["commits"] for o in ordered]),
        "active_contributor_absence_factor": _absence_factor(
            [o["commits"] for o in ordered if _active(o)]
        ),
        "note": _OWNERS_NOTE,
    }
    if len(ordered) > limit:
        result["omitted"] = len(ordered) - limit
    return result


def _git_out(root: Path, *args: str) -> str | None:
    """Stdout of ``git <args>`` in ``root``, or ``None`` on any failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            env=git_env(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = result.stdout.strip()
    return out if result.returncode == 0 and out else None


def _freshness() -> dict:
    """The ``freshness`` block of the repo overview (local project).

    Returns
    -------
    dict
        ``head`` (``git rev-parse HEAD``), ``last_scanned_head`` (the
        portal row's, ``None`` without one or before the first scan),
        ``commits_behind`` (HEAD commits the last scan has not seen; ``0``
        when equal, ``None`` when unknown or never scanned) and
        ``codegraph_indexed`` (the CodeGraph database exists).
    """
    root = repo_root()
    ctx = current_project()
    scanned = ctx.scanned_head() if ctx and ctx.scanned_head else None
    head = _git_out(root, "rev-parse", "HEAD")
    behind = None
    if head and scanned:
        if head == scanned:
            behind = 0
        else:
            out = _git_out(root, "rev-list", "--count", f"{scanned}..HEAD")
            behind = int(out) if out and out.isdigit() else None
    db = get_config().codegraph_db or root / CODEGRAPH_DB_RELPATH
    return {
        "head": head,
        "last_scanned_head": scanned,
        "commits_behind": behind,
        "codegraph_indexed": db.is_file(),
    }


def _repo_overview_resource() -> dict:
    """Read the resource backing ``whygraph://repo/overview``.

    Aggregates counts, scan-freshness timestamps, LLM-description
    coverage, and the top-10 commit authors. Aggregations come from the
    ``commit`` / ``pull_request`` / ``issue`` / ``pr_issue_link`` tables
    directly — ``top_contributors`` is computed from ``Commit`` (not the
    ``Author`` table) because authors-resolution is a separate scan step
    that may not have run.

    ``freshness`` says whether the scan is current: ``head``,
    ``last_scanned_head``, ``commits_behind`` and ``codegraph_indexed``
    (see :func:`_freshness`). A linked project's summary - freshness
    included - is the platform's, over its server clone.
    """
    _log.debug("repo overview resource read")
    remote = _remote()
    if remote is not None:
        return _linked_read(remote, remote.overview, {})
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
        "freshness": _freshness(),
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
            "scan freshness (incl. HEAD vs the last scanned head), "
            "LLM-description coverage, top contributors."
        ),
        mime_type="application/json",
    )(_repo_overview_resource)
