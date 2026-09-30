"""Tests for the ``whygraph install`` subcommand.

The command prints a POSIX ``sh`` installer to stdout (the ``docker run …
install | sh`` bootstrap). These assert the emitted script is clean (no log
leakage on stdout), pins the baked version, and — end to end — writes an
executable, syntactically-valid shim that carries the ephemeral ``docker run``
invocation verbatim, plus a ``whygraph-mcp`` removal stub that never calls
``docker``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from click.testing import CliRunner

from whygraph.cli import main
from whygraph.cli.commands.install import IMAGE_REPO, render_installer
from whygraph.cli.stubs import MCP_REMOVED, SERVE_REMOVED


def test_stdout_is_a_clean_script_pinned_to_the_baked_version() -> None:
    # The `… install | sh` pipe requires stdout to be *only* the script;
    # logging must stay on stderr.
    result = CliRunner().invoke(main, ["install"], env={"WHYGRAPH_VERSION": "9.9.9"})
    assert result.exit_code == 0, result.output
    # stdout only: config / deprecation logging may land on stderr.
    assert result.stdout.startswith("#!/usr/bin/env sh")
    assert f"{IMAGE_REPO}:9.9.9" in result.stdout


def test_defaults_to_latest_without_the_env(monkeypatch) -> None:
    monkeypatch.delenv("WHYGRAPH_VERSION", raising=False)
    result = CliRunner().invoke(main, ["install"])
    assert result.exit_code == 0, result.output
    assert f"{IMAGE_REPO}:latest" in result.stdout


def test_render_installer_carries_the_shim_and_the_mcp_stub() -> None:
    script = render_installer(f"{IMAGE_REPO}:1.2.3")
    # Writes the shim and the whygraph-mcp stub onto PATH...
    assert 'cat > "$BIN_DIR/whygraph"' in script
    assert 'cat > "$BIN_DIR/whygraph-mcp"' in script
    # ...the shim baking the ref as an overridable default (the stub has none)...
    assert script.count(f'IMAGE="${{WHYGRAPH_IMAGE:-{IMAGE_REPO}:1.2.3}}"') == 1
    # ...and running the image ephemerally against the cwd with token passthrough.
    assert '-v "$PWD:/workspace" -w /workspace' in script
    assert "-e GH_TOKEN -e GITHUB_TOKEN" in script
    assert '"$IMAGE" whygraph "$@"' in script
    # The 1.x docker shim for whygraph-mcp is gone.
    assert 'whygraph-mcp "$@"' not in script
    assert script.rstrip().endswith('echo "done. Try:  whygraph up"')


def _install(bin_dir: Path) -> None:
    script = render_installer(f"{IMAGE_REPO}:1.2.3")
    installer = bin_dir.parent / "installer.sh"
    installer.write_text(script)
    # `sh -n` parses the outer installer without executing it.
    assert subprocess.run(["sh", "-n", str(installer)]).returncode == 0
    # Run it into an isolated bin dir (never touches the real PATH).
    env = {**os.environ, "WHYGRAPH_BIN_DIR": str(bin_dir)}
    assert subprocess.run(["sh", str(installer)], env=env).returncode == 0


def test_emitted_installer_writes_a_valid_shim_and_stub(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _install(bin_dir)

    for name in ("whygraph", "whygraph-mcp"):
        shim = bin_dir / name
        assert shim.exists(), name
        assert os.access(shim, os.X_OK), name
        assert subprocess.run(["sh", "-n", str(shim)]).returncode == 0, name
    assert f"{IMAGE_REPO}:1.2.3" in (bin_dir / "whygraph").read_text()


def _no_docker_path(tmp_path: Path) -> str:
    """A PATH with the system tools but a ``docker`` that records any call."""
    fake = tmp_path / "fake-bin"
    fake.mkdir()
    docker = fake / "docker"
    docker.write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path / "docker.calls"}"\n')
    docker.chmod(0o755)
    return f"{fake}:/usr/bin:/bin"


def test_mcp_stub_prints_the_removal_message_and_never_calls_docker(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    _install(bin_dir)
    stub = bin_dir / "whygraph-mcp"
    assert "docker" not in stub.read_text().replace("calls docker", "")

    for path in (_no_docker_path(tmp_path), "/usr/bin:/bin"):
        result = subprocess.run(
            [str(stub), "--anything"],
            capture_output=True,
            text=True,
            env={"PATH": path, "HOME": str(tmp_path)},
        )
        assert result.returncode == 2
        assert result.stdout == ""  # nothing an MCP client could misparse
        assert result.stderr.strip() == MCP_REMOVED
    assert not (tmp_path / "docker.calls").exists()


def test_shim_serve_is_removed_but_stop_still_cleans_up(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _install(bin_dir)
    shim = str(bin_dir / "whygraph")
    env = {"PATH": _no_docker_path(tmp_path), "HOME": str(tmp_path)}

    for args in ([], ["--detach"], ["--logs"], ["--help"]):
        result = subprocess.run(
            [shim, "serve", *args], capture_output=True, text=True, env=env
        )
        assert result.returncode == 2, args
        assert result.stdout == ""
        assert result.stderr.strip() == SERVE_REMOVED
    assert not (tmp_path / "docker.calls").exists()

    stop = subprocess.run([shim, "serve", "--stop"], env=env)
    assert stop.returncode == 0
    calls = (tmp_path / "docker.calls").read_text().splitlines()
    assert calls == ["rm -f whygraph-serve"]
