"""Tests for the ``whygraph install`` subcommand.

The command prints a POSIX ``sh`` installer to stdout (the ``docker run …
install | sh`` bootstrap). These assert the emitted script is clean (no log
leakage on stdout), pins the baked version, and — end to end — writes two
executable, syntactically-valid shims that carry the ephemeral ``docker run``
invocation verbatim.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from click.testing import CliRunner

from whygraph.cli import main
from whygraph.cli.commands.install import IMAGE_REPO, render_installer


def test_stdout_is_a_clean_script_pinned_to_the_baked_version() -> None:
    # The `… install | sh` pipe requires stdout to be *only* the script;
    # logging must stay on stderr.
    result = CliRunner().invoke(main, ["install"], env={"WHYGRAPH_VERSION": "9.9.9"})
    assert result.exit_code == 0, result.output
    assert result.output.startswith("#!/usr/bin/env sh")
    assert f"{IMAGE_REPO}:9.9.9" in result.output


def test_defaults_to_latest_without_the_env(monkeypatch) -> None:
    monkeypatch.delenv("WHYGRAPH_VERSION", raising=False)
    result = CliRunner().invoke(main, ["install"])
    assert result.exit_code == 0, result.output
    assert f"{IMAGE_REPO}:latest" in result.output


def test_render_installer_carries_both_shims_and_the_docker_run_line() -> None:
    script = render_installer(f"{IMAGE_REPO}:1.2.3")
    # Writes both shims onto PATH...
    assert 'cat > "$BIN_DIR/whygraph"' in script
    assert 'cat > "$BIN_DIR/whygraph-mcp"' in script
    # ...each baking the ref as an overridable default...
    assert script.count(f'IMAGE="${{WHYGRAPH_IMAGE:-{IMAGE_REPO}:1.2.3}}"') == 2
    # ...and running the image ephemerally against the cwd with token passthrough.
    assert '-v "$PWD:/workspace" -w /workspace' in script
    assert "-e GH_TOKEN -e GITHUB_TOKEN" in script
    assert '"$IMAGE" whygraph "$@"' in script
    assert '"$IMAGE" whygraph-mcp "$@"' in script


def test_emitted_installer_writes_two_valid_executable_shims() -> None:
    script = render_installer(f"{IMAGE_REPO}:1.2.3")
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("installer.sh").write_text(script)
        # `sh -n` parses the outer installer without executing it.
        assert subprocess.run(["sh", "-n", "installer.sh"]).returncode == 0

        # Run it into an isolated bin dir (never touches the real PATH).
        env = {**os.environ, "WHYGRAPH_BIN_DIR": str(Path("bin").resolve())}
        assert subprocess.run(["sh", "installer.sh"], env=env).returncode == 0

        for name in ("whygraph", "whygraph-mcp"):
            shim = Path("bin") / name
            assert shim.exists(), name
            assert os.access(shim, os.X_OK), name
            # Each shim parses and pins the baked image default.
            assert subprocess.run(["sh", "-n", str(shim)]).returncode == 0, name
            assert f"{IMAGE_REPO}:1.2.3" in shim.read_text()
