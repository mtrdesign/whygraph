from pathlib import Path
from typing import Any

import pytest

from whygraph import mcp_server
from whygraph.scan.db import Database
from whygraph.scan.git import Commit
from whygraph.scan.github import Issue, PullRequest


@pytest.fixture
def whygraph_db(tmp_path: Path, monkeypatch) -> Path:
    db_path = tmp_path / ".whygraph" / "whygraph.db"
    monkeypatch.setenv("WHYGRAPH_DB", str(db_path))
    with Database(db_path) as db:
        db.upsert_commit(
            Commit(
                sha="a" * 40,
                parent_shas=["b" * 40],
                author_name="Alice",
                author_email="alice@example.com",
                authored_at="2026-04-01T00:00:00+00:00",
                committed_at="2026-04-01T00:00:00+00:00",
                subject="initial",
                body="explained body",
                files_changed=2,
                insertions=10,
                deletions=1,
            )
        )
        db.set_llm_description("a" * 40, "added foo bar in src/x.py", "haiku")
        db.upsert_pull_request(
            PullRequest(
                number=42,
                title="Add foo",
                body="rationale",
                state="closed",
                draft=False,
                created_at="2026-04-01T00:00:00Z",
                updated_at="2026-04-02T00:00:00Z",
                closed_at="2026-04-02T00:00:00Z",
                merged_at="2026-04-02T00:00:00Z",
                merge_commit_sha="a" * 40,
                head_sha="c" * 40,
                head_ref="feat",
                base_ref="main",
                author="alice",
                html_url="https://github.com/o/r/pull/42",
                labels=["bug"],
                commit_titles=[
                    {
                        "oid": "a" * 40,
                        "headline": "initial",
                        "author_login": "alice",
                        "author_name": "Alice",
                        "author_email": "alice@example.com",
                    }
                ],
                comments=[
                    {
                        "author": "bob",
                        "body": "LGTM",
                        "created_at": "2026-04-02T10:00:00Z",
                    }
                ],
            )
        )
        db.upsert_issue(
            Issue(
                number=7,
                title="Bug",
                body="broken",
                state="closed",
                created_at="2026-04-01T00:00:00Z",
                updated_at="2026-04-02T00:00:00Z",
                closed_at="2026-04-02T00:00:00Z",
                author="bob",
                html_url="https://github.com/o/r/issues/7",
                labels=["bug"],
            )
        )
        db.set_pr_closing_issues(42, [7])
    return db_path


def test_repo_overview_resource(whygraph_db: Path) -> None:
    out = mcp_server.repo_overview()
    assert out["commits"] == 1
    assert out["pull_requests"] == 1
    assert out["issues"] == 1
    assert out["llm_described_commits"] == 1


def test_commit_resource_includes_pr_and_issue(whygraph_db: Path) -> None:
    out = mcp_server.commit_resource("a" * 40)
    assert out["commit"]["sha"] == "a" * 40
    assert out["commit"]["llm_description"] == "added foo bar in src/x.py"
    assert out["commit"]["parent_shas"] == ["b" * 40]
    assert len(out["linked_prs"]) == 1
    pr = out["linked_prs"][0]
    assert pr["number"] == 42
    assert pr["labels"] == ["bug"]
    assert isinstance(pr["commit_titles"][0], dict)
    assert pr["commit_titles"][0]["author_login"] == "alice"
    assert pr["closing_issues"][0]["number"] == 7


def test_commit_resource_not_found(whygraph_db: Path) -> None:
    out = mcp_server.commit_resource("z" * 40)
    assert out == {"error": "not_found", "sha": "z" * 40}


def test_pr_resource_includes_closing_issues(whygraph_db: Path) -> None:
    out = mcp_server.pr_resource("42")
    assert out["pull_request"]["number"] == 42
    assert out["pull_request"]["labels"] == ["bug"]
    assert out["pull_request"]["comments"][0]["author"] == "bob"
    assert out["closing_issues"][0]["number"] == 7


def test_pr_resource_not_found(whygraph_db: Path) -> None:
    out = mcp_server.pr_resource("999")
    assert out == {"error": "not_found", "number": 999}


def test_issue_resource_includes_closing_prs(whygraph_db: Path) -> None:
    out = mcp_server.issue_resource("7")
    assert out["issue"]["number"] == 7
    assert out["issue"]["labels"] == ["bug"]
    assert out["closing_prs"][0]["number"] == 42


def test_issue_resource_not_found(whygraph_db: Path) -> None:
    out = mcp_server.issue_resource("999")
    assert out == {"error": "not_found", "number": 999}
