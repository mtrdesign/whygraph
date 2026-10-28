"""Tests for :mod:`whygraph.scan.github_crawler` (incremental GitHub crawl)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from rich.progress import Progress
from sqlmodel import select

from whygraph.db import get_session
from whygraph.db.models import ForgeCrawlState, Issue, PRIssueLink, PullRequest
from whygraph.scan import github_crawler
from whygraph.scan.github_crawler import GitHubCrawler
from whygraph.services.github import GitHubError
from whygraph.services.github.issue import Issue as IssueDC
from whygraph.services.github.pull_request import Comment, Review
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


class FakePRs:
    def __init__(self, prs: list[PullRequestDC]) -> None:
        self.prs = prs  # newest-first
        self.len_calls = 0
        self.iter_calls = 0
        self.since_marks: list[str] = []
        self.by_numbers_calls: list[list[int]] = []
        self.listing_override: list[tuple[int, str]] | None = None
        self.on_by_numbers = None

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


class FakeClient:
    def __init__(self, prs=(), issues=(), owner="Acme", name="Widgets") -> None:
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
    assert c.summary == "3 PRs · 2 issues"
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
    assert client.pull_requests.by_numbers_calls == [[]]
    assert client.pull_requests.len_calls == client.issues.len_calls == 0
    assert c.summary == "0 new · 0 updated PRs · 0 new · 0 updated issues"
    after = snapshot()
    assert after["prs"] == before["prs"]
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
    assert sorted(client.pull_requests.by_numbers_calls[0]) == [5, 9]
    assert c.summary == "0 new · 2 updated PRs · 0 new · 0 updated issues"
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
    assert client.pull_requests.by_numbers_calls == [[5, 9]]


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

    # incremental listing
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

    assert c.summary == "0 new · 0 updated PRs · 0 new · 1 updated issues"
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
            )
        ],
        issues=[issue_dc(2, milestone=4, assignees=("ann",), state_reason="COMPLETED")],
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
        assert (issue.milestone, issue.state_reason) == (4, "COMPLETED")
        assert json.loads(issue.assignees) == ["ann"]


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
