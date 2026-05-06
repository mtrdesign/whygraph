import json
from pathlib import Path
from unittest.mock import patch

import pytest

from whygraph.scan.github import (
    GitHubError,
    _parse_issue_node,
    _parse_pr_node,
    check_auth,
    detect_repo,
    list_issues,
    list_pull_requests,
)


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_PR_NODE = {
    "number": 42,
    "title": "Add foo",
    "body": "Fixes #1",
    "state": "MERGED",
    "isDraft": False,
    "url": "https://github.com/o/r/pull/42",
    "createdAt": "2026-01-01T00:00:00Z",
    "updatedAt": "2026-01-02T00:00:00Z",
    "closedAt": "2026-01-02T00:00:00Z",
    "mergedAt": "2026-01-02T00:00:00Z",
    "mergeCommit": {"oid": "deadbeef" * 5},
    "headRefOid": "feedface" * 5,
    "headRefName": "feature/foo",
    "baseRefName": "main",
    "author": {"login": "alice"},
    "labels": {"nodes": [{"name": "bug"}, {"name": "priority"}]},
    "commits": {
        "nodes": [
            {
                "commit": {
                    "oid": "abc1234deadbeef",
                    "messageHeadline": "fix tests",
                    "author": {
                        "name": "Alice",
                        "email": "alice@example.com",
                        "user": {"login": "alice"},
                    },
                }
            },
            {
                "commit": {
                    "oid": "def5678cafebabe",
                    "messageHeadline": "implement",
                    "author": {
                        "name": "Bob",
                        "email": "bob@example.com",
                        "user": None,
                    },
                }
            },
        ]
    },
    "closingIssuesReferences": {"nodes": [{"number": 1}, {"number": 7}]},
    "comments": {
        "nodes": [
            {
                "author": {"login": "bob"},
                "body": "LGTM",
                "createdAt": "2026-01-02T10:00:00Z",
            },
            {
                "author": None,
                "body": "ghost says hi",
                "createdAt": "2026-01-02T11:00:00Z",
            },
        ]
    },
}


_ISSUE_NODE = {
    "number": 9,
    "title": "Bug report",
    "body": "It doesn't work",
    "state": "OPEN",
    "url": "https://github.com/o/r/issues/9",
    "createdAt": "2026-02-01T00:00:00Z",
    "updatedAt": "2026-02-02T00:00:00Z",
    "closedAt": None,
    "author": {"login": "bob"},
    "labels": {"nodes": [{"name": "bug"}]},
}


def _wrap_pr_page(nodes: list[dict], has_next: bool = False, end_cursor: str | None = None) -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequests": {
                        "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                        "nodes": nodes,
                    }
                }
            }
        }
    )


def _wrap_issue_page(nodes: list[dict], has_next: bool = False, end_cursor: str | None = None) -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "issues": {
                        "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                        "nodes": nodes,
                    }
                }
            }
        }
    )


def test_detect_repo_https(tmp_path: Path) -> None:
    with patch(
        "whygraph.scan.github.subprocess.run",
        return_value=_FakeResult(stdout="https://github.com/owner/repo\n"),
    ):
        assert detect_repo(tmp_path) == ("owner", "repo")


def test_detect_repo_https_dot_git(tmp_path: Path) -> None:
    with patch(
        "whygraph.scan.github.subprocess.run",
        return_value=_FakeResult(stdout="https://github.com/owner/repo.git\n"),
    ):
        assert detect_repo(tmp_path) == ("owner", "repo")


def test_detect_repo_ssh(tmp_path: Path) -> None:
    with patch(
        "whygraph.scan.github.subprocess.run",
        return_value=_FakeResult(stdout="git@github.com:owner/repo.git\n"),
    ):
        assert detect_repo(tmp_path) == ("owner", "repo")


def test_detect_repo_ssh_protocol(tmp_path: Path) -> None:
    with patch(
        "whygraph.scan.github.subprocess.run",
        return_value=_FakeResult(stdout="ssh://git@github.com/owner/repo.git\n"),
    ):
        assert detect_repo(tmp_path) == ("owner", "repo")


def test_detect_repo_non_github(tmp_path: Path) -> None:
    with patch(
        "whygraph.scan.github.subprocess.run",
        return_value=_FakeResult(stdout="https://gitlab.com/owner/repo.git\n"),
    ):
        assert detect_repo(tmp_path) is None


def test_detect_repo_no_origin(tmp_path: Path) -> None:
    with patch(
        "whygraph.scan.github.subprocess.run",
        return_value=_FakeResult(returncode=1),
    ):
        assert detect_repo(tmp_path) is None


def test_check_auth_ok() -> None:
    with patch(
        "whygraph.scan.github.subprocess.run",
        return_value=_FakeResult(),
    ):
        check_auth()


def test_check_auth_unauthenticated() -> None:
    with patch(
        "whygraph.scan.github.subprocess.run",
        return_value=_FakeResult(returncode=1, stderr="not logged in"),
    ):
        with pytest.raises(GitHubError, match="not authenticated"):
            check_auth()


def test_check_auth_gh_missing() -> None:
    with patch(
        "whygraph.scan.github.subprocess.run",
        side_effect=FileNotFoundError,
    ):
        with pytest.raises(GitHubError, match="not installed"):
            check_auth()


def test_parse_pr_node_full() -> None:
    pr = _parse_pr_node(_PR_NODE)
    assert pr.number == 42
    assert pr.title == "Add foo"
    assert pr.state == "closed"  # MERGED maps to closed
    assert pr.merged_at == "2026-01-02T00:00:00Z"
    assert pr.merge_commit_sha == "deadbeef" * 5
    assert pr.head_sha == "feedface" * 5
    assert pr.author == "alice"
    assert pr.labels == ["bug", "priority"]
    assert pr.commit_titles == [
        {
            "oid": "abc1234deadbeef",
            "headline": "fix tests",
            "author_login": "alice",
            "author_name": "Alice",
            "author_email": "alice@example.com",
        },
        {
            "oid": "def5678cafebabe",
            "headline": "implement",
            "author_login": None,
            "author_name": "Bob",
            "author_email": "bob@example.com",
        },
    ]
    assert pr.closing_issue_numbers == [1, 7]
    assert pr.comments == [
        {"author": "bob", "body": "LGTM", "created_at": "2026-01-02T10:00:00Z"},
        {
            "author": None,
            "body": "ghost says hi",
            "created_at": "2026-01-02T11:00:00Z",
        },
    ]


def test_parse_pr_node_handles_null_author_and_body() -> None:
    node = {**_PR_NODE, "author": None, "body": None}
    pr = _parse_pr_node(node)
    assert pr.author is None
    assert pr.body is None


def test_parse_pr_node_open_state() -> None:
    node = {**_PR_NODE, "state": "OPEN"}
    pr = _parse_pr_node(node)
    assert pr.state == "open"


def test_parse_issue_node_full() -> None:
    issue = _parse_issue_node(_ISSUE_NODE)
    assert issue.number == 9
    assert issue.state == "open"
    assert issue.author == "bob"
    assert issue.labels == ["bug"]


def test_list_pull_requests_single_page() -> None:
    with patch("whygraph.scan.github._run_gh", return_value=_wrap_pr_page([_PR_NODE])):
        prs = list_pull_requests("o", "r")
    assert len(prs) == 1
    assert prs[0].number == 42


def test_list_pull_requests_paginates() -> None:
    page_a = _wrap_pr_page([_PR_NODE], has_next=True, end_cursor="cur1")
    page_b = _wrap_pr_page([{**_PR_NODE, "number": 43}])
    with patch(
        "whygraph.scan.github._run_gh", side_effect=[page_a, page_b]
    ):
        prs = list_pull_requests("o", "r")
    assert [p.number for p in prs] == [42, 43]


def test_list_pull_requests_empty() -> None:
    with patch("whygraph.scan.github._run_gh", return_value=_wrap_pr_page([])):
        assert list_pull_requests("o", "r") == []


def test_list_issues_paginates() -> None:
    page_a = _wrap_issue_page([_ISSUE_NODE], has_next=True, end_cursor="cur1")
    page_b = _wrap_issue_page([{**_ISSUE_NODE, "number": 10, "state": "CLOSED"}])
    with patch(
        "whygraph.scan.github._run_gh", side_effect=[page_a, page_b]
    ):
        issues = list_issues("o", "r")
    assert len(issues) == 2
    assert issues[1].state == "closed"


def test_graphql_errors_raise() -> None:
    body = json.dumps({"errors": [{"message": "Resource not accessible"}]})
    with patch("whygraph.scan.github._run_gh", return_value=body):
        with pytest.raises(GitHubError, match="Resource not accessible"):
            list_pull_requests("o", "r")


def test_missing_repository_raises() -> None:
    body = json.dumps({"data": {}})
    with patch("whygraph.scan.github._run_gh", return_value=body):
        with pytest.raises(GitHubError, match="missing repository"):
            list_pull_requests("o", "r")
