"""The 2.0 CLI surface: removal stubs and the portal marker reader.

* ``whygraph init`` / ``whygraph serve`` / ``whygraph scan`` /
  ``whygraph-mcp`` print their removal message to stderr and exit ``2``;
  ``whygraph analyze`` is gone.
* ``read_portal_marker`` validates ``.whygraph/portal.json``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from whygraph.cli import main as whygraph_main
from whygraph.cli.stubs import INIT_REMOVED, MCP_REMOVED, SCAN_REMOVED, SERVE_REMOVED
from whygraph.project_setup import PortalMarker, read_portal_marker


@pytest.fixture(autouse=True)
def _no_logging_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """The group callback's ``configure_logging`` would replace root handlers."""
    monkeypatch.setattr("whygraph.cli.configure_logging", lambda *a, **kw: None)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    monkeypatch.chdir(root)
    return root


def _write_marker(root: Path, payload: object) -> None:
    (root / ".whygraph").mkdir(exist_ok=True)
    (root / ".whygraph" / "portal.json").write_text(json.dumps(payload))


def _invoke(*args: str):
    return CliRunner().invoke(whygraph_main, list(args))


# ---- removal stubs --------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["init"], INIT_REMOVED),
        (["init", "--agent", "claude", "--yes"], INIT_REMOVED),
        (["init", "--help"], INIT_REMOVED),
        (["serve"], SERVE_REMOVED),
        (["serve", "--port", "9000"], SERVE_REMOVED),
        (["scan"], SCAN_REMOVED),
        (["scan", "--skip-analyze", "--no-remote"], SCAN_REMOVED),
        (["scan", "--help"], SCAN_REMOVED),
    ],
)
def test_removed_commands_exit_2_with_their_message(
    repo: Path, args: list[str], message: str
) -> None:
    result = _invoke(*args)
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.strip() == message
    assert not (repo / ".whygraph").exists()  # nothing was bootstrapped


def test_messages_point_at_whygraph_up_only() -> None:
    for message in (INIT_REMOVED, SERVE_REMOVED, SCAN_REMOVED, MCP_REMOVED):
        assert "whygraph up" in message
        assert "whygraph portal" not in message
        assert "'" not in message  # baked into single-quoted shell strings


def test_analyze_is_an_unknown_command() -> None:
    result = _invoke("analyze", "HEAD")
    assert result.exit_code == 2
    assert "No such command 'analyze'" in result.output


def test_removed_commands_are_hidden_from_help() -> None:
    result = _invoke("--help")
    assert result.exit_code == 0
    for name in ("init", "serve", "scan", "analyze"):
        assert f"  {name} " not in result.output


def test_whygraph_mcp_console_script_prints_to_stderr_only() -> None:
    # The console script's target, run as a real process.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from whygraph.cli.stubs import whygraph_mcp; whygraph_mcp()",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.strip() == MCP_REMOVED


def test_whygraph_mcp_entry_point_is_the_stub() -> None:
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text())["project"]["scripts"]
    assert scripts["whygraph-mcp"] == "whygraph.cli.stubs:whygraph_mcp"
    assert scripts["whygraph"] == "whygraph.cli:main"


# ---- the marker -----------------------------------------------------------


def test_read_portal_marker_cases(repo: Path) -> None:
    assert read_portal_marker(repo) == (None, None)

    _write_marker(repo, {"slug": "demo", "port": 8765})
    assert read_portal_marker(repo) == (PortalMarker("demo", 8765), None)

    for bad in (
        {"slug": "Bad Slug", "port": 8765},
        {"slug": "demo", "port": "8765"},
        {"slug": "demo", "port": True},
        {"slug": "demo"},
        ["demo", 8765],
    ):
        _write_marker(repo, bad)
        marker, warning = read_portal_marker(repo)
        assert marker is None and "not a valid portal marker" in (warning or ""), bad

    (repo / ".whygraph" / "portal.json").write_text("{not json")
    assert read_portal_marker(repo)[0] is None


def test_a_symlinked_marker_is_ignored(repo: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"slug": "demo", "port": 8765}))
    (repo / ".whygraph").mkdir()
    (repo / ".whygraph" / "portal.json").symlink_to(elsewhere)
    marker, warning = read_portal_marker(repo)
    assert marker is None and "symbolic link" in (warning or "")
