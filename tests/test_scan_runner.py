import subprocess
from pathlib import Path
from unittest.mock import patch

from whygraph.scan import db as db_module
from whygraph.scan.github import Issue, PullRequest
from whygraph.scan.runner import run_scan


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "a.txt").write_text("hello\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "first")
    return tmp_path


def _fake_pr(
    number: int,
    merge_commit_sha: str | None = None,
    closing_issue_numbers: list[int] | None = None,
    commit_titles: list[dict] | None = None,
) -> PullRequest:
    return PullRequest(
        number=number,
        title=f"PR {number}",
        body=None,
        state="open",
        draft=False,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        closed_at=None,
        merged_at=None,
        merge_commit_sha=merge_commit_sha,
        head_sha="a" * 40,
        head_ref="branch",
        base_ref="main",
        author="alice",
        html_url=f"https://github.com/o/r/pull/{number}",
        labels=[],
        commit_titles=commit_titles or [],
        closing_issue_numbers=closing_issue_numbers or [],
    )


def _fake_issue(number: int) -> Issue:
    return Issue(
        number=number,
        title=f"Issue {number}",
        body=None,
        state="open",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        closed_at=None,
        author="alice",
        html_url=f"https://github.com/o/r/issues/{number}",
        labels=[],
    )


def test_run_scan_both_crawlers(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    sample_entry = {
        "oid": "abc1234",
        "headline": "sample",
        "author_login": "alice",
        "author_name": "Alice",
        "author_email": "alice@example.com",
    }
    pr = _fake_pr(
        1,
        commit_titles=[sample_entry],
        closing_issue_numbers=[7],
    )
    with (
        patch("whygraph.scan.github.detect_repo", return_value=("o", "r")),
        patch("whygraph.scan.github.check_auth"),
        patch("whygraph.scan.github.list_pull_requests", return_value=[pr]),
        patch(
            "whygraph.scan.github.list_issues",
            return_value=[_fake_issue(7), _fake_issue(8)],
        ),
    ):
        rc = run_scan(repo_root=root)
    assert rc == 0
    with db_module.Database(db_module.default_db_path(root)) as db:
        assert db.commit_count() == 1
        assert db.pull_request_count() == 1
        assert db.issue_count() == 2
        cur = db._conn.cursor()
        cur.execute("SELECT commit_titles FROM pull_requests WHERE number = 1")
        (raw,) = cur.fetchone()
        import json as _json

        assert _json.loads(raw) == [sample_entry]
        cur.execute(
            "SELECT issue_number, link_kind FROM pr_issue_links WHERE pr_number = 1"
        )
        assert cur.fetchall() == [(7, "closes")]


def test_run_scan_non_github_origin(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    # Fixture repo has no origin set, so detect_repo will return None.
    rc = run_scan(repo_root=root)
    assert rc == 0
    with db_module.Database(db_module.default_db_path(root)) as db:
        assert db.commit_count() == 1
        assert db.pull_request_count() == 0
        assert db.issue_count() == 0


def test_run_scan_populates_scores(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    # Add a second, content-rich commit so TF-IDF has a non-trivial corpus.
    (root / "b.txt").write_text("content\n")
    subprocess.run(
        ["git", "-C", str(root), "add", "b.txt"], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "commit",
            "-q",
            "-m",
            "implement Levenshtein memoization",
        ],
        check=True,
        capture_output=True,
    )
    rc = run_scan(repo_root=root)
    assert rc == 0
    with db_module.Database(db_module.default_db_path(root)) as db:
        cur = db._conn.cursor()
        cur.execute(
            "SELECT subject, subject_tfidf_score FROM commits ORDER BY subject_tfidf_score DESC"
        )
        rows = cur.fetchall()
    assert len(rows) == 2
    # Top-scoring subject is the distinctive one, not 'first'.
    assert "Levenshtein" in rows[0][0]
    assert rows[0][1] > 0


def test_run_scan_no_score_skips_scoring(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    rc = run_scan(repo_root=root, skip_score=True)
    assert rc == 0
    with db_module.Database(db_module.default_db_path(root)) as db:
        cur = db._conn.cursor()
        cur.execute("SELECT subject_tfidf_score, body_tfidf_score FROM commits")
        for row in cur.fetchall():
            assert row == (0, 0)


def test_run_scan_no_llm_description_skips_phase(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    rc = run_scan(repo_root=root, skip_llm_descriptions=True)
    assert rc == 0
    with db_module.Database(db_module.default_db_path(root)) as db:
        cur = db._conn.cursor()
        cur.execute(
            "SELECT llm_description, llm_description_model FROM commits"
        )
        for row in cur.fetchall():
            assert row == (None, None)
