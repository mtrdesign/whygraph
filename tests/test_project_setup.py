"""Tests for ``whygraph.project_setup`` and the HTTP agent-config writer.

Covers :func:`whygraph.project_setup.initialize_project` (extracted from
``whygraph init``) and the HTTP variant of the agent snippets in
:mod:`whygraph.agents` (plan 4.4 / 4.4.1): per-agent URL forms, the
VS Code ``servers`` key, refuse-on-parse-failure, backups, the
git-tracked check, detection, removal and the portal markers.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from whygraph import agents, assets, project_setup
from whygraph.core.safe_paths import UnsafePathError
from whygraph.hooks import HOOK_NAMES, SENTINEL
from whygraph.project_setup import (
    HttpMcp,
    PortalMarker,
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
            },
            "codegraph": {
                "type": "http",
                "url": "http://127.0.0.1:${WHYGRAPH_PORT:-8765}/mcp/demo/code",
            },
        }
    }
    cursor = json.loads(agents.render_http_snippet(_target("cursor"), MCP))
    assert cursor == {
        "mcpServers": {
            "whygraph": {"url": "http://127.0.0.1:8765/mcp/demo"},
            "codegraph": {"url": "http://127.0.0.1:8765/mcp/demo/code"},
        }
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
            },
            "codegraph": {
                "type": "http",
                "url": "http://127.0.0.1:${input:whygraph-port}/mcp/demo/code",
            },
        },
    }


def test_render_codex_snippet() -> None:
    text = agents.render_http_snippet(_target("codex"), MCP)
    assert tomllib.loads(text) == {
        "mcp_servers": {
            "whygraph": {
                "url": "http://127.0.0.1:8765/mcp/demo",
                "tool_timeout_sec": 120,
            },
            "codegraph": {
                "url": "http://127.0.0.1:8765/mcp/demo/code",
                "tool_timeout_sec": 120,
            },
        }
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
    assert data["mcp_servers"]["whygraph"] == {
        "url": "http://127.0.0.1:8765/mcp/demo",
        "tool_timeout_sec": 120,
    }
    assert data["mcp_servers"]["codegraph"]["url"].endswith("/mcp/demo/code")


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


# ---- the codegraph entry ---------------------------------------------------

FOREIGN_CODEGRAPH = {
    "type": "stdio",
    "command": "codegraph",
    "args": ["serve", "--mcp"],
}


def test_code_url_follows_the_whygraph_port_form() -> None:
    for name in ("claude", "cursor", "vscode", "codex"):
        assert MCP.code_url_for(name) == MCP.url_for(name) + "/code"


@pytest.mark.parametrize(
    ("entry", "ours"),
    [
        ({"url": "http://127.0.0.1:8765/mcp/demo/code"}, True),
        ({"type": "http", "url": "http://h:${WHYGRAPH_PORT:-1}/mcp/a-b/code"}, True),
        ({"url": "http://127.0.0.1:8765/mcp/demo"}, False),
        ({"url": "http://127.0.0.1:8765/mcp/demo/code", "command": "x"}, False),
        ({"url": "http://127.0.0.1:8765/mcp/Demo/code"}, False),
        ({"url": "ftp://127.0.0.1/mcp/demo/code"}, False),
        ({"url": 5}, False),
        (FOREIGN_CODEGRAPH, False),
        ("text", False),
    ],
)
def test_codegraph_ownership_rule(entry: object, ours: bool) -> None:
    assert agents._is_whygraph_codegraph(entry) is ours


def test_one_merge_one_backup_one_diff_for_both_entries(repo: Path) -> None:
    path = repo / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    out = agents.apply_http_entry(_target("claude"), repo, MCP)
    assert out.status == "overwrite" and out.diff
    assert '"whygraph"' in out.diff and '"codegraph"' in out.diff
    assert len(_backups(repo)) == 1


def test_a_tracked_file_needs_one_confirmation_for_both(repo: Path) -> None:
    (repo / ".mcp.json").write_text("{}\n")
    _git_add(repo, ".mcp.json")
    out = agents.apply_http_entry(_target("claude"), repo, MCP)
    assert out.status == "needs_confirmation"
    assert '"codegraph"' in (out.diff or "")


def test_vscode_has_one_port_input_for_both_entries(repo: Path) -> None:
    agents.apply_http_entry(_target("vscode"), repo, MCP)
    data = json.loads((repo / ".vscode" / "mcp.json").read_text())
    assert [i["id"] for i in data["inputs"]] == ["whygraph-port"]
    assert {"whygraph", "codegraph"} <= set(data["servers"])
    assert "${input:whygraph-port}" in data["servers"]["codegraph"]["url"]


def test_a_foreign_codegraph_entry_stays_byte_identical(repo: Path) -> None:
    path = repo / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {"codegraph": FOREIGN_CODEGRAPH}}))
    out = agents.apply_http_entry(_target("claude"), repo, MCP)
    data = json.loads(path.read_text())

    assert data["mcpServers"]["codegraph"] == FOREIGN_CODEGRAPH
    assert "whygraph" in data["mcpServers"]
    assert out.foreign_codegraph == "a local command (stdio) server"
    assert "--mcp" not in (out.foreign_codegraph or "")  # never command / args
    # Still reported when the second run is a skip.
    again = agents.apply_http_entry(_target("claude"), repo, MCP)
    assert again.status == "skip" and again.foreign_codegraph is not None


def test_replace_foreign_swaps_it_for_ours(repo: Path) -> None:
    path = repo / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {"codegraph": FOREIGN_CODEGRAPH}}))
    out = agents.apply_http_entry(_target("claude"), repo, MCP, replace_foreign=True)
    assert out.foreign_codegraph is None
    assert json.loads(path.read_text())["mcpServers"]["codegraph"] == {
        "type": "http",
        "url": MCP.code_url_for("claude"),
    }


def test_update_mode_never_adds_and_skip_mode_leaves_the_key(repo: Path) -> None:
    path = repo / ".cursor" / "mcp.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"mcpServers": {"whygraph": {"url": "http://x/mcp/demo"}}})
    )
    agents.apply_http_entry(_target("cursor"), repo, MCP, codegraph="update")
    assert "codegraph" not in json.loads(path.read_text())["mcpServers"]

    agents.apply_http_entry(_target("cursor"), repo, MCP)
    moved = HttpMcp(slug="demo", port=9100)
    agents.apply_http_entry(_target("cursor"), repo, moved, codegraph="update")
    servers = json.loads(path.read_text())["mcpServers"]
    assert servers["codegraph"]["url"] == "http://127.0.0.1:9100/mcp/demo/code"

    agents.apply_http_entry(_target("cursor"), repo, MCP, codegraph="skip")
    servers = json.loads(path.read_text())["mcpServers"]
    assert servers["codegraph"]["url"] == "http://127.0.0.1:9100/mcp/demo/code"


def test_remove_pops_our_codegraph_but_not_a_foreign_one(repo: Path) -> None:
    agents.apply_http_entry(_target("claude"), repo, MCP)
    agents.remove_entry(_target("claude"), repo)
    assert json.loads((repo / ".mcp.json").read_text()) == {"mcpServers": {}}

    (repo / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"codegraph": FOREIGN_CODEGRAPH}})
    )
    out = agents.remove_entry(_target("claude"), repo)
    assert out.status == "skip"
    assert json.loads((repo / ".mcp.json").read_text())["mcpServers"] == {
        "codegraph": FOREIGN_CODEGRAPH
    }


def test_detect_entries_ignores_codegraph_and_detect_codegraph_reports_it(
    repo: Path,
) -> None:
    agents.apply_http_entry(_target("codex"), repo, MCP)
    (repo / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"codegraph": FOREIGN_CODEGRAPH}})
    )
    assert [(e.agent, e.key) for e in agents.detect_entries(repo)] == [
        ("codex", "mcp_servers.whygraph")
    ]
    found = {e.agent: e for e in agents.detect_codegraph_entries(repo)}
    assert found["claude"].ours is False and found["claude"].transport == "stdio"
    assert found["codex"].ours is True and found["codex"].transport == "http"
    assert found["codex"].key == "mcp_servers.codegraph"


def test_detect_codegraph_sections_and_hooks_are_read_only(repo: Path) -> None:
    (repo / "AGENTS.md").write_text("x\n<!-- CODEGRAPH_START -->\ny\n")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "CLAUDE.md").write_text("no section here\n")
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "post-merge").write_text("# >>> codegraph sync hook >>>\n")
    (hooks / "post-commit").write_text("# >>> whygraph managed >>>\n")
    before = {p: p.read_bytes() for p in (repo / "AGENTS.md", hooks / "post-merge")}

    assert agents.detect_codegraph_sections(repo) == ["AGENTS.md"]
    assert agents.detect_codegraph_hooks(repo) == [str(hooks / "post-merge")]
    assert before == {p: p.read_bytes() for p in before}


def test_initialize_replace_codegraph_is_per_file(repo: Path) -> None:
    for rel in (".mcp.json", ".cursor/mcp.json"):
        p = repo / rel
        p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps({"mcpServers": {"codegraph": FOREIGN_CODEGRAPH}}))
    result = initialize_project(
        repo,
        agents=["claude", "cursor"],
        hooks=False,
        mcp=MCP,
        marker=None,
        replace_codegraph=[".cursor/mcp.json"],
    )
    by_file = {f.file: f for f in result.agent_files}
    assert by_file[".mcp.json"].foreign_codegraph is not None
    assert by_file[".cursor/mcp.json"].foreign_codegraph is None
    claude = json.loads((repo / ".mcp.json").read_text())["mcpServers"]
    cursor = json.loads((repo / ".cursor" / "mcp.json").read_text())["mcpServers"]
    assert claude["codegraph"] == FOREIGN_CODEGRAPH
    assert cursor["codegraph"]["url"].endswith("/mcp/demo/code")


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


def test_initialize_writes_gitignore_hooks_entry_and_assets(repo: Path) -> None:
    result = initialize_project(
        repo, agents=["claude"], hooks=True, mcp=HttpMcp(slug="demo"), marker=None
    )

    assert result.gitignore_added == ("whygraph.toml", ".whygraph/", ".codegraph/")
    gitignore = (repo / ".gitignore").read_text().splitlines()
    for entry in project_setup.GITIGNORE_ENTRIES:
        assert entry in gitignore
    assert _managed_hooks(repo) == set(HOOK_NAMES)
    assert result.hooks is not None and result.hooks_error is None
    assert json.loads((repo / ".mcp.json").read_text()) == {
        "mcpServers": {
            "whygraph": {
                "type": "http",
                "url": "http://127.0.0.1:${WHYGRAPH_PORT:-8765}/mcp/demo",
            },
            "codegraph": {
                "type": "http",
                "url": "http://127.0.0.1:${WHYGRAPH_PORT:-8765}/mcp/demo/code",
            },
        }
    }
    assert (repo / ".claude" / "agents" / "planner.md").is_file()
    assert "<!-- BEGIN whygraph -->" in (repo / ".claude" / "CLAUDE.md").read_text()
    assert result.configured_agents == ("claude",)
    assert result.marker_written is False
    assert not (repo / ".whygraph" / "portal.json").exists()
    assert not (repo / ".whygraph" / "portal.env").exists()
    assert result.assets["claude"].written  # raw InstallResult is exposed


def test_initialize_without_agent_still_does_gitignore_and_hooks(repo: Path) -> None:
    result = initialize_project(
        repo, hooks=True, mcp=HttpMcp(slug="demo"), agents=[], marker=None
    )
    assert result.agent_files == () and result.asset_files == ()
    assert _managed_hooks(repo) == set(HOOK_NAMES)


def test_initialize_hooks_selection_and_errors_are_best_effort(repo: Path) -> None:
    result = initialize_project(
        repo, hooks=["post-commit"], mcp=HttpMcp(slug="demo"), agents=[], marker=None
    )
    assert _managed_hooks(repo) == {"post-commit"}

    bad = initialize_project(
        repo, hooks=["post-comit"], mcp=HttpMcp(slug="demo"), agents=[], marker=None
    )
    assert bad.hooks is None and "post-comit" in (bad.hooks_error or "")
    assert result.hooks_error is None

    off = initialize_project(
        repo, hooks=[], mcp=HttpMcp(slug="demo"), agents=[], marker=None
    )
    assert _managed_hooks(repo) == set()
    assert off.hooks is not None


def test_initialize_outside_a_git_repo_reports_hooks_error(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = initialize_project(
        plain, agents=["cursor"], mcp=HttpMcp(slug="demo"), hooks=False, marker=None
    )
    assert result.hooks_error and "not a git repository" in result.hooks_error
    assert (plain / ".cursor" / "mcp.json").exists()


def test_initialize_resolves_aliases_and_collapses_duplicates(repo: Path) -> None:
    result = initialize_project(
        repo,
        agents=["copilot", "vscode"],
        hooks=False,
        mcp=HttpMcp(slug="demo"),
        marker=None,
    )
    assert result.configured_agents == ("vscode",)
    assert len([f for f in result.agent_files]) == 1


def test_initialize_two_agents_write_two_files(repo: Path) -> None:
    result = initialize_project(
        repo,
        agents=["claude", "codex"],
        hooks=False,
        mcp=HttpMcp(slug="demo"),
        marker=None,
    )
    assert {f.file for f in result.agent_files} == {
        ".mcp.json",
        ".claude/settings.json",
        ".codex/config.toml",
    }
    assert (repo / ".mcp.json").is_file()
    assert (repo / ".codex" / "config.toml").is_file()
    assert (repo / "AGENTS.md").is_file()
    assert result.configured_agents == ("claude", "codex")


def test_initialize_http_writes_urls_and_refused_is_reported(repo: Path) -> None:
    (repo / ".vscode").mkdir()
    (repo / ".vscode" / "mcp.json").write_text('{\n // c\n "servers": {}\n}\n')

    result = initialize_project(
        repo,
        agents=["claude", "vscode"],
        hooks=False,
        mcp=HttpMcp(slug="demo"),
        marker=None,
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

    plain = initialize_project(
        repo, agents=["claude"], hooks=False, mcp=HttpMcp(slug="demo"), marker=None
    )
    assert stale.read_text() == "1.x CONTENT"
    # An edited (here: pre-manifest) file is never touched without force.
    assert _status(plain, ".claude/agents/planner.md") == "needs_confirmation"

    preview = initialize_project(
        repo,
        agents=["claude"],
        hooks=False,
        mcp=HttpMcp(slug="demo"),
        force=True,
        dry_run=True,
        marker=None,
    )
    assert preview.dry_run is True
    assert _status(preview, ".claude/agents/planner.md") == "overwrite"
    assert stale.read_text() == "1.x CONTENT"

    initialize_project(
        repo,
        agents=["claude"],
        hooks=False,
        mcp=HttpMcp(slug="demo"),
        force=True,
        marker=None,
    )
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
    kwargs = dict(
        agents=["claude"],
        hooks=False,
        mcp=HttpMcp(slug="demo"),
        marker=None,
        force=True,
    )
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
    assert again.skipped and not again.overwritten  # nothing changed: all skipped


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
        marker=None,
    )

    assert json.loads(cursor.read_text()) == {"mcpServers": {"other": {"command": "o"}}}
    assert result.configured_agents == ("claude",)
    assert not (repo / ".cursor" / "rules").exists()  # removal installs no assets
    assert {f.file for f in result.agent_files} == {
        ".mcp.json",
        ".claude/settings.json",
        ".cursor/mcp.json",
    }


def test_agent_actions_can_override_an_agent_to_remove(repo: Path) -> None:
    (repo / ".mcp.json").write_text('{"mcpServers": {"whygraph": {"command": "w"}}}')
    result = initialize_project(
        repo,
        agents=["claude"],
        hooks=False,
        mcp=HttpMcp(slug="demo"),
        agent_actions={"claude": "remove"},
        marker=None,
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
    initialize_project(
        repo, hooks=False, mcp=HttpMcp(slug="demo"), agents=[], marker=None
    )
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
        plain,
        hooks=True,
        mcp=HttpMcp(slug="demo"),
        marker=PortalMarker("demo", 8765),
        agents=[],
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


# ---- the asset upgrade manifest ------------------------------------------


def _init(repo: Path, **kw):
    kw.setdefault("agents", ["claude"])
    return initialize_project(
        repo,
        hooks=False,
        mcp=HttpMcp(slug="demo"),
        marker=PortalMarker("demo", 8765),
        **kw,
    )


def test_initialize_writes_the_manifest_and_a_reinit_is_quiet(repo: Path) -> None:
    _init(repo)
    manifest = json.loads((repo / ".whygraph" / "assets.json").read_text())
    assert manifest["version"] == assets.package_version()
    assert ".claude/agents/planner.md" not in manifest["files"]
    assert "claude/agents/planner.md" in manifest["files"]

    again = _init(repo)

    assert {f.status for f in again.asset_files} == {"skip"}


def test_edited_asset_is_flagged_but_does_not_withhold_the_marker(
    repo: Path,
) -> None:
    _init(repo)
    planner = repo / ".claude" / "agents" / "planner.md"
    planner.write_text("MINE")
    manifest_path = repo / ".whygraph" / "assets.json"
    data = json.loads(manifest_path.read_text())
    # A newer package: the recorded hash is for the old bundled copy.
    data["files"]["claude/agents/planner.md"] = "0" * 64
    data["version"] = "0.0.1"
    manifest_path.write_text(json.dumps(data))

    result = _init(repo)

    assert _status(result, ".claude/agents/planner.md") == "needs_confirmation"
    diff = next(f.diff for f in result.asset_files if f.status == "needs_confirmation")
    assert "-MINE" in diff
    assert result.needs_confirmation == () and result.marker_written is True
    assert planner.read_text() == "MINE"
    # Still stale: the recorded version stays old so Settings keeps offering Update.
    assert assets.manifest_status(repo)["installed_version"] == "0.0.1"

    forced = _init(repo, force=True)
    assert _status(forced, ".claude/agents/planner.md") == "overwrite"
    assert assets.manifest_status(repo)["outdated"] is False


def test_removing_an_agent_leaves_the_manifest_alone(repo: Path) -> None:
    _init(repo)
    manifest_path = repo / ".whygraph" / "assets.json"
    before = manifest_path.read_text()

    _init(repo, agents=[], agent_actions={"claude": "remove"})

    assert manifest_path.read_text() == before
    assert (repo / ".claude" / "agents" / "planner.md").exists()


# ---- retired assets (files WhyGraph no longer ships) ------------------------


def _old_cursor_command(
    repo: Path, monkeypatch: pytest.MonkeyPatch, content: str = "OLD COMMAND"
) -> Path:
    """Seed a pre-manifest ``.cursor/commands/rationale.md`` known as ``OLD COMMAND``."""
    monkeypatch.setitem(
        assets.RETIRED_ASSETS,
        "cursor",
        {
            "commands/rationale.md": frozenset(
                {hashlib.sha256(b"OLD COMMAND").hexdigest()}
            )
        },
    )
    old = repo / ".cursor" / "commands" / "rationale.md"
    old.parent.mkdir(parents=True)
    old.write_text(content)
    return old


def test_unedited_retired_asset_is_a_delete_outcome(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _old_cursor_command(repo, monkeypatch)
    kw = dict(agents=["cursor"], contained=True)

    preview = _init(repo, dry_run=True, **kw)
    outcome = next(
        f for f in preview.asset_files if f.file == ".cursor/commands/rationale.md"
    )
    assert outcome.status == "delete" and outcome.agent == "cursor"
    assert outcome.reason == "no longer shipped by WhyGraph"
    assert "-OLD COMMAND" in (outcome.diff or "")
    assert old.exists()

    real = _init(repo, **kw)
    assert _status(real, ".cursor/commands/rationale.md") == "delete"
    assert not (repo / ".cursor" / "commands").exists()
    assert (repo / ".cursor" / "skills" / "rationale" / "SKILL.md").is_file()


def test_edited_retired_asset_asks_then_force_deletes(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _old_cursor_command(repo, monkeypatch, content="MY COMMAND")

    asked = _init(repo, agents=["cursor"], contained=True)
    outcome = next(
        f for f in asked.asset_files if f.file == ".cursor/commands/rationale.md"
    )
    assert outcome.status == "needs_confirmation"
    assert "no longer shipped" in (outcome.reason or "")
    assert "edited" in (outcome.reason or "")
    assert "-MY COMMAND" in (outcome.diff or "")
    assert old.read_text() == "MY COMMAND"

    forced = _init(repo, agents=["cursor"], contained=True, force=True)
    assert _status(forced, ".cursor/commands/rationale.md") == "delete"
    assert not old.exists()


def test_retired_asset_path_goes_through_check_inside(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        assets.RETIRED_ASSETS,
        "cursor",
        {"commands/rationale.md": frozenset({"0" * 64})},
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "rationale.md").write_text("NOT YOURS")
    (repo / ".cursor").mkdir()
    (repo / ".cursor" / "commands").symlink_to(outside)

    with pytest.raises(UnsafePathError):
        _init(repo, agents=["cursor"], contained=True, force=True)
    assert (outside / "rationale.md").read_text() == "NOT YOURS"


def test_removing_an_agent_leaves_retired_asset_files(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _old_cursor_command(repo, monkeypatch)

    _init(repo, agents=[], agent_actions={"cursor": "remove"})

    assert old.read_text() == "OLD COMMAND"


# ---- Claude Code hooks (plan 40 step 12) -----------------------------------


def test_claude_hooks_false_writes_no_settings_entries_and_no_helper(
    repo: Path,
) -> None:
    from whygraph.hooks import agent_hook_helper_path

    result = initialize_project(
        repo,
        agents=["claude"],
        hooks=True,
        mcp=MCP,
        marker=PortalMarker(slug="demo", port=8765),
        contained=True,
        claude_hooks=False,
    )

    assert [f.file for f in result.agent_files] == [".mcp.json"]
    assert not (repo / ".claude" / "settings.json").exists()
    assert not agent_hook_helper_path(repo).exists()
    assert result.marker_written is True


def test_claude_hooks_install_the_settings_entries_and_the_helper(repo: Path) -> None:
    from whygraph.hooks import AGENT_HOOK_HELPER, agent_hook_helper_path

    result = initialize_project(
        repo, agents=["claude"], hooks=False, mcp=MCP, marker=None, contained=True
    )

    by_file = {f.file: f for f in result.agent_files}
    assert by_file[".claude/settings.json"].status == "write"
    assert by_file[".claude/settings.json"].agent == "claude-settings"
    assert result.configured_agents == ("claude",)
    settings = json.loads((repo / ".claude" / "settings.json").read_text())
    assert set(settings["hooks"]) == {"SessionStart", "PostToolUse"}
    assert agent_hook_helper_path(repo).read_text() == AGENT_HOOK_HELPER


def test_a_dry_run_writes_no_settings_and_no_helper(repo: Path) -> None:
    from whygraph.hooks import agent_hook_helper_path

    result = initialize_project(
        repo, agents=["claude"], hooks=True, mcp=MCP, marker=None, dry_run=True
    )

    assert {f.file: f.status for f in result.agent_files} == {
        ".mcp.json": "write",
        ".claude/settings.json": "write",
    }
    assert not (repo / ".claude").exists()
    assert not agent_hook_helper_path(repo).exists()


@pytest.mark.parametrize("claude_hooks", [True, False])
def test_claude_remove_strips_the_hooks_and_deletes_the_helper(
    repo: Path, claude_hooks: bool
) -> None:
    from whygraph.hooks import agent_hook_helper_path

    settings = repo / ".claude" / "settings.json"
    initialize_project(repo, agents=["claude"], hooks=False, mcp=MCP, marker=None)
    data = json.loads(settings.read_text())
    data["permissions"] = {"allow": []}
    settings.write_text(json.dumps(data))
    assert agent_hook_helper_path(repo).is_file()

    result = initialize_project(
        repo,
        agents=[],
        hooks=False,
        mcp=MCP,
        marker=None,
        agent_actions={"claude": "remove"},
        contained=True,
        claude_hooks=claude_hooks,
    )

    assert {f.file for f in result.agent_files} == {
        ".mcp.json",
        ".claude/settings.json",
    }
    assert json.loads(settings.read_text()) == {"permissions": {"allow": []}}
    assert not agent_hook_helper_path(repo).exists()
    assert not (repo / ".git" / "whygraph").exists()  # deleted before sync_hooks


def test_another_agent_leaves_the_agent_helper_alone(repo: Path) -> None:
    from whygraph.hooks import agent_hook_helper_path

    initialize_project(repo, agents=["claude"], hooks=False, mcp=MCP, marker=None)
    initialize_project(repo, agents=["cursor"], hooks=False, mcp=MCP, marker=None)

    assert agent_hook_helper_path(repo).is_file()


def test_touched_paths_follow_claude_hooks(repo: Path) -> None:
    settings = repo / ".claude" / "settings.json"

    def touched(action: str, claude_hooks: bool) -> list[Path]:
        return list(
            project_setup._touched_paths(
                repo, {"claude": action}, None, claude_hooks=claude_hooks
            )
        )

    assert settings in touched("migrate", True)
    assert settings not in touched("migrate", False)
    assert settings in touched("remove", True)
    assert settings in touched("remove", False)
