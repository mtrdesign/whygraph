from pathlib import Path
from typing import Any

from whygraph.scan.db import Database, default_db_path
from whygraph.scan.git import Commit
from whygraph.scan.github import Issue, PullRequest


def _sample_commit(sha: str = "a" * 40, parents: list[str] | None = None) -> Commit:
    return Commit(
        sha=sha,
        parent_shas=parents or [],
        author_name="Alice",
        author_email="alice@example.com",
        authored_at="2026-01-01T12:00:00+00:00",
        committed_at="2026-01-01T12:00:00+00:00",
        subject="Initial commit",
        body="",
        files_changed=3,
        insertions=10,
        deletions=2,
    )


def _sample_issue(number: int = 1, **overrides: Any) -> Issue:
    base: dict[str, Any] = dict(
        number=number,
        title=f"Issue #{number}",
        body="body",
        state="open",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        closed_at=None,
        author="alice",
        html_url=f"https://github.com/o/r/issues/{number}",
        labels=["bug"],
    )
    base.update(overrides)
    return Issue(**base)


def _sample_pr(number: int = 1, **overrides: Any) -> PullRequest:
    base: dict[str, Any] = dict(
        number=number,
        title=f"PR #{number}",
        body="body",
        state="open",
        draft=False,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        closed_at=None,
        merged_at=None,
        merge_commit_sha=None,
        head_sha="a" * 40,
        head_ref="feature",
        base_ref="main",
        author="alice",
        html_url=f"https://github.com/o/r/pull/{number}",
        labels=["bug"],
    )
    base.update(overrides)
    return PullRequest(**base)


def test_default_db_path(tmp_path: Path) -> None:
    assert default_db_path(tmp_path) == tmp_path / ".whygraph" / "whygraph.db"


def test_db_creates_parent_dirs_and_schema(tmp_path: Path) -> None:
    db_path = tmp_path / ".whygraph" / "whygraph.db"
    with Database(db_path) as db:
        assert db.commit_count() == 0
    assert db_path.exists()


def test_upsert_commit_idempotent(tmp_path: Path) -> None:
    with Database(tmp_path / "whygraph.db") as db:
        commit = _sample_commit()
        assert db.upsert_commit(commit) is True
        assert db.upsert_commit(commit) is False
        assert db.commit_count() == 1


def test_commit_exists(tmp_path: Path) -> None:
    with Database(tmp_path / "whygraph.db") as db:
        commit = _sample_commit()
        assert db.commit_exists(commit.sha) is False
        db.upsert_commit(commit)
        assert db.commit_exists(commit.sha) is True


def test_scan_state_set_and_get(tmp_path: Path) -> None:
    with Database(tmp_path / "whygraph.db") as db:
        assert db.get_scan_state("last_walked_sha") is None
        db.set_scan_state("last_walked_sha", "abc123")
        assert db.get_scan_state("last_walked_sha") == "abc123"
        db.set_scan_state("last_walked_sha", "def456")
        assert db.get_scan_state("last_walked_sha") == "def456"


def test_db_persists_across_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "whygraph.db"
    with Database(db_path) as db:
        db.upsert_commit(_sample_commit())
        db.set_scan_state("last_walked_sha", "abc")
    with Database(db_path) as db:
        assert db.commit_count() == 1
        assert db.get_scan_state("last_walked_sha") == "abc"


def test_upsert_pr_inserts_then_idempotent(tmp_path: Path) -> None:
    with Database(tmp_path / "whygraph.db") as db:
        pr = _sample_pr()
        assert db.upsert_pull_request(pr) is True
        assert db.upsert_pull_request(pr) is False
        assert db.pull_request_count() == 1


def test_upsert_pr_refreshes_mutable_fields(tmp_path: Path) -> None:
    with Database(tmp_path / "whygraph.db") as db:
        db.upsert_pull_request(_sample_pr(state="open"))
        db.upsert_pull_request(
            _sample_pr(state="closed", merged_at="2026-02-01T00:00:00Z")
        )
        cur = db._conn.cursor()
        cur.execute("SELECT state, merged_at FROM pull_requests WHERE number = 1")
        row = cur.fetchone()
        assert row == ("closed", "2026-02-01T00:00:00Z")


def test_upsert_pr_roundtrips_comments(tmp_path: Path) -> None:
    comments = [
        {"author": "alice", "body": "ship it", "created_at": "2026-01-01T00:00:00Z"},
    ]
    with Database(tmp_path / "whygraph.db") as db:
        db.upsert_pull_request(_sample_pr(comments=comments))
        cur = db._conn.cursor()
        cur.execute("SELECT comments FROM pull_requests WHERE number = 1")
        (raw,) = cur.fetchone()
        import json as _json

        assert _json.loads(raw) == comments


def test_upsert_pr_roundtrips_commit_titles(tmp_path: Path) -> None:
    entries = [
        {
            "oid": "abc1234",
            "headline": "first",
            "author_login": "alice",
            "author_name": "Alice",
            "author_email": "alice@example.com",
        },
        {
            "oid": "def5678",
            "headline": "second",
            "author_login": None,
            "author_name": "Bob",
            "author_email": "bob@example.com",
        },
    ]
    with Database(tmp_path / "whygraph.db") as db:
        db.upsert_pull_request(_sample_pr(commit_titles=entries))
        cur = db._conn.cursor()
        cur.execute("SELECT commit_titles FROM pull_requests WHERE number = 1")
        (raw,) = cur.fetchone()
        import json as _json

        assert _json.loads(raw) == entries


def test_upsert_issue_inserts_then_idempotent(tmp_path: Path) -> None:
    with Database(tmp_path / "whygraph.db") as db:
        issue = _sample_issue()
        assert db.upsert_issue(issue) is True
        assert db.upsert_issue(issue) is False
        assert db.issue_count() == 1


def test_upsert_issue_refreshes_state(tmp_path: Path) -> None:
    with Database(tmp_path / "whygraph.db") as db:
        db.upsert_issue(_sample_issue(state="open"))
        db.upsert_issue(_sample_issue(state="closed", closed_at="2026-02-01T00:00:00Z"))
        cur = db._conn.cursor()
        cur.execute("SELECT state, closed_at FROM issues WHERE number = 1")
        assert cur.fetchone() == ("closed", "2026-02-01T00:00:00Z")


def test_set_pr_closing_issues_replaces_links(tmp_path: Path) -> None:
    with Database(tmp_path / "whygraph.db") as db:
        db.upsert_pull_request(_sample_pr(number=10))
        db.set_pr_closing_issues(10, [1, 2, 3])
        cur = db._conn.cursor()
        cur.execute(
            "SELECT issue_number FROM pr_issue_links WHERE pr_number = 10 ORDER BY issue_number"
        )
        assert [r[0] for r in cur.fetchall()] == [1, 2, 3]
        # Replace
        db.set_pr_closing_issues(10, [5])
        cur.execute(
            "SELECT issue_number FROM pr_issue_links WHERE pr_number = 10 ORDER BY issue_number"
        )
        assert [r[0] for r in cur.fetchall()] == [5]


def test_migration_v6_adds_score_columns(tmp_path: Path) -> None:
    with Database(tmp_path / "whygraph.db") as db:
        cur = db._conn.cursor()
        for table, expected in {
            "commits": {"subject_tfidf_score", "body_tfidf_score"},
            "pull_requests": {"title_tfidf_score", "body_tfidf_score"},
            "issues": {"title_tfidf_score", "body_tfidf_score"},
        }.items():
            cols = {row[1] for row in cur.execute(f"PRAGMA table_info({table})")}
            assert expected <= cols, f"{table} missing {expected - cols}"
        commit = _sample_commit()
        db.upsert_commit(commit)
        cur.execute(
            "SELECT subject_tfidf_score, body_tfidf_score FROM commits WHERE sha = ?",
            (commit.sha,),
        )
        assert cur.fetchone() == (0, 0)


def test_migration_v7_adds_llm_columns(tmp_path: Path) -> None:
    with Database(tmp_path / "whygraph.db") as db:
        cur = db._conn.cursor()
        cols = {row[1] for row in cur.execute("PRAGMA table_info(commits)")}
        assert {"llm_description", "llm_description_model"} <= cols
        commit = _sample_commit()
        db.upsert_commit(commit)
        cur.execute(
            "SELECT llm_description, llm_description_model FROM commits WHERE sha = ?",
            (commit.sha,),
        )
        assert cur.fetchone() == (None, None)


def test_set_llm_description_and_filter(tmp_path: Path) -> None:
    with Database(tmp_path / "whygraph.db") as db:
        a = _sample_commit(sha="a" * 40)
        b = _sample_commit(sha="b" * 40)
        db.upsert_commit(a)
        db.upsert_commit(b)
        # both NULL initially
        assert db.commits_without_llm_description([a.sha, b.sha]) == {a.sha, b.sha}
        db.set_llm_description(a.sha, "added foo", "claude-haiku-4-5-20251001")
        assert db.commits_without_llm_description([a.sha, b.sha]) == {b.sha}
        cur = db._conn.cursor()
        cur.execute(
            "SELECT llm_description, llm_description_model FROM commits WHERE sha = ?",
            (a.sha,),
        )
        assert cur.fetchone() == ("added foo", "claude-haiku-4-5-20251001")


def test_pr_join_with_commits(tmp_path: Path) -> None:
    with Database(tmp_path / "whygraph.db") as db:
        commit = _sample_commit(sha="b" * 40)
        db.upsert_commit(commit)
        db.upsert_pull_request(_sample_pr(number=42, merge_commit_sha="b" * 40))
        cur = db._conn.cursor()
        cur.execute(
            """
            SELECT p.number, c.subject FROM pull_requests p
            JOIN commits c ON c.sha = p.merge_commit_sha
            """
        )
        rows = cur.fetchall()
        assert rows == [(42, "Initial commit")]
