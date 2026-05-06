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

    def fake_invoke(prompt, *, model, timeout_sec, anthropic_api_key=None):
        captured["prompt"] = prompt
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
