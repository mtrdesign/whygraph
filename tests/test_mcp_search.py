from pathlib import Path

import pytest

from whygraph import mcp_server
from whygraph.scan.db import Database
from whygraph.scan.git import Commit
from whygraph.scan.github import Issue, PullRequest


@pytest.fixture
def search_db(tmp_path: Path, monkeypatch) -> Path:
    db_path = tmp_path / ".whygraph" / "whygraph.db"
    monkeypatch.setenv("WHYGRAPH_DB", str(db_path))
    with Database(db_path) as db:
        # 3 commits, only one with llm_description.
        for i, sha in enumerate(("a" * 40, "b" * 40, "c" * 40)):
            db.upsert_commit(
                Commit(
                    sha=sha,
                    parent_shas=[],
                    author_name="Alice",
                    author_email="alice@example.com",
                    authored_at=f"2026-04-0{i+1}T00:00:00+00:00",
                    committed_at=f"2026-04-0{i+1}T00:00:00+00:00",
                    subject=f"cache fix {i}",
                    body=f"adjust cache lookup body {i}",
                    files_changed=1,
                    insertions=1,
                    deletions=0,
                )
            )
        # b only has llm_description; a and c have non-zero scores.
        db.set_llm_description(
            "b" * 40, "renamed cache_lookup→cache_get in src/cache.py", "haiku"
        )
        cur = db._conn.cursor()
        cur.execute(
            "UPDATE commits SET subject_tfidf_score = 1.0 WHERE sha = ?", ("a" * 40,)
        )
        cur.execute(
            "UPDATE commits SET subject_tfidf_score = 0.5 WHERE sha = ?", ("c" * 40,)
        )
        # b stays at score 0 — only its llm_description gets it past the gate.
        db._conn.commit()

        # Two PRs with different scores so the percentile threshold sits
        # strictly below the matching one.
        for n, title, score in (
            (1, "cache fix", 1.0),
            (2, "unrelated readme", 0.05),
        ):
            db.upsert_pull_request(
                PullRequest(
                    number=n,
                    title=title,
                    body=f"body {n}",
                    state="open",
                    draft=False,
                    created_at="2026-04-01T00:00:00Z",
                    updated_at="2026-04-01T00:00:00Z",
                    closed_at=None,
                    merged_at=None,
                    merge_commit_sha=None,
                    head_sha="0" * 40,
                    head_ref="feat",
                    base_ref="main",
                    author="alice",
                    html_url=f"https://github.com/o/r/pull/{n}",
                    labels=[],
                )
            )
            cur.execute(
                "UPDATE pull_requests SET title_tfidf_score = ? WHERE number = ?",
                (score, n),
            )
        db._conn.commit()

        for n, title, score in (
            (7, "cache miss bug", 1.0),
            (8, "minor typo", 0.05),
        ):
            db.upsert_issue(
                Issue(
                    number=n,
                    title=title,
                    body=f"body {n}",
                    state="open",
                    created_at="2026-04-01T00:00:00Z",
                    updated_at="2026-04-01T00:00:00Z",
                    closed_at=None,
                    author="bob",
                    html_url=f"https://github.com/o/r/issues/{n}",
                    labels=[],
                )
            )
            cur.execute(
                "UPDATE issues SET title_tfidf_score = ? WHERE number = ?",
                (score, n),
            )
        db._conn.commit()
    return db_path


def test_search_returns_all_kinds_by_default(search_db: Path) -> None:
    out = mcp_server.whygraph_search("cache", min_score_pct=0.0)
    kinds = {h["kind"] for h in out}
    assert kinds == {"commit", "pr", "issue"}


def test_search_kinds_filter(search_db: Path) -> None:
    out = mcp_server.whygraph_search("cache", kinds=["pr"], min_score_pct=0.0)
    assert all(h["kind"] == "pr" for h in out)
    assert len(out) == 1


def test_search_llm_described_commit_passes_strict_gate(search_db: Path) -> None:
    out = mcp_server.whygraph_search(
        "cache", kinds=["commit"], min_score_pct=0.99
    )
    # b only passes because of llm_description.
    sha_b = "b" * 40
    assert any(h["id"] == sha_b for h in out)


def test_search_rejects_invalid_kind(search_db: Path) -> None:
    with pytest.raises(mcp_server.WhyGraphError, match="unknown kinds"):
        mcp_server.whygraph_search("cache", kinds=["bogus"])


def test_search_respects_limit(search_db: Path) -> None:
    out = mcp_server.whygraph_search("cache", limit=2, min_score_pct=0.0)
    assert len(out) <= 2
