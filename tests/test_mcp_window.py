"""End-to-end tests for `whygraph_window` and its `mcp_queries` helpers."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from whygraph import mcp_queries, mcp_server
from whygraph.scan import authors as authors_module
from whygraph.scan.db import Database
from whygraph.scan.git import Commit
from whygraph.scan.github import Issue, PullRequest


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(sha: str, *, when: str, email: str = "alice@example.com", name: str = "Alice") -> Commit:
    return Commit(
        sha=sha,
        parent_shas=[],
        author_name=name,
        author_email=email,
        authored_at=when,
        committed_at=when,
        subject=f"commit {sha[:6]}",
        body="",
        files_changed=1,
        insertions=1,
        deletions=0,
    )


def _pr(
    number: int,
    *,
    when: str,
    author: str = "alice",
    state: str = "closed",
    merged_at: str | None = None,
    labels: list[str] | None = None,
    title: str | None = None,
) -> PullRequest:
    return PullRequest(
        number=number,
        title=title or f"pr-{number}",
        body="b",
        state=state,
        draft=False,
        created_at=when,
        updated_at=when,
        closed_at=None,
        merged_at=merged_at,
        merge_commit_sha=None,
        head_sha="0" * 40,
        head_ref="feat",
        base_ref="main",
        author=author,
        html_url=f"https://github.com/o/r/pull/{number}",
        labels=labels or [],
        commit_titles=[],
    )


def _issue(
    number: int,
    *,
    when: str,
    author: str = "alice",
    labels: list[str] | None = None,
    state: str = "open",
) -> Issue:
    return Issue(
        number=number,
        title=f"issue-{number}",
        body="b",
        state=state,
        created_at=when,
        updated_at=when,
        closed_at=None,
        author=author,
        html_url=f"https://github.com/o/r/issues/{number}",
        labels=labels or [],
    )


@pytest.fixture
def window_db(tmp_path: Path, monkeypatch) -> tuple[Path, str, str]:
    """Seed a DB across two windows (in-window vs out-of-window) plus
    rows for path-prefix testing on a real git repo."""
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / ".whygraph" / "whygraph.db"
    monkeypatch.setenv("WHYGRAPH_DB", str(db_path))

    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "alice@example.com")
    _git(tmp_path, "config", "user.name", "Alice")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "core.py").write_text("x\n")
    _git(tmp_path, "add", "src/core.py")
    _git(tmp_path, "commit", "-q", "-m", "in-window src commit")
    in_sha = _git_out(tmp_path, "rev-parse", "HEAD")

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text("d\n")
    _git(tmp_path, "add", "docs/readme.md")
    _git(tmp_path, "commit", "-q", "-m", "in-window docs commit")
    docs_sha = _git_out(tmp_path, "rev-parse", "HEAD")

    now = datetime.now(tz=timezone.utc)
    in_window = (now - timedelta(days=5)).isoformat()
    out_of_window = (now - timedelta(days=200)).isoformat()

    with Database(db_path) as db:
        # Real git SHAs so path_prefix can find them.
        db.upsert_commit(
            Commit(
                sha=in_sha,
                parent_shas=[],
                author_name="Alice",
                author_email="alice@example.com",
                authored_at=in_window,
                committed_at=in_window,
                subject="in-window src commit",
                body="",
                files_changed=1,
                insertions=1,
                deletions=0,
            )
        )
        db.upsert_commit(
            Commit(
                sha=docs_sha,
                parent_shas=[],
                author_name="Alice",
                author_email="alice@example.com",
                authored_at=in_window,
                committed_at=in_window,
                subject="in-window docs commit",
                body="",
                files_changed=1,
                insertions=1,
                deletions=0,
            )
        )
        # An out-of-window commit (fake SHA — path_prefix won't see it).
        db.upsert_commit(_commit("c" * 40, when=out_of_window))

        # PRs/issues — mix of in- and out-of-window plus a closed-but-merged PR.
        db.upsert_pull_request(
            _pr(1, when=in_window, merged_at=in_window, labels=["bug"], title="fix nasty bug")
        )
        db.upsert_pull_request(
            _pr(2, when=in_window, state="open", title="ongoing work")
        )
        db.upsert_pull_request(_pr(3, when=out_of_window))
        db.upsert_issue(_issue(10, when=in_window, labels=["bug"]))
        db.upsert_issue(_issue(11, when=out_of_window))

        authors_module.build_authors(db)
    return db_path, in_sha, docs_sha


def test_parse_window_bound_relative_shorthand() -> None:
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert mcp_queries.parse_window_bound("30d", now=now) == now - timedelta(days=30)
    assert mcp_queries.parse_window_bound("4w", now=now) == now - timedelta(days=28)
    assert mcp_queries.parse_window_bound("3m", now=now) == now - timedelta(days=90)
    assert mcp_queries.parse_window_bound("1y", now=now) == now - timedelta(days=365)


def test_parse_window_bound_iso() -> None:
    out = mcp_queries.parse_window_bound("2026-01-01")
    assert out.year == 2026 and out.month == 1 and out.day == 1
    assert out.tzinfo is not None  # naïve treated as UTC


def test_parse_window_bound_now() -> None:
    fixed = datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert mcp_queries.parse_window_bound("now", now=fixed) == fixed


def test_parse_window_bound_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="parse"):
        mcp_queries.parse_window_bound("yesterday")


def test_window_filters_by_date_only(window_db) -> None:
    out = mcp_server.whygraph_window(since="30d")
    ids = {(r["kind"], r["id"]) for r in out}
    # Out-of-window items must not surface.
    assert ("pr", 3) not in ids
    assert ("issue", 11) not in ids
    # In-window items must surface.
    assert ("pr", 1) in ids
    assert ("issue", 10) in ids


def test_window_kind_selection(window_db) -> None:
    only_prs = mcp_server.whygraph_window(since="30d", kinds=["pr"])
    assert {r["kind"] for r in only_prs} == {"pr"}
    only_issues = mcp_server.whygraph_window(since="30d", kinds=["issue"])
    assert {r["kind"] for r in only_issues} == {"issue"}


def test_window_state_merged_filters_unmerged_prs(window_db) -> None:
    merged = mcp_server.whygraph_window(
        since="30d", kinds=["pr"], state="merged"
    )
    # PR 1 is merged in-window; PR 2 is open in-window — only 1 survives.
    assert {r["id"] for r in merged} == {1}


def test_window_label_filters_prs(window_db) -> None:
    out = mcp_server.whygraph_window(
        since="30d", kinds=["pr"], label="bug"
    )
    assert {r["id"] for r in out} == {1}


def test_window_label_filters_issues(window_db) -> None:
    out = mcp_server.whygraph_window(
        since="30d", kinds=["issue"], label="bug"
    )
    assert {r["id"] for r in out} == {10}


def test_window_path_prefix_filters_commits(window_db) -> None:
    db_path, in_sha, docs_sha = window_db
    src_only = mcp_server.whygraph_window(
        since="30d", kinds=["commit"], path_prefix="src/"
    )
    src_shas = {r["id"] for r in src_only}
    assert in_sha in src_shas
    assert docs_sha not in src_shas


def test_window_author_filter_resolves_through_authors_table(window_db) -> None:
    out = mcp_server.whygraph_window(
        since="30d", kinds=["pr"], author="alice"
    )
    # PR 1 (merged) and PR 2 (open) both authored by alice in-window.
    assert {r["id"] for r in out} == {1, 2}


def test_window_unknown_author_raises(window_db) -> None:
    with pytest.raises(mcp_server.WhyGraphError, match="did not resolve"):
        mcp_server.whygraph_window(since="30d", author="nobody-x")


def test_window_limit_caps(window_db) -> None:
    out = mcp_server.whygraph_window(since="30d", limit=2)
    assert len(out) <= 2


def test_window_rejects_inverted_range(window_db) -> None:
    with pytest.raises(mcp_server.WhyGraphError, match="after"):
        mcp_server.whygraph_window(since="2026-05-01", until="2026-01-01")


def test_window_rejects_unknown_kind(window_db) -> None:
    with pytest.raises(mcp_server.WhyGraphError, match="unknown kinds"):
        mcp_server.whygraph_window(since="30d", kinds=["bogus"])


def test_window_rejects_unknown_state(window_db) -> None:
    with pytest.raises(mcp_server.WhyGraphError, match="unknown state"):
        mcp_server.whygraph_window(since="30d", state="bogus")


def test_window_orders_newest_first(window_db) -> None:
    out = mcp_server.whygraph_window(since="30d")
    times = [r.get("at") for r in out if r.get("at")]
    assert times == sorted(times, reverse=True)
