"""GitHubCrawler - pull a repository's PRs and issues from GitHub.

Reads the pull requests and issues exposed by
:class:`whygraph.services.github.GitHubClient` into the ``pullrequest``
and ``issue`` tables, one row per number. PR ``closing_issue_numbers``
are flattened into ``prissuelink`` rows under ``link_kind='closes'``.
Milestones are few: every crawl re-lists all of them into the ``milestone``
table and deletes the ones GitHub no longer has (kind ``"milestone"`` keeps
a state row too, but its watermark is never used to filter).

Each kind keeps a ``forge_crawl_state`` row (the repository and the
newest ``updatedAt`` seen). With a matching row the crawl is
*incremental*: it asks GitHub only for items updated since that mark,
re-reads and rewrites every PR that listing returns (some discussion edits
do not move ``updatedAt``), and rewrites only issues that changed. Without
a matching row (first crawl, another repository, a database written by the
old insert-only crawler, or a row stored below :data:`FORGE_FIELDS_VERSION`)
it is a *full walk*, newest-first; the last case also rewrites rows whose
``updatedAt`` is unchanged, once.

:meth:`GitHubCrawler.work` runs in three phases so SQLite's write lock is
never held across the network: (a) read the state in a short session,
(b) drain GitHub with no session open, (c) write rows in chunked
transactions and the state rows last, in a transaction of their own. A
crawl that fails anywhere leaves the state unchanged, so the next one
re-lists the same items.

After the state is saved, a resumable **thread pass** fetches the review
threads of PRs that have any and are not synced yet, newest first, writing
each batch in its own short transaction and stopping at a rate-limit
floor; the next crawl resumes it. Its stops and errors never fail the crawl.

What is stored follows plan 62's R19: bot and minimized comments are not
stored, except a review thread's opener (kept, flagged and clipped, as
context for a human reply).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import NamedTuple, TypeVar

from rich.progress import Progress
from sqlmodel import col, delete, select

from whygraph.db import get_session
from whygraph.db.models.forge_crawl_state import ForgeCrawlState
from whygraph.db.models.issue import Issue as IssueRow
from whygraph.db.models.milestone import Milestone as MilestoneRow
from whygraph.db.models.pr_issue_link import PRIssueLink
from whygraph.db.models.pull_request import PullRequest as PullRequestRow
from whygraph.services.github import GitHubClient
from whygraph.services.github.commands import parse_github_ts
from whygraph.services.github.issue import Issue as IssueDC
from whygraph.services.github.milestones import Milestone as MilestoneDC
from whygraph.services.github.pull_request import Comment as CommentDC
from whygraph.services.github.pull_request import PullRequest as PullRequestDC
from whygraph.services.github.pull_request import Review as ReviewDC
from whygraph.services.github.pull_request import ReviewThread as ReviewThreadDC
from whygraph.services.github.pull_requests import THREAD_BATCH

from .crawler import Crawler

_T = TypeVar("_T")

WRITE_CHUNK = 500

FORGE_FIELDS_VERSION = 2
"""Version of the fields the crawl stores (``forge_crawl_state.fields_version``).

A kind stored below it is walked in full and every row rewritten once,
bypassing the ``updated_at`` equality skip, so a database written before
the phase 2 fields (milestone, assignees, reviews, PR files, ...) is
backfilled by one scan. Bump it whenever a crawl must re-read old rows.

1: milestone, assignees, reviews, PR files and line counts.
2: review summaries, thread totals, issue comments, comment totals; bot and
minimized comments no longer stored (R19).
"""

THREAD_PASS_POINT_FLOOR = 1000
"""Rate-limit points the thread pass leaves unspent (20 % of an hourly budget).

Kept for the installation's other projects and the next forge run; the
pass stops below it and the next crawl resumes.
"""

BOT_OPENER_STORE_CHARS = 300
"""Characters kept of a bot or minimized review-thread opener's body (R19)."""


class _State(NamedTuple):
    """Plain copy of a ``forge_crawl_state`` row (ORM rows expire on commit)."""

    repo: str
    watermark: str | None
    fields_version: int = 0


def _chunks(items: list[_T], size: int) -> Iterator[list[_T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _is_full(state: _State | None, repo: str) -> bool:
    return state is None or state.repo != repo or _is_stale(state)


def _is_stale(state: _State | None) -> bool:
    """Whether ``state`` was written before :data:`FORGE_FIELDS_VERSION`."""
    return state is not None and state.fields_version < FORGE_FIELDS_VERSION


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
                s.kind: _State(
                    repo=s.repo,
                    watermark=s.watermark,
                    fields_version=s.fields_version,
                )
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
        # An incremental PR run rewrites every PR the listing returns, not only
        # those whose updatedAt moved: some discussion edits (review-thread
        # comment edits, bursts of reviews) do not move it (plan 62, risk 3a).
        # Issues keep the equality skip.
        force_pr = _is_stale(pr_state) or _incremental(pr_state, repo)
        force_issue = _is_stale(issue_state)
        if full_pr or full_issue:
            self.set_total(
                (len(self._client.pull_requests) if full_pr else 0)
                + (len(self._client.issues) if full_issue else 0)
            )
        prs, pr_marks = self._drain_prs(pr_state, repo)
        issues = self._drain(self._iter_issues(issue_state, repo))
        issue_marks = [i.updated_at for i in issues]
        milestones = self._drain(iter(self._client.milestones))
        milestone_marks = [m.updated_at for m in milestones]

        # (c) rows in short chunked transactions, then the state last
        fetched_at = datetime.now(timezone.utc).isoformat()
        pr_new = pr_upd = is_new = is_upd = 0
        for chunk in _chunks(prs, WRITE_CHUNK):
            with get_session() as session:
                n, u = _upsert_prs(
                    session,
                    chunk,
                    stored=stored_prs,
                    fetched_at=fetched_at,
                    force=force_pr,
                )
            pr_new, pr_upd = pr_new + n, pr_upd + u
        for chunk in _chunks(issues, WRITE_CHUNK):
            with get_session() as session:
                n, u = _upsert_issues(
                    session,
                    chunk,
                    stored=stored_issues,
                    fetched_at=fetched_at,
                    force=force_issue,
                )
            is_new, is_upd = is_new + n, is_upd + u
        with get_session() as session:
            _replace_milestones(session, milestones, fetched_at=fetched_at)
        with get_session() as session:
            _save_state(session, "pull_request", repo, pr_marks, pr_state, fetched_at)
            _save_state(session, "issue", repo, issue_marks, issue_state, fetched_at)
            _save_state(
                session,
                "milestone",
                repo,
                milestone_marks,
                states.get("milestone"),
                fetched_at,
            )

        self.set_total(len(prs) + len(issues) + len(milestones))
        if full_pr and full_issue:
            self.summary = (
                f"{len(prs)} PRs · {len(issues)} issues · {len(milestones)} milestones"
            )
        else:
            self.summary = (
                f"{pr_new} new · {pr_upd} updated PRs · "
                f"{is_new} new · {is_upd} updated issues · "
                f"{len(milestones)} milestones"
            )
        self.summary += self._thread_pass()

    def _thread_pass(self) -> str:
        """Fetch and store review threads of unsynced PRs; return a summary suffix.

        Runs after the main crawl's rows and state are saved. Selects PRs
        with ``review_threads_total > 0`` and ``review_threads_synced = 0``,
        newest ``updated_at`` first, and drains
        :meth:`PullRequests.review_threads` with no session open; the
        results are written in groups of :data:`THREAD_BATCH`, each in its
        own short transaction, between the iterator's network requests, and
        a PR is yielded only after its whole batch returned, so a stop never
        writes a partial PR. A floor stop or a :class:`GitHubError` ends the
        pass without failing the crawl (what was yielded is still written);
        skipped PRs (a 502 / 504 alone) stay unsynced for the next crawl.

        Returns
        -------
        str
            ``""`` when no PR was due, else ``" · threads: N PRs synced"``
            plus ``" · M PRs pending (rate limit)"`` or
            ``" · thread pass stopped: <error>"`` when it stopped.
        """
        with get_session() as session:
            numbers = list(
                session.exec(
                    select(PullRequestRow.number)
                    .where(
                        col(PullRequestRow.review_threads_total) > 0,
                        PullRequestRow.review_threads_synced == 0,
                    )
                    .order_by(
                        col(PullRequestRow.updated_at).desc(),
                        col(PullRequestRow.number).desc(),
                    )
                ).all()
            )
        if not numbers:
            return ""
        run = self._client.pull_requests.review_threads(
            numbers, point_floor=THREAD_PASS_POINT_FLOOR
        )
        synced = 0
        error: Exception | None = None
        batch: list[tuple[int, tuple[ReviewThreadDC, ...] | None]] = []
        try:
            for result in run:
                batch.append(result)
                if len(batch) >= THREAD_BATCH:
                    synced += _write_threads(batch)
                    batch = []
        except Exception as exc:  # noqa: BLE001 - the thread pass never fails the crawl
            error = exc
        if batch:
            synced += _write_threads(batch)
        suffix = f" · threads: {synced} PRs synced"
        if error is not None:
            suffix += f" · thread pass stopped: {error}"
        elif run.stopped:
            suffix += f" · {len(run.pending)} PRs pending (rate limit)"
        return suffix

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
        self, state: _State | None, repo: str
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
        # Re-read every listed PR, not only those whose updatedAt changed: the
        # listing is already bounded by the lookback, and edits that do not
        # move updatedAt (review-thread comments) are then still picked up
        # (plan 62, risk 3a).
        prs = self._drain(self._client.pull_requests.by_numbers(list(listing)))
        return prs, list(listing.values())


def _upsert_prs(
    session,
    prs: list[PullRequestDC],
    *,
    stored: dict[int, str],
    fetched_at: str,
    force: bool = False,
) -> tuple[int, int]:
    """Write new and changed PRs; return ``(new, updated)``.

    Equal rows are skipped unless ``force`` (a fields backfill, or an
    incremental run, rewrites them). The thread pass owns
    ``review_threads``: an updated row keeps the stored threads (a transient
    row would carry the ``'[]'`` default), and every written row gets the
    sync flag from :func:`_set_thread_sync`.
    """
    new = updated = 0
    for pr in prs:
        if pr.number not in stored:
            session.add(_set_thread_sync(_pr_to_row(pr, fetched_at=fetched_at)))
            new += 1
        elif not force and stored[pr.number] == pr.updated_at:
            continue
        else:
            row = _pr_to_row(pr, fetched_at=fetched_at)
            existing = session.get(PullRequestRow, pr.number)
            if existing is not None:
                row.review_threads = existing.review_threads
            session.merge(_set_thread_sync(row))
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


def _set_thread_sync(row: PullRequestRow) -> PullRequestRow:
    """Set the thread-sync state of a PR row about to be written; return it.

    A PR with threads on GitHub is due for the thread pass again
    (``review_threads_synced = 0``; its stored threads stay until the pass
    replaces them); one without has nothing to fetch, so its threads are
    cleared and it counts as synced.
    """
    if row.review_threads_total:
        row.review_threads_synced = 0
    else:
        row.review_threads = "[]"
        row.review_threads_synced = 1
    return row


def _write_threads(
    results: list[tuple[int, tuple[ReviewThreadDC, ...] | None]],
) -> int:
    """Store one group of thread-pass results in one transaction; return the count.

    A not-found PR (``None``) is stored with no threads; either way the row
    is marked synced. A PR whose row is gone is skipped.
    """
    written = 0
    with get_session() as session:
        for number, threads in results:
            row = session.get(PullRequestRow, number)
            if row is None:
                continue
            row.review_threads = json.dumps(_stored_threads(threads or ()))
            row.review_threads_synced = 1
            session.add(row)
            written += 1
    return written


def _upsert_issues(
    session,
    issues: list[IssueDC],
    *,
    stored: dict[int, str],
    fetched_at: str,
    force: bool = False,
) -> tuple[int, int]:
    """Write new and changed issues; return ``(new, updated)``.

    ``force`` rewrites rows whose ``updated_at`` is unchanged (a fields backfill).
    """
    new = updated = 0
    for issue in issues:
        if issue.number not in stored:
            session.add(_issue_to_row(issue, fetched_at=fetched_at))
            new += 1
        elif force or stored[issue.number] != issue.updated_at:
            session.merge(_issue_to_row(issue, fetched_at=fetched_at))
            updated += 1
    return new, updated


def _replace_milestones(
    session, milestones: list[MilestoneDC], *, fetched_at: str
) -> None:
    """Store the full milestone listing and delete the rows it no longer has."""
    numbers = [m.number for m in milestones]
    session.exec(delete(MilestoneRow).where(col(MilestoneRow.number).not_in(numbers)))
    for m in milestones:
        session.merge(
            MilestoneRow(
                number=m.number,
                title=m.title,
                description=m.description,
                state=m.state,
                due_on=m.due_on,
                closed_at=m.closed_at,
                updated_at=m.updated_at,
                progress_pct=m.progress_pct,
                html_url=m.html_url,
                fetched_at=fetched_at,
            )
        )


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
        ForgeCrawlState(
            kind=kind,
            repo=repo,
            watermark=watermark,
            crawled_at=now,
            fields_version=FORGE_FIELDS_VERSION,
        )
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
        comments=json.dumps(_stored_comments(dc.comments)),
        milestone=dc.milestone,
        assignees=json.dumps(list(dc.assignees)),
        review_decision=dc.review_decision,
        latest_reviews=json.dumps(
            [
                {"author": r.author, "state": r.state, "submitted_at": r.submitted_at}
                for r in dc.latest_reviews
            ]
        ),
        review_requests=json.dumps(list(dc.review_requests)),
        first_review_at=dc.first_review_at,
        files=json.dumps(list(dc.files)),
        additions=dc.additions,
        deletions=dc.deletions,
        changed_files=dc.changed_files,
        review_summaries=json.dumps(_stored_summaries(dc.review_summaries)),
        review_threads_total=dc.review_threads_total,
        comments_total=dc.comments_total,
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
        milestone=dc.milestone,
        assignees=json.dumps(list(dc.assignees)),
        state_reason=dc.state_reason,
        comments=json.dumps(_stored_comments(dc.comments)),
        comments_total=dc.comments_total,
    )


def _is_human(c: CommentDC | ReviewDC) -> bool:
    """Whether a parsed comment or review is stored (neither bot nor minimized)."""
    return not c.bot and not c.minimized


def _stored_comments(comments: tuple[CommentDC, ...]) -> list[dict]:
    """Conversation comments (PR or issue) as stored: human, non-minimized only.

    Stored without ``bot`` / ``minimized`` keys (R19).
    """
    return [
        {"author": c.author, "body": c.body, "created_at": c.created_at}
        for c in comments
        if _is_human(c)
    ]


def _stored_summaries(reviews: tuple[ReviewDC, ...]) -> list[dict]:
    """Review summaries as stored: human, non-minimized only, without flags (R19)."""
    return [
        {
            "author": r.author,
            "state": r.state,
            "body": r.body,
            "submitted_at": r.submitted_at,
        }
        for r in reviews
        if _is_human(r)
    ]


def _stored_threads(threads: tuple[ReviewThreadDC, ...]) -> list[dict]:
    """Review threads as stored (R19).

    Bot and minimized comments are dropped except the opener, and a thread
    with no human, non-minimized comment among those fetched (the first
    10) is not stored. The opener always carries ``bot`` and ``minimized``;
    a bot or minimized opener's body is clipped to
    :data:`BOT_OPENER_STORE_CHARS`. Other comments carry no flags. The
    anchor is stored as ``{"lines": [...], "added": bool}`` or ``None``;
    no raw diff hunk is stored.
    """
    stored: list[dict] = []
    for t in threads:
        if not t.comments or not any(_is_human(c) for c in t.comments):
            continue
        opener, rest = t.comments[0], t.comments[1:]
        body = (
            opener.body if _is_human(opener) else opener.body[:BOT_OPENER_STORE_CHARS]
        )
        comments = [
            {
                "author": opener.author,
                "body": body,
                "created_at": opener.created_at,
                "bot": opener.bot,
                "minimized": opener.minimized,
            }
        ]
        comments += _stored_comments(rest)
        stored.append(
            {
                "path": t.path,
                "original_line": t.original_line,
                "anchor": (
                    {"lines": list(t.anchor.lines), "added": t.anchor.added}
                    if t.anchor is not None
                    else None
                ),
                "resolved": t.resolved,
                "outdated": t.outdated,
                "comments": comments,
                "comments_total": t.comments_total,
            }
        )
    return stored
