"""Tests for the WhyGraph MCP read-only resources.

Each test seeds an isolated WhyGraph DB via ``whygraph_db_initialized``
and exercises a single resource. Happy-path tests call the resource
functions directly (they're plain Python — same convention as the
evidence-tool tests) and one registration test reads through
``mcp.read_resource`` to confirm the URI binding, mime type, and
JSON-encoding round-trip work end to end.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import pytest

from whygraph.db import get_session
from whygraph.db.models import Commit, Issue, PRIssueLink, PullRequest
from whygraph.mcp.errors import WhyGraphError
from whygraph.mcp.resources import (
    _commit_resource,
    _issue_resource,
    _pr_resource,
    _repo_overview_resource,
)


# ---- row builders --------------------------------------------------------


def _db_commit(
    sha: str,
    *,
    subject: str = "a change",
    committed_at: str = "2026-01-01T00:00:00+00:00",
    authored_at: str | None = None,
    parent_shas: str = "",
    author_name: str = "Test User",
    author_email: str = "tester@example.com",
    body: str = "Some body.",
    llm_description: str | None = "Mechanical diff summary.",
    files_changed: int = 1,
    insertions: int = 1,
    deletions: int = 0,
    scanned_at: str = "2026-05-01T00:00:00+00:00",
    refactor_score: int = 0,
) -> Commit:
    row = Commit(
        sha=sha,
        parent_shas=parent_shas,
        author_name=author_name,
        author_email=author_email,
        authored_at=authored_at or committed_at,
        committed_at=committed_at,
        subject=subject,
        body=body,
        files_changed=files_changed,
        insertions=insertions,
        deletions=deletions,
        scanned_at=scanned_at,
        llm_description=llm_description,
    )
    row.refactor_score = refactor_score
    return row


def _db_pr(
    *,
    number: int,
    merge_commit_sha: str | None = None,
    commit_titles: str = "[]",
    comments: str = "[]",
    labels: str = '["enhancement"]',
    head_sha: str = "headsha",
    fetched_at: str = "2026-02-02T00:00:00+00:00",
    body: str | None = "PR body.",
) -> PullRequest:
    return PullRequest(
        number=number,
        title="A pull request",
        body=body,
        state="merged",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-02-01T00:00:00+00:00",
        merged_at="2026-02-01T00:00:00+00:00",
        merge_commit_sha=merge_commit_sha,
        head_sha=head_sha,
        base_ref="main",
        author="octocat",
        html_url=f"https://example.com/pr/{number}",
        labels=labels,
        fetched_at=fetched_at,
        commit_titles=commit_titles,
        comments=comments,
    )


def _db_issue(
    *,
    number: int,
    labels: str = '["bug"]',
    fetched_at: str = "2026-02-02T00:00:00+00:00",
) -> Issue:
    return Issue(
        number=number,
        title="An issue",
        body="Issue body.",
        state="closed",
        created_at="2025-12-01T00:00:00+00:00",
        updated_at="2026-02-01T00:00:00+00:00",
        author="reporter",
        html_url=f"https://example.com/issue/{number}",
        labels=labels,
        fetched_at=fetched_at,
    )


# ---- registration / discoverability --------------------------------------


def test_resources_registered() -> None:
    """The four resources are reachable through the live FastMCP server."""
    from whygraph.mcp.server import mcp

    concrete = asyncio.run(mcp.list_resources())
    templates = asyncio.run(mcp.list_resource_templates())

    assert {r.name for r in concrete} == {"whygraph_repo_overview"}
    assert {t.uriTemplate for t in templates} == {
        "whygraph://commit/{sha}",
        "whygraph://pr/{number}",
        "whygraph://issue/{number}",
    }


def test_repo_overview_round_trips_through_mcp(
    whygraph_db_initialized: Path,
) -> None:
    """Reading via ``mcp.read_resource`` returns JSON content with the
    declared mime type — confirms the full FastMCP pipeline is wired up."""
    from whygraph.mcp.server import mcp

    contents = list(asyncio.run(mcp.read_resource("whygraph://repo/overview")))
    assert len(contents) == 1
    assert contents[0].mime_type == "application/json"
    payload = json.loads(contents[0].content)
    assert payload["counts"]["commits"] == 0


# ---- commit resource -----------------------------------------------------


def test_commit_resource_via_merge_commit_sha(
    whygraph_db_initialized: Path,
) -> None:
    sha = "a" * 40
    with get_session() as session:
        session.add(_db_commit(sha))
        session.add(_db_pr(number=5, merge_commit_sha=sha))

    result = _commit_resource(sha)

    assert result["commit"]["sha"] == sha
    assert result["commit"]["refactor_score"] == 0
    assert [pr["number"] for pr in result["linked_prs"]] == [5]
    pr = result["linked_prs"][0]
    # Heavy JSON blobs must be omitted from nested PR views.
    assert "commit_titles" not in pr
    assert "comments" not in pr
    assert pr["labels"] == ["enhancement"]


def test_commit_resource_via_commit_titles_match(
    whygraph_db_initialized: Path,
) -> None:
    sha = "b" * 40
    commit_titles_blob = json.dumps([{"oid": sha, "messageHeadline": "x"}])
    with get_session() as session:
        session.add(_db_commit(sha))
        # No merge_commit_sha / head_sha match — only commit_titles ties them.
        session.add(_db_pr(number=7, commit_titles=commit_titles_blob))

    result = _commit_resource(sha)
    assert [pr["number"] for pr in result["linked_prs"]] == [7]


def test_commit_resource_not_found(whygraph_db_initialized: Path) -> None:
    assert _commit_resource("deadbeef") == {
        "error": "not_found",
        "sha": "deadbeef",
    }


# ---- pr resource ---------------------------------------------------------


def test_pr_resource_includes_full_blobs(
    whygraph_db_initialized: Path,
) -> None:
    """A direct PR read decodes ``commit_titles`` and ``comments`` as lists."""
    commit_titles = json.dumps(
        [{"oid": "c" * 40, "messageHeadline": "first"}]
    )
    comments = json.dumps([{"author": "alice", "body": "lgtm"}])
    with get_session() as session:
        session.add(
            _db_pr(number=42, commit_titles=commit_titles, comments=comments)
        )

    result = _pr_resource(42)
    pr = result["pull_request"]
    assert pr["number"] == 42
    assert pr["commit_titles"] == [{"oid": "c" * 40, "messageHeadline": "first"}]
    assert pr["comments"] == [{"author": "alice", "body": "lgtm"}]
    assert result["closing_issues"] == []


def test_pr_resource_closing_issues(whygraph_db_initialized: Path) -> None:
    with get_session() as session:
        session.add(_db_pr(number=11))
        session.add(_db_issue(number=99))
        session.add(_db_issue(number=100))
        session.add(PRIssueLink(pr_number=11, issue_number=99, link_kind="closes"))
        session.add(PRIssueLink(pr_number=11, issue_number=100, link_kind="closes"))

    result = _pr_resource(11)
    assert [issue["number"] for issue in result["closing_issues"]] == [99, 100]
    assert result["closing_issues"][0]["labels"] == ["bug"]


def test_pr_resource_link_kind_other_than_closes_is_ignored(
    whygraph_db_initialized: Path,
) -> None:
    with get_session() as session:
        session.add(_db_pr(number=12))
        session.add(_db_issue(number=200))
        session.add(
            PRIssueLink(pr_number=12, issue_number=200, link_kind="mentions")
        )

    result = _pr_resource(12)
    assert result["closing_issues"] == []


def test_pr_resource_not_found(whygraph_db_initialized: Path) -> None:
    assert _pr_resource(404) == {"error": "not_found", "number": 404}


# ---- issue resource ------------------------------------------------------


def test_issue_resource_closing_prs(whygraph_db_initialized: Path) -> None:
    with get_session() as session:
        session.add(_db_issue(number=55))
        session.add(_db_pr(number=21))
        session.add(_db_pr(number=22))
        session.add(PRIssueLink(pr_number=21, issue_number=55, link_kind="closes"))
        session.add(PRIssueLink(pr_number=22, issue_number=55, link_kind="closes"))

    result = _issue_resource(55)
    assert result["issue"]["number"] == 55
    assert [pr["number"] for pr in result["closing_prs"]] == [21, 22]
    # Nested PR views drop the heavy blobs.
    assert "commit_titles" not in result["closing_prs"][0]
    assert "comments" not in result["closing_prs"][0]


def test_issue_resource_not_found(whygraph_db_initialized: Path) -> None:
    assert _issue_resource(404) == {"error": "not_found", "number": 404}


# ---- repo overview -------------------------------------------------------


def test_repo_overview_empty_db(whygraph_db_initialized: Path) -> None:
    result = _repo_overview_resource()
    assert result["counts"] == {
        "commits": 0,
        "pull_requests": 0,
        "issues": 0,
        "pr_issue_links": 0,
    }
    assert result["commit_date_range"] == {
        "earliest_authored_at": None,
        "latest_authored_at": None,
    }
    assert result["scan_freshness"] == {
        "latest_scanned_at": None,
        "latest_pr_fetched_at": None,
        "latest_issue_fetched_at": None,
    }
    assert result["llm_description_coverage"] == {
        "total_commits": 0,
        "described": 0,
        "fraction": 0.0,
    }
    assert result["top_contributors"] == []


def test_repo_overview_populated(whygraph_db_initialized: Path) -> None:
    with get_session() as session:
        session.add(
            _db_commit(
                "a" * 40,
                authored_at="2026-01-01T00:00:00+00:00",
                committed_at="2026-01-01T00:00:00+00:00",
                scanned_at="2026-05-26T00:00:00+00:00",
                author_name="Alice",
                author_email="alice@example.com",
            )
        )
        session.add(
            _db_commit(
                "b" * 40,
                authored_at="2026-02-01T00:00:00+00:00",
                committed_at="2026-02-01T00:00:00+00:00",
                scanned_at="2026-05-26T00:00:00+00:00",
                author_name="Alice",
                author_email="alice@example.com",
                llm_description=None,
            )
        )
        session.add(
            _db_commit(
                "c" * 40,
                authored_at="2026-03-01T00:00:00+00:00",
                committed_at="2026-03-01T00:00:00+00:00",
                scanned_at="2026-05-26T00:00:00+00:00",
                author_name="Bob",
                author_email="bob@example.com",
            )
        )
        session.add(
            _db_pr(
                number=1,
                merge_commit_sha="c" * 40,
                fetched_at="2026-05-25T00:00:00+00:00",
            )
        )
        session.add(_db_issue(number=1, fetched_at="2026-05-24T00:00:00+00:00"))
        session.add(PRIssueLink(pr_number=1, issue_number=1, link_kind="closes"))

    result = _repo_overview_resource()

    assert result["counts"] == {
        "commits": 3,
        "pull_requests": 1,
        "issues": 1,
        "pr_issue_links": 1,
    }
    assert result["commit_date_range"] == {
        "earliest_authored_at": "2026-01-01T00:00:00+00:00",
        "latest_authored_at": "2026-03-01T00:00:00+00:00",
    }
    assert result["scan_freshness"] == {
        "latest_scanned_at": "2026-05-26T00:00:00+00:00",
        "latest_pr_fetched_at": "2026-05-25T00:00:00+00:00",
        "latest_issue_fetched_at": "2026-05-24T00:00:00+00:00",
    }
    assert result["llm_description_coverage"]["total_commits"] == 3
    assert result["llm_description_coverage"]["described"] == 2
    assert math.isclose(result["llm_description_coverage"]["fraction"], 2 / 3)
    # Alice has 2 commits and ranks first; Bob has 1.
    assert result["top_contributors"] == [
        {
            "author_name": "Alice",
            "author_email": "alice@example.com",
            "commit_count": 2,
        },
        {
            "author_name": "Bob",
            "author_email": "bob@example.com",
            "commit_count": 1,
        },
    ]


def test_resource_errors_on_uninitialized_db(whygraph_db: Path) -> None:
    """Every resource raises ``WhyGraphError`` when the DB hasn't been migrated.

    Uses ``whygraph_db`` (not ``whygraph_db_initialized``) so the tables
    don't exist — SQLAlchemy will raise ``OperationalError``, which the
    resource bodies translate into the actionable WhyGraph error.
    """
    with pytest.raises(WhyGraphError, match="whygraph scan"):
        _repo_overview_resource()
    with pytest.raises(WhyGraphError, match="whygraph scan"):
        _commit_resource("a" * 40)
    with pytest.raises(WhyGraphError, match="whygraph scan"):
        _pr_resource(1)
    with pytest.raises(WhyGraphError, match="whygraph scan"):
        _issue_resource(1)
