"""Tests for :mod:`whygraph.scan.github_crawler` (incremental GitHub crawl)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from rich.progress import Progress
from sqlmodel import select

from whygraph.db import get_session
from whygraph.db.models import (
    ForgeCrawlState,
    Issue,
    Milestone,
    PRIssueLink,
    PullRequest,
)
from whygraph.scan import github_crawler
from whygraph.scan.github_crawler import GitHubCrawler
from whygraph.services.github import GitHubError
from whygraph.services.github.issue import Issue as IssueDC
from whygraph.services.github.milestones import Milestone as MilestoneDC
from whygraph.core.code_anchor import Anchor
from whygraph.services.github.pull_request import Comment, Review, ReviewThread
from whygraph.services.github.pull_request import PullRequest as PullRequestDC

T0 = "2026-10-01T10:00:00Z"
T1 = "2026-10-02T10:00:00Z"
T2 = "2026-10-03T10:00:00Z"
T3 = "2026-10-04T10:00:00Z"


def pr_dc(number: int, updated_at: str = T0, **kw) -> PullRequestDC:
    base = dict(
        number=number,
        title=f"PR {number}",
        body="body",
        state="open",
        draft=False,
        created_at=T0,
        updated_at=updated_at,
        closed_at=None,
        merged_at=None,
        merge_commit_sha=None,
        head_sha=f"head{number}",
        head_ref="feature",
        base_ref="main",
        author="dev",
        html_url=f"https://example.test/pr/{number}",
        labels=(),
        commits=(),
        closing_issue_numbers=(),
        comments=(),
    )
    base.update(kw)
    return PullRequestDC(**base)


def issue_dc(number: int, updated_at: str = T0, **kw) -> IssueDC:
    base = dict(
        number=number,
        title=f"Issue {number}",
        body="body",
        state="open",
        created_at=T0,
        updated_at=updated_at,
        closed_at=None,
        author="dev",
        html_url=f"https://example.test/issues/{number}",
        labels=(),
    )
    base.update(kw)
    return IssueDC(**base)


class FakeThreadPass:
    """A ``ThreadPass`` stand-in: yields results, then stops, fails or ends.

    ``threads`` maps a PR number to its threads (``None`` = not found); a
    number missing from it is not fetched. ``stop_after`` stops at the
    floor after that many PRs; ``fail_after`` raises ``GitHubError`` then.
    ``on_fetch`` runs before each PR is yielded (the network moment).
    """

    def __init__(
        self,
        numbers,
        threads,
        *,
        stop_after=None,
        fail_after=None,
        skip=(),
        on_fetch=None,
    ) -> None:
        self.stopped = False
        self.pending: tuple[int, ...] = ()
        self.skipped: tuple[int, ...] = tuple(n for n in numbers if n in skip)
        self.remaining = None
        self.reset_at = None
        self._items = self._run(
            [n for n in numbers if n not in skip],
            threads,
            stop_after,
            fail_after,
            on_fetch,
        )

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._items)

    def _run(self, numbers, threads, stop_after, fail_after, on_fetch):
        for i, n in enumerate(numbers):
            if stop_after is not None and i >= stop_after:
                self.stopped = True
                self.pending = tuple(numbers[i:])
                return
            if fail_after is not None and i >= fail_after:
                raise GitHubError("thread batch failed")
            if on_fetch is not None:
                on_fetch(n)
            yield n, threads.get(n, ())


class FakePRs:
    def __init__(self, prs: list[PullRequestDC]) -> None:
        self.prs = prs  # newest-first
        self.len_calls = 0
        self.iter_calls = 0
        self.since_marks: list[str] = []
        self.by_numbers_calls: list[list[int]] = []
        self.listing_override: list[tuple[int, str]] | None = None
        self.on_by_numbers = None
        self.threads: dict[int, tuple[ReviewThread, ...] | None] = {}
        self.thread_calls: list[tuple[list[int], int]] = []
        self.thread_pass_kw: dict = {}

    def __len__(self) -> int:
        self.len_calls += 1
        return len(self.prs)

    def __iter__(self):
        self.iter_calls += 1
        return iter(list(self.prs))

    def updates_since(self, mark: str):
        self.since_marks.append(mark)
        if self.listing_override is not None:
            return iter(self.listing_override)
        return iter([(p.number, p.updated_at) for p in self.prs])

    def by_numbers(self, numbers):
        self.by_numbers_calls.append(list(numbers))
        if self.on_by_numbers is not None:
            return self.on_by_numbers(self, list(numbers))
        return iter([p for p in self.prs if p.number in numbers])

    def review_threads(self, numbers, *, point_floor):
        self.thread_calls.append((list(numbers), point_floor))
        return FakeThreadPass(list(numbers), self.threads, **self.thread_pass_kw)


class FakeIssues:
    def __init__(self, issues: list[IssueDC]) -> None:
        self.issues = issues
        self.len_calls = 0
        self.iter_calls = 0
        self.since_marks: list[str] = []
        self.on_since = None

    def __len__(self) -> int:
        self.len_calls += 1
        return len(self.issues)

    def __iter__(self):
        self.iter_calls += 1
        return iter(list(self.issues))

    def updated_since(self, mark: str):
        self.since_marks.append(mark)
        if self.on_since is not None:
            return self.on_since(self)
        return iter(list(self.issues))


class FakeMilestones:
    def __init__(self, milestones: list[MilestoneDC]) -> None:
        self.milestones = milestones
        self.iter_calls = 0

    def __iter__(self):
        self.iter_calls += 1
        return iter(list(self.milestones))


class FakeClient:
    def __init__(
        self, prs=(), issues=(), owner="Acme", name="Widgets", milestones=()
    ) -> None:
        self.milestones = FakeMilestones(list(milestones))
        self.owner = owner
        self.name = name
        self.pull_requests = FakePRs(list(prs))
        self.issues = FakeIssues(list(issues))


@pytest.fixture(autouse=True)
def _auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "whygraph.scan.github_crawler.GitHubClient.check_auth",
        staticmethod(lambda env=None: None),
    )


def crawl(client: FakeClient) -> GitHubCrawler:
    c = GitHubCrawler(Progress(), client=client)
    c.run()
    return c


def states() -> dict[str, ForgeCrawlState]:
    with get_session() as session:
        rows = session.exec(select(ForgeCrawlState)).all()
        return {r.kind: ForgeCrawlState(**r.model_dump()) for r in rows}


def snapshot() -> dict:
    with get_session() as session:
        return {
            "prs": sorted(
                (r.number, r.updated_at, r.state, r.fetched_at)
                for r in session.exec(select(PullRequest)).all()
            ),
            "issues": sorted(
                (r.number, r.updated_at, r.state, r.fetched_at)
                for r in session.exec(select(Issue)).all()
            ),
            "links": sorted(
                (r.pr_number, r.issue_number)
                for r in session.exec(select(PRIssueLink)).all()
            ),
            "state": sorted(
                (r.kind, r.repo, r.watermark, r.crawled_at)
                for r in session.exec(select(ForgeCrawlState)).all()
            ),
        }


def fresh_client() -> FakeClient:
    return FakeClient(
        prs=[pr_dc(9, T2), pr_dc(5, T1), pr_dc(1, T0)],
        issues=[issue_dc(12, T2), issue_dc(3, T0)],
    )


def test_first_crawl_walks_everything(whygraph_db_initialized: Path) -> None:
    client = fresh_client()
    c = crawl(client)

    assert c.error is None
    assert c.summary == "3 PRs · 2 issues · 0 milestones"
    assert (client.pull_requests.len_calls, client.pull_requests.iter_calls) == (1, 1)
    assert (client.issues.len_calls, client.issues.iter_calls) == (1, 1)
    assert client.pull_requests.since_marks == []
    assert client.pull_requests.by_numbers_calls == []
    assert client.issues.since_marks == []
    s = states()
    assert s["pull_request"].watermark == T2
    assert s["pull_request"].repo == "acme/widgets"
    assert s["issue"].watermark == T2
    snap = snapshot()
    assert [p[0] for p in snap["prs"]] == [1, 5, 9]
    assert [i[0] for i in snap["issues"]] == [3, 12]


def test_second_crawl_with_nothing_changed(whygraph_db_initialized: Path) -> None:
    client = fresh_client()
    crawl(client)
    before = snapshot()
    client.pull_requests.len_calls = client.issues.len_calls = 0

    c = crawl(client)

    assert c.error is None
    assert client.pull_requests.since_marks == [T2]
    assert client.issues.since_marks == [T2]
    # every listed PR is re-read and rewritten (plan 62, risk 3a)
    assert client.pull_requests.by_numbers_calls == [[9, 5, 1]]
    assert client.pull_requests.len_calls == client.issues.len_calls == 0
    assert (
        c.summary == "0 new · 3 updated PRs · 0 new · 0 updated issues · 0 milestones"
    )
    after = snapshot()
    assert [p[:3] for p in after["prs"]] == [p[:3] for p in before["prs"]]
    assert all(a[3] != b[3] for a, b in zip(after["prs"], before["prs"]))
    assert after["issues"] == before["issues"]


def test_changed_prs_are_requested_and_rewritten(
    whygraph_db_initialized: Path,
) -> None:
    client = fresh_client()
    crawl(client)
    client.pull_requests.prs = [
        pr_dc(
            9,
            T3,
            state="closed",
            merged_at=T3,
            closed_at=T3,
            comments=(Comment(author="a", body="lgtm", created_at=T3),),
        ),
        pr_dc(5, T3),
        pr_dc(1, T0),
    ]

    c = crawl(client)

    assert c.error is None
    assert sorted(client.pull_requests.by_numbers_calls[0]) == [1, 5, 9]
    assert (
        c.summary == "0 new · 3 updated PRs · 0 new · 0 updated issues · 0 milestones"
    )
    with get_session() as session:
        row = session.get(PullRequest, 9)
        assert row.state == "closed"
        assert row.merged_at == T3
        assert "lgtm" in row.comments
        assert row.updated_at == T3
        unchanged = session.get(PullRequest, 1)
        assert unchanged.updated_at == T0
    assert states()["pull_request"].watermark == T3


def test_ac_1a_exact_numbers(whygraph_db_initialized: Path) -> None:
    client = fresh_client()
    crawl(client)
    client.pull_requests.prs = [pr_dc(5, T3), pr_dc(9, T3), pr_dc(1, T0)]
    client.pull_requests.listing_override = [(5, T3), (9, T3), (1, T0)]
    crawl(client)
    # every listed number, in listing order, unchanged ones included
    assert client.pull_requests.by_numbers_calls == [[5, 9, 1]]


def test_closing_links_replaced_and_deduplicated(
    whygraph_db_initialized: Path,
) -> None:
    client = FakeClient(
        prs=[
            pr_dc(9, T2, closing_issue_numbers=(12, 12)),
            pr_dc(5, T1, closing_issue_numbers=(3, 4)),
            pr_dc(1, T0, closing_issue_numbers=(7,)),
        ]
    )
    crawl(client)
    with get_session() as session:
        links = {
            (r.pr_number, r.issue_number) for r in session.exec(select(PRIssueLink))
        }
    assert links == {(9, 12), (5, 3), (5, 4), (1, 7)}

    client.pull_requests.prs = [
        pr_dc(5, T3, closing_issue_numbers=(4, 20, 20)),
        pr_dc(9, T2, closing_issue_numbers=(12, 12)),
        pr_dc(1, T0, closing_issue_numbers=(7,)),
    ]
    crawl(client)
    with get_session() as session:
        links = {
            (r.pr_number, r.issue_number) for r in session.exec(select(PRIssueLink))
        }
    assert links == {(9, 12), (5, 4), (5, 20), (1, 7)}


def test_duplicate_numbers_first_sighting_wins(
    whygraph_db_initialized: Path,
) -> None:
    # full walk
    client = FakeClient(
        prs=[pr_dc(5, T2, title="first"), pr_dc(5, T1, title="second")],
        issues=[issue_dc(3, T2, title="first"), issue_dc(3, T1, title="second")],
    )
    c = crawl(client)
    assert c.error is None
    with get_session() as session:
        assert session.get(PullRequest, 5).title == "first"
        assert session.get(Issue, 3).title == "first"

    # incremental listing: each listed number requested once
    client.pull_requests.prs = [pr_dc(5, T3, title="newer")]
    client.pull_requests.listing_override = [(5, T3), (5, T3)]
    c = crawl(client)
    assert c.error is None
    assert client.pull_requests.by_numbers_calls == [[5]]
    with get_session() as session:
        assert session.get(PullRequest, 5).title == "newer"


def test_changed_issue_updated_and_lookback_issue_not_rewritten(
    whygraph_db_initialized: Path,
) -> None:
    client = fresh_client()
    crawl(client)
    before = {i[0]: i for i in snapshot()["issues"]}
    client.issues.issues = [
        issue_dc(3, T3, state="closed", closed_at=T3),
        issue_dc(12, T2),  # lookback item: equal updated_at
    ]

    c = crawl(client)

    # the listed PRs are rewritten; issues keep the equality skip
    assert (
        c.summary == "0 new · 3 updated PRs · 0 new · 1 updated issues · 0 milestones"
    )
    after = {i[0]: i for i in snapshot()["issues"]}
    assert after[3][1:3] == (T3, "closed")
    assert after[12] == before[12]


def test_pr_watermark_comes_from_the_listing(whygraph_db_initialized: Path) -> None:
    client = fresh_client()
    crawl(client)
    client.pull_requests.prs = [pr_dc(5, "2026-10-05T10:00:00Z")]  # details: T2'
    client.pull_requests.listing_override = [(5, T3), (9, T2)]

    crawl(client)

    assert states()["pull_request"].watermark == T3


def test_failure_mid_drain_changes_nothing(whygraph_db_initialized: Path) -> None:
    client = fresh_client()
    crawl(client)
    before = snapshot()
    client.pull_requests.prs = [pr_dc(5, T3), pr_dc(9, T2), pr_dc(1, T0)]

    def boom(fake, numbers):
        yield fake.prs[0]
        raise GitHubError("page 2 failed")

    client.pull_requests.on_by_numbers = boom
    c = crawl(client)
    assert isinstance(c.error, GitHubError)
    assert snapshot() == before

    client.pull_requests.on_by_numbers = None
    client.issues.issues = [issue_dc(3, T3)]

    def bad_issues(fake):
        yield fake.issues[0]
        raise GitHubError("issues page 2 failed")

    client.issues.on_since = bad_issues
    c = crawl(client)
    assert isinstance(c.error, GitHubError)
    assert snapshot() == before


def test_failure_after_first_chunk_keeps_state(
    whygraph_db_initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient(prs=[pr_dc(1, T0)], issues=[issue_dc(1, T0)])
    crawl(client)
    state_before = snapshot()["state"]
    client.pull_requests.prs = [pr_dc(n, T3) for n in (11, 12, 13, 14)]
    client.pull_requests.listing_override = [(n, T3) for n in (11, 12, 13, 14)]

    monkeypatch.setattr(github_crawler, "WRITE_CHUNK", 2)
    real = github_crawler._upsert_prs
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(github_crawler, "_upsert_prs", flaky)
    c = crawl(client)

    assert isinstance(c.error, RuntimeError)
    snap = snapshot()
    assert [p[0] for p in snap["prs"]] == [1, 11, 12]
    assert snap["state"] == state_before


def test_other_repo_state_forces_full_walk(whygraph_db_initialized: Path) -> None:
    client = fresh_client()
    crawl(client)
    other = FakeClient(
        prs=[pr_dc(5, T3, title="renamed")],
        issues=[issue_dc(3, T3)],
        owner="Other",
        name="Repo",
    )

    c = crawl(other)

    assert c.error is None
    assert (other.pull_requests.len_calls, other.pull_requests.iter_calls) == (1, 1)
    assert other.pull_requests.since_marks == []
    assert states()["pull_request"].repo == "other/repo"
    with get_session() as session:
        assert session.get(PullRequest, 5).title == "renamed"


def test_rows_without_state_get_full_walk(whygraph_db_initialized: Path) -> None:
    client = fresh_client()
    crawl(client)
    with get_session() as session:
        for row in session.exec(select(ForgeCrawlState)).all():
            session.delete(row)
    client.pull_requests.prs = [pr_dc(5, T3, title="fresh"), pr_dc(1, T0)]
    client.pull_requests.len_calls = 0

    c = crawl(client)

    assert c.error is None
    assert client.pull_requests.len_calls == 1
    assert client.pull_requests.since_marks == []
    with get_session() as session:
        assert session.get(PullRequest, 5).title == "fresh"
    assert states()["pull_request"].repo == "acme/widgets"


def test_lock_not_held_while_draining(whygraph_db_initialized: Path) -> None:
    client = fresh_client()
    crawl(client)
    client.pull_requests.prs = [pr_dc(5, T3), pr_dc(9, T2), pr_dc(1, T0)]
    db_path = whygraph_db_initialized
    outcome: dict[str, object] = {}

    def by_numbers(fake, numbers):
        yield fake.prs[0]
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            conn.execute(
                'INSERT INTO "commit" (sha, parent_shas, author_name, author_email, '
                "authored_at, committed_at, subject, body, files_changed, insertions, "
                "deletions, scanned_at, on_default_branch) VALUES "
                "('abc', '', 'n', 'e', 'a', 'c', 's', 'b', 1, 1, 0, 'x', 1)"
            )
            conn.commit()
            outcome["ok"] = True
        except sqlite3.OperationalError as exc:
            outcome["ok"] = str(exc)
        finally:
            conn.close()

    client.pull_requests.on_by_numbers = by_numbers
    c = crawl(client)

    assert c.error is None
    assert outcome == {"ok": True}


def test_empty_repository_stores_null_watermark(
    whygraph_db_initialized: Path,
) -> None:
    client = FakeClient()
    c = crawl(client)
    assert c.error is None
    s = states()
    assert s["pull_request"].watermark is None
    assert s["issue"].watermark is None

    client.pull_requests.len_calls = client.issues.len_calls = 0
    client.pull_requests.iter_calls = client.issues.iter_calls = 0
    c = crawl(client)

    assert c.error is None
    assert client.pull_requests.iter_calls == client.issues.iter_calls == 1
    assert client.pull_requests.len_calls == client.issues.len_calls == 0
    assert client.pull_requests.since_marks == []


def test_forge_fields_round_trip(whygraph_db_initialized: Path) -> None:
    client = FakeClient(
        prs=[
            pr_dc(
                1,
                milestone=3,
                assignees=("ann",),
                review_decision="APPROVED",
                latest_reviews=(Review("bob", "APPROVED", T1),),
                review_requests=("cy",),
                first_review_at=T1,
                files=("a.py", "b/c.py"),
                additions=5,
                deletions=2,
                changed_files=2,
                comments=(Comment("ann", "why this", T1),),
                comments_total=1,
                review_summaries=(Review("bob", "APPROVED", T1, body="ship it"),),
            )
        ],
        issues=[
            issue_dc(
                2,
                milestone=4,
                assignees=("ann",),
                state_reason="COMPLETED",
                comments=(Comment("cy", "repro steps", T1),),
                comments_total=3,
            )
        ],
    )
    crawl(client)
    with get_session() as session:
        pr, issue = session.get(PullRequest, 1), session.get(Issue, 2)
        assert (pr.milestone, pr.review_decision, pr.first_review_at) == (
            3,
            "APPROVED",
            T1,
        )
        assert json.loads(pr.assignees) == ["ann"]
        assert json.loads(pr.review_requests) == ["cy"]
        assert json.loads(pr.files) == ["a.py", "b/c.py"]
        assert json.loads(pr.latest_reviews) == [
            {"author": "bob", "state": "APPROVED", "submitted_at": T1}
        ]
        assert (pr.additions, pr.deletions, pr.changed_files) == (5, 2, 2)
        assert json.loads(pr.comments) == [
            {"author": "ann", "body": "why this", "created_at": T1}
        ]
        assert json.loads(pr.review_summaries) == [
            {
                "author": "bob",
                "state": "APPROVED",
                "body": "ship it",
                "submitted_at": T1,
            }
        ]
        assert (pr.comments_total, pr.review_threads_total) == (1, 0)
        assert (json.loads(pr.review_threads), pr.review_threads_synced) == ([], 1)
        assert (issue.milestone, issue.state_reason) == (4, "COMPLETED")
        assert json.loads(issue.assignees) == ["ann"]
        assert json.loads(issue.comments) == [
            {"author": "cy", "body": "repro steps", "created_at": T1}
        ]
        assert issue.comments_total == 3


def test_old_fields_version_backfills_once_past_the_updated_at_skip(
    whygraph_db_initialized: Path,
) -> None:
    client = fresh_client()
    crawl(client)
    with get_session() as session:
        for row in session.exec(select(ForgeCrawlState)).all():
            row.fields_version = 0
            session.add(row)
    client.pull_requests.prs = [
        pr_dc(p.number, p.updated_at, files=("x.py",)) for p in client.pull_requests.prs
    ]
    client.pull_requests.len_calls = 0

    crawl(client)

    assert client.pull_requests.len_calls == 1  # a full walk, not an incremental
    assert client.pull_requests.since_marks == []
    with get_session() as session:
        assert json.loads(session.get(PullRequest, 5).files) == ["x.py"]
    assert {s.fields_version for s in states().values()} == {
        github_crawler.FORGE_FIELDS_VERSION
    }

    client.pull_requests.len_calls = 0
    crawl(client)  # now current: incremental again
    assert client.pull_requests.len_calls == 0
    assert client.pull_requests.since_marks


def ms_dc(number: int, **kw) -> MilestoneDC:
    base = dict(
        number=number,
        title=f"M{number}",
        description=None,
        state="open",
        due_on=None,
        closed_at=None,
        updated_at=T0,
        progress_pct=50.0,
        html_url=f"https://example.test/milestone/{number}",
    )
    base.update(kw)
    return MilestoneDC(**base)


def test_milestones_are_relisted_and_absent_ones_deleted(
    whygraph_db_initialized: Path,
) -> None:
    client = FakeClient(milestones=[ms_dc(1), ms_dc(2, state="closed")])
    c = crawl(client)
    assert c.error is None
    assert c.summary == "0 PRs · 0 issues · 2 milestones"
    with get_session() as session:
        rows = {m.number: m.model_dump() for m in session.exec(select(Milestone)).all()}
    assert set(rows) == {1, 2}
    assert rows[1]["progress_pct"] == 50.0 and rows[2]["state"] == "closed"
    assert states()["milestone"].repo == "acme/widgets"

    client.milestones.milestones = [ms_dc(2, title="renamed", progress_pct=100.0)]
    c = crawl(client)
    assert c.error is None
    assert client.milestones.iter_calls == 2  # a full re-list each crawl
    with get_session() as session:
        rows = {m.number: m.model_dump() for m in session.exec(select(Milestone)).all()}
    assert set(rows) == {2}
    assert rows[2]["title"] == "renamed" and rows[2]["progress_pct"] == 100.0

    client.milestones.milestones = []
    crawl(client)
    with get_session() as session:
        assert session.exec(select(Milestone)).all() == []


# ---------------------------------------------------------------------------
# Plan 62: forge fields version 2, ingest rules (R19), the thread pass
# ---------------------------------------------------------------------------


def cm(author: str, body: str = "a human reply", **kw) -> Comment:
    return Comment(author, body, T1, **kw)


def thread(*comments: Comment, path: str = "a.py", anchor=None, total=None):
    return ReviewThread(
        path=path,
        original_line=7,
        resolved=False,
        outdated=False,
        anchor=anchor,
        comments=tuple(comments),
        comments_total=len(comments) if total is None else total,
    )


def pr_row(number: int) -> PullRequest:
    with get_session() as session:
        return PullRequest(**session.get(PullRequest, number).model_dump())


def test_v1_state_forces_full_walk_and_rewrite(whygraph_db_initialized: Path) -> None:
    client = fresh_client()
    crawl(client)
    with get_session() as session:
        for row in session.exec(select(ForgeCrawlState)).all():
            row.fields_version = 1
            session.add(row)
    client.pull_requests.prs = [
        pr_dc(
            p.number,
            p.updated_at,
            comments=(cm("ann"), cm("codecov", "coverage 90%", bot=True)),
        )
        for p in client.pull_requests.prs
    ]
    client.issues.issues = [
        issue_dc(i.number, i.updated_at, comments=(cm("cy"),), comments_total=1)
        for i in client.issues.issues
    ]
    client.pull_requests.len_calls = client.issues.len_calls = 0

    c = crawl(client)

    assert c.error is None
    assert client.pull_requests.len_calls == client.issues.len_calls == 1
    assert client.pull_requests.since_marks == client.issues.since_marks == []
    for n in (1, 5, 9):
        assert [x["author"] for x in json.loads(pr_row(n).comments)] == ["ann"]
    with get_session() as session:
        assert json.loads(session.get(Issue, 3).comments)[0]["author"] == "cy"
    assert {s.fields_version for s in states().values()} == {2}

    client.pull_requests.len_calls = 0
    crawl(client)  # v2: incremental again
    assert client.pull_requests.len_calls == 0
    assert client.pull_requests.since_marks == [T2]


def test_ingest_rules_conversation_comments_and_summaries(
    whygraph_db_initialized: Path,
) -> None:
    client = FakeClient(
        prs=[
            pr_dc(
                4,
                author="dependabot",  # a bot-authored PR keeps its row
                comments=(
                    cm("ann", "human"),
                    cm("dependabot", "@dependabot rebase", bot=True),
                    cm("bob", "hidden", minimized=True),
                    cm("cy", "human too"),
                ),
                comments_total=7,
                review_summaries=(
                    Review("bob", "APPROVED", T1, body="lgtm"),
                    Review(
                        "coderabbitai", "COMMENTED", T1, body="walkthrough", bot=True
                    ),
                    Review("cy", "COMMENTED", T1, body="spam", minimized=True),
                ),
            )
        ],
        issues=[
            issue_dc(
                8,
                comments=(
                    cm("ann", "same here"),
                    cm("github-actions", "stale", bot=True),
                    cm("bob", "off-topic", minimized=True),
                ),
                comments_total=3,
            )
        ],
    )
    c = crawl(client)

    assert c.error is None
    row = pr_row(4)
    assert row.author == "dependabot"
    assert json.loads(row.comments) == [
        {"author": "ann", "body": "human", "created_at": T1},
        {"author": "cy", "body": "human too", "created_at": T1},
    ]
    assert row.comments_total == 7
    assert json.loads(row.review_summaries) == [
        {"author": "bob", "state": "APPROVED", "body": "lgtm", "submitted_at": T1}
    ]
    with get_session() as session:
        issue = session.get(Issue, 8)
        assert json.loads(issue.comments) == [
            {"author": "ann", "body": "same here", "created_at": T1}
        ]
        assert issue.comments_total == 3


def test_ingest_rules_review_threads(whygraph_db_initialized: Path) -> None:
    long = "x" * 1000
    anchor = Anchor(
        lines=("if attempts > MAX:", "raise RetryError(attempts)"), added=True
    )
    client = FakeClient(prs=[pr_dc(3, review_threads_total=6)])
    client.pull_requests.threads[3] = (
        # human opener; the bot reply is dropped, flags only on the opener
        thread(
            cm("ann", "why 3?"),
            cm("copilot", "suggestion", bot=True),
            cm("dev", "rate limit"),
            anchor=anchor,
            total=14,
        ),
        # bot opener a human answered: kept, flagged, clipped
        thread(cm("copilot", long, bot=True), cm("dev", "not realistic, because")),
        # bot-only thread: dropped
        thread(cm("copilot", "nit", bot=True), cm("codecov", "ok", bot=True)),
        # minimized human opener with a human reply: kept, flagged, clipped
        thread(cm("bob", long, minimized=True), cm("dev", "answered")),
        # no human, non-minimized comment: dropped
        thread(cm("bob", "hidden", minimized=True), cm("copilot", "x", bot=True)),
        # the only human comment beyond the first 10 fetched: dropped (accepted)
        thread(cm("copilot", "first", bot=True), total=12),
    )

    c = crawl(client)

    assert c.error is None
    assert c.summary.endswith(" · threads: 1 PRs synced")
    row = pr_row(3)
    assert row.review_threads_synced == 1
    stored = json.loads(row.review_threads)
    assert len(stored) == 3
    first, bot_opened, hidden_opened = stored
    assert first == {
        "path": "a.py",
        "original_line": 7,
        "anchor": {
            "lines": ["if attempts > MAX:", "raise RetryError(attempts)"],
            "added": True,
        },
        "resolved": False,
        "outdated": False,
        "comments": [
            {
                "author": "ann",
                "body": "why 3?",
                "created_at": T1,
                "bot": False,
                "minimized": False,
            },
            {"author": "dev", "body": "rate limit", "created_at": T1},
        ],
        "comments_total": 14,
    }
    assert bot_opened["anchor"] is None
    assert bot_opened["comments"][0] == {
        "author": "copilot",
        "body": "x" * github_crawler.BOT_OPENER_STORE_CHARS,
        "created_at": T1,
        "bot": True,
        "minimized": False,
    }
    assert bot_opened["comments"][1] == {
        "author": "dev",
        "body": "not realistic, because",
        "created_at": T1,
    }
    assert hidden_opened["comments"][0]["minimized"] is True
    assert hidden_opened["comments"][0]["bot"] is False
    assert len(hidden_opened["comments"][0]["body"]) == 300
    assert (
        "diffHunk" not in row.review_threads and "diff_hunk" not in row.review_threads
    )


def test_upsert_keeps_stored_threads_and_resets_sync_only_with_threads(
    whygraph_db_initialized: Path,
) -> None:
    client = FakeClient(prs=[pr_dc(9, T1, review_threads_total=1), pr_dc(5, T0)])
    client.pull_requests.threads[9] = (thread(cm("dev", "because")),)
    crawl(client)
    assert pr_row(9).review_threads_synced == 1
    assert pr_row(5).review_threads_synced == 1  # no threads: nothing to fetch
    kept = pr_row(9).review_threads
    assert json.loads(kept)[0]["comments"][0]["body"] == "because"

    # PR 9 changes; the thread pass fails, so the upsert alone is visible
    client.pull_requests.prs = [pr_dc(9, T2, title="edited", review_threads_total=1)]
    client.pull_requests.thread_pass_kw = {"fail_after": 0}
    c = crawl(client)
    assert c.error is None
    assert c.summary.endswith(
        " · threads: 0 PRs synced · thread pass stopped: thread batch failed"
    )
    row = pr_row(9)
    assert (row.title, row.review_threads, row.review_threads_synced) == (
        "edited",
        kept,
        0,
    )

    # GitHub now reports no threads: cleared and synced, no fetch
    client.pull_requests.prs = [pr_dc(9, T3, review_threads_total=0)]
    client.pull_requests.thread_calls.clear()
    crawl(client)
    row = pr_row(9)
    assert (json.loads(row.review_threads), row.review_threads_synced) == ([], 1)
    assert client.pull_requests.thread_calls == []

    # a new row with threads starts unsynced
    client.pull_requests.prs = [pr_dc(11, T3, review_threads_total=2)]
    client.pull_requests.thread_pass_kw = {"stop_after": 0}
    c = crawl(client)
    assert c.summary.endswith(" · threads: 0 PRs synced · 1 PRs pending (rate limit)")
    assert (pr_row(11).review_threads, pr_row(11).review_threads_synced) == ("[]", 0)


def test_thread_pass_newest_first_resumes_after_a_stop(
    whygraph_db_initialized: Path,
) -> None:
    stamps = {n: f"2026-10-{10 + n:02d}T10:00:00Z" for n in range(1, 8)}
    client = FakeClient(
        prs=[pr_dc(n, stamps[n], review_threads_total=1) for n in (3, 7, 1, 5, 2, 6, 4)]
    )
    client.pull_requests.threads = {n: (thread(cm("dev", f"pr {n}")),) for n in stamps}
    client.pull_requests.thread_pass_kw = {"stop_after": 3}

    c = crawl(client)

    assert c.error is None
    assert client.pull_requests.thread_calls == [([7, 6, 5, 4, 3, 2, 1], 1000)]
    assert c.summary.endswith(" · threads: 3 PRs synced · 4 PRs pending (rate limit)")
    assert {n for n in stamps if pr_row(n).review_threads_synced} == {7, 6, 5}
    assert states()["pull_request"].fields_version == 2  # the main crawl is saved

    # next crawl: nothing re-listed, the pass resumes with the pending PRs only
    client.pull_requests.listing_override = []
    client.pull_requests.thread_pass_kw = {}
    c = crawl(client)
    assert client.pull_requests.thread_calls[1] == ([4, 3, 2, 1], 1000)
    assert c.summary.endswith(" · threads: 4 PRs synced")
    for n in stamps:
        row = pr_row(n)
        assert row.review_threads_synced == 1
        assert json.loads(row.review_threads)[0]["comments"][0]["body"] == f"pr {n}"

    client.pull_requests.listing_override = []
    c = crawl(client)  # all synced: no thread request, no suffix
    assert len(client.pull_requests.thread_calls) == 2
    assert "threads" not in c.summary


def test_thread_pass_writes_per_batch_with_no_session_open(
    whygraph_db_initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_crawler, "THREAD_BATCH", 2)
    db_path = whygraph_db_initialized
    client = FakeClient(
        prs=[
            pr_dc(n, f"2026-10-1{n}T10:00:00Z", review_threads_total=1)
            for n in (1, 2, 3, 4, 5)
        ]
    )
    seen: list[tuple[int, int, object]] = []

    def on_fetch(number: int) -> None:
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            synced = conn.execute(
                "SELECT COUNT(*) FROM pull_request WHERE review_threads_synced = 1"
            ).fetchone()[0]
            conn.execute("UPDATE tag SET subject = 'x' WHERE 0")  # takes the lock
            conn.commit()
            seen.append((number, synced, True))
        except sqlite3.OperationalError as exc:
            seen.append((number, -1, str(exc)))
        finally:
            conn.close()

    client.pull_requests.thread_pass_kw = {"fail_after": 3, "on_fetch": on_fetch}

    c = crawl(client)

    assert c.error is None  # a thread-pass error never fails the crawl
    # PRs 5, 4 are written together before 3 is fetched; 3 is flushed at the end
    assert seen == [(5, 0, True), (4, 0, True), (3, 2, True)]
    assert {n for n in range(1, 6) if pr_row(n).review_threads_synced} == {5, 4, 3}
    assert c.summary.endswith(
        " · threads: 3 PRs synced · thread pass stopped: thread batch failed"
    )


def test_thread_pass_not_found_and_skipped(whygraph_db_initialized: Path) -> None:
    client = FakeClient(prs=[pr_dc(n, T1, review_threads_total=2) for n in (1, 2, 3)])
    client.pull_requests.threads = {1: None, 2: (thread(cm("dev")),)}
    client.pull_requests.thread_pass_kw = {"skip": (3,)}

    c = crawl(client)

    assert c.error is None
    assert (pr_row(1).review_threads, pr_row(1).review_threads_synced) == ("[]", 1)
    assert pr_row(2).review_threads_synced == 1
    assert (pr_row(3).review_threads, pr_row(3).review_threads_synced) == ("[]", 0)
    assert c.summary.endswith(" · threads: 2 PRs synced")
