import subprocess
from pathlib import Path

import pytest

from whygraph import mcp_server
from whygraph.scan.db import Database
from whygraph.scan.git import Commit
from whygraph.scan.github import PullRequest


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_repo_with_two_commits(tmp_path: Path) -> tuple[Path, str, str]:
    """Repo with one file f.txt where commit-1 wrote lines 1-2 and commit-2 added line 3."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "alice@example.com")
    _git(tmp_path, "config", "user.name", "Alice")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "f.txt").write_text("alpha\nbeta\n")
    _git(tmp_path, "add", "f.txt")
    _git(tmp_path, "commit", "-q", "-m", "first")
    sha1 = _git_out(tmp_path, "rev-parse", "HEAD")

    (tmp_path / "f.txt").write_text("alpha\nbeta\ngamma\n")
    _git(tmp_path, "add", "f.txt")
    _git(tmp_path, "config", "user.email", "bob@example.com")
    _git(tmp_path, "config", "user.name", "Bob")
    _git(tmp_path, "commit", "-q", "-m", "add gamma")
    sha2 = _git_out(tmp_path, "rev-parse", "HEAD")
    return tmp_path, sha1, sha2


def _commit_for(sha: str, *, author_email: str, author_name: str, subject: str, llm: str | None = None) -> Commit:
    return Commit(
        sha=sha,
        parent_shas=[],
        author_name=author_name,
        author_email=author_email,
        authored_at="2026-04-01T00:00:00+00:00",
        committed_at="2026-04-01T00:00:00+00:00",
        subject=subject,
        body="",
        files_changed=1,
        insertions=1,
        deletions=0,
    )


@pytest.fixture
def repo_and_db(tmp_path: Path, monkeypatch):
    repo_root, sha1, sha2 = _make_repo_with_two_commits(tmp_path)
    db_path = tmp_path / ".whygraph" / "whygraph.db"
    monkeypatch.setenv("WHYGRAPH_DB", str(db_path))
    monkeypatch.chdir(repo_root)
    with Database(db_path) as db:
        # First commit gets a high body score and llm_description.
        db.upsert_commit(
            _commit_for(
                sha1,
                author_email="alice@example.com",
                author_name="Alice",
                subject="first",
            )
        )
        db.set_llm_description(
            sha1,
            "added f.txt with alpha and beta lines",
            "haiku",
        )
        # Second commit, no llm_description — exercises subject fallback.
        db.upsert_commit(
            _commit_for(
                sha2,
                author_email="bob@example.com",
                author_name="Bob",
                subject="add gamma",
            )
        )
        # Distinct scores so the gate threshold sits between them. sha2's
        # score must exceed the bottom-percentile threshold to surface in
        # tests that don't rely on llm_description.
        cur = db._conn.cursor()
        cur.execute(
            "UPDATE commits SET subject_tfidf_score = 0.1, body_tfidf_score = 0.0 WHERE sha = ?",
            (sha1,),
        )
        cur.execute(
            "UPDATE commits SET subject_tfidf_score = 1.0, body_tfidf_score = 0.0 WHERE sha = ?",
            (sha2,),
        )
        db._conn.commit()
        # PR pointing at sha2 with closing-issue link is added in a later test.
    return repo_root, sha1, sha2, db_path


def test_evidence_for_path_returns_blame_owners(repo_and_db) -> None:
    repo_root, sha1, sha2, _ = repo_and_db
    out = mcp_server.whygraph_evidence_for(
        path="f.txt", line_start=1, line_end=3, min_score_pct=0.0
    )
    shas = {item["sha"] for item in out}
    assert shas == {sha1, sha2}
    by_sha = {item["sha"]: item for item in out}
    assert by_sha[sha1]["blame_lines"] == 2  # alpha + beta
    assert by_sha[sha2]["blame_lines"] == 1  # gamma
    assert by_sha[sha1]["narrative"] == "added f.txt with alpha and beta lines"
    assert by_sha[sha1]["narrative_source"] == "llm_description"


def test_evidence_for_subset_lines_only(repo_and_db) -> None:
    """Asking for line 3 only should surface only sha2."""
    _, sha1, sha2, _ = repo_and_db
    out = mcp_server.whygraph_evidence_for(
        path="f.txt", line_start=3, line_end=3, min_score_pct=0.0
    )
    assert {item["sha"] for item in out} == {sha2}


def test_evidence_for_drops_commits_failing_gate(repo_and_db, monkeypatch) -> None:
    """Without an llm_description and with a strict gate, sha2 should be dropped."""
    repo_root, sha1, sha2, db_path = repo_and_db
    # Make sha2 unable to pass: score 0 and no llm_description.
    with Database(db_path) as db:
        cur = db._conn.cursor()
        cur.execute(
            "UPDATE commits SET subject_tfidf_score = 0, body_tfidf_score = 0 WHERE sha = ?",
            (sha2,),
        )
        db._conn.commit()
    out = mcp_server.whygraph_evidence_for(
        path="f.txt", line_start=1, line_end=3, min_score_pct=0.5
    )
    shas = {item["sha"] for item in out}
    # sha1 still in via llm_description; sha2 dropped.
    assert sha1 in shas
    assert sha2 not in shas


def test_evidence_for_includes_pr_and_authors(repo_and_db) -> None:
    repo_root, sha1, sha2, db_path = repo_and_db
    with Database(db_path) as db:
        db.upsert_pull_request(
            PullRequest(
                number=42,
                title="Add gamma",
                body="rationale",
                state="closed",
                draft=False,
                created_at="2026-04-01T00:00:00Z",
                updated_at="2026-04-01T00:00:00Z",
                closed_at=None,
                merged_at="2026-04-01T00:00:00Z",
                merge_commit_sha=sha2,
                head_sha="0" * 40,
                head_ref="feat",
                base_ref="main",
                author="bob",
                html_url="https://github.com/o/r/pull/42",
                labels=[],
                commit_titles=[
                    {
                        "oid": sha2,
                        "headline": "add gamma",
                        "author_login": "bob",
                        "author_name": "Bob",
                        "author_email": "bob@example.com",
                    }
                ],
            )
        )
        # Set PR title score so it passes the open gate.
        cur = db._conn.cursor()
        cur.execute("UPDATE pull_requests SET title_tfidf_score = 1.0")
        db._conn.commit()
    out = mcp_server.whygraph_evidence_for(
        path="f.txt", line_start=3, line_end=3, min_score_pct=0.0
    )
    assert len(out) == 1
    item = out[0]
    assert item["sha"] == sha2
    assert len(item["prs"]) == 1
    assert item["prs"][0]["number"] == 42
    logins = {a.get("login") for a in item["all_authors"]}
    assert "bob" in logins


def test_evidence_for_rejects_mixed_target() -> None:
    with pytest.raises(mcp_server.WhyGraphError, match="either"):
        mcp_server.whygraph_evidence_for(
            path="x.py", line_start=1, line_end=10, qualified_name="pkg.foo"
        )


def test_evidence_for_rejects_partial_path_target() -> None:
    with pytest.raises(mcp_server.WhyGraphError, match="Must pass"):
        mcp_server.whygraph_evidence_for(path="x.py")


def test_evidence_for_qualified_name_requires_codegraph(repo_and_db, monkeypatch) -> None:
    monkeypatch.delenv("CODEGRAPH_DB", raising=False)
    with pytest.raises(mcp_server.WhyGraphError, match="CodeGraph"):
        mcp_server.whygraph_evidence_for(qualified_name="pkg.foo")


def test_evidence_for_qualified_name_resolves_via_backend(
    repo_and_db, fake_codegraph_db, monkeypatch
) -> None:
    """Use the conftest CodeGraph fixture to simulate symbol resolution."""
    repo_root, sha1, sha2, _ = repo_and_db
    # The fake CodeGraph has node pkg.a → src/pkg/a.py lines 1-5; create a matching file.
    target_dir = repo_root / "src" / "pkg"
    target_dir.mkdir(parents=True)
    (target_dir / "a.py").write_text("alpha\nbeta\ngamma\ndelta\nepsilon\n")
    _git(repo_root, "add", "src/pkg/a.py")
    _git(repo_root, "commit", "-q", "-m", "add a.py")
    monkeypatch.setenv("CODEGRAPH_DB", str(fake_codegraph_db))
    out = mcp_server.whygraph_evidence_for(
        qualified_name="pkg.a", min_score_pct=0.0
    )
    # Whatever evidence comes back, the resolution path worked (no exception).
    assert isinstance(out, list)
