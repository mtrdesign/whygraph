import os
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
    items = out["evidence"]
    shas = {item["sha"] for item in items}
    assert shas == {sha1, sha2}
    by_sha = {item["sha"]: item for item in items}
    assert by_sha[sha1]["blame_lines"] == 2  # alpha + beta
    assert by_sha[sha2]["blame_lines"] == 1  # gamma
    assert (
        by_sha[sha1]["narratives"]["llm_description"]
        == "added f.txt with alpha and beta lines"
    )
    # Path+lines targeting → no graph node → empty neighbour lists.
    assert out["callers"] == []
    assert out["callees"] == []
    assert out["target"]["qualified_name"] is None


def test_evidence_for_subset_lines_only(repo_and_db) -> None:
    """Asking for line 3 only should surface only sha2."""
    _, sha1, sha2, _ = repo_and_db
    out = mcp_server.whygraph_evidence_for(
        path="f.txt", line_start=3, line_end=3, min_score_pct=0.0
    )
    assert {item["sha"] for item in out["evidence"]} == {sha2}


def test_evidence_for_surfaces_blame_when_sha_missing_from_db(
    repo_and_db, monkeypatch
) -> None:
    """Stale scan DB: blame returns SHAs that aren't in commits — we
    still surface them with blame-derived metadata and db_commit_present
    set to False, instead of silently dropping the entry."""
    repo_root, sha1, sha2, db_path = repo_and_db
    # Add a third commit AFTER the scan was simulated; this commit's SHA
    # will appear in blame but not in the DB.
    (repo_root / "f.txt").write_text("alpha\nbeta\ngamma\ndelta\n")
    _git(repo_root, "add", "f.txt")
    _git(repo_root, "config", "user.email", "carol@example.com")
    _git(repo_root, "config", "user.name", "Carol")
    _git(repo_root, "commit", "-q", "-m", "add delta")
    sha3 = _git_out(repo_root, "rev-parse", "HEAD")
    out = mcp_server.whygraph_evidence_for(
        path="f.txt", line_start=4, line_end=4, min_score_pct=0.0
    )
    items = out["evidence"]
    assert len(items) == 1
    item = items[0]
    assert item["sha"] == sha3
    assert item["db_commit_present"] is False
    assert item["blame_lines"] == 1
    assert item["narratives"] == {"git_blame_summary": "add delta"}
    assert item["commit_author"]["email"] == "carol@example.com"
    assert item["commit_author"]["name"] == "Carol"
    assert item["committed_at"] is not None


def test_evidence_for_keeps_commit_with_null_narrative(repo_and_db, monkeypatch) -> None:
    """Commits failing the narrative gate still surface — blame is signal."""
    repo_root, sha1, sha2, db_path = repo_and_db
    # Make sha2 unable to pass via narrative: zero scores, no llm_description.
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
    by_sha = {item["sha"]: item for item in out["evidence"]}
    # sha1 still in via llm_description.
    assert "llm_description" in by_sha[sha1]["narratives"]
    # sha2 surfaces with empty narratives — blame line ownership is preserved.
    assert sha2 in by_sha
    assert by_sha[sha2]["narratives"] == {}
    assert by_sha[sha2]["blame_lines"] == 1


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
    items = out["evidence"]
    assert len(items) == 1
    item = items[0]
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
    # Resolution path worked.
    assert out["target"]["qualified_name"] == "pkg.a"
    assert out["target"]["path"] == "src/pkg/a.py"
    # The fake CodeGraph defines pkg.a → calls pkg.b. So callees has pkg.b.
    callee_qns = {n["qualified_name"] for n in out["callees"]}
    assert "pkg.b" in callee_qns
    # No callers point at pkg.a in the fixture.
    assert out["callers"] == []


def _commit_pkg_b(
    repo_root: Path, content: str, message: str, *, when: str | None = None
) -> str:
    """Write src/pkg/b.py and commit. Returns the new SHA.

    ``when`` (ISO-8601, e.g. ``2026-04-01T10:00:00+00:00``) pins the
    author/committer timestamps so blame ordering is deterministic.
    """
    target_dir = repo_root / "src" / "pkg"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "b.py").write_text(content)
    _git(repo_root, "add", "src/pkg/b.py")
    if when:
        env = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
        subprocess.run(
            ["git", "-C", str(repo_root), "commit", "-q", "-m", message],
            check=True,
            capture_output=True,
            env={**os.environ, **env},
        )
    else:
        _git(repo_root, "commit", "-q", "-m", message)
    return _git_out(repo_root, "rev-parse", "HEAD")


def test_evidence_for_enriches_neighbour_with_top3_recent(
    repo_and_db, fake_codegraph_db, monkeypatch
) -> None:
    """pkg.b (callee of pkg.a) should be enriched with its own top-3
    commits sorted by recency."""
    repo_root, *_ = repo_and_db
    # Target file (pkg.a) needs to exist for the target-side blame call.
    (repo_root / "src" / "pkg").mkdir(parents=True, exist_ok=True)
    (repo_root / "src" / "pkg" / "a.py").write_text(
        "alpha\nbeta\ngamma\ndelta\nepsilon\n"
    )
    _git(repo_root, "add", "src/pkg/a.py")
    _git(repo_root, "commit", "-q", "-m", "add a.py")
    # 5 commits to b.py — each overwrites a different line so blame
    # attributes 5 distinct SHAs to lines 1..5. Pinned timestamps so
    # the recency sort is deterministic regardless of how fast pytest
    # runs.
    shas = []
    body = ["L1", "L2", "L3", "L4", "L5"]
    for i in range(5):
        body[i] = f"L{i + 1}-v{i + 1}"
        when = f"2026-04-{i + 1:02d}T10:00:00+00:00"
        shas.append(
            _commit_pkg_b(
                repo_root, "\n".join(body) + "\n", f"b-rev-{i + 1}", when=when
            )
        )
    monkeypatch.setenv("CODEGRAPH_DB", str(fake_codegraph_db))
    out = mcp_server.whygraph_evidence_for(
        qualified_name="pkg.a", min_score_pct=0.0
    )
    callees_by_qn = {n["qualified_name"]: n for n in out["callees"]}
    pkg_b = callees_by_qn["pkg.b"]
    assert "evidence" in pkg_b
    # Top-3 cap.
    assert len(pkg_b["evidence"]) == 3
    # Recency sort: first item should be the most recent SHA.
    times = [ev.get("committed_at") for ev in pkg_b["evidence"]]
    assert times == sorted(times, reverse=True)
    assert pkg_b["evidence"][0]["sha"] == shas[-1]
    # Each evidence item carries the same shape as the target's items.
    first = pkg_b["evidence"][0]
    assert "narratives" in first
    assert "prs" in first
    assert "issues" in first


def test_evidence_for_neighbour_missing_file_returns_empty_evidence(
    repo_and_db, codegraph_db_factory, monkeypatch
) -> None:
    """Neighbour whose file_path doesn't exist on disk should still
    appear, with evidence=[] rather than raising."""
    repo_root, *_ = repo_and_db
    (repo_root / "src" / "pkg").mkdir(parents=True, exist_ok=True)
    (repo_root / "src" / "pkg" / "a.py").write_text(
        "alpha\nbeta\ngamma\ndelta\nepsilon\n"
    )
    _git(repo_root, "add", "src/pkg/a.py")
    _git(repo_root, "commit", "-q", "-m", "add a.py")
    cg_path = codegraph_db_factory(
        nodes=[
            {
                "id": "n_a",
                "kind": "function",
                "name": "a",
                "qualified_name": "pkg.a",
                "file_path": "src/pkg/a.py",
                "language": "python",
                "start_line": 1,
                "end_line": 5,
                "docstring": "doc-a",
                "signature": "def a()",
            },
            {
                "id": "n_ghost",
                "kind": "function",
                "name": "ghost",
                "qualified_name": "pkg.ghost",
                "file_path": "src/pkg/ghost.py",  # never created on disk
                "language": "python",
                "start_line": 1,
                "end_line": 5,
                "docstring": "ghostly callee",
                "signature": "def ghost()",
            },
        ],
        edges=[("n_a", "n_ghost", "calls")],
    )
    monkeypatch.setenv("CODEGRAPH_DB", str(cg_path))
    out = mcp_server.whygraph_evidence_for(
        qualified_name="pkg.a", min_score_pct=0.0
    )
    callees_by_qn = {n["qualified_name"]: n for n in out["callees"]}
    ghost = callees_by_qn["pkg.ghost"]
    assert ghost["evidence"] == []
    # Docstring still propagates from the graph node.
    assert ghost["docstring"] == "ghostly callee"


def test_evidence_for_neighbour_propagates_docstring(
    repo_and_db, codegraph_db_factory, monkeypatch
) -> None:
    """SymbolNode.docstring must surface in the neighbour dict."""
    repo_root, *_ = repo_and_db
    (repo_root / "src" / "pkg").mkdir(parents=True, exist_ok=True)
    (repo_root / "src" / "pkg" / "a.py").write_text(
        "alpha\nbeta\ngamma\ndelta\nepsilon\n"
    )
    _git(repo_root, "add", "src/pkg/a.py")
    _git(repo_root, "commit", "-q", "-m", "add a.py")
    _commit_pkg_b(repo_root, "L1\nL2\nL3\nL4\nL5\n", "init b")
    cg_path = codegraph_db_factory(
        nodes=[
            {
                "id": "n_a",
                "kind": "function",
                "name": "a",
                "qualified_name": "pkg.a",
                "file_path": "src/pkg/a.py",
                "language": "python",
                "start_line": 1,
                "end_line": 5,
                "docstring": "doc-a",
                "signature": "def a()",
            },
            {
                "id": "n_b",
                "kind": "function",
                "name": "b",
                "qualified_name": "pkg.b",
                "file_path": "src/pkg/b.py",
                "language": "python",
                "start_line": 1,
                "end_line": 5,
                "docstring": "computes the b-thing",
                "signature": "def b()",
            },
        ],
        edges=[("n_a", "n_b", "calls")],
    )
    monkeypatch.setenv("CODEGRAPH_DB", str(cg_path))
    out = mcp_server.whygraph_evidence_for(
        qualified_name="pkg.a", min_score_pct=0.0
    )
    callees_by_qn = {n["qualified_name"]: n for n in out["callees"]}
    assert callees_by_qn["pkg.b"]["docstring"] == "computes the b-thing"
