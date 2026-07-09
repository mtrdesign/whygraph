"""GitHubCrawler — pull a repository's PRs and issues from GitHub.

Walks the pull requests and issues exposed by
:class:`whygraph.services.github.GitHubClient`, inserting one row per
*new* number into the ``pullrequest`` and ``issue`` tables. Existing
numbers are skipped without modification, so re-scans on a repository
whose PR/issue list only grows are no-ops. PR ``closing_issue_numbers``
are flattened into ``prissuelink`` rows under ``link_kind='closes'``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from rich.progress import Progress
from sqlmodel import select

from whygraph.db import get_session
from whygraph.db.models.issue import Issue as IssueRow
from whygraph.db.models.pr_issue_link import PRIssueLink
from whygraph.db.models.pull_request import PullRequest as PullRequestRow
from whygraph.services.github import GitHubClient
from whygraph.services.github.issue import Issue as IssueDC
from whygraph.services.github.pull_request import PullRequest as PullRequestDC

from .crawler import Crawler


class GitHubCrawler(Crawler):
    """Crawl every pull request and issue in the repository.

    Sizes the progress bar from ``len(client.pull_requests) +
    len(client.issues)`` (each from a cheap ``totalCount`` GraphQL
    query) and advances per item — insert or skip. PRs are processed
    first, then issues; numbers already present in the ``pullrequest``
    or ``issue`` tables are skipped without updates so re-scans are
    idempotent.

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

        prs = self._client.pull_requests
        issues = self._client.issues
        self.set_total(len(prs) + len(issues))

        with get_session() as session:
            existing_prs: set[int] = set(
                session.exec(select(PullRequestRow.number)).all()
            )
            existing_issues: set[int] = set(session.exec(select(IssueRow.number)).all())
            fetched_at = datetime.now(timezone.utc).isoformat()

            for pr in prs:
                if pr.number not in existing_prs:
                    session.add(_pr_to_row(pr, fetched_at=fetched_at))
                    for issue_number in pr.closing_issue_numbers:
                        session.add(
                            PRIssueLink(
                                pr_number=pr.number,
                                issue_number=issue_number,
                                link_kind="closes",
                            )
                        )
                self.advance(1)

            for issue in issues:
                if issue.number not in existing_issues:
                    session.add(_issue_to_row(issue, fetched_at=fetched_at))
                self.advance(1)

        self.summary = f"{len(prs)} PRs · {len(issues)} issues"


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
