"""The 2.0 CLI surface: removal stubs and the refusal in portal-managed repos.

* ``whygraph init`` / ``whygraph serve`` / ``whygraph-mcp`` print their
  removal message to stderr and exit ``2``; ``whygraph analyze`` is gone.
* ``whygraph scan`` refuses (exit ``2``, a message built only from the
  validated slug and port) in a repo whose ``.whygraph/portal.json`` is a
  valid, untracked marker; ignores a tracked or malformed one with a
  warning; runs normally without one; and ``--managed-by-portal`` (the
  portal's own child scans) bypasses the check.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from whygraph.cli import main as whygraph_main
from whygraph.cli.commands import scan as scan_mod
from whygraph.cli.stubs import INIT_REMOVED, MCP_REMOVED, SERVE_REMOVED
from whygraph.project_setup import PortalMarker, read_portal_marker

REFUSAL = (
    "This project is managed by the WhyGraph portal "
    "(http://127.0.0.1:8765/p/demo). Run scans from the portal."
)


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
    for message in (INIT_REMOVED, SERVE_REMOVED, MCP_REMOVED):
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
    assert "scan" in result.output
    for name in ("init", "serve", "analyze"):
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


# ---- whygraph scan in a portal-managed repo --------------------------------


@pytest.fixture
def no_real_scan(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stop ``scan`` right after the refusal check (no DB, no crawl)."""
    reached: list[str] = []

    def _stop() -> None:
        reached.append("db")
        raise SystemExit(0)

    monkeypatch.setattr("whygraph.db.ensure_initialized", _stop)
    return reached


def test_scan_refuses_with_an_untracked_marker(repo: Path, no_real_scan) -> None:
    _write_marker(repo, {"slug": "demo", "port": 8765})

    result = _invoke("scan", "--no-codegraph")

    assert result.exit_code == 2
    assert REFUSAL in result.stderr
    assert "delete .whygraph/portal.json" in result.stderr
    assert no_real_scan == []  # refused before any DB access
    assert not (repo / ".whygraph" / "whygraph.db").exists()


def test_scan_refuses_from_a_subdirectory(
    repo: Path, no_real_scan, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_marker(repo, {"slug": "demo", "port": 8765})
    (repo / "src").mkdir()
    monkeypatch.chdir(repo / "src")
    assert _invoke("scan").exit_code == 2


def test_scan_message_uses_only_validated_fields(repo: Path, no_real_scan) -> None:
    _write_marker(repo, {"slug": "demo", "port": 9100, "note": "$(rm -rf /) evil"})
    result = _invoke("scan")
    assert result.exit_code == 2
    assert "http://127.0.0.1:9100/p/demo" in result.stderr
    assert "evil" not in result.stderr


def test_scan_ignores_a_tracked_marker_with_a_warning(repo: Path, no_real_scan) -> None:
    _write_marker(repo, {"slug": "demo", "port": 8765})
    _git(repo, "add", "-f", ".whygraph/portal.json")

    result = _invoke("scan")

    assert "tracked by git" in result.stderr
    assert "managed by the WhyGraph portal" not in result.stderr
    assert no_real_scan == ["db"]  # the scan went ahead


def test_scan_ignores_a_malformed_marker_with_a_warning(
    repo: Path, no_real_scan
) -> None:
    _write_marker(repo, {"slug": "../../x", "port": 8765})
    result = _invoke("scan")
    assert "not a valid portal marker" in result.stderr
    assert no_real_scan == ["db"]


def test_scan_runs_normally_without_a_marker(repo: Path, no_real_scan) -> None:
    result = _invoke("scan")
    assert result.stderr == ""
    assert no_real_scan == ["db"]


def test_managed_by_portal_bypasses_the_check(
    repo: Path, no_real_scan, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_marker(repo, {"slug": "demo", "port": 8765})
    called: list[bool] = []
    monkeypatch.setattr(
        scan_mod, "_refuse_if_portal_managed", lambda: called.append(True)
    )

    _invoke("scan", "--managed-by-portal")

    assert called == []
    assert no_real_scan == ["db"]


def test_managed_by_portal_stays_hidden() -> None:
    result = _invoke("scan", "--help")
    assert "--managed-by-portal" not in result.output
