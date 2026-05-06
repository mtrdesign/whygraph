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
    assert "deterministic JSON producer" in captured["system_prompt"]
    assert "deterministic JSON producer" not in captured["prompt"]
    # Bundle includes callers/callees keys (path+lines target → empty lists,
    # but the keys must be present so the model knows the schema).
    payload = json.loads(captured["prompt"].split("\n\n", 1)[1])
    assert "callers" in payload and "callees" in payload
    assert "evidence" in payload and "target" in payload


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
            "narrative": "x",
            "narrative_source": "llm_description",
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
    return {
        "sha": "a" * 40,
        "narrative": "x" if narrative_source else None,
        "narrative_source": narrative_source,
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
    # Narrative-source=None contributes nothing to commit-tier signals.
    # Authors still count (one author across the single item).
    null_src_authors_only = mcp_server._score_confidence(
        evidence=[_ev(None)], constraints=[], risks=[]
    )
    assert null_src_authors_only > 0  # authors signal still fires


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
