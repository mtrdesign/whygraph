"""Tests for multi-client ``whygraph init`` wiring.

Two layers of coverage:

* Direct unit tests on :mod:`whygraph.clients` — these stand alone and
  do not exercise the CLI or any DB code, so they're robust to the
  current mid-rewrite state of unrelated modules.
* CLI-flow tests via :class:`click.testing.CliRunner` that patch
  ``ensure_initialized`` so we don't depend on the DB layer here either.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from whygraph import clients
from whygraph.cli import main as whygraph_main


# ---------- clients.py direct tests ----------------------------------------


def test_known_client_names_includes_canonical_and_aliases() -> None:
    names = set(clients.known_client_names())
    assert {"claude", "cursor", "vscode", "copilot", "codex", "claude-desktop"} <= names


def test_resolve_client_canonical() -> None:
    target = clients.resolve_client("claude")
    assert target.name == "claude"
    assert target.scope == "project"
    assert target.format == "json"


def test_resolve_client_alias_copilot_to_vscode() -> None:
    target = clients.resolve_client("copilot")
    assert target.name == "vscode"


def test_resolve_client_case_insensitive() -> None:
    assert clients.resolve_client("CLAUDE").name == "claude"
    assert clients.resolve_client("Cursor").name == "cursor"


def test_resolve_client_unknown_raises() -> None:
    with pytest.raises(clients.UnknownClientError):
        clients.resolve_client("emacs")


def test_render_snippet_json_shape() -> None:
    target = clients.resolve_client("claude")
    snippet = clients.render_snippet(target)
    payload = json.loads(snippet)
    assert payload == {"mcpServers": {"whygraph": {"command": "whygraph-mcp"}}}


def test_render_snippet_toml_shape() -> None:
    target = clients.resolve_client("codex")
    snippet = clients.render_snippet(target)
    assert "[mcp_servers.whygraph]" in snippet
    assert 'command = "whygraph-mcp"' in snippet


def test_config_path_for_project_anchored_at_root(tmp_path: Path) -> None:
    target = clients.resolve_client("cursor")
    path = clients.config_path_for(target, tmp_path)
    assert path == tmp_path / ".cursor" / "mcp.json"


def test_config_path_for_user_anchored_at_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() reads $HOME on POSIX; resolve dynamically.
    target = clients.resolve_client("codex")
    expected = Path.home() / ".codex" / "config.toml"
    assert clients.config_path_for(target, tmp_path) == expected


def test_is_write_supported_only_project_json() -> None:
    assert clients.is_write_supported(clients.resolve_client("claude"))
    assert clients.is_write_supported(clients.resolve_client("cursor"))
    assert clients.is_write_supported(clients.resolve_client("vscode"))
    assert not clients.is_write_supported(clients.resolve_client("codex"))
    assert not clients.is_write_supported(clients.resolve_client("claude-desktop"))


def test_write_snippet_creates_file(tmp_path: Path) -> None:
    target = clients.resolve_client("claude")
    path = clients.write_snippet(target, tmp_path)
    assert path == tmp_path / ".mcp.json"
    data = json.loads(path.read_text())
    assert data == {"mcpServers": {"whygraph": {"command": "whygraph-mcp"}}}


def test_write_snippet_creates_nested_directory(tmp_path: Path) -> None:
    target = clients.resolve_client("cursor")
    path = clients.write_snippet(target, tmp_path)
    assert path == tmp_path / ".cursor" / "mcp.json"
    assert path.exists()


def test_write_snippet_merges_with_existing_servers(tmp_path: Path) -> None:
    target = clients.resolve_client("claude")
    existing = tmp_path / ".mcp.json"
    existing.write_text(
        json.dumps(
            {
                "mcpServers": {"other": {"command": "other-cmd"}},
                "unrelatedTopLevel": "keepme",
            }
        )
    )
    clients.write_snippet(target, tmp_path)
    data = json.loads(existing.read_text())
    assert data["mcpServers"]["other"] == {"command": "other-cmd"}
    assert data["mcpServers"]["whygraph"] == {"command": "whygraph-mcp"}
    assert data["unrelatedTopLevel"] == "keepme"


def test_write_snippet_replaces_old_whygraph_entry(tmp_path: Path) -> None:
    target = clients.resolve_client("claude")
    existing = tmp_path / ".mcp.json"
    existing.write_text(
        json.dumps({"mcpServers": {"whygraph": {"command": "old-cmd"}}})
    )
    clients.write_snippet(target, tmp_path)
    data = json.loads(existing.read_text())
    assert data["mcpServers"]["whygraph"]["command"] == "whygraph-mcp"


def test_write_snippet_overwrites_malformed_json(tmp_path: Path) -> None:
    target = clients.resolve_client("claude")
    existing = tmp_path / ".mcp.json"
    existing.write_text("{not valid json")
    clients.write_snippet(target, tmp_path)
    data = json.loads(existing.read_text())
    assert data == {"mcpServers": {"whygraph": {"command": "whygraph-mcp"}}}


def test_write_snippet_rejects_user_scope(tmp_path: Path) -> None:
    target = clients.resolve_client("codex")
    with pytest.raises(ValueError, match="print-only"):
        clients.write_snippet(target, tmp_path)


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


def test_init_list_clients_does_not_touch_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    called = {"n": 0}

    def _fake() -> Path:
        called["n"] += 1
        return tmp_path / "x.db"

    monkeypatch.setattr("whygraph.cli.commands.init._ensure_db_initialized", _fake)
    runner = CliRunner()
    result = runner.invoke(whygraph_main, ["init", "--list-clients"])
    assert result.exit_code == 0, result.output
    assert called["n"] == 0
    assert "claude" in result.output
    assert "cursor" in result.output
    assert "codex" in result.output


def test_init_no_flag_writes_no_client_config(
    stub_init, tmp_path: Path
) -> None:
    result, cwd = _invoke_in(tmp_path, "init")
    assert result.exit_code == 0, result.output
    assert not (cwd / ".mcp.json").exists()
    assert not (cwd / ".cursor").exists()
    assert not (cwd / ".vscode").exists()
    assert "Initialized WhyGraph database" in result.output


def test_init_client_claude_writes_mcp_json(stub_init, tmp_path: Path) -> None:
    result, cwd = _invoke_in(tmp_path, "init", "--client", "claude")
    assert result.exit_code == 0, result.output
    mcp_path = cwd / ".mcp.json"
    assert mcp_path.exists()
    data = json.loads(mcp_path.read_text())
    assert data["mcpServers"]["whygraph"]["command"] == "whygraph-mcp"
    assert "Wrote whygraph MCP entry" in result.output
    assert "/plugin marketplace add" in result.output  # Claude-specific tip


def test_init_client_cursor_writes_nested_path(stub_init, tmp_path: Path) -> None:
    result, cwd = _invoke_in(tmp_path, "init", "--client", "cursor")
    assert result.exit_code == 0, result.output
    cursor_path = cwd / ".cursor" / "mcp.json"
    assert cursor_path.exists()
    data = json.loads(cursor_path.read_text())
    assert data["mcpServers"]["whygraph"]["command"] == "whygraph-mcp"


def test_init_client_copilot_aliases_to_vscode(stub_init, tmp_path: Path) -> None:
    result, cwd = _invoke_in(tmp_path, "init", "--client", "copilot")
    assert result.exit_code == 0, result.output
    assert (cwd / ".vscode" / "mcp.json").exists()


def test_init_client_codex_prints_and_does_not_write(
    stub_init, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Route ~/ at a temp home so we can confirm no file appears there.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    result, cwd = _invoke_in(tmp_path, "init", "--client", "codex")
    assert result.exit_code == 0, result.output
    assert "[mcp_servers.whygraph]" in result.output
    assert 'command = "whygraph-mcp"' in result.output
    assert not (home / ".codex" / "config.toml").exists()


def test_init_client_claude_with_print_does_not_write(
    stub_init, tmp_path: Path
) -> None:
    result, cwd = _invoke_in(tmp_path, "init", "--client", "claude", "--print")
    assert result.exit_code == 0, result.output
    assert not (cwd / ".mcp.json").exists()
    # JSON snippet still printed for the user to paste.
    assert '"whygraph-mcp"' in result.output


def test_init_unknown_client_errors(stub_init, tmp_path: Path) -> None:
    result, _ = _invoke_in(tmp_path, "init", "--client", "emacs")
    assert result.exit_code != 0
    # Click's Choice produces a usage error mentioning the bad value.
    assert "emacs" in result.output or "Invalid value" in result.output
