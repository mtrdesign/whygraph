"""Tests for ``scripts/dev_folders.sh`` - the dev portals' shared-folder list.

``make dev-local`` / ``dev-docker`` / ``prod`` share the scratch repos plus
``DEV_FOLDERS`` (from the environment, else ``.env.dev``). The script prints the
``:``-joined list and stops the target on a folder the portal would refuse or
skip, so a typo fails loudly instead of quietly sharing less.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "dev_folders.sh"


def _run(
    tmp_path: Path, env_file: str | None = None, dev_folders: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the script against ``tmp_path/dev`` with a controlled environment."""
    dev = tmp_path / "dev"
    (dev / "repos").mkdir(parents=True, exist_ok=True)
    path = tmp_path / "env"
    if env_file is not None:
        path.write_text(env_file)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home")}
    if dev_folders is not None:
        env["DEV_FOLDERS"] = dev_folders
    return subprocess.run(
        ["sh", str(SCRIPT), str(dev), str(path)],
        env=env,
        capture_output=True,
        text=True,
    )


def _folders(result: subprocess.CompletedProcess[str]) -> list[str]:
    assert result.returncode == 0, result.stderr
    return result.stdout.rstrip("\n").split(":")


def _real(path: Path) -> str:
    return str(path.resolve())


def test_script_parses() -> None:
    assert subprocess.run(["sh", "-n", str(SCRIPT)]).returncode == 0


def test_only_the_scratch_folder_by_default(tmp_path: Path) -> None:
    assert _folders(_run(tmp_path)) == [str(tmp_path / "dev" / "repos")]


def test_reads_the_env_file_without_leaking_the_rest(tmp_path: Path) -> None:
    a = tmp_path / "a"
    a.mkdir()
    result = _run(
        tmp_path,
        env_file=(
            "WHYGRAPH_GITHUB_APP_SLUG=leak\n"
            f"DEV_FOLDERS={a}\n"
            'echo "$WHYGRAPH_GITHUB_APP_SLUG"\n'
        ),
    )
    assert _folders(result) == [str(tmp_path / "dev" / "repos"), _real(a)]
    assert "leak" not in result.stdout + result.stderr


def test_the_environment_wins_over_the_file(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    result = _run(tmp_path, env_file=f"DEV_FOLDERS={a}\n", dev_folders=str(b))
    assert _folders(result)[1:] == [_real(b)]


def test_expands_canonicalises_and_dedupes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "x").mkdir(parents=True)
    a = tmp_path / "a b"
    a.mkdir()
    (tmp_path / "link").symlink_to(a)
    repos = tmp_path / "dev" / "repos"
    value = f"~/x::{a}:{tmp_path / 'link'}:{repos}:~"
    assert _folders(_run(tmp_path, dev_folders=value)) == [
        str(repos),
        _real(home / "x"),
        _real(a),
        _real(home),
    ]


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("{tmp}/missing", "is not a directory"),
        ("relative/dir", "is not an absolute path"),
        ("/", "refusing to share '/'"),
        ("{tmp}/a,b", "are not allowed in the path"),
        ("{tmp}", "contains the dev directory"),
        ("{tmp}/dev/repos/demo", "is inside the dev directory"),
    ],
)
def test_refuses_what_the_portal_would_not_share(
    tmp_path: Path, entry: str, message: str
) -> None:
    (tmp_path / "a,b").mkdir()
    (tmp_path / "dev" / "repos" / "demo").mkdir(parents=True)
    result = _run(tmp_path, env_file=f"DEV_FOLDERS='{entry.format(tmp=tmp_path)}'\n")
    assert result.returncode == 1
    assert result.stdout == ""
    assert message in result.stderr
    assert "DEV_FOLDERS in" in result.stderr
