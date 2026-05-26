"""Tests for multi-agent ``whygraph init`` wiring.

Two layers of coverage:

* Direct unit tests on :mod:`whygraph.agents` — these stand alone and
  do not exercise the CLI or any DB code, so they're robust to the
  current mid-rewrite state of unrelated modules.
* CLI-flow tests via :class:`click.testing.CliRunner` that patch
  ``ensure_initialized`` so we don't depend on the DB layer here either.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from whygraph import agents
from whygraph.cli import main as whygraph_main


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


def test_render_snippet_json_shape() -> None:
    target = agents.resolve_agent("claude")
    snippet = agents.render_snippet(target)
    payload = json.loads(snippet)
    assert payload == {"mcpServers": {"whygraph": {"command": "whygraph-mcp"}}}


def test_render_snippet_toml_shape() -> None:
    target = agents.resolve_agent("codex")
    snippet = agents.render_snippet(target)
    assert "[mcp_servers.whygraph]" in snippet
    assert 'command = "whygraph-mcp"' in snippet


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


def test_is_write_supported_project_scoped() -> None:
    """All registered (project-scoped) agents are writeable — JSON or TOML."""
    assert agents.is_write_supported(agents.resolve_agent("claude"))
    assert agents.is_write_supported(agents.resolve_agent("cursor"))
    assert agents.is_write_supported(agents.resolve_agent("vscode"))
    assert agents.is_write_supported(agents.resolve_agent("codex"))


def test_write_snippet_creates_file(tmp_path: Path) -> None:
    target = agents.resolve_agent("claude")
    path = agents.write_snippet(target, tmp_path)
    assert path == tmp_path / ".mcp.json"
    data = json.loads(path.read_text())
    assert data == {"mcpServers": {"whygraph": {"command": "whygraph-mcp"}}}


def test_write_snippet_creates_nested_directory(tmp_path: Path) -> None:
    target = agents.resolve_agent("cursor")
    path = agents.write_snippet(target, tmp_path)
    assert path == tmp_path / ".cursor" / "mcp.json"
    assert path.exists()


def test_write_snippet_merges_with_existing_servers(tmp_path: Path) -> None:
    target = agents.resolve_agent("claude")
    existing = tmp_path / ".mcp.json"
    existing.write_text(
        json.dumps(
            {
                "mcpServers": {"other": {"command": "other-cmd"}},
                "unrelatedTopLevel": "keepme",
            }
        )
    )
    agents.write_snippet(target, tmp_path)
    data = json.loads(existing.read_text())
    assert data["mcpServers"]["other"] == {"command": "other-cmd"}
    assert data["mcpServers"]["whygraph"] == {"command": "whygraph-mcp"}
    assert data["unrelatedTopLevel"] == "keepme"


def test_write_snippet_replaces_old_whygraph_entry(tmp_path: Path) -> None:
    target = agents.resolve_agent("claude")
    existing = tmp_path / ".mcp.json"
    existing.write_text(
        json.dumps({"mcpServers": {"whygraph": {"command": "old-cmd"}}})
    )
    agents.write_snippet(target, tmp_path)
    data = json.loads(existing.read_text())
    assert data["mcpServers"]["whygraph"]["command"] == "whygraph-mcp"


def test_write_snippet_overwrites_malformed_json(tmp_path: Path) -> None:
    target = agents.resolve_agent("claude")
    existing = tmp_path / ".mcp.json"
    existing.write_text("{not valid json")
    agents.write_snippet(target, tmp_path)
    data = json.loads(existing.read_text())
    assert data == {"mcpServers": {"whygraph": {"command": "whygraph-mcp"}}}


def test_write_snippet_rejects_user_scope(tmp_path: Path) -> None:
    """The defensive guard still trips for synthetic user-scoped targets."""
    user_scoped = agents.AgentTarget(
        name="synthetic-user",
        aliases=(),
        relative_path=(".synthetic", "config.json"),
        scope="user",
        format="json",
        description="synthetic user-scoped target",
    )
    with pytest.raises(ValueError, match="user-scoped"):
        agents.write_snippet(user_scoped, tmp_path)


# ---------- TOML write_snippet tests (Codex path) ---------------------------


def test_write_snippet_toml_creates_file(tmp_path: Path) -> None:
    target = agents.resolve_agent("codex")
    path = agents.write_snippet(target, tmp_path)
    assert path == tmp_path / ".codex" / "config.toml"
    with path.open("rb") as f:
        data = tomllib.load(f)
    assert data == {"mcp_servers": {"whygraph": {"command": "whygraph-mcp"}}}


def test_write_snippet_toml_merges_with_existing_servers(tmp_path: Path) -> None:
    target = agents.resolve_agent("codex")
    existing = tmp_path / ".codex" / "config.toml"
    existing.parent.mkdir(parents=True)
    # Top-level scalar must come before any table header — TOML scopes
    # subsequent keys to the most recently opened table.
    existing.write_text(
        'unrelated_top_level = "keepme"\n\n'
        '[mcp_servers.other]\ncommand = "other-cmd"\n',
        encoding="utf-8",
    )
    agents.write_snippet(target, tmp_path)
    with existing.open("rb") as f:
        data = tomllib.load(f)
    assert data["mcp_servers"]["other"] == {"command": "other-cmd"}
    assert data["mcp_servers"]["whygraph"] == {"command": "whygraph-mcp"}
    assert data["unrelated_top_level"] == "keepme"


def test_write_snippet_toml_replaces_old_whygraph_entry(tmp_path: Path) -> None:
    target = agents.resolve_agent("codex")
    existing = tmp_path / ".codex" / "config.toml"
    existing.parent.mkdir(parents=True)
    existing.write_text(
        '[mcp_servers.whygraph]\ncommand = "old-cmd"\n',
        encoding="utf-8",
    )
    agents.write_snippet(target, tmp_path)
    with existing.open("rb") as f:
        data = tomllib.load(f)
    assert data["mcp_servers"]["whygraph"]["command"] == "whygraph-mcp"


def test_write_snippet_toml_overwrites_malformed(tmp_path: Path) -> None:
    target = agents.resolve_agent("codex")
    existing = tmp_path / ".codex" / "config.toml"
    existing.parent.mkdir(parents=True)
    existing.write_text("not = valid = toml", encoding="utf-8")
    agents.write_snippet(target, tmp_path)
    with existing.open("rb") as f:
        data = tomllib.load(f)
    assert data == {"mcp_servers": {"whygraph": {"command": "whygraph-mcp"}}}


# ---------- CLI flow tests --------------------------------------------------


@pytest.fixture
def stub_init(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Replace ``_ensure_db_initialized`` so CLI tests don't hit the DB layer.

    The current branch has the DB bootstrap chain mid-rewrite; this stub
    lets us exercise the new ``init_cmd`` flags without depending on it.
    """
    fake_db = tmp_path / ".whygraph" / "whygraph.db"

    def _fake() -> Path:
        fake_db.parent.mkdir(parents=True, exist_ok=True)
        fake_db.touch()
        return fake_db

    monkeypatch.setattr("whygraph.cli.commands.init._ensure_db_initialized", _fake)
    return fake_db


def _invoke_in(cwd: Path, *args: str):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=cwd):
        return runner.invoke(whygraph_main, list(args)), Path.cwd()


def test_init_list_agents_does_not_touch_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    called = {"n": 0}

    def _fake() -> Path:
        called["n"] += 1
        return tmp_path / "x.db"

    monkeypatch.setattr("whygraph.cli.commands.init._ensure_db_initialized", _fake)
    runner = CliRunner()
    result = runner.invoke(whygraph_main, ["init", "--list-agents"])
    assert result.exit_code == 0, result.output
    assert called["n"] == 0
    assert "claude" in result.output
    assert "cursor" in result.output
    assert "codex" in result.output


def test_init_no_flag_writes_no_agent_config(
    stub_init, tmp_path: Path
) -> None:
    result, cwd = _invoke_in(tmp_path, "init")
    assert result.exit_code == 0, result.output
    assert not (cwd / ".mcp.json").exists()
    assert not (cwd / ".cursor").exists()
    assert not (cwd / ".vscode").exists()
    assert "Initialized WhyGraph database" in result.output


def test_init_agent_claude_writes_mcp_json_and_installs_assets(
    stub_init, tmp_path: Path
) -> None:
    result, cwd = _invoke_in(tmp_path, "init", "--agent", "claude")
    assert result.exit_code == 0, result.output
    mcp_path = cwd / ".mcp.json"
    assert mcp_path.exists()
    data = json.loads(mcp_path.read_text())
    assert data["mcpServers"]["whygraph"]["command"] == "whygraph-mcp"
    assert "Wrote whygraph MCP entry" in result.output
    # Bundled assets land in .claude/.
    assert (cwd / ".claude" / "agents" / "planner.md").is_file()
    assert (cwd / ".claude" / "commands" / "rationale.md").is_file()
    assert (cwd / ".claude" / "skills" / "pre-edit" / "SKILL.md").is_file()
    assert "Installed assets for claude" in result.output


def test_init_agent_claude_no_install_assets_skips_dot_claude(
    stub_init, tmp_path: Path
) -> None:
    result, cwd = _invoke_in(
        tmp_path, "init", "--agent", "claude", "--no-install-assets"
    )
    assert result.exit_code == 0, result.output
    assert (cwd / ".mcp.json").exists()
    assert not (cwd / ".claude").exists()
    assert "Installed assets for" not in result.output


def test_init_agent_claude_force_overwrites_existing(
    stub_init, tmp_path: Path
) -> None:
    # Pre-seed a user edit at the install destination.
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        cwd = Path.cwd()
        (cwd / ".claude" / "agents").mkdir(parents=True)
        (cwd / ".claude" / "agents" / "planner.md").write_text("USER EDIT")
        result = runner.invoke(
            whygraph_main, ["init", "--agent", "claude", "--force"]
        )
        assert result.exit_code == 0, result.output
        text = (cwd / ".claude" / "agents" / "planner.md").read_text()
        assert text != "USER EDIT"


def test_init_agent_claude_default_skips_existing(
    stub_init, tmp_path: Path
) -> None:
    """Without ``--force``, an existing .claude file is left alone."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        cwd = Path.cwd()
        (cwd / ".claude" / "agents").mkdir(parents=True)
        (cwd / ".claude" / "agents" / "planner.md").write_text("USER EDIT")
        result = runner.invoke(whygraph_main, ["init", "--agent", "claude"])
        assert result.exit_code == 0, result.output
        text = (cwd / ".claude" / "agents" / "planner.md").read_text()
        assert text == "USER EDIT"


def test_init_agent_cursor_writes_mcp_json_and_installs_rules(
    stub_init, tmp_path: Path
) -> None:
    """Cursor gets ``.cursor/mcp.json`` plus the bundled MDC rule tree.

    Confirms the generalized asset installer fires for any agent whose
    ``has_assets`` is True — Claude-Code-specific assets do not bleed
    into the Cursor target.
    """
    result, cwd = _invoke_in(tmp_path, "init", "--agent", "cursor")
    assert result.exit_code == 0, result.output
    cursor_path = cwd / ".cursor" / "mcp.json"
    assert cursor_path.exists()
    data = json.loads(cursor_path.read_text())
    assert data["mcpServers"]["whygraph"]["command"] == "whygraph-mcp"
    # Bundled MDC rules land in .cursor/rules/.
    assert (cwd / ".cursor" / "rules" / "whygraph-pre-edit.mdc").is_file()
    assert (cwd / ".cursor" / "rules" / "whygraph-ask-why.mdc").is_file()
    # Slash commands and subagents land in their respective subdirs.
    assert (cwd / ".cursor" / "commands" / "whygraph-plan.md").is_file()
    assert (cwd / ".cursor" / "agents" / "planner.md").is_file()
    assert "Installed assets for cursor" in result.output
    # No Claude-Code assets bleed into the Cursor target.
    assert not (cwd / ".claude").exists()


def test_init_agent_vscode_writes_mcp_and_installs_full_tree(
    stub_init, tmp_path: Path
) -> None:
    """VS Code gets ``.vscode/mcp.json`` plus the bundled ``.github/`` asset tree.

    Confirms the generalized asset installer fires for any agent whose
    ``has_assets`` is True — and that the ``.github/`` destination is
    correctly anchored under the project root.
    """
    result, cwd = _invoke_in(tmp_path, "init", "--agent", "vscode")
    assert result.exit_code == 0, result.output
    # MCP wiring lands in .vscode/mcp.json (writeable).
    mcp_path = cwd / ".vscode" / "mcp.json"
    assert mcp_path.exists()
    data = json.loads(mcp_path.read_text())
    assert data["mcpServers"]["whygraph"]["command"] == "whygraph-mcp"
    # Bundled assets land under .github/.
    assert (cwd / ".github" / "copilot-instructions.md").is_file()
    assert (
        cwd / ".github" / "instructions" / "pre-edit.instructions.md"
    ).is_file()
    assert (cwd / ".github" / "prompts" / "whygraph-plan.prompt.md").is_file()
    assert (cwd / ".github" / "agents" / "planner.agent.md").is_file()
    assert "Installed assets for vscode" in result.output
    # No other agents' assets bleed in.
    assert not (cwd / ".claude").exists()
    assert not (cwd / ".cursor").exists()


def test_init_agent_copilot_aliases_to_vscode(stub_init, tmp_path: Path) -> None:
    """The ``copilot`` alias resolves to ``vscode`` and installs the same tree."""
    result, cwd = _invoke_in(tmp_path, "init", "--agent", "copilot")
    assert result.exit_code == 0, result.output
    assert (cwd / ".vscode" / "mcp.json").exists()
    # Alias still routes to the vscode asset tree.
    assert (cwd / ".github" / "copilot-instructions.md").is_file()
    assert not (cwd / ".claude").exists()


def test_init_agent_vscode_merges_existing_copilot_instructions(
    stub_init, tmp_path: Path
) -> None:
    """User-authored copilot-instructions.md is preserved; WhyGraph block appends."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        cwd = Path.cwd()
        (cwd / ".github").mkdir()
        (cwd / ".github" / "copilot-instructions.md").write_text(
            "# Our team rules\n\nWrite tests for everything.\n",
            encoding="utf-8",
        )
        result = runner.invoke(whygraph_main, ["init", "--agent", "vscode"])
        assert result.exit_code == 0, result.output
        merged = (cwd / ".github" / "copilot-instructions.md").read_text(
            encoding="utf-8"
        )
        # User content preserved verbatim.
        assert "# Our team rules" in merged
        assert "Write tests for everything." in merged
        # WhyGraph block appended after user content.
        assert "<!-- BEGIN whygraph -->" in merged
        assert "<!-- END whygraph -->" in merged
        # User content comes first.
        assert merged.find("Our team rules") < merged.find(
            "<!-- BEGIN whygraph -->"
        )


def test_init_agent_codex_writes_and_installs_full_tree(
    stub_init, tmp_path: Path
) -> None:
    """Codex gets project-scoped ``.codex/config.toml`` plus the bundled tree.

    The TOML MCP config writes the ``[mcp_servers.whygraph]`` table at
    ``.codex/config.toml``. The asset tree lands at the repo root —
    ``AGENTS.md`` (append-merged) plus the ``.codex/agents/*.toml``
    subagents. No user-global writes occur (the project-only rule).
    """
    result, cwd = _invoke_in(tmp_path, "init", "--agent", "codex")
    assert result.exit_code == 0, result.output
    # MCP config lands at project-scoped .codex/config.toml.
    config_path = cwd / ".codex" / "config.toml"
    assert config_path.exists()
    with config_path.open("rb") as f:
        config_data = tomllib.load(f)
    assert (
        config_data["mcp_servers"]["whygraph"]["command"] == "whygraph-mcp"
    )
    # AGENTS.md at the repo root has the WhyGraph block (append-merged).
    agents_md = cwd / "AGENTS.md"
    assert agents_md.is_file()
    body = agents_md.read_text(encoding="utf-8")
    assert "<!-- BEGIN whygraph -->" in body
    assert "<!-- END whygraph -->" in body
    # Subagents land under .codex/agents/.
    assert (cwd / ".codex" / "agents" / "planner.toml").is_file()
    assert "Installed assets for codex" in result.output
    # No other agents' assets bleed in.
    assert not (cwd / ".claude").exists()
    assert not (cwd / ".cursor").exists()


def test_init_agent_codex_merges_existing_agents_md(
    stub_init, tmp_path: Path
) -> None:
    """User-authored AGENTS.md is preserved; the WhyGraph block appends."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        cwd = Path.cwd()
        (cwd / "AGENTS.md").write_text(
            "# Our team rules\n\nWrite tests for everything.\n",
            encoding="utf-8",
        )
        result = runner.invoke(whygraph_main, ["init", "--agent", "codex"])
        assert result.exit_code == 0, result.output
        merged = (cwd / "AGENTS.md").read_text(encoding="utf-8")
        # User content preserved verbatim.
        assert "# Our team rules" in merged
        assert "Write tests for everything." in merged
        # WhyGraph block appended after user content.
        assert "<!-- BEGIN whygraph -->" in merged
        assert "<!-- END whygraph -->" in merged
        assert merged.find("Our team rules") < merged.find(
            "<!-- BEGIN whygraph -->"
        )


def test_init_agent_claude_with_print_skips_mcp_write_but_installs_assets(
    stub_init, tmp_path: Path
) -> None:
    """``--print`` suppresses the MCP write; asset install runs normally."""
    result, cwd = _invoke_in(tmp_path, "init", "--agent", "claude", "--print")
    assert result.exit_code == 0, result.output
    assert not (cwd / ".mcp.json").exists()
    # JSON snippet still printed for the user to paste.
    assert '"whygraph-mcp"' in result.output
    # Assets are governed by --no-install-assets, not --print.
    assert (cwd / ".claude" / "agents" / "planner.md").is_file()


def test_init_unknown_agent_errors(stub_init, tmp_path: Path) -> None:
    result, _ = _invoke_in(tmp_path, "init", "--agent", "emacs")
    assert result.exit_code != 0
    # Click's Choice produces a usage error mentioning the bad value.
    assert "emacs" in result.output or "Invalid value" in result.output
