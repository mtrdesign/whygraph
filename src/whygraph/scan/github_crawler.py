"""GitHubCrawler - pull a repository's PRs and issues from GitHub.

Reads the pull requests and issues exposed by
:class:`whygraph.services.github.GitHubClient` into the ``pullrequest``
and ``issue`` tables, one row per number. PR ``closing_issue_numbers``
are flattened into ``prissuelink`` rows under ``link_kind='closes'``.

Each kind keeps a ``forge_crawl_state`` row (the repository and the
newest ``updatedAt`` seen). With a matching row the crawl is
*incremental*: it asks GitHub only for items updated since that mark,
downloads PR content only for PRs whose ``updatedAt`` differs from the
stored one, and rewrites only rows that changed. Without a matching row
(first crawl, another repository, a database written by the old
insert-only crawler) it is a *full walk*, newest-first.

:meth:`GitHubCrawler.work` runs in three phases so SQLite's write lock is
never held across the network: (a) read the state in a short session,
(b) drain GitHub with no session open, (c) write rows in chunked
transactions and the state rows last, in a transaction of their own. A
crawl that fails anywhere leaves the state unchanged, so the next one
re-lists the same items.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import NamedTuple, TypeVar

from rich.progress import Progress
from sqlmodel import delete, select

from whygraph.db import get_session
from whygraph.db.models.forge_crawl_state import ForgeCrawlState
from whygraph.db.models.issue import Issue as IssueRow
from whygraph.db.models.pr_issue_link import PRIssueLink
from whygraph.db.models.pull_request import PullRequest as PullRequestRow
from whygraph.services.github import GitHubClient
from whygraph.services.github.commands import parse_github_ts
from whygraph.services.github.issue import Issue as IssueDC
from whygraph.services.github.pull_request import PullRequest as PullRequestDC

from .crawler import Crawler

_T = TypeVar("_T")

WRITE_CHUNK = 500


class _State(NamedTuple):
    """Plain copy of a ``forge_crawl_state`` row (ORM rows expire on commit)."""

    repo: str
    watermark: str | None


def _chunks(items: list[_T], size: int) -> Iterator[list[_T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _is_full(state: _State | None, repo: str) -> bool:
    return state is None or state.repo != repo


def _incremental(state: _State | None, repo: str) -> bool:
    return not _is_full(state, repo) and state.watermark is not None


class GitHubCrawler(Crawler):
    """Crawl the repository's pull requests and issues, incrementally.

    A kind with no matching ``forge_crawl_state`` row is walked in full
    (sized by a ``totalCount`` query); an empty-repository row walks the
    first page again; otherwise only what changed since the stored
    watermark is fetched and only changed rows are written. See the
    module docstring for the fetch-then-write order.

    :class:`GitHubClient.check_auth` runs at the top of :meth:`work`
    so a missing or unauthenticated ``gh`` CLI surfaces as
    :class:`whygraph.services.github.GitHubError` before any pagination
    starts. The base class captures the exception into :attr:`error`
    without affecting any sibling crawler in the same scan.

    Parameters
    ----------
    progress : rich.progress.Progress
        Shared Progress instance owned by the orchestrator.
    client : GitHubClient
        The GitHub client to read PRs and issues through. Typically
        constructed via :meth:`GitHubClient.for_repository`.
    """

    def __init__(self, progress: Progress, *, client: GitHubClient) -> None:
        super().__init__("github", progress, total=None)
        self._client = client

    def work(self) -> None:
        GitHubClient.check_auth()
        repo = f"{self._client.owner}/{self._client.name}".lower()

        # (a) short read session, no writes. Copy to plain values: get_session()
        # commits (expiring ORM objects) and closes.
        with get_session() as session:
            states = {
                s.kind: _State(repo=s.repo, watermark=s.watermark)
                for s in session.exec(select(ForgeCrawlState)).all()
            }
            stored_prs = dict(
                session.exec(
                    select(PullRequestRow.number, PullRequestRow.updated_at)
                ).all()
            )
            stored_issues = dict(
                session.exec(select(IssueRow.number, IssueRow.updated_at)).all()
            )

        # (b) drain the network with NO session open
        pr_state, issue_state = states.get("pull_request"), states.get("issue")
        full_pr, full_issue = _is_full(pr_state, repo), _is_full(issue_state, repo)
        if full_pr or full_issue:
            self.set_total(
                (len(self._client.pull_requests) if full_pr else 0)
                + (len(self._client.issues) if full_issue else 0)
            )
        prs, pr_marks = self._drain_prs(pr_state, repo, stored_prs)
        issues = self._drain(self._iter_issues(issue_state, repo))
        issue_marks = [i.updated_at for i in issues]

        # (c) rows in short chunked transactions, then the state last
        fetched_at = datetime.now(timezone.utc).isoformat()
        pr_new = pr_upd = is_new = is_upd = 0
        for chunk in _chunks(prs, WRITE_CHUNK):
            with get_session() as session:
                n, u = _upsert_prs(
                    session, chunk, stored=stored_prs, fetched_at=fetched_at
                )
            pr_new, pr_upd = pr_new + n, pr_upd + u
        for chunk in _chunks(issues, WRITE_CHUNK):
            with get_session() as session:
                n, u = _upsert_issues(
                    session, chunk, stored=stored_issues, fetched_at=fetched_at
                )
            is_new, is_upd = is_new + n, is_upd + u
        with get_session() as session:
            _save_state(session, "pull_request", repo, pr_marks, pr_state, fetched_at)
            _save_state(session, "issue", repo, issue_marks, issue_state, fetched_at)

        self.set_total(len(prs) + len(issues))
        if full_pr and full_issue:
            self.summary = f"{len(prs)} PRs · {len(issues)} issues"
        else:
            self.summary = (
                f"{pr_new} new · {pr_upd} updated PRs · "
                f"{is_new} new · {is_upd} updated issues"
            )

    def _drain(self, items: Iterator[_T]) -> list[_T]:
        """Collect ``items``, keeping the first per ``number``; advance per kept item."""
        kept: dict[int, _T] = {}
        for item in items:
            number = item.number  # type: ignore[attr-defined]
            if number not in kept:
                kept[number] = item
                self.advance(1)
        return list(kept.values())

    def _iter_issues(self, state: _State | None, repo: str) -> Iterator[IssueDC]:
        if _incremental(state, repo):
            return self._client.issues.updated_since(state.watermark)
        return iter(self._client.issues)

    def _drain_prs(
        self, state: _State | None, repo: str, stored: dict[int, str]
    ) -> tuple[list[PullRequestDC], list[str]]:
        """Fetch the PRs to write and the ``updatedAt`` candidates for the mark."""
        if not _incremental(state, repo):
            prs = self._drain(iter(self._client.pull_requests))
            return prs, [p.updated_at for p in prs]
        listing: dict[int, str] = {}
        for number, updated_at in self._client.pull_requests.updates_since(
            state.watermark
        ):
            listing.setdefault(number, updated_at)
        changed = [n for n, u in listing.items() if stored.get(n) != u]
        prs = self._drain(self._client.pull_requests.by_numbers(changed))
        return prs, list(listing.values())


def _upsert_prs(
    session,
    prs: list[PullRequestDC],
    *,
    stored: dict[int, str],
    fetched_at: str,
) -> tuple[int, int]:
    """Write new and changed PRs; return ``(new, updated)``. Equal rows are skipped."""
    new = updated = 0
    for pr in prs:
        if pr.number not in stored:
            session.add(_pr_to_row(pr, fetched_at=fetched_at))
            new += 1
        elif stored[pr.number] == pr.updated_at:
            continue
        else:
            session.merge(_pr_to_row(pr, fetched_at=fetched_at))
            session.exec(
                delete(PRIssueLink).where(
                    PRIssueLink.pr_number == pr.number,
                    PRIssueLink.link_kind == "closes",
                )
            )
            updated += 1
        for issue_number in dict.fromkeys(pr.closing_issue_numbers):
            session.add(
                PRIssueLink(
                    pr_number=pr.number,
                    issue_number=issue_number,
                    link_kind="closes",
                )
            )
    return new, updated


def _upsert_issues(
    session,
    issues: list[IssueDC],
    *,
    stored: dict[int, str],
    fetched_at: str,
) -> tuple[int, int]:
    """Write new and changed issues; return ``(new, updated)``."""
    new = updated = 0
    for issue in issues:
        if issue.number not in stored:
            session.add(_issue_to_row(issue, fetched_at=fetched_at))
            new += 1
        elif stored[issue.number] != issue.updated_at:
            session.merge(_issue_to_row(issue, fetched_at=fetched_at))
            updated += 1
    return new, updated


def _save_state(
    session,
    kind: str,
    repo: str,
    marks: list[str],
    previous: _State | None,
    now: str,
) -> None:
    """Store the new watermark: the newest of ``marks`` and the previous mark."""
    candidates = list(marks)
    if previous is not None and previous.repo == repo and previous.watermark:
        candidates.append(previous.watermark)
    watermark = max(candidates, key=parse_github_ts) if candidates else None
    session.merge(
        ForgeCrawlState(kind=kind, repo=repo, watermark=watermark, crawled_at=now)
    )


def _pr_to_row(dc: PullRequestDC, *, fetched_at: str) -> PullRequestRow:
    return PullRequestRow(
        number=dc.number,
        title=dc.title,
        body=dc.body,
        state=dc.state,
        draft=1 if dc.draft else 0,
        created_at=dc.created_at,
        updated_at=dc.updated_at,
        closed_at=dc.closed_at,
        merged_at=dc.merged_at,
        merge_commit_sha=dc.merge_commit_sha,
        head_sha=dc.head_sha,
        head_ref=dc.head_ref,
        base_ref=dc.base_ref,
        author=dc.author,
        html_url=dc.html_url,
        labels=json.dumps(list(dc.labels)),
        fetched_at=fetched_at,
        commit_titles=json.dumps(
            [
                {
                    "oid": c.oid,
                    "headline": c.headline,
                    "author_login": c.author_login,
                    "author_name": c.author_name,
                    "author_email": c.author_email,
                }
                for c in dc.commits
            ]
        ),
        comments=json.dumps(
            [
                {"author": c.author, "body": c.body, "created_at": c.created_at}
                for c in dc.comments
            ]
        ),
    )


def _issue_to_row(dc: IssueDC, *, fetched_at: str) -> IssueRow:
    return IssueRow(
        number=dc.number,
        title=dc.title,
        body=dc.body,
        state=dc.state,
        created_at=dc.created_at,
        updated_at=dc.updated_at,
        closed_at=dc.closed_at,
        author=dc.author,
        html_url=dc.html_url,
        labels=json.dumps(list(dc.labels)),
        fetched_at=fetched_at,
    )
