import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from whygraph import mcp_server
from whygraph.scan import authors as authors_module
from whygraph.scan.db import Database
from whygraph.scan.git import Commit
from whygraph.scan.github import PullRequest


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def velocity_db(tmp_path: Path, monkeypatch) -> Path:
    db_path = tmp_path / ".whygraph" / "whygraph.db"
    monkeypatch.setenv("WHYGRAPH_DB", str(db_path))
    now = datetime.now(tz=timezone.utc)
    in_window = (now - timedelta(days=10)).isoformat()
    out_of_window = (now - timedelta(days=200)).isoformat()
    with Database(db_path) as db:
        db.upsert_commit(
            Commit(
                sha="a" * 40,
                parent_shas=[],
                author_name="Alice",
                author_email="alice@example.com",
                authored_at=in_window,
                committed_at=in_window,
                subject="x",
                body="",
                files_changed=3,
                insertions=1,
                deletions=0,
            )
        )
        db.upsert_commit(
            Commit(
                sha="b" * 40,
                parent_shas=[],
                author_name="Alice",
                author_email="alice@example.com",
                authored_at=in_window,
                committed_at=in_window,
                subject="y",
                body="",
                files_changed=1,
                insertions=1,
                deletions=0,
            )
        )
        db.upsert_commit(
            Commit(
                sha="c" * 40,
                parent_shas=[],
                author_name="Bob",
                author_email="bob@example.com",
                authored_at=out_of_window,
                committed_at=out_of_window,
                subject="old",
                body="",
                files_changed=10,
                insertions=1,
                deletions=0,
            )
        )
    return db_path


def test_velocity_summary_author_mode(velocity_db: Path) -> None:
    out = mcp_server.whygraph_velocity_summary(window_days=30, top_n=10)
    assert out[0]["author_email"] == "alice@example.com"
    assert out[0]["window_commits"] == 2
    bob_row = next(r for r in out if r["author_email"] == "bob@example.com")
    assert bob_row["window_commits"] == 0
    assert bob_row["all_time_commits"] == 1


def test_velocity_summary_rejects_unknown_group_by(velocity_db: Path) -> None:
    with pytest.raises(mcp_server.WhyGraphError, match="unknown group_by"):
        mcp_server.whygraph_velocity_summary(group_by="bogus")


def test_velocity_summary_pr_join_uses_authors_table(
    tmp_path: Path, monkeypatch
) -> None:
    """When the commit email's localpart differs from the PR author's
    GitHub login, the new authors-backed join attributes the PR
    correctly (the old localpart heuristic would have missed it)."""
    db_path = tmp_path / ".whygraph" / "whygraph.db"
    monkeypatch.setenv("WHYGRAPH_DB", str(db_path))
    now = datetime.now(tz=timezone.utc)
    in_window = (now - timedelta(days=5)).isoformat()
    with Database(db_path) as db:
        db.upsert_commit(
            Commit(
                sha="a" * 40,
                parent_shas=[],
                author_name="Alice Example",
                author_email="alice.example@anthropic.com",
                authored_at=in_window,
                committed_at=in_window,
                subject="x",
                body="",
                files_changed=1,
                insertions=1,
                deletions=0,
            )
        )
        # PR opener login is "aliceX-gh" — wholly disjoint from the
        # commit's email localpart ("alice.example"). The link must come
        # via the authors table.
        db.upsert_pull_request(
            PullRequest(
                number=1,
                title="t",
                body="b",
                state="closed",
                draft=False,
                created_at=in_window,
                updated_at=in_window,
                closed_at=None,
                merged_at=in_window,
                merge_commit_sha="a" * 40,
                head_sha="0" * 40,
                head_ref="feat",
                base_ref="main",
                author="aliceX-gh",
                html_url="https://github.com/o/r/pull/1",
                labels=[],
                commit_titles=[
                    {
                        "oid": "a" * 40,
                        "headline": "x",
                        "author_login": "aliceX-gh",
                        "author_name": "Alice Example",
                        "author_email": "alice.example@anthropic.com",
                    }
                ],
            )
        )
        authors_module.build_authors(db)
    out = mcp_server.whygraph_velocity_summary(window_days=30, top_n=10)
    alice = next(r for r in out if r["author_email"] == "alice.example@anthropic.com")
    # Old heuristic would have failed: localpart "alice.example" ≠ login "aliceX-gh".
    assert alice["window_prs_authored"] == 1


def test_velocity_summary_path_prefix_mode(tmp_path: Path, monkeypatch) -> None:
    """Set up a small git repo and confirm path_prefix walks the log."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WHYGRAPH_DB", str(tmp_path / ".whygraph" / "whygraph.db"))
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "alice@example.com")
    _git(tmp_path, "config", "user.name", "Alice")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "core.py").write_text("x\n")
    _git(tmp_path, "add", "src/core.py")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    out = mcp_server.whygraph_velocity_summary(
        window_days=30, group_by="path_prefix", top_n=5
    )
    prefixes = {row["path_prefix"] for row in out}
    assert any("src" in p for p in prefixes)
    assert all("file_touches" in row for row in out)
    assert all("distinct_commits" in row for row in out)
