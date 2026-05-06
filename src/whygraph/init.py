"""Bootstrap CodeGraph in the current repo via `whygraph init`."""

from __future__ import annotations

import enum
import os
import re
import shutil
import subprocess
from pathlib import Path

import click

MIN_NODE_MAJOR = 22
NVM_VERSION = "v0.40.1"
CODEGRAPH_PKG = "@colbymchenry/codegraph"
NVM_INSTALL_URL = (
    f"https://raw.githubusercontent.com/nvm-sh/nvm/{NVM_VERSION}/install.sh"
)


class Action(enum.Enum):
    NODE_OK = "node_ok"
    USE_NVM = "use_nvm"
    BOOTSTRAP_NVM = "bootstrap_nvm"


def _parse_node_version(output: str) -> tuple[int, int, int] | None:
    m = re.match(r"v(\d+)\.(\d+)\.(\d+)", output.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _detect_node() -> tuple[int, int, int] | None:
    if not shutil.which("node"):
        return None
    try:
        result = subprocess.run(
            ["node", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return _parse_node_version(result.stdout)


def _detect_nvm() -> Path | None:
    candidates: list[Path] = []
    nvm_dir = os.environ.get("NVM_DIR")
    if nvm_dir:
        candidates.append(Path(nvm_dir) / "nvm.sh")
    candidates.append(Path.home() / ".nvm" / "nvm.sh")
    for c in candidates:
        if c.is_file():
            return c
    return None


def _decide_action(
    node_version: tuple[int, int, int] | None,
    has_nvm: bool,
) -> Action:
    if node_version is not None and node_version[0] >= MIN_NODE_MAJOR:
        return Action.NODE_OK
    if has_nvm:
        return Action.USE_NVM
    return Action.BOOTSTRAP_NVM


def _run_via_npx() -> int:
    if not shutil.which("npx"):
        click.echo(
            "Found Node but `npx` is missing. Install npm "
            "(e.g. `apt install npm` on Debian/Ubuntu) and re-run `whygraph init`.",
            err=True,
        )
        return 1
    return subprocess.run(
        ["npx", "-y", CODEGRAPH_PKG, "init", "-i"],
        check=False,
    ).returncode


def _run_via_nvm(nvm_sh: Path) -> int:
    # nvm is a shell function; its activation must happen in the same subshell
    # as the npx call, otherwise `npx` resolves against the original PATH.
    script = (
        f'export NVM_DIR="{nvm_sh.parent}" && '
        f'\\. "{nvm_sh}" && '
        f"nvm install {MIN_NODE_MAJOR} && "
        f"nvm use {MIN_NODE_MAJOR} && "
        f"npx -y {CODEGRAPH_PKG} init -i"
    )
    return subprocess.run(["bash", "-c", script], check=False).returncode


def _bootstrap_nvm_then_run(assume_yes: bool) -> int:
    click.echo(
        f"Node {MIN_NODE_MAJOR}+ is required and nvm isn't installed. "
        "With your permission I'll:\n"
        f"  1. Install nvm {NVM_VERSION} via its official script\n"
        f"     ({NVM_INSTALL_URL}). This writes to your shell rc file.\n"
        f"  2. Run `nvm install {MIN_NODE_MAJOR} && nvm use {MIN_NODE_MAJOR}`.\n"
        f"  3. Continue with `npx {CODEGRAPH_PKG} init -i`."
    )
    if not assume_yes and not click.confirm("Proceed?", default=False):
        click.echo(
            f"\nAborted. To install Node {MIN_NODE_MAJOR}+ manually, choose one:\n"
            "  - Node from https://nodejs.org (LTS)\n"
            "  - Homebrew:  brew install node\n"
            "  - nvm:       https://github.com/nvm-sh/nvm\n"
            "Then re-run `whygraph init`.",
            err=True,
        )
        return 1
    rc = subprocess.run(
        ["bash", "-c", f"curl -o- {NVM_INSTALL_URL} | bash"],
        check=False,
    ).returncode
    if rc != 0:
        click.echo("nvm install failed. See output above.", err=True)
        return rc
    nvm_sh = _detect_nvm()
    if nvm_sh is None:
        click.echo(
            "nvm install reported success but nvm.sh was not found. "
            "Open a new shell and re-run `whygraph init`.",
            err=True,
        )
        return 1
    return _run_via_nvm(nvm_sh)


def _verify_codegraph_dir(cwd: Path) -> bool:
    return (cwd / ".codegraph").is_dir()


def run_init(assume_yes: bool = False) -> int:
    cwd = Path.cwd()
    node_version = _detect_node()
    nvm_sh = _detect_nvm()
    action = _decide_action(node_version, nvm_sh is not None)

    if action is Action.NODE_OK:
        rc = _run_via_npx()
    elif action is Action.USE_NVM:
        assert nvm_sh is not None
        rc = _run_via_nvm(nvm_sh)
    else:
        rc = _bootstrap_nvm_then_run(assume_yes)

    if rc != 0:
        return rc

    if not _verify_codegraph_dir(cwd):
        click.echo(
            "CodeGraph init returned 0 but `.codegraph/` was not created. "
            "It may have been skipped or aborted. Re-run `whygraph init` to retry.",
            err=True,
        )
        return 1

    click.echo(f"\nwhygraph initialized for {cwd}")
    return 0
