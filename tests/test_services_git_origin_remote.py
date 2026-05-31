"""Tests for :attr:`Repository.origin_url` and its configurable remote.

``origin_url`` reads the remote named by ``origin_remote`` (default
``"origin"``). These pin that the default still reads ``origin`` and that
a custom remote name (e.g. ``"upstream"``) is honored — covering the
``[scan].remote`` config knob end-to-end at the git layer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from whygraph.services.git.repository import Repository


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _repo_with_remotes(tmp_path: Path) -> Path:
    # Use a non-github host so a developer's global `url.<base>.insteadOf`
    # rewrite (commonly https://github.com → git@github.com) can't change
    # the stored URL out from under the assertions.
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(
        tmp_path, "remote", "add", "origin", "https://example.test/acme/origin-repo.git"
    )
    _git(
        tmp_path,
        "remote",
        "add",
        "upstream",
        "https://example.test/acme/upstream-repo.git",
    )
    return tmp_path


def test_origin_url_defaults_to_origin_remote(tmp_path: Path) -> None:
    repo = Repository(_repo_with_remotes(tmp_path))
    assert repo.origin_url == "https://example.test/acme/origin-repo.git"


def test_origin_url_reads_configured_remote(tmp_path: Path) -> None:
    repo = Repository(_repo_with_remotes(tmp_path), origin_remote="upstream")
    assert repo.origin_url == "https://example.test/acme/upstream-repo.git"


def test_origin_url_is_none_for_missing_remote(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    repo = Repository(tmp_path, origin_remote="nope")
    assert repo.origin_url is None
