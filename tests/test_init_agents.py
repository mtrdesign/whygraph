"""Tests for multi-agent wiring: :mod:`whygraph.agents` + ``initialize_project``.

Two layers of coverage:

* Direct unit tests on the agent registry in :mod:`whygraph.agents`.
* Per-agent wiring through :func:`whygraph.project_setup.initialize_project`
  (what the portal's Initialize runs): the HTTP MCP entry each agent
  gets, and the bundled asset tree it installs. The 1.x ``whygraph
  init`` command these used to drive was removed in 2.0.0.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from whygraph import agents
from whygraph.project_setup import HttpMcp, InitializeResult, initialize_project

MCP = HttpMcp(slug="demo")


# ---------- agents.py direct tests ------------------------------------------


def test_known_agent_names_includes_canonical_and_aliases() -> None:
    names = set(agents.known_agent_names())
    assert {"claude", "cursor", "vscode", "copilot", "codex"} == names


def test_resolve_agent_canonical() -> None:
    target = agents.resolve_agent("claude")
    assert target.name == "claude"
    assert target.scope == "project"
    assert target.format == "json"


def test_resolve_agent_alias_copilot_to_vscode() -> None:
    target = agents.resolve_agent("copilot")
    assert target.name == "vscode"


def test_resolve_agent_case_insensitive() -> None:
    assert agents.resolve_agent("CLAUDE").name == "claude"
    assert agents.resolve_agent("Cursor").name == "cursor"


def test_resolve_agent_unknown_raises() -> None:
    with pytest.raises(agents.UnknownAgentError):
        agents.resolve_agent("emacs")


def test_config_path_for_project_anchored_at_root(tmp_path: Path) -> None:
    target = agents.resolve_agent("cursor")
    path = agents.config_path_for(target, tmp_path)
    assert path == tmp_path / ".cursor" / "mcp.json"


def test_config_path_for_user_anchored_at_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``config_path_for`` anchors user-scoped targets at ``Path.home()``.

    No registered agent is user-scoped anymore (Claude Desktop was
    dropped in v1), so this exercises the ``else`` branch via a
    synthetic target.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    user_scoped = agents.AgentTarget(
        name="synthetic-user",
        aliases=(),
        relative_path=(".synthetic", "config.json"),
        scope="user",
        format="json",
        description="synthetic user-scoped target",
    )
    expected = Path.home() / ".synthetic" / "config.json"
    assert agents.config_path_for(user_scoped, tmp_path) == expected


# ---------- per-agent wiring through initialize_project ---------------------


def _init(root: Path, *names: str, force: bool = False) -> InitializeResult:
    """Wire ``names`` into ``root`` the way the portal's Initialize does."""
    return initialize_project(
        root, agents=list(names), hooks=False, mcp=MCP, marker=None, force=force
    )


def test_no_agent_writes_no_agent_config(tmp_path: Path) -> None:
    result = _init(tmp_path)
    assert result.agent_files == ()
    assert not (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / ".cursor").exists()
    assert not (tmp_path / ".vscode").exists()


def test_gitignore_entries_are_added(tmp_path: Path) -> None:
    """Initialize keeps the user config + generated caches out of git."""
    result = _init(tmp_path)
    assert set(result.gitignore_added) == {"whygraph.toml", ".whygraph/", ".codegraph/"}
    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in ("whygraph.toml", ".whygraph/", ".codegraph/"):
        assert entry in lines
    # The committable example stays trackable.
    assert "whygraph.example.toml" not in lines


def test_gitignore_is_idempotent(tmp_path: Path) -> None:
    """Pre-existing entries are not duplicated and re-runs are no-ops."""
    (tmp_path / ".gitignore").write_text(
        "node_modules/\n.whygraph/\n", encoding="utf-8"
    )
    _init(tmp_path)
    again = _init(tmp_path)
    assert again.gitignore_added == ()
    body = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    # User content preserved; already-present entry not duplicated.
    assert "node_modules/" in body
    assert body.count(".whygraph/") == 1
    # The remaining entries were appended.
    assert "whygraph.toml" in body.splitlines()
    assert ".codegraph/" in body.splitlines()


def test_claude_writes_mcp_json_and_installs_assets(tmp_path: Path) -> None:
    result = _init(tmp_path, "claude")
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert data["mcpServers"]["whygraph"] == {
        "type": "http",
        "url": "http://127.0.0.1:${WHYGRAPH_PORT:-8765}/mcp/demo",
    }
    assert result.configured_agents == ("claude",)
    # Bundled assets land in .claude/.
    assert (tmp_path / ".claude" / "agents" / "planner.md").is_file()
    assert (tmp_path / ".claude" / "skills" / "rationale" / "SKILL.md").is_file()
    assert (tmp_path / ".claude" / "skills" / "pre-edit" / "SKILL.md").is_file()
    # CodeGraph guidance is merged into .claude/CLAUDE.md (forcing block).
    claude_md = (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "<!-- BEGIN whygraph -->" in claude_md
    assert "## CodeGraph" in claude_md
    assert result.assets["claude"].written


def test_claude_force_overwrites_existing(tmp_path: Path) -> None:
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    (tmp_path / ".claude" / "agents" / "planner.md").write_text("USER EDIT")
    _init(tmp_path, "claude", force=True)
    text = (tmp_path / ".claude" / "agents" / "planner.md").read_text()
    assert text != "USER EDIT"


def test_claude_default_skips_existing(tmp_path: Path) -> None:
    """Without ``force``, an existing .claude file is left alone."""
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    (tmp_path / ".claude" / "agents" / "planner.md").write_text("USER EDIT")
    _init(tmp_path, "claude")
    text = (tmp_path / ".claude" / "agents" / "planner.md").read_text()
    assert text == "USER EDIT"


def _assert_merged(path: Path) -> None:
    merged = path.read_text(encoding="utf-8")
    # User content preserved verbatim.
    assert "# Our team rules" in merged
    assert "Write tests for everything." in merged
    # WhyGraph block appended after user content.
    assert "<!-- BEGIN whygraph -->" in merged
    assert "<!-- END whygraph -->" in merged
    assert merged.find("Our team rules") < merged.find("<!-- BEGIN whygraph -->")


def _seed_rules(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Our team rules\n\nWrite tests for everything.\n", encoding="utf-8"
    )


def test_claude_merges_existing_claude_md(tmp_path: Path) -> None:
    """User-authored .claude/CLAUDE.md is preserved; the WhyGraph block appends."""
    _seed_rules(tmp_path / ".claude" / "CLAUDE.md")
    _init(tmp_path, "claude")
    _assert_merged(tmp_path / ".claude" / "CLAUDE.md")
    assert "## CodeGraph" in (tmp_path / ".claude" / "CLAUDE.md").read_text()


def test_cursor_writes_mcp_json_and_installs_rules(tmp_path: Path) -> None:
    """Cursor gets ``.cursor/mcp.json`` (literal port) plus the MDC rule tree.

    Claude-Code-specific assets do not bleed into the Cursor target.
    """
    _init(tmp_path, "cursor")
    data = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    assert data["mcpServers"]["whygraph"] == {"url": "http://127.0.0.1:8765/mcp/demo"}
    # Bundled MDC rules land in .cursor/rules/.
    assert (tmp_path / ".cursor" / "rules" / "whygraph-pre-edit.mdc").is_file()
    assert (tmp_path / ".cursor" / "rules" / "whygraph-ask-why.mdc").is_file()
    # The CodeGraph forcing rule lands and is always-applied.
    codegraph_rule = (
        tmp_path / ".cursor" / "rules" / "whygraph-codegraph.mdc"
    ).read_text(encoding="utf-8")
    assert "alwaysApply: true" in codegraph_rule
    # Slash commands and subagents land in their respective subdirs.
    assert (tmp_path / ".cursor" / "commands" / "whygraph-plan.md").is_file()
    assert (tmp_path / ".cursor" / "agents" / "planner.md").is_file()
    assert not (tmp_path / ".claude").exists()


def test_vscode_writes_mcp_and_installs_full_tree(tmp_path: Path) -> None:
    """VS Code gets ``.vscode/mcp.json`` (``servers`` + port input) and ``.github/``."""
    _init(tmp_path, "vscode")
    data = json.loads((tmp_path / ".vscode" / "mcp.json").read_text())
    assert data["servers"]["whygraph"] == {
        "type": "http",
        "url": "http://127.0.0.1:${input:whygraph-port}/mcp/demo",
    }
    assert "mcpServers" not in data
    assert data["inputs"][0]["id"] == "whygraph-port"
    # Bundled assets land under .github/.
    assert (tmp_path / ".github" / "copilot-instructions.md").is_file()
    assert (
        tmp_path / ".github" / "instructions" / "pre-edit.instructions.md"
    ).is_file()
    assert (tmp_path / ".github" / "prompts" / "whygraph-plan.prompt.md").is_file()
    assert (tmp_path / ".github" / "agents" / "planner.agent.md").is_file()
    # No other agents' assets bleed in.
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".cursor").exists()


def test_copilot_aliases_to_vscode(tmp_path: Path) -> None:
    """The ``copilot`` alias resolves to ``vscode`` and installs the same tree."""
    result = _init(tmp_path, "copilot")
    assert result.configured_agents == ("vscode",)
    assert (tmp_path / ".vscode" / "mcp.json").exists()
    assert (tmp_path / ".github" / "copilot-instructions.md").is_file()
    assert not (tmp_path / ".claude").exists()


def test_vscode_merges_existing_copilot_instructions(tmp_path: Path) -> None:
    """User-authored copilot-instructions.md is preserved; WhyGraph block appends."""
    _seed_rules(tmp_path / ".github" / "copilot-instructions.md")
    _init(tmp_path, "vscode")
    _assert_merged(tmp_path / ".github" / "copilot-instructions.md")


def test_codex_writes_and_installs_full_tree(tmp_path: Path) -> None:
    """Codex gets project-scoped ``.codex/config.toml`` plus the bundled tree.

    The ``[mcp_servers.whygraph]`` table carries a literal URL. The asset
    tree lands at the repo root - ``AGENTS.md`` (append-merged) plus the
    ``.codex/agents/*.toml`` subagents. No user-global writes occur.
    """
    _init(tmp_path, "codex")
    with (tmp_path / ".codex" / "config.toml").open("rb") as f:
        config_data = tomllib.load(f)
    assert config_data["mcp_servers"]["whygraph"] == {
        "url": "http://127.0.0.1:8765/mcp/demo"
    }
    body = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "<!-- BEGIN whygraph -->" in body
    assert "<!-- END whygraph -->" in body
    assert (tmp_path / ".codex" / "agents" / "planner.toml").is_file()
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".cursor").exists()


def test_codex_merges_existing_agents_md(tmp_path: Path) -> None:
    """User-authored AGENTS.md is preserved; the WhyGraph block appends."""
    _seed_rules(tmp_path / "AGENTS.md")
    _init(tmp_path, "codex")
    _assert_merged(tmp_path / "AGENTS.md")


def test_unknown_agent_raises(tmp_path: Path) -> None:
    with pytest.raises(agents.UnknownAgentError):
        _init(tmp_path, "emacs")
    assert not (tmp_path / ".gitignore").exists()  # nothing was written
