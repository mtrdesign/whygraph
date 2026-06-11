"""Tests for :meth:`Repository.fetch_refs` and :meth:`Repository.commit_metadata`.

Both run the real ``git`` binary against repos on disk (same pattern as
``test_services_git_diff.py``). ``fetch_refs`` is exercised against a local
bare "remote" so no network is involved; ``commit_metadata`` parses a single
commit's full message + stats.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from whygraph.services.git import GitError, Repository


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "commit.gpgsign", "false")


def test_commit_metadata_reads_single_commit_body_and_stats(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init(root)
    (root / "a.txt").write_text("one\ntwo\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "first")
    (root / "a.txt").write_text("one\ntwo\nthree\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "second subject\n\nA longer body line.")
    sha = _git(root, "rev-parse", "HEAD").strip()

    commit = Repository(root).commit_metadata(sha)

    assert commit.sha == sha
    assert commit.subject == "second subject"
    assert "A longer body line." in commit.body
    # Only the single commit's stats — not its ancestors'.
    assert commit.stats.files_changed == 1
    assert commit.stats.insertions == 1


def test_commit_metadata_unknown_ref_raises_git_error(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init(root)
    (root / "a.txt").write_text("x\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "only")

    with pytest.raises(GitError):
        Repository(root).commit_metadata("0" * 40)


def test_fetch_refs_brings_remote_commit_local(tmp_path: Path) -> None:
    """A refspec fetch pins a commit that is not reachable from any local
    branch under our own ref namespace, so it becomes readable offline."""
    remote = tmp_path / "remote"
    _init(remote)
    (remote / "f.txt").write_text("remote work\n")
    _git(remote, "add", "f.txt")
    _git(remote, "commit", "-q", "-m", "remote commit")
    remote_sha = _git(remote, "rev-parse", "HEAD").strip()
    # Park the commit under a non-branch ref the clone won't pull by default.
    _git(remote, "update-ref", "refs/pull/1/head", remote_sha)

    local = tmp_path / "local"
    _init(local)
    (local / "g.txt").write_text("local\n")
    _git(local, "add", "g.txt")
    _git(local, "commit", "-q", "-m", "local commit")
    _git(local, "remote", "add", "origin", str(remote))

    repo = Repository(local)
    # Before the fetch the object is absent — metadata read fails.
    with pytest.raises(GitError):
        repo.commit_metadata(remote_sha)

    repo.fetch_refs(["refs/pull/1/head:refs/whygraph/pull/1"])

    # Now it resolves, and the local ref lives in our namespace.
    fetched = repo.commit_metadata(remote_sha)
    assert fetched.sha == remote_sha
    assert fetched.subject == "remote commit"
    assert _git(local, "rev-parse", "refs/whygraph/pull/1").strip() == remote_sha


def test_fetch_refs_empty_is_noop(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init(root)
    (root / "a.txt").write_text("x\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "only")
    # No remote configured: an empty refspec list must not invoke git at all.
    Repository(root).fetch_refs([])
