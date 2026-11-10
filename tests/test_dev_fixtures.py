"""Tests for ``scripts/dev_fixtures.sh`` - the dev modes' scratch repos.

The script clones the checkout it lives in, so each test copies it into a tiny
throwaway "checkout" (a one-commit repo) to keep the clone fast and offline.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "dev_fixtures.sh"


def _git(*args: str, cwd: Path) -> str:
    """Run git with no user or system config and return its stdout."""
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(cwd),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout


def _checkout(tmp_path: Path) -> Path:
    """A one-commit repo holding a copy of the script under ``scripts/``."""
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / SCRIPT.name)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("add", "-A", cwd=root)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t.invalid",
        "commit",
        "-q",
        "-m",
        "init",
        cwd=root,
    )
    return root


def _run(checkout: Path, repos: Path) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(checkout.parent / "home"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    result = subprocess.run(
        ["sh", str(checkout / "scripts" / SCRIPT.name), str(repos)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result


def _head(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo).strip()


def _empty_files(root: Path) -> None:
    """Delete every file under ``root`` but keep the directories."""
    for path in root.rglob("*"):
        if path.is_file() or path.is_symlink():
            path.unlink()


def test_script_parses() -> None:
    assert subprocess.run(["sh", "-n", str(SCRIPT)]).returncode == 0


def test_creates_both_repos(tmp_path: Path) -> None:
    checkout, repos = _checkout(tmp_path), tmp_path / "repos"
    _run(checkout, repos)

    assert _head(repos / "whygraph") == _head(checkout)
    assert _git("rev-list", "--count", "HEAD", cwd=repos / "demo").strip() == "3"


def test_healthy_repos_are_left_alone(tmp_path: Path) -> None:
    checkout, repos = _checkout(tmp_path), tmp_path / "repos"
    _run(checkout, repos)
    marker = repos / "whygraph" / "local-edit.txt"
    marker.write_text("keep me")
    demo_head = _head(repos / "demo")

    result = _run(checkout, repos)

    assert result.stdout == ""
    assert marker.read_text() == "keep me"
    assert _head(repos / "demo") == demo_head


def test_rebuilds_repos_emptied_down_to_their_directories(tmp_path: Path) -> None:
    checkout, repos = _checkout(tmp_path), tmp_path / "repos"
    _run(checkout, repos)
    for name in ("whygraph", "demo"):
        _empty_files(repos / name)
        assert (repos / name / ".git").is_dir()
        assert not (repos / name / ".git" / "HEAD").exists()

    result = _run(checkout, repos)

    assert "cloning" in result.stdout and "creating" in result.stdout
    assert _head(repos / "whygraph") == _head(checkout)
    assert _git("rev-list", "--count", "HEAD", cwd=repos / "demo").strip() == "3"
