"""``Repository.worktree_matches`` and ``Repository.resolve_commit`` (plan 63 step 5).

``worktree_matches`` is what lets a linked checkout send a range or a
symbol anchor to the platform: it must answer ``True`` only when the
working-tree file is byte-identical to the default branch's, and ``False``
for every way git can be talked into skipping the file (assume-unchanged,
skip-worktree) or a path it does not track at the ref.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from whygraph.services.git import Repository

D = "refs/remotes/origin/main"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Dev",
            "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "Dev",
            "GIT_COMMITTER_EMAIL": "dev@example.com",
        },
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Repository, Path, str]:
    """A checkout whose ``origin/main`` holds ``a.py``, ``g*[x].py`` and ``pkg/b.py``."""
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.py").write_text("one\ntwo\n")
    (root / "g*[x].py").write_text("glob\n")
    (root / "gx.py").write_text("plain\n")
    (root / "pkg").mkdir()
    (root / "pkg" / "b.py").write_text("bee\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "first")
    sha = _git(root, "rev-parse", "HEAD").strip()
    _git(root, "update-ref", D, sha)
    return Repository(root), root, sha


def test_identical_file_matches(repo: tuple[Repository, Path, str]) -> None:
    r, _root, _sha = repo
    assert r.worktree_matches(D, "a.py") is True
    assert r.worktree_matches(D, "pkg/b.py") is True


def test_modified_file_does_not_match(repo: tuple[Repository, Path, str]) -> None:
    r, root, _sha = repo
    (root / "a.py").write_text("one\nTWO\n")
    assert r.worktree_matches(D, "a.py") is False


def test_staged_change_does_not_match(repo: tuple[Repository, Path, str]) -> None:
    r, root, _sha = repo
    (root / "a.py").write_text("one\nstaged\n")
    _git(root, "add", "a.py")
    assert r.worktree_matches(D, "a.py") is False


def test_committed_unpushed_change_does_not_match(
    repo: tuple[Repository, Path, str],
) -> None:
    r, root, _sha = repo
    (root / "a.py").write_text("one\nlocal commit\n")
    _git(root, "commit", "-q", "-am", "local")
    assert r.worktree_matches(D, "a.py") is False


def test_skip_worktree_hides_nothing(repo: tuple[Repository, Path, str]) -> None:
    r, root, _sha = repo
    _git(root, "update-index", "--skip-worktree", "a.py")
    (root / "a.py").write_text("one\nhidden edit\n")
    # git diff itself is fooled - which is why the index tags are read.
    assert (
        subprocess.run(
            ["git", "diff", "--quiet", D, "--", "a.py"], cwd=root, check=False
        ).returncode
        == 0
    )
    assert r.worktree_matches(D, "a.py") is False


def test_assume_unchanged_hides_nothing(repo: tuple[Repository, Path, str]) -> None:
    r, root, _sha = repo
    _git(root, "update-index", "--assume-unchanged", "a.py")
    (root / "a.py").write_text("one\nhidden edit\n")
    assert r.worktree_matches(D, "a.py") is False
    # Even an unmodified file is refused while the flag is set.
    (root / "a.py").write_text("one\ntwo\n")
    assert r.worktree_matches(D, "a.py") is False


def test_untracked_file_does_not_match(repo: tuple[Repository, Path, str]) -> None:
    r, root, _sha = repo
    (root / "new.py").write_text("new\n")
    assert r.worktree_matches(D, "new.py") is False
    # A file that neither side has: git diff exits 0, the tree check refuses.
    assert r.worktree_matches(D, "missing.py") is False


def test_file_removed_from_the_index_does_not_match(
    repo: tuple[Repository, Path, str],
) -> None:
    r, root, _sha = repo
    _git(root, "rm", "-q", "--cached", "a.py")
    assert r.worktree_matches(D, "a.py") is False


def test_directory_is_not_a_file_match(repo: tuple[Repository, Path, str]) -> None:
    r, _root, _sha = repo
    assert r.worktree_matches(D, "pkg") is False


def test_glob_characters_are_literal(repo: tuple[Repository, Path, str]) -> None:
    r, root, _sha = repo
    assert r.worktree_matches(D, "g*[x].py") is True
    # A pattern-shaped name that only exists locally never matches a tracked
    # file it would glob onto (``g*`` -> ``gx.py``).
    (root / "g*.py").write_text("plain\n")
    assert r.worktree_matches(D, "g*.py") is False
    (root / "g*[x].py").write_text("changed\n")
    assert r.worktree_matches(D, "g*[x].py") is False
    assert r.worktree_matches(D, "gx.py") is True


def test_unusable_arguments(repo: tuple[Repository, Path, str]) -> None:
    r, _root, _sha = repo
    assert r.worktree_matches("--output=x", "a.py") is False
    assert r.worktree_matches(D, "") is False
    assert r.worktree_matches("refs/remotes/origin/nope", "a.py") is False
    # An option-shaped path goes after `--`: just an unknown path.
    assert r.worktree_matches(D, "--output=x") is False


def test_resolve_commit_expands_a_prefix(repo: tuple[Repository, Path, str]) -> None:
    r, _root, sha = repo
    assert r.resolve_commit(sha[:7]) == sha
    assert r.resolve_commit(sha[:12].upper()) == sha
    assert r.resolve_commit(sha) == sha


def test_resolve_commit_unknown_and_unusable(
    repo: tuple[Repository, Path, str],
) -> None:
    r, root, _sha = repo
    assert r.resolve_commit("deadbee") is None
    assert r.resolve_commit("--output=x") is None
    assert not (root / "x").exists()
    assert r.resolve_commit("HEAD") is None
    assert r.resolve_commit("main") is None
    assert r.resolve_commit("abc") is None  # too short
    assert r.resolve_commit("") is None


def test_resolve_commit_refuses_a_hex_named_ref(
    repo: tuple[Repository, Path, str],
) -> None:
    """A branch spelled in hex resolves to its own commit; that is not the prefix's."""
    r, root, sha = repo
    (root / "a.py").write_text("one\nother\n")
    _git(root, "commit", "-q", "-am", "other")
    other = _git(root, "rev-parse", "HEAD").strip()
    name = next(
        c * 7 for c in "abcdef" if not sha.startswith(c) and not other.startswith(c)
    )
    _git(root, "branch", name, other)
    assert r.resolve_commit(name) is None
