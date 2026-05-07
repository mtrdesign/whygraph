import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from whygraph import mcp_server
from whygraph.scan.db import Database
from whygraph.scan.git import Commit


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo_and_db(tmp_path: Path, monkeypatch):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "alice@example.com")
    _git(tmp_path, "config", "user.name", "Alice")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "f.py").write_text("def foo():\n    return 1\n")
    _git(tmp_path, "add", "f.py")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    sha = _git_out(tmp_path, "rev-parse", "HEAD")
    db_path = tmp_path / ".whygraph" / "whygraph.db"
    monkeypatch.setenv("WHYGRAPH_DB", str(db_path))
    monkeypatch.chdir(tmp_path)
    with Database(db_path) as db:
        db.upsert_commit(
            Commit(
                sha=sha,
                parent_shas=[],
                author_name="Alice",
                author_email="alice@example.com",
                authored_at="2026-04-01T00:00:00+00:00",
                committed_at="2026-04-01T00:00:00+00:00",
                subject="initial",
                body="",
                files_changed=1,
                insertions=2,
                deletions=0,
            )
        )
        db.set_llm_description(sha, "added f.py with foo()", "haiku")
    return tmp_path, sha


def _canned_rationale(**overrides: object) -> str:
    payload = {
        "purpose": "p",
        "why": "w",
        "constraints": [],
        "tradeoffs": [],
        "risks": [],
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_rationale_cache_hit_skips_llm(repo_and_db) -> None:
    """Second call with identical evidence must read from cache and never
    re-invoke the LLM."""
    invocations = {"n": 0}

    def fake_invoke(prompt, *, model, timeout_sec, anthropic_api_key=None, system_prompt=None):
        invocations["n"] += 1
        return _canned_rationale(purpose="cached purpose")

    with patch("whygraph.mcp_server.llm_subprocess.invoke_claude", side_effect=fake_invoke):
        first = mcp_server.whygraph_rationale_brief(
            path="f.py", line_start=1, line_end=2, min_score_pct=0.0
        )
        second = mcp_server.whygraph_rationale_brief(
            path="f.py", line_start=1, line_end=2, min_score_pct=0.0
        )
    assert invocations["n"] == 1
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["purpose"] == "cached purpose"
    assert second["why"] == first["why"]


def test_rationale_force_refresh_bypasses_cache(repo_and_db) -> None:
    """force_refresh=True must hit the LLM even when a cached row exists,
    and must overwrite the cached row."""
    sequence = iter(
        [
            _canned_rationale(purpose="first"),
            _canned_rationale(purpose="second"),
        ]
    )

    def fake_invoke(prompt, *, model, timeout_sec, anthropic_api_key=None, system_prompt=None):
        return next(sequence)

    with patch("whygraph.mcp_server.llm_subprocess.invoke_claude", side_effect=fake_invoke):
        mcp_server.whygraph_rationale_brief(
            path="f.py", line_start=1, line_end=2, min_score_pct=0.0
        )
        refreshed = mcp_server.whygraph_rationale_brief(
            path="f.py",
            line_start=1,
            line_end=2,
            min_score_pct=0.0,
            force_refresh=True,
        )
        # Now the cache holds "second" — a vanilla call returns it.
        third = mcp_server.whygraph_rationale_brief(
            path="f.py", line_start=1, line_end=2, min_score_pct=0.0
        )
    assert refreshed["cached"] is False
    assert refreshed["purpose"] == "second"
    assert third["cached"] is True
    assert third["purpose"] == "second"


def test_rationale_cache_invalidates_when_bundle_changes(repo_and_db) -> None:
    """A new commit on the lines must change the bundle signature and miss
    the cache, even though the call args are identical."""
    repo_root, _sha = repo_and_db
    invocations = {"n": 0}

    def fake_invoke(prompt, *, model, timeout_sec, anthropic_api_key=None, system_prompt=None):
        invocations["n"] += 1
        return _canned_rationale(purpose=f"v{invocations['n']}")

    with patch("whygraph.mcp_server.llm_subprocess.invoke_claude", side_effect=fake_invoke):
        first = mcp_server.whygraph_rationale_brief(
            path="f.py", line_start=1, line_end=2, min_score_pct=0.0
        )
        # Add a new commit that touches the same lines so blame attributes
        # the new SHA to at least one of them.
        (repo_root / "f.py").write_text("def foo():\n    return 99\n")
        _git(repo_root, "add", "f.py")
        _git(repo_root, "config", "user.email", "carol@example.com")
        _git(repo_root, "config", "user.name", "Carol")
        _git(repo_root, "commit", "-q", "-m", "bump")
        new_sha = _git_out(repo_root, "rev-parse", "HEAD")
        # Insert the new commit into the scan DB so blame's SHA isn't
        # treated as missing-from-DB.
        from whygraph.scan.db import Database

        with Database(repo_root / ".whygraph" / "whygraph.db") as db:
            db.upsert_commit(
                Commit(
                    sha=new_sha,
                    parent_shas=[],
                    author_name="Carol",
                    author_email="carol@example.com",
                    authored_at="2026-04-02T00:00:00+00:00",
                    committed_at="2026-04-02T00:00:00+00:00",
                    subject="bump",
                    body="",
                    files_changed=1,
                    insertions=1,
                    deletions=1,
                )
            )

        second = mcp_server.whygraph_rationale_brief(
            path="f.py", line_start=1, line_end=2, min_score_pct=0.0
        )
    assert invocations["n"] == 2
    assert first["cached"] is False
    assert second["cached"] is False
    assert first["purpose"] != second["purpose"]


def test_rationale_brief_invokes_claude_and_parses_json(repo_and_db) -> None:
    repo_root, sha = repo_and_db
    canned = json.dumps(
        {
            "purpose": "returns 1",
            "why": "added by initial commit",
            "constraints": ["foo must return an int"],
            "tradeoffs": [],
            "risks": ["any caller relying on return type 1 specifically"],
        }
    )
    captured: dict = {}

    def fake_invoke(prompt, *, model, timeout_sec, anthropic_api_key=None, system_prompt=None):
        captured["prompt"] = prompt
        captured["system_prompt"] = system_prompt
        captured["model"] = model
        return canned

    with patch("whygraph.mcp_server.llm_subprocess.invoke_claude", side_effect=fake_invoke):
        out = mcp_server.whygraph_rationale_brief(
            path="f.py", line_start=1, line_end=2, min_score_pct=0.0, model="m"
        )

    assert out["purpose"] == "returns 1"
    assert "constraints" in out and out["constraints"] == ["foo must return an int"]
    assert out["risks"] == ["any caller relying on return type 1 specifically"]
    assert out["model"] == "m"
    assert out["target"]["path"] == "f.py"
    assert out["evidence_count"]["commits"] >= 1
    # Confidence formula has 0.85 ceiling.
    assert 0.0 <= out["confidence"] <= 0.85
    assert "Produce the rationale" in captured["prompt"]
    # System prompt is sent via --system-prompt, not concatenated into stdin.
    assert captured["system_prompt"] is not None
    assert "analyst that explains why code exists" in captured["system_prompt"]
    assert "RAW JSON only" in captured["system_prompt"]
    assert "analyst" not in captured["prompt"]
    # Bundle is structured text now, not JSON. The target header and the
    # commits section must be present even when neighbours are empty.
    assert "Symbol:" in captured["prompt"]
    assert "Evidence:" in captured["prompt"]
    assert "Commits (newest first):" in captured["prompt"]
    # llm_description on the seed commit shows up under its labelled section.
    assert "LLM diff summary: added f.py with foo()" in captured["prompt"]


def test_rationale_prompt_includes_both_llm_summary_and_human_body(
    tmp_path: Path, monkeypatch
) -> None:
    """When a commit carries both llm_description and a body that passes
    the gate, the rendered prompt must show both under their labels — no
    winner-takes-all suppression."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "alice@example.com")
    _git(tmp_path, "config", "user.name", "Alice")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "f.py").write_text("def foo():\n    return 1\n")
    _git(tmp_path, "add", "f.py")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    sha = _git_out(tmp_path, "rev-parse", "HEAD")
    db_path = tmp_path / ".whygraph" / "whygraph.db"
    monkeypatch.setenv("WHYGRAPH_DB", str(db_path))
    monkeypatch.chdir(tmp_path)
    with Database(db_path) as db:
        db.upsert_commit(
            Commit(
                sha=sha,
                parent_shas=[],
                author_name="Alice",
                author_email="alice@example.com",
                authored_at="2026-04-01T00:00:00+00:00",
                committed_at="2026-04-01T00:00:00+00:00",
                subject="initial",
                body="The reason we did this is to satisfy the legacy auth path.",
                files_changed=1,
                insertions=2,
                deletions=0,
            )
        )
        # Sibling commit with zero-scored fields so the percentile gate
        # sits below the real commit's body/subject scores.
        db.upsert_commit(
            Commit(
                sha="0" * 40,
                parent_shas=[],
                author_name="Alice",
                author_email="alice@example.com",
                authored_at="2026-04-01T00:00:00+00:00",
                committed_at="2026-04-01T00:00:00+00:00",
                subject="empty",
                body="",
                files_changed=0,
                insertions=0,
                deletions=0,
            )
        )
        db.set_llm_description(sha, "added f.py with foo()", "haiku")
        cur = db._conn.cursor()
        # Force the real commit's scores above the gate; sibling stays
        # at the default 0 and pulls the threshold down.
        cur.execute(
            "UPDATE commits SET body_tfidf_score = 1.0, "
            "subject_tfidf_score = 1.0 WHERE sha = ?",
            (sha,),
        )
        db._conn.commit()

    captured: dict = {}

    def fake_invoke(prompt, *, model, timeout_sec, anthropic_api_key=None, system_prompt=None):
        captured["prompt"] = prompt
        return json.dumps(
            {
                "purpose": "p",
                "why": "w",
                "constraints": [],
                "tradeoffs": [],
                "risks": [],
            }
        )

    with patch("whygraph.mcp_server.llm_subprocess.invoke_claude", side_effect=fake_invoke):
        mcp_server.whygraph_rationale_brief(
            path="f.py", line_start=1, line_end=2, min_score_pct=0.0
        )

    prompt = captured["prompt"]
    # All three commit-narrative kinds appear under their labels.
    assert "LLM diff summary: added f.py with foo()" in prompt
    assert "Subject: initial" in prompt
    assert "Body:" in prompt
    assert "satisfy the legacy auth path" in prompt
    # Section header tells the model to read commits in recency order.
    assert "Commits (newest first):" in prompt


def test_rationale_brief_strips_json_fences(repo_and_db) -> None:
    fenced = (
        "```json\n"
        + json.dumps(
            {
                "purpose": "p",
                "why": "w",
                "constraints": [],
                "tradeoffs": [],
                "risks": [],
            }
        )
        + "\n```"
    )
    with patch(
        "whygraph.mcp_server.llm_subprocess.invoke_claude", return_value=fenced
    ):
        out = mcp_server.whygraph_rationale_brief(
            path="f.py", line_start=1, line_end=2, min_score_pct=0.0
        )
    assert out["purpose"] == "p"


def test_rationale_brief_raises_on_bad_json(repo_and_db) -> None:
    with patch(
        "whygraph.mcp_server.llm_subprocess.invoke_claude",
        return_value="not json at all",
    ):
        with pytest.raises(mcp_server.WhyGraphError, match="parse JSON"):
            mcp_server.whygraph_rationale_brief(
                path="f.py", line_start=1, line_end=2, min_score_pct=0.0
            )


def test_rationale_brief_raises_on_missing_required_field(repo_and_db) -> None:
    bad = json.dumps({"purpose": "p", "constraints": [], "tradeoffs": [], "risks": []})
    with patch("whygraph.mcp_server.llm_subprocess.invoke_claude", return_value=bad):
        with pytest.raises(mcp_server.WhyGraphError, match="why"):
            mcp_server.whygraph_rationale_brief(
                path="f.py", line_start=1, line_end=2, min_score_pct=0.0
            )


def test_score_confidence_signals_blend() -> None:
    evidence_full = [
        {
            "sha": "a" * 40,
            "narratives": {"llm_description": "x"},
            "all_authors": [{"login": "alice", "name": None, "email": None}],
            "prs": [{"number": 1}],
            "issues": [{"number": 7}],
        }
    ]
    high = mcp_server._score_confidence(
        evidence=evidence_full,
        constraints=["c"],
        risks=["r"],
    )
    low = mcp_server._score_confidence(
        evidence=[],
        constraints=[],
        risks=[],
    )
    assert high > low
    assert high <= mcp_server.CONFIDENCE_CEILING
    assert low == 0.0


def _ev(narrative_source: str | None) -> dict:
    """Build a minimal evidence item using the new `narratives` shape."""
    narratives = {narrative_source: "x"} if narrative_source else {}
    return {
        "sha": "a" * 40,
        "narratives": narratives,
        "all_authors": [{"login": "alice", "name": None, "email": None}],
        "prs": [],
        "issues": [],
    }


def test_score_confidence_tiers_llm_description_above_subject() -> None:
    llm = mcp_server._score_confidence(
        evidence=[_ev("llm_description")], constraints=[], risks=[]
    )
    sub = mcp_server._score_confidence(
        evidence=[_ev("subject")], constraints=[], risks=[]
    )
    blame = mcp_server._score_confidence(
        evidence=[_ev("git_blame_summary")], constraints=[], risks=[]
    )
    null_src = mcp_server._score_confidence(
        evidence=[_ev(None)], constraints=[], risks=[]
    )
    # Strict ordering by tier weight.
    assert llm > sub > blame > null_src
    # Empty narratives contribute nothing to commit-tier signals.
    # Authors still count (one author across the single item).
    assert null_src > 0  # authors signal still fires


def test_score_confidence_multi_narrative_uses_max_tier() -> None:
    """A commit with both llm_description and body should score at
    llm_description's tier (1.0), not double-count."""
    multi = {
        "sha": "a" * 40,
        "narratives": {"llm_description": "x", "body": "y"},
        "all_authors": [{"login": "alice", "name": None, "email": None}],
        "prs": [],
        "issues": [],
    }
    multi_score = mcp_server._score_confidence(
        evidence=[multi], constraints=[], risks=[]
    )
    llm_only_score = mcp_server._score_confidence(
        evidence=[_ev("llm_description")], constraints=[], risks=[]
    )
    assert multi_score == llm_only_score


def test_score_confidence_blame_only_evidence_caps_low() -> None:
    """A bundle of 10 blame-only items shouldn't approach the ceiling."""
    evidence = [_ev("git_blame_summary") for _ in range(10)]
    score = mcp_server._score_confidence(
        evidence=evidence, constraints=[], risks=[]
    )
    # Tier sum = 10 * 0.25 = 2.5 → num_commits_norm = 0.5
    # has_any_commits = 1.0, num_authors_norm = 0.333 (one alice).
    # raw = 0.20 + 0.10 + 0.067 + 0 + 0 + 0 = 0.367 → 0.367 * 0.85 ≈ 0.312
    assert 0.25 < score < 0.40


def test_score_confidence_saturates_at_5_strong_commits() -> None:
    five_strong = [_ev("llm_description") for _ in range(5)]
    ten_strong = [_ev("llm_description") for _ in range(10)]
    score_five = mcp_server._score_confidence(
        evidence=five_strong, constraints=["c"], risks=["r"]
    )
    score_ten = mcp_server._score_confidence(
        evidence=ten_strong, constraints=["c"], risks=["r"]
    )
    # num_commits_norm caps at 1.0 by 5 strong commits, so adding more
    # doesn't push the score higher.
    assert score_five == score_ten
