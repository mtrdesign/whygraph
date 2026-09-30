"""Tests for ``whygraph.project_setup`` and the HTTP agent-config writer.

Covers :func:`whygraph.project_setup.initialize_project` (extracted from
``whygraph init``) and the HTTP variant of the agent snippets in
:mod:`whygraph.agents` (plan 4.4 / 4.4.1): per-agent URL forms, the
VS Code ``servers`` key, refuse-on-parse-failure, backups, the
git-tracked check, detection, removal and the portal markers.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from whygraph import agents, assets, project_setup
from whygraph.cli.commands.init import init_cmd
from whygraph.hooks import HOOK_NAMES, SENTINEL
from whygraph.project_setup import (
    HttpMcp,
    PortalMarker,
    StdioMcp,
    initialize_project,
)

MCP = HttpMcp(slug="demo")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def _target(name: str) -> agents.AgentTarget:
    return agents.resolve_agent(name)


def _git_add(repo: Path, rel: str) -> None:
    subprocess.run(["git", "add", "--", rel], cwd=repo, check=True)


def _backups(repo: Path) -> list[Path]:
    d = repo / ".whygraph" / "backups"
    return sorted(d.iterdir()) if d.is_dir() else []


# ---- URL forms and snippets ----------------------------------------------


def test_url_forms_per_agent() -> None:
    assert MCP.url_for("claude") == "http://127.0.0.1:${WHYGRAPH_PORT:-8765}/mcp/demo"
    assert MCP.url_for("cursor") == "http://127.0.0.1:8765/mcp/demo"
    assert MCP.url_for("codex") == "http://127.0.0.1:8765/mcp/demo"
    assert MCP.url_for("vscode") == "http://127.0.0.1:${input:whygraph-port}/mcp/demo"
    assert MCP.url_for("copilot") == MCP.url_for("vscode")


def test_non_default_port_is_the_default_of_the_interpolation() -> None:
    mcp = HttpMcp(slug="demo", port=9000)
    assert mcp.url_for("claude") == "http://127.0.0.1:${WHYGRAPH_PORT:-9000}/mcp/demo"
    assert mcp.url_for("cursor") == "http://127.0.0.1:9000/mcp/demo"


def test_http_mcp_is_frozen_with_reserved_bearer_var() -> None:
    assert MCP.bearer_env_var is None
    with pytest.raises(Exception):
        MCP.slug = "other"  # type: ignore[misc]


def test_render_json_snippets() -> None:
    claude = json.loads(agents.render_http_snippet(_target("claude"), MCP))
    assert claude == {
        "mcpServers": {
            "whygraph": {
                "type": "http",
                "url": "http://127.0.0.1:${WHYGRAPH_PORT:-8765}/mcp/demo",
            }
        }
    }
    cursor = json.loads(agents.render_http_snippet(_target("cursor"), MCP))
    assert cursor == {
        "mcpServers": {"whygraph": {"url": "http://127.0.0.1:8765/mcp/demo"}}
    }
    vscode = json.loads(agents.render_http_snippet(_target("vscode"), MCP))
    assert vscode == {
        "inputs": [
            {
                "id": "whygraph-port",
                "type": "promptString",
                "description": "WhyGraph portal port",
                "default": "8765",
            }
        ],
        "servers": {
            "whygraph": {
                "type": "http",
                "url": "http://127.0.0.1:${input:whygraph-port}/mcp/demo",
            }
        },
    }


def test_render_codex_snippet() -> None:
    text = agents.render_http_snippet(_target("codex"), MCP)
    assert tomllib.loads(text) == {
        "mcp_servers": {"whygraph": {"url": "http://127.0.0.1:8765/mcp/demo"}}
    }


# ---- apply_http_entry: shapes on disk ------------------------------------


@pytest.mark.parametrize("name", ["claude", "cursor", "vscode", "codex"])
def test_apply_writes_the_rendered_shape(repo: Path, name: str) -> None:
    target = _target(name)
    out = agents.apply_http_entry(target, repo, MCP)

    assert out.status == "write"
    assert out.file == "/".join(target.relative_path)
    path = agents.config_path_for(target, repo)
    rendered = agents.render_http_snippet(target, MCP)
    if target.format == "json":
        assert json.loads(path.read_text()) == json.loads(rendered)
    else:
        assert tomllib.loads(path.read_text()) == tomllib.loads(rendered)
    assert _backups(repo) == []  # nothing to back up for a new file


def test_apply_preserves_other_servers_and_keys(repo: Path) -> None:
    path = repo / ".mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": 1}),
    )
    out = agents.apply_http_entry(_target("claude"), repo, MCP)
    data = json.loads(path.read_text())

    assert out.status == "overwrite"
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert data["keep"] == 1
    assert data["mcpServers"]["whygraph"]["type"] == "http"


def test_apply_replaces_a_stdio_entry_in_place(repo: Path) -> None:
    path = repo / ".cursor" / "mcp.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"mcpServers": {"whygraph": {"command": "whygraph-mcp"}}})
    )
    agents.apply_http_entry(_target("cursor"), repo, MCP)
    assert json.loads(path.read_text())["mcpServers"]["whygraph"] == {
        "url": "http://127.0.0.1:8765/mcp/demo"
    }


def test_apply_toml_preserves_other_servers(repo: Path) -> None:
    path = repo / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text('model = "x"\n\n[mcp_servers.other]\ncommand = "y"\n')
    out = agents.apply_http_entry(_target("codex"), repo, MCP)
    data = tomllib.loads(path.read_text())

    assert out.status == "overwrite"
    assert data["model"] == "x"
    assert data["mcp_servers"]["other"] == {"command": "y"}
    assert data["mcp_servers"]["whygraph"] == {"url": "http://127.0.0.1:8765/mcp/demo"}


def test_second_apply_is_a_skip_and_makes_no_backup(repo: Path) -> None:
    agents.apply_http_entry(_target("claude"), repo, MCP)
    path = repo / ".mcp.json"
    before = path.read_bytes()
    out = agents.apply_http_entry(_target("claude"), repo, MCP)

    assert out.status == "skip"
    assert path.read_bytes() == before
    assert _backups(repo) == []


def test_apply_does_not_reformat_an_equivalent_committed_file(repo: Path) -> None:
    """Same data, different formatting: left byte-identical (parsed compare)."""
    agents.apply_http_entry(_target("cursor"), repo, MCP)
    path = repo / ".cursor" / "mcp.json"
    compact = json.dumps(json.loads(path.read_text()))
    path.write_text(compact)
    out = agents.apply_http_entry(_target("cursor"), repo, MCP)
    assert out.status == "skip"
    assert path.read_text() == compact


# ---- VS Code migration ----------------------------------------------------


def test_upgraded_vscode_file_loses_stale_mcpservers_and_keeps_the_rest(
    repo: Path,
) -> None:
    path = repo / ".vscode" / "mcp.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "inputs": [{"id": "token", "type": "promptString"}],
                "mcpServers": {"whygraph": {"command": "whygraph-mcp"}},
                "servers": {"other": {"command": "z"}},
            }
        )
    )
    agents.apply_http_entry(_target("vscode"), repo, MCP)
    data = json.loads(path.read_text())

    assert "mcpServers" not in data
    assert data["servers"]["other"] == {"command": "z"}
    assert data["servers"]["whygraph"]["type"] == "http"
    ids = [i["id"] for i in data["inputs"]]
    assert ids == ["token", "whygraph-port"]


def test_vscode_keeps_mcpservers_when_other_servers_remain(repo: Path) -> None:
    path = repo / ".vscode" / "mcp.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {"mcpServers": {"whygraph": {"command": "w"}, "keepme": {"command": "k"}}}
        )
    )
    agents.apply_http_entry(_target("vscode"), repo, MCP)
    data = json.loads(path.read_text())
    assert data["mcpServers"] == {"keepme": {"command": "k"}}


def test_vscode_port_input_is_replaced_not_duplicated(repo: Path) -> None:
    agents.apply_http_entry(_target("vscode"), repo, MCP)
    agents.apply_http_entry(_target("vscode"), repo, HttpMcp(slug="demo", port=9000))
    data = json.loads((repo / ".vscode" / "mcp.json").read_text())
    assert [i["id"] for i in data["inputs"]] == ["whygraph-port"]
    assert data["inputs"][0]["default"] == "9000"


# ---- refuse, never overwrite ---------------------------------------------


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("claude", "{not json"),
        ("vscode", '{\n  // a comment\n  "servers": {}\n}\n'),
        ("cursor", "[1, 2]"),
        ("codex", "model = \n"),
        ("codex", '# my notes\n[mcp_servers.other]\ncommand = "y"\n'),
    ],
)
def test_unparseable_or_commented_files_are_refused_byte_identical(
    repo: Path, name: str, content: str
) -> None:
    target = _target(name)
    path = agents.config_path_for(target, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)

    out = agents.apply_http_entry(target, repo, MCP)

    assert out.status == "refused"
    assert out.reason
    assert out.snippet == agents.render_http_snippet(target, MCP)
    assert path.read_text() == content
    assert _backups(repo) == []


def test_non_table_servers_key_is_refused(repo: Path) -> None:
    path = repo / ".mcp.json"
    path.write_text('{"mcpServers": []}')
    out = agents.apply_http_entry(_target("claude"), repo, MCP)
    assert out.status == "refused"
    assert path.read_text() == '{"mcpServers": []}'


# ---- backup ---------------------------------------------------------------


def test_backup_precedes_every_rewrite(repo: Path) -> None:
    path = repo / ".mcp.json"
    original = json.dumps({"mcpServers": {"other": {"command": "x"}}})
    path.write_text(original)

    agents.apply_http_entry(_target("claude"), repo, MCP)

    backups = _backups(repo)
    assert len(backups) == 1
    assert backups[0].name.startswith(".mcp.json.")
    assert backups[0].read_text() == original


def test_backup_names_do_not_collide_across_same_named_files(repo: Path) -> None:
    for name in ("cursor", "vscode"):
        path = agents.config_path_for(_target(name), repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        agents.apply_http_entry(_target(name), repo, MCP)
    assert len(_backups(repo)) == 2


# ---- git-tracked targets --------------------------------------------------


def test_tracked_target_needs_confirmation_and_is_untouched(repo: Path) -> None:
    path = repo / ".mcp.json"
    original = json.dumps({"mcpServers": {"whygraph": {"command": "whygraph-mcp"}}})
    path.write_text(original)
    _git_add(repo, ".mcp.json")

    out = agents.apply_http_entry(_target("claude"), repo, MCP)

    assert out.status == "needs_confirmation"
    assert out.diff and "${WHYGRAPH_PORT:-8765}" in out.diff
    assert "-" in out.diff and "+" in out.diff
    assert path.read_text() == original
    assert _backups(repo) == []


def test_tracked_target_is_written_with_confirm_tracked(repo: Path) -> None:
    path = repo / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {"whygraph": {"command": "w"}}}))
    _git_add(repo, ".mcp.json")

    out = agents.apply_http_entry(
        _target("claude"), repo, MCP, confirm_tracked=[".mcp.json"]
    )

    assert out.status == "overwrite"
    assert json.loads(path.read_text())["mcpServers"]["whygraph"]["type"] == "http"
    assert len(_backups(repo)) == 1


def test_untracked_target_needs_no_confirmation(repo: Path) -> None:
    (repo / ".mcp.json").write_text("{}")
    out = agents.apply_http_entry(_target("claude"), repo, MCP)
    assert out.status == "overwrite"


def test_is_git_tracked_false_outside_a_repo(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x")
    assert agents.is_git_tracked(tmp_path, "f") is False


def test_dry_run_writes_and_backs_up_nothing(repo: Path) -> None:
    path = repo / ".mcp.json"
    path.write_text("{}")
    out = agents.apply_http_entry(_target("claude"), repo, MCP, dry_run=True)
    assert out.status == "overwrite"
    assert path.read_text() == "{}"
    assert _backups(repo) == []

    fresh = agents.apply_http_entry(_target("cursor"), repo, MCP, dry_run=True)
    assert fresh.status == "write"
    assert not (repo / ".cursor").exists()


# ---- detection ------------------------------------------------------------


def test_detect_finds_entries_in_all_four_files(repo: Path) -> None:
    (repo / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"whygraph": {"command": "whygraph-mcp"}}})
    )
    (repo / ".cursor").mkdir()
    (repo / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"whygraph": {"url": "http://x/mcp/a"}}})
    )
    (repo / ".vscode").mkdir()
    (repo / ".vscode" / "mcp.json").write_text(
        json.dumps(
            {
                "servers": {"whygraph": {"type": "http", "url": "u"}},
                "mcpServers": {"whygraph": {"command": "w"}},
            }
        )
    )
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text(
        '# team file\n[mcp_servers.whygraph]\ncommand = "whygraph-mcp"\n'
    )

    found = agents.detect_entries(repo)
    by_key = {(d.agent, d.key): d for d in found}

    assert set(by_key) == {
        ("claude", "mcpServers.whygraph"),
        ("cursor", "mcpServers.whygraph"),
        ("vscode", "servers.whygraph"),
        ("vscode", "mcpServers.whygraph"),
        ("codex", "mcp_servers.whygraph"),
    }
    assert by_key[("claude", "mcpServers.whygraph")].transport == "stdio"
    assert by_key[("cursor", "mcpServers.whygraph")].transport == "http"
    assert by_key[("vscode", "mcpServers.whygraph")].stale is True
    assert by_key[("vscode", "servers.whygraph")].stale is False
    assert by_key[("codex", "mcp_servers.whygraph")].transport == "stdio"


def test_detect_is_empty_for_a_clean_repo_and_skips_broken_files(repo: Path) -> None:
    assert agents.detect_entries(repo) == []
    (repo / ".mcp.json").write_text("{nope")
    assert agents.detect_entries(repo) == []


# ---- remove ---------------------------------------------------------------


def test_remove_strips_only_the_whygraph_key(repo: Path) -> None:
    path = repo / ".mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {"whygraph": {"command": "w"}, "other": {"command": "o"}},
                "keep": True,
            }
        )
    )
    out = agents.remove_entry(_target("claude"), repo)
    assert out.status == "overwrite"
    assert json.loads(path.read_text()) == {
        "mcpServers": {"other": {"command": "o"}},
        "keep": True,
    }
    assert len(_backups(repo)) == 1


def test_remove_from_toml_and_vscode_stale_key(repo: Path) -> None:
    toml_path = repo / ".codex" / "config.toml"
    toml_path.parent.mkdir()
    toml_path.write_text(
        'model = "m"\n[mcp_servers.whygraph]\nurl = "u"\n[mcp_servers.other]\ncommand = "o"\n'
    )
    agents.remove_entry(_target("codex"), repo)
    assert tomllib.loads(toml_path.read_text()) == {
        "model": "m",
        "mcp_servers": {"other": {"command": "o"}},
    }

    vs = repo / ".vscode" / "mcp.json"
    vs.parent.mkdir()
    vs.write_text(
        json.dumps(
            {
                "inputs": [1],
                "servers": {"whygraph": {"url": "u"}, "o": {"command": "c"}},
                "mcpServers": {"whygraph": {"command": "w"}},
            }
        )
    )
    agents.remove_entry(_target("vscode"), repo)
    assert json.loads(vs.read_text()) == {
        "inputs": [1],
        "servers": {"o": {"command": "c"}},
    }


def test_remove_skips_when_nothing_to_remove(repo: Path) -> None:
    assert agents.remove_entry(_target("claude"), repo).status == "skip"
    (repo / ".mcp.json").write_text('{"mcpServers": {"other": {}}}')
    assert agents.remove_entry(_target("claude"), repo).status == "skip"
    assert _backups(repo) == []


def test_remove_refuses_unparseable_and_respects_tracked(repo: Path) -> None:
    (repo / ".mcp.json").write_text("{bad")
    assert agents.remove_entry(_target("claude"), repo).status == "refused"

    cursor = repo / ".cursor" / "mcp.json"
    cursor.parent.mkdir()
    cursor.write_text(json.dumps({"mcpServers": {"whygraph": {"url": "u"}}}))
    _git_add(repo, ".cursor/mcp.json")
    out = agents.remove_entry(_target("cursor"), repo)
    assert out.status == "needs_confirmation"
    assert "whygraph" in json.loads(cursor.read_text())["mcpServers"]
    out = agents.remove_entry(
        _target("cursor"), repo, confirm_tracked=[".cursor/mcp.json"]
    )
    assert out.status == "overwrite"


# ---- initialize_project: parity with `init` ------------------------------


def _managed_hooks(repo: Path) -> set[str]:
    hooks_dir = repo / ".git" / "hooks"
    return {
        n
        for n in HOOK_NAMES
        if (hooks_dir / n).exists() and SENTINEL in (hooks_dir / n).read_text()
    }


def test_initialize_stdio_matches_what_init_writes(repo: Path) -> None:
    result = initialize_project(repo, agents=["claude"], hooks=True, mcp=StdioMcp())

    assert result.gitignore_added == ("whygraph.toml", ".whygraph/", ".codegraph/")
    gitignore = (repo / ".gitignore").read_text().splitlines()
    for entry in project_setup.GITIGNORE_ENTRIES:
        assert entry in gitignore
    assert _managed_hooks(repo) == set(HOOK_NAMES)
    assert result.hooks is not None and result.hooks_error is None
    assert json.loads((repo / ".mcp.json").read_text()) == {
        "mcpServers": {"whygraph": {"command": "whygraph-mcp"}}
    }
    assert (repo / ".claude" / "agents" / "planner.md").is_file()
    assert "<!-- BEGIN whygraph -->" in (repo / ".claude" / "CLAUDE.md").read_text()
    assert result.configured_agents == ("claude",)
    assert result.marker_written is False
    assert not (repo / ".whygraph" / "portal.json").exists()
    assert not (repo / ".whygraph" / "portal.env").exists()
    assert result.assets["claude"].written  # raw InstallResult is exposed


def test_initialize_without_agent_still_does_gitignore_and_hooks(repo: Path) -> None:
    result = initialize_project(repo, hooks=True, mcp=StdioMcp())
    assert result.agent_files == () and result.asset_files == ()
    assert _managed_hooks(repo) == set(HOOK_NAMES)


def test_initialize_hooks_selection_and_errors_are_best_effort(repo: Path) -> None:
    result = initialize_project(repo, hooks=["post-commit"], mcp=StdioMcp())
    assert _managed_hooks(repo) == {"post-commit"}

    bad = initialize_project(repo, hooks=["post-comit"], mcp=StdioMcp())
    assert bad.hooks is None and "post-comit" in (bad.hooks_error or "")
    assert result.hooks_error is None

    off = initialize_project(repo, hooks=[], mcp=StdioMcp())
    assert _managed_hooks(repo) == set()
    assert off.hooks is not None


def test_initialize_outside_a_git_repo_reports_hooks_error(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = initialize_project(plain, agents=["cursor"], mcp=StdioMcp())
    assert result.hooks_error and "not a git repository" in result.hooks_error
    assert (plain / ".cursor" / "mcp.json").exists()


def test_initialize_resolves_aliases_and_collapses_duplicates(repo: Path) -> None:
    result = initialize_project(
        repo, agents=["copilot", "vscode"], hooks=False, mcp=StdioMcp()
    )
    assert result.configured_agents == ("vscode",)
    assert len([f for f in result.agent_files]) == 1


def test_initialize_two_agents_write_two_files(repo: Path) -> None:
    result = initialize_project(
        repo, agents=["claude", "codex"], hooks=False, mcp=HttpMcp(slug="demo")
    )
    assert {f.file for f in result.agent_files} == {".mcp.json", ".codex/config.toml"}
    assert (repo / ".mcp.json").is_file()
    assert (repo / ".codex" / "config.toml").is_file()
    assert (repo / "AGENTS.md").is_file()
    assert result.configured_agents == ("claude", "codex")


def test_initialize_http_writes_urls_and_refused_is_reported(repo: Path) -> None:
    (repo / ".vscode").mkdir()
    (repo / ".vscode" / "mcp.json").write_text('{\n // c\n "servers": {}\n}\n')

    result = initialize_project(
        repo, agents=["claude", "vscode"], hooks=False, mcp=HttpMcp(slug="demo")
    )

    assert [f.file for f in result.refused] == [".vscode/mcp.json"]
    assert result.refused[0].snippet
    assert result.configured_agents == ("claude",)
    assert (repo / ".vscode" / "mcp.json").read_text().startswith("{\n // c")
    claude = json.loads((repo / ".mcp.json").read_text())
    assert claude["mcpServers"]["whygraph"]["url"].endswith("/mcp/demo")


# ---- force / dry_run -----------------------------------------------------


def _seed_1x_claude_asset(repo: Path) -> Path:
    stale = repo / ".claude" / "agents" / "planner.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("1.x CONTENT")
    return stale


def test_force_overwrites_a_1x_asset_and_dry_run_reports_overwrite(
    repo: Path,
) -> None:
    stale = _seed_1x_claude_asset(repo)

    plain = initialize_project(repo, agents=["claude"], hooks=False, mcp=StdioMcp())
    assert stale.read_text() == "1.x CONTENT"
    assert _status(plain, ".claude/agents/planner.md") == "skip"

    preview = initialize_project(
        repo, agents=["claude"], hooks=False, mcp=StdioMcp(), force=True, dry_run=True
    )
    assert preview.dry_run is True
    assert _status(preview, ".claude/agents/planner.md") == "overwrite"
    assert stale.read_text() == "1.x CONTENT"

    initialize_project(repo, agents=["claude"], hooks=False, mcp=StdioMcp(), force=True)
    assert stale.read_text() != "1.x CONTENT"


def _status(result, rel: str) -> str:
    return next(f.status for f in result.asset_files if f.file == rel)


def test_dry_run_touches_nothing(repo: Path) -> None:
    result = initialize_project(
        repo,
        agents=["claude", "vscode", "codex"],
        hooks=True,
        mcp=HttpMcp(slug="demo"),
        marker=PortalMarker("demo", 8765),
        dry_run=True,
    )

    assert {f.status for f in result.agent_files} == {"write"}
    assert any(f.status == "write" for f in result.asset_files)
    assert result.marker_written is False
    assert result.gitignore_added == () and result.hooks is None
    # Only the (empty) work tree of `git init` remains.
    assert [p.name for p in repo.iterdir()] == [".git"]


def test_dry_run_agrees_with_the_real_run(repo: Path) -> None:
    _seed_1x_claude_asset(repo)
    kwargs = dict(agents=["claude"], hooks=False, mcp=HttpMcp(slug="demo"), force=True)
    preview = initialize_project(repo, dry_run=True, **kwargs)
    real = initialize_project(repo, **kwargs)
    assert preview.asset_files == real.asset_files
    assert [(f.file, f.status) for f in preview.agent_files] == [
        (f.file, f.status) for f in real.agent_files
    ]


def test_install_assets_dry_run_classifies_without_writing(tmp_path: Path) -> None:
    target = agents.resolve_agent("vscode")
    dry = assets.install_assets(target, tmp_path, dry_run=True)
    assert dry.written and not any(tmp_path.iterdir())
    real = assets.install_assets(target, tmp_path)
    assert sorted(real.written) == sorted(dry.written)
    again = assets.install_assets(target, tmp_path, dry_run=True)
    assert again.skipped and again.overwritten  # files skipped, merge file overwritten


# ---- agent_actions and confirm_tracked -----------------------------------


def test_agent_actions_remove_and_migrate(repo: Path) -> None:
    (repo / ".cursor").mkdir()
    cursor = repo / ".cursor" / "mcp.json"
    cursor.write_text(
        json.dumps(
            {"mcpServers": {"whygraph": {"command": "w"}, "other": {"command": "o"}}}
        )
    )
    result = initialize_project(
        repo,
        agents=["claude"],
        hooks=False,
        mcp=HttpMcp(slug="demo"),
        agent_actions={"cursor": "remove"},
    )

    assert json.loads(cursor.read_text()) == {"mcpServers": {"other": {"command": "o"}}}
    assert result.configured_agents == ("claude",)
    assert not (repo / ".cursor" / "rules").exists()  # removal installs no assets
    assert {f.file for f in result.agent_files} == {".mcp.json", ".cursor/mcp.json"}


def test_agent_actions_can_override_an_agent_to_remove(repo: Path) -> None:
    (repo / ".mcp.json").write_text('{"mcpServers": {"whygraph": {"command": "w"}}}')
    result = initialize_project(
        repo,
        agents=["claude"],
        hooks=False,
        mcp=HttpMcp(slug="demo"),
        agent_actions={"claude": "remove"},
    )
    assert json.loads((repo / ".mcp.json").read_text()) == {"mcpServers": {}}
    assert result.configured_agents == ()
    assert not (repo / ".claude").exists()


def test_tracked_file_withholds_the_marker_until_confirmed(repo: Path) -> None:
    (repo / ".mcp.json").write_text('{"mcpServers": {"whygraph": {"command": "w"}}}')
    _git_add(repo, ".mcp.json")
    kwargs = dict(
        agents=["claude"],
        hooks=False,
        mcp=HttpMcp(slug="demo"),
        marker=PortalMarker("demo", 8765),
    )

    first = initialize_project(repo, **kwargs)
    assert [f.file for f in first.needs_confirmation] == [".mcp.json"]
    assert first.marker_written is False
    assert not (repo / ".whygraph" / "portal.json").exists()
    assert first.configured_agents == ()

    second = initialize_project(repo, confirm_tracked=[".mcp.json"], **kwargs)
    assert second.needs_confirmation == ()
    assert second.marker_written is True
    assert (repo / ".whygraph" / "portal.json").exists()
    assert second.configured_agents == ("claude",)


# ---- markers --------------------------------------------------------------


def test_marker_files_are_written_last_and_gitignored(repo: Path) -> None:
    (repo / ".whygraph" / "scan.lock").mkdir(parents=True)

    result = initialize_project(
        repo,
        agents=["claude"],
        hooks=False,
        mcp=HttpMcp(slug="demo-1", port=9001),
        marker=PortalMarker("demo-1", 9001),
    )

    assert result.marker_written is True
    assert json.loads((repo / ".whygraph" / "portal.json").read_text()) == {
        "slug": "demo-1",
        "port": 9001,
    }
    assert (repo / ".whygraph" / "portal.env").read_text() == "slug=demo-1\nport=9001\n"
    assert not (repo / ".whygraph" / "scan.lock").exists()
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".whygraph/portal.json"], cwd=repo
    )
    assert ignored.returncode == 0
    assert not list((repo / ".whygraph").glob("*.tmp"))


def test_stale_scan_lock_is_kept_without_a_marker(repo: Path) -> None:
    (repo / ".whygraph" / "scan.lock").mkdir(parents=True)
    initialize_project(repo, hooks=False, mcp=StdioMcp())
    assert (repo / ".whygraph" / "scan.lock").is_dir()


@pytest.mark.parametrize("failing", ["gitignore", "agent", "assets"])
def test_a_failure_in_any_step_leaves_no_marker(
    repo: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    def boom(*a, **k):
        raise OSError("disk full")

    if failing == "gitignore":
        monkeypatch.setattr(project_setup, "ensure_gitignore_entries", boom)
    elif failing == "agent":
        monkeypatch.setattr(agents, "apply_http_entry", boom)
    else:
        monkeypatch.setattr(assets, "install_assets", boom)

    with pytest.raises(OSError):
        initialize_project(
            repo,
            agents=["claude"],
            hooks=False,
            mcp=HttpMcp(slug="demo"),
            marker=PortalMarker("demo", 8765),
        )

    assert not (repo / ".whygraph" / "portal.json").exists()
    assert not (repo / ".whygraph" / "portal.env").exists()


def test_a_hooks_failure_is_a_warning_and_the_marker_is_still_written(
    tmp_path: Path,
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = initialize_project(
        plain, hooks=True, mcp=HttpMcp(slug="demo"), marker=PortalMarker("demo", 8765)
    )
    assert result.hooks_error
    assert result.marker_written is True


@pytest.mark.parametrize(
    ("slug", "port"),
    [
        ("Bad", 8765),
        ("a b", 8765),
        ("-x", 8765),
        ("$(touch x)", 8765),
        ("ok", 0),
        ("ok", 70000),
    ],
)
def test_portal_marker_validates_what_the_hook_helper_will_parse(
    slug: str, port: int
) -> None:
    with pytest.raises(ValueError):
        PortalMarker(slug, port)


# ---- CLI init stays marker-free ------------------------------------------


def test_cli_init_writes_no_portal_markers(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_db() -> Path:
        db = repo / ".whygraph" / "whygraph.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.touch()
        return db

    monkeypatch.setattr("whygraph.cli.commands.init._ensure_db_initialized", _fake_db)
    monkeypatch.setattr("whygraph.cli.commands.init._run_preflight", lambda: None)
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(
        init_cmd, ["--yes", "--agent", "claude"], catch_exceptions=False
    )

    assert result.exit_code == 0, result.output
    assert "Wrote whygraph MCP entry" in result.output
    assert not (repo / ".whygraph" / "portal.json").exists()
    assert not (repo / ".whygraph" / "portal.env").exists()
    # CLI init stays on the stdio shape until step 10.
    assert json.loads((repo / ".mcp.json").read_text()) == {
        "mcpServers": {"whygraph": {"command": "whygraph-mcp"}}
    }
