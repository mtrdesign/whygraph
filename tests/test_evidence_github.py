from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from whygraph.evidence.github import (
    GitHubEvidenceCollector,
    collect_github_evidence,
    detect_github_repo,
    parse_closing_refs,
    parse_github_repo,
    parse_hash_refs,
)
from whygraph.evidence.types import EvidenceRow


def test_parse_closing_refs_basic() -> None:
    assert parse_closing_refs("closes #1, fix #2, resolved: #3") == [1, 2, 3]


def test_parse_closing_refs_case_insensitive() -> None:
    assert parse_closing_refs("CLOSES #10 Fixes #20 RESOLVE #30") == [10, 20, 30]


def test_parse_closing_refs_dedupes() -> None:
    assert parse_closing_refs("closes #5 and resolves #5 too") == [5]


def test_parse_closing_refs_handles_punctuation_variants() -> None:
    # Verbs: close/closes/closed, fix/fixes/fixed, resolve/resolves/resolved.
    assert parse_closing_refs("Closed: #1\nFixed #2\nResolved: #3") == [1, 2, 3]


def test_parse_closing_refs_ignores_unrelated_hash_refs() -> None:
    # bare #99 (not preceded by a closing keyword) → not matched.
    assert parse_closing_refs("see #99 for context") == []


def test_parse_closing_refs_preserves_first_seen_order() -> None:
    assert parse_closing_refs("fix #3, closes #1, resolves #2") == [3, 1, 2]


def test_parse_hash_refs_squash_merge_subject() -> None:
    assert parse_hash_refs("feat: thing (#589)") == [589]


def test_parse_hash_refs_handles_leading_position() -> None:
    assert parse_hash_refs("#1 was the first") == [1]


def test_parse_hash_refs_skips_inside_words() -> None:
    # `abc#42` should NOT match because `#` isn't preceded by space/paren.
    assert parse_hash_refs("see abc#42 example") == []


def test_parse_hash_refs_dedupes() -> None:
    assert parse_hash_refs("(#7) and #7 again") == [7]


def test_parse_github_repo_ssh() -> None:
    assert parse_github_repo("git@github.com:cvetty/whygraph.git") == "cvetty/whygraph"


def test_parse_github_repo_ssh_no_dot_git() -> None:
    assert parse_github_repo("git@github.com:cvetty/whygraph") == "cvetty/whygraph"


def test_parse_github_repo_https() -> None:
    assert parse_github_repo("https://github.com/cvetty/whygraph.git") == "cvetty/whygraph"


def test_parse_github_repo_http_redirects_supported() -> None:
    assert parse_github_repo("http://github.com/cvetty/whygraph") == "cvetty/whygraph"


def test_parse_github_repo_returns_none_for_gitlab() -> None:
    assert parse_github_repo("git@gitlab.com:o/r.git") is None
    assert parse_github_repo("https://gitlab.com/o/r.git") is None


def test_parse_github_repo_handles_trailing_whitespace() -> None:
    assert parse_github_repo("git@github.com:o/r.git\n") == "o/r"


def test_detect_github_repo_returns_none_for_non_repo(tmp_path: Path) -> None:
    assert detect_github_repo(tmp_path) is None


def test_detect_github_repo_reads_remote_origin(init_git_repo) -> None:
    repo = init_git_repo()
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:owner/proj.git"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    assert detect_github_repo(repo) == "owner/proj"


# ---------------------------------------------------------------------------
# GitHubEvidenceCollector — exercise via monkeypatched _gh.
# ---------------------------------------------------------------------------


def _make_collector(repo: str = "owner/proj") -> GitHubEvidenceCollector:
    """Build a collector whose availability is forced on, regardless of `gh` PATH."""
    c = GitHubEvidenceCollector.__new__(GitHubEvidenceCollector)
    c.repo_root = Path("/tmp/fake")
    c.repo = repo
    c._available = True
    c._prs_by_commit = {}
    c._pr_details = {}
    c._issue_details = {}
    return c


def test_collector_unavailable_returns_empty_lookups(tmp_path: Path) -> None:
    # No remote → not available.
    c = GitHubEvidenceCollector(tmp_path)
    assert c.is_available() is False
    assert c.pr_numbers_for_commit("abc") == []
    assert c.pr(1) is None
    assert c.issue(1) is None


def test_pr_numbers_for_commit_prefers_merged_prs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _make_collector()
    monkeypatch.setattr(
        c,
        "_gh",
        lambda args: json.dumps(
            [
                {"number": 1, "merged_at": None},
                {"number": 2, "merged_at": "2026-01-01T00:00:00Z"},
                {"number": 3, "merged_at": "2026-01-02T00:00:00Z"},
            ]
        ),
    )
    assert c.pr_numbers_for_commit("abc") == [2, 3]


def test_pr_numbers_for_commit_falls_back_to_all_when_none_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _make_collector()
    monkeypatch.setattr(
        c,
        "_gh",
        lambda args: json.dumps(
            [
                {"number": 10, "merged_at": None},
                {"number": 11, "merged_at": None},
            ]
        ),
    )
    assert c.pr_numbers_for_commit("abc") == [10, 11]


def test_pr_numbers_for_commit_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _make_collector()
    calls = {"n": 0}

    def fake(args):
        calls["n"] += 1
        return json.dumps([{"number": 99, "merged_at": "2026-01-01T00:00:00Z"}])

    monkeypatch.setattr(c, "_gh", fake)
    c.pr_numbers_for_commit("abc")
    c.pr_numbers_for_commit("abc")
    assert calls["n"] == 1


def test_pr_numbers_for_commit_handles_gh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _make_collector()
    monkeypatch.setattr(c, "_gh", lambda args: None)
    assert c.pr_numbers_for_commit("abc") == []


def test_pr_view_parses_full_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _make_collector()
    monkeypatch.setattr(
        c,
        "_gh",
        lambda args: json.dumps(
            {
                "number": 42,
                "title": "Compliance fix",
                "body": "Closes #100 and fixes #101.",
                "state": "MERGED",
                "mergedAt": "2026-03-12T10:00:00Z",
                "createdAt": "2026-03-10T09:00:00Z",
                "author": {"login": "alice"},
                "url": "https://github.com/owner/proj/pull/42",
            }
        ),
    )
    pr = c.pr(42)
    assert pr is not None
    assert pr.number == 42
    assert pr.title == "Compliance fix"
    assert pr.merged is True
    assert pr.merged_at == "2026-03-12T10:00:00Z"
    assert pr.author == "alice"
    assert pr.closes_issues == [100, 101]


def test_pr_view_caches_negative_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _make_collector()
    calls = {"n": 0}

    def fake(args):
        calls["n"] += 1
        return None

    monkeypatch.setattr(c, "_gh", fake)
    assert c.pr(7) is None
    assert c.pr(7) is None  # served from cache
    assert calls["n"] == 1


def test_pr_view_handles_unparseable_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _make_collector()
    monkeypatch.setattr(c, "_gh", lambda args: "not json")
    assert c.pr(1) is None


def test_pr_view_handles_missing_optional_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _make_collector()
    monkeypatch.setattr(
        c,
        "_gh",
        lambda args: json.dumps(
            {"number": 1, "title": None, "state": "OPEN", "author": None}
        ),
    )
    pr = c.pr(1)
    assert pr is not None
    assert pr.title == ""
    assert pr.author == ""
    assert pr.merged is False
    assert pr.closes_issues == []


def test_issue_view_parses_full_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _make_collector()
    monkeypatch.setattr(
        c,
        "_gh",
        lambda args: json.dumps(
            {
                "number": 802,
                "title": "Legal review needed",
                "body": "Token storage compliance gap.",
                "state": "CLOSED",
                "createdAt": "2026-02-01T00:00:00Z",
                "closedAt": "2026-03-12T10:00:00Z",
                "author": {"login": "bob"},
                "url": "https://github.com/owner/proj/issues/802",
                "labels": [{"name": "compliance"}, {"name": "legal"}],
            }
        ),
    )
    issue = c.issue(802)
    assert issue is not None
    assert issue.number == 802
    assert issue.labels == ["compliance", "legal"]
    assert issue.author == "bob"


def test_collect_github_evidence_returns_empty_when_unavailable(
    tmp_path: Path,
) -> None:
    c = GitHubEvidenceCollector(tmp_path)
    assert collect_github_evidence(c, []) == []


def test_collect_github_evidence_pulls_prs_via_commit_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _make_collector()

    pr_payload = {
        "number": 5,
        "title": "feat",
        "body": "closes #999",
        "state": "MERGED",
        "mergedAt": "2026-01-01T00:00:00Z",
        "createdAt": None,
        "author": {"login": "x"},
        "url": "u",
    }
    issue_payload = {
        "number": 999,
        "title": "bug",
        "body": "",
        "state": "CLOSED",
        "createdAt": None,
        "closedAt": None,
        "author": {"login": "y"},
        "url": "u",
        "labels": [],
    }

    def fake_gh(args):
        if args[0] == "api":  # /repos/.../commits/SHA/pulls
            return json.dumps([{"number": 5, "merged_at": "2026-01-01T00:00:00Z"}])
        if args[0] == "pr":
            return json.dumps(pr_payload)
        if args[0] == "issue":
            return json.dumps(issue_payload)
        return None

    monkeypatch.setattr(c, "_gh", fake_gh)
    git_rows = [
        EvidenceRow(
            source="git_commit",
            ref="abc123",
            payload={"subject": "subject line", "body": ""},
        )
    ]
    rows = collect_github_evidence(c, git_rows)
    sources = [r.source for r in rows]
    assert "pr" in sources
    assert "issue" in sources
    pr_row = next(r for r in rows if r.source == "pr")
    assert pr_row.ref == "5"
    issue_row = next(r for r in rows if r.source == "issue")
    assert issue_row.ref == "999"


def test_collect_github_evidence_squash_merge_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _make_collector()

    def fake_gh(args):
        if args[0] == "api":
            return json.dumps([])  # commit not associated with a PR (squash-merge case)
        if args[0] == "pr" and args[2] == "777":
            return json.dumps(
                {
                    "number": 777,
                    "title": "feat: thing",
                    "body": "",
                    "state": "MERGED",
                    "mergedAt": "2026-01-01T00:00:00Z",
                    "createdAt": None,
                    "author": {"login": "x"},
                    "url": "u",
                }
            )
        return None

    monkeypatch.setattr(c, "_gh", fake_gh)
    git_rows = [
        EvidenceRow(
            source="git_commit",
            ref="abc123",
            payload={"subject": "feat: thing (#777)", "body": ""},
        )
    ]
    rows = collect_github_evidence(c, git_rows)
    pr_rows = [r for r in rows if r.source == "pr"]
    assert len(pr_rows) == 1
    assert pr_rows[0].ref == "777"


def test_collect_github_evidence_skips_blame_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _make_collector()
    monkeypatch.setattr(c, "_gh", lambda args: json.dumps([]))
    git_rows = [
        EvidenceRow(source="git_blame", ref="abc", payload={"summary": "x"}),
    ]
    assert collect_github_evidence(c, git_rows) == []
