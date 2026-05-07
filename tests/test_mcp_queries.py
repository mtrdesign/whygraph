from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from whygraph import mcp_queries
from whygraph.scan.db import Database
from whygraph.scan.git import Commit
from whygraph.scan.github import Issue, PullRequest
from whygraph.scan.scoring import ValueGate


def _commit(
    sha: str,
    *,
    subject: str = "subj",
    body: str = "",
    committed_at: str = "2026-04-01T00:00:00+00:00",
    author_email: str = "alice@example.com",
    author_name: str = "Alice",
    files_changed: int = 1,
) -> Commit:
    return Commit(
        sha=sha,
        parent_shas=[],
        author_name=author_name,
        author_email=author_email,
        authored_at=committed_at,
        committed_at=committed_at,
        subject=subject,
        body=body,
        files_changed=files_changed,
        insertions=1,
        deletions=0,
    )


def _pr(number: int, **overrides: Any) -> PullRequest:
    base: dict[str, Any] = dict(
        number=number,
        title=f"PR {number}",
        body="body",
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
        html_url=f"https://github.com/o/r/pull/{number}",
        labels=[],
    )
    base.update(overrides)
    return PullRequest(**base)


def _issue(number: int, **overrides: Any) -> Issue:
    base: dict[str, Any] = dict(
        number=number,
        title=f"Issue {number}",
        body="body",
        state="open",
        created_at="2026-04-01T00:00:00Z",
        updated_at="2026-04-01T00:00:00Z",
        closed_at=None,
        author="bob",
        html_url=f"https://github.com/o/r/issues/{number}",
        labels=[],
    )
    base.update(overrides)
    return Issue(**base)


def _open_gate_admitting_everything() -> ValueGate:
    return ValueGate(
        thresholds={
            ("commits", "subject"): -1.0,
            ("commits", "body"): -1.0,
            ("pull_requests", "title"): -1.0,
            ("pull_requests", "body"): -1.0,
            ("issues", "title"): -1.0,
            ("issues", "body"): -1.0,
        }
    )


def _strict_gate() -> ValueGate:
    """Threshold high enough that no row passes on score alone."""
    return ValueGate(
        thresholds={
            ("commits", "subject"): 1e9,
            ("commits", "body"): 1e9,
            ("pull_requests", "title"): 1e9,
            ("pull_requests", "body"): 1e9,
            ("issues", "title"): 1e9,
            ("issues", "body"): 1e9,
        }
    )


def test_commit_narrative_prefers_llm_description() -> None:
    gate = _strict_gate()  # score-based fields can't pass
    commit = {
        "llm_description": "added foo",
        "body": "human body",
        "subject": "subj",
        "body_tfidf_score": 0.0,
        "subject_tfidf_score": 0.0,
    }
    text, src = mcp_queries.commit_narrative(commit, gate)
    assert text == "added foo"
    assert src == "llm_description"


def test_commit_narrative_falls_back_to_body_when_above_gate() -> None:
    gate = _open_gate_admitting_everything()
    commit = {
        "llm_description": None,
        "body": "long body",
        "subject": "short",
        "body_tfidf_score": 0.5,
        "subject_tfidf_score": 0.5,
    }
    text, src = mcp_queries.commit_narrative(commit, gate)
    assert src == "body"
    assert text == "long body"


def test_commit_narrative_returns_none_when_all_fail() -> None:
    gate = _strict_gate()
    commit = {
        "llm_description": None,
        "body": "body",
        "subject": "subj",
        "body_tfidf_score": 0.0,
        "subject_tfidf_score": 0.0,
    }
    text, src = mcp_queries.commit_narrative(commit, gate)
    assert text is None and src is None


def test_commit_narratives_returns_llm_and_body_together() -> None:
    """When both llm_description and body qualify, both ship — the
    plural helper does not pick a winner."""
    gate = _open_gate_admitting_everything()
    commit = {
        "llm_description": "added foo",
        "body": "long human body",
        "subject": "subj",
        "body_tfidf_score": 0.5,
        "subject_tfidf_score": 0.5,
    }
    out = mcp_queries.commit_narratives(commit, gate)
    assert out["llm_description"] == "added foo"
    assert out["body"] == "long human body"
    assert out["subject"] == "subj"


def test_commit_narratives_llm_passes_even_with_strict_gate() -> None:
    """`llm_description` is mechanical and bypasses the harshness gate;
    body/subject are still gated."""
    gate = _strict_gate()
    commit = {
        "llm_description": "added foo",
        "body": "human body",
        "subject": "subj",
        "body_tfidf_score": 0.0,
        "subject_tfidf_score": 0.0,
    }
    out = mcp_queries.commit_narratives(commit, gate)
    assert out == {"llm_description": "added foo"}


def test_commit_narratives_returns_empty_when_nothing_qualifies() -> None:
    gate = _strict_gate()
    commit = {
        "llm_description": None,
        "body": "body",
        "subject": "subj",
        "body_tfidf_score": 0.0,
        "subject_tfidf_score": 0.0,
    }
    assert mcp_queries.commit_narratives(commit, gate) == {}


def test_pr_narratives_returns_title_and_body_when_both_qualify() -> None:
    gate = _open_gate_admitting_everything()
    pr = {
        "title": "Add gamma",
        "body": "Rationale here",
        "title_tfidf_score": 0.5,
        "body_tfidf_score": 0.5,
    }
    out = mcp_queries.pr_narratives(pr, gate)
    assert out == {"title": "Add gamma", "body": "Rationale here"}


def test_issue_narratives_returns_title_and_body_when_both_qualify() -> None:
    gate = _open_gate_admitting_everything()
    issue = {
        "title": "Bug X",
        "body": "Steps to reproduce",
        "title_tfidf_score": 0.5,
        "body_tfidf_score": 0.5,
    }
    out = mcp_queries.issue_narratives(issue, gate)
    assert out == {"title": "Bug X", "body": "Steps to reproduce"}


def test_prs_containing_commit_matches_merge_head_and_oid(tmp_path: Path) -> None:
    target = "a" * 40
    other = "b" * 40
    with Database(tmp_path / "whygraph.db") as db:
        db.upsert_pull_request(_pr(1, merge_commit_sha=target))
        db.upsert_pull_request(_pr(2, head_sha=target))
        db.upsert_pull_request(
            _pr(
                3,
                commit_titles=[
                    {
                        "oid": target,
                        "headline": "x",
                        "author_login": "alice",
                        "author_name": "Alice",
                        "author_email": "alice@example.com",
                    }
                ],
            )
        )
        db.upsert_pull_request(_pr(4, merge_commit_sha=other, head_sha=other))
        prs = mcp_queries.prs_containing_commit(db, target)
    assert {p["number"] for p in prs} == {1, 2, 3}


def test_closing_issues_for_pr_uses_links(tmp_path: Path) -> None:
    with Database(tmp_path / "whygraph.db") as db:
        db.upsert_pull_request(_pr(10))
        db.upsert_issue(_issue(7))
        db.upsert_issue(_issue(8))
        db.set_pr_closing_issues(10, [7, 8])
        issues = mcp_queries.closing_issues_for_pr(db, 10)
    assert [i["number"] for i in issues] == [7, 8]


def test_pr_authors_dedupes_and_includes_commit_authors() -> None:
    pr = {
        "author": "alice",
        "commit_titles": '[{"oid":"x","headline":"h","author_login":"alice","author_name":"Alice","author_email":"alice@example.com"},{"oid":"y","headline":"h","author_login":null,"author_name":"Bob","author_email":"bob@example.com"}]',
    }
    authors = mcp_queries.pr_authors(pr)
    logins = [a["login"] for a in authors]
    assert "alice" in logins
    assert any(a["email"] == "bob@example.com" for a in authors)
    # alice appears once even though listed twice
    assert sum(1 for a in authors if a.get("login") == "alice") == 1


def test_search_text_admits_llm_described_commit_through_strict_gate(
    tmp_path: Path,
) -> None:
    with Database(tmp_path / "whygraph.db") as db:
        db.upsert_commit(_commit("a" * 40, subject="alpha", body="alpha world"))
        db.upsert_commit(_commit("b" * 40, subject="beta", body="other"))
        db.set_llm_description("a" * 40, "renamed alpha→omega in src/x.py", "haiku")
        gate = _strict_gate()
        hits = mcp_queries.search_text(db, "alpha", gate=gate)
    # Commit a passes via llm_description; commit b is filtered out.
    assert {h["id"] for h in hits} == {"a" * 40}
    assert hits[0]["narrative_source"] == "llm_description"


def test_search_text_kinds_filter(tmp_path: Path) -> None:
    with Database(tmp_path / "whygraph.db") as db:
        db.upsert_commit(_commit("a" * 40, subject="cache miss"))
        db.upsert_pull_request(_pr(1, body="cache miss in handler"))
        db.upsert_issue(_issue(7, title="cache miss bug"))
        # Set scores so the open gate admits all.
        cur = db._conn.cursor()
        cur.execute("UPDATE commits SET subject_tfidf_score = 1.0")
        cur.execute("UPDATE pull_requests SET body_tfidf_score = 1.0")
        cur.execute("UPDATE issues SET title_tfidf_score = 1.0")
        db._conn.commit()
        gate = _open_gate_admitting_everything()
        hits = mcp_queries.search_text(db, "cache", kinds=("pr",), gate=gate)
    assert {h["kind"] for h in hits} == {"pr"}


def test_velocity_by_author_groups_and_counts(tmp_path: Path) -> None:
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    in_window = "2026-04-15T00:00:00+00:00"
    out_of_window = "2025-12-01T00:00:00+00:00"
    with Database(tmp_path / "whygraph.db") as db:
        db.upsert_commit(
            _commit(
                "a" * 40,
                committed_at=in_window,
                author_email="alice@example.com",
                author_name="Alice",
                files_changed=3,
            )
        )
        db.upsert_commit(
            _commit(
                "b" * 40,
                committed_at=in_window,
                author_email="alice@example.com",
                author_name="Alice",
                files_changed=2,
            )
        )
        db.upsert_commit(
            _commit(
                "c" * 40,
                committed_at=out_of_window,
                author_email="alice@example.com",
                author_name="Alice",
                files_changed=10,
            )
        )
        db.upsert_commit(
            _commit(
                "d" * 40,
                committed_at=in_window,
                author_email="bob@example.com",
                author_name="Bob",
                files_changed=1,
            )
        )
        out = mcp_queries.velocity_by_author(
            db, window_days=90, top_n=10, now=now
        )
    by_email = {r["author_email"]: r for r in out}
    assert by_email["alice@example.com"]["window_commits"] == 2
    assert by_email["alice@example.com"]["all_time_commits"] == 3
    assert by_email["alice@example.com"]["window_files_changed"] == 5
    assert by_email["bob@example.com"]["window_commits"] == 1
    assert out[0]["author_email"] == "alice@example.com"  # sorted desc


def test_repo_overview_reports_counts_and_coverage(tmp_path: Path) -> None:
    with Database(tmp_path / "whygraph.db") as db:
        db.upsert_commit(
            _commit("a" * 40, committed_at="2026-04-01T00:00:00+00:00")
        )
        db.upsert_commit(
            _commit("b" * 40, committed_at="2026-04-15T00:00:00+00:00")
        )
        db.set_llm_description("a" * 40, "desc", "haiku")
        db.upsert_pull_request(_pr(1))
        db.upsert_issue(_issue(7))
        cur = db._conn.cursor()
        cur.execute("UPDATE commits SET body_tfidf_score = 0.5 WHERE sha = ?", ("a" * 40,))
        db._conn.commit()
        ov = mcp_queries.repo_overview(db)
    assert ov["commits"] == 2
    assert ov["pull_requests"] == 1
    assert ov["issues"] == 1
    assert ov["llm_described_commits"] == 1
    assert ov["scored_commits"] == 1
    assert ov["first_commit_at"] == "2026-04-01T00:00:00+00:00"
    assert ov["last_commit_at"] == "2026-04-15T00:00:00+00:00"
    assert any(
        c["author_email"] == "alice@example.com" for c in ov["top_contributors"]
    )
