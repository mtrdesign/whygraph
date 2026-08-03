"""Tests for :class:`Repository`'s default-branch resolution (plan §4.1).

Every case builds a real repository (and, where the remote-tracking refs
matter, a real clone) with ``subprocess`` — the resolution chain reads
``refs/remotes/*`` and shallow-clone state that no fake can reproduce
faithfully.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from whygraph.core import Shell
from whygraph.services.git import Repository
from whygraph.services.git.commands import GitRevListShasCmd


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _init(root: Path, branch: str = "main") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", branch)
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "commit.gpgsign", "false")
    return root


def _commit(root: Path, name: str) -> str:
    (root / name).write_text(f"{name}\n")
    _git(root, "add", name)
    _git(root, "commit", "-q", "-m", name)
    return _git(root, "rev-parse", "HEAD").strip()


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare-ish upstream on ``main`` with two commits."""
    root = _init(tmp_path / "origin")
    _commit(root, "one.txt")
    _commit(root, "two.txt")
    return root


def _clone(origin_root: Path, dest: Path, *extra: str) -> Path:
    subprocess.run(
        ["git", "clone", "-q", *extra, str(origin_root), str(dest)],
        check=True,
        capture_output=True,
    )
    _git(dest, "config", "user.email", "test@example.com")
    _git(dest, "config", "user.name", "Test User")
    _git(dest, "config", "commit.gpgsign", "false")
    return dest


def test_symbolic_ref_resolves_remote_head(origin: Path, tmp_path: Path) -> None:
    """Case 1 — a plain clone sets ``origin/HEAD``; it is authoritative."""
    work = _clone(origin, tmp_path / "work")

    repo = Repository(work)

    assert "origin/main" in repo.default_branch_refs


def test_falls_back_to_remote_main_when_head_unset(
    origin: Path, tmp_path: Path
) -> None:
    """Case 2 — no ``origin/HEAD`` (the plain-fetch state) → ``origin/main``."""
    work = _clone(origin, tmp_path / "work")
    # `update-ref -d` would deref the symref and delete origin/main instead.
    _git(work, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")

    repo = Repository(work)

    assert repo.default_branch_refs == ("origin/main", "main")


def test_falls_back_to_remote_master(tmp_path: Path) -> None:
    """Case 3 — a ``master`` upstream resolves through the second candidate."""
    upstream = _init(tmp_path / "upstream", branch="master")
    _commit(upstream, "one.txt")
    work = _clone(upstream, tmp_path / "work")
    # `update-ref -d` would deref the symref and delete origin/main instead.
    _git(work, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")

    repo = Repository(work)

    assert repo.default_branch_refs == ("origin/master", "master")


def test_union_includes_unpushed_local_commits(origin: Path, tmp_path: Path) -> None:
    """Case 4 — a local commit not yet pushed is still on the default branch."""
    work = _clone(origin, tmp_path / "work")
    local_sha = _commit(work, "three.txt")

    repo = Repository(work)

    assert repo.default_branch_refs == ("origin/main", "main")
    assert local_sha in repo.default_branch_shas
    # Guard the point of the union: the remote-tracking ref alone would miss it.
    remote_only = Shell().run(GitRevListShasCmd(("origin/main",)), cwd=work)
    assert local_sha not in remote_only


def test_no_remote_and_exotic_branch_resolves_nothing(tmp_path: Path) -> None:
    """Case 5 — nothing to judge against degrades to the empty answer."""
    root = _init(tmp_path / "solo", branch="trunk")
    _commit(root, "one.txt")

    repo = Repository(root)

    assert repo.default_branch_refs == ()
    assert repo.default_branch_shas == frozenset()


def test_configured_override_replaces_resolution(origin: Path, tmp_path: Path) -> None:
    """Case 6 — the override wins outright, ignoring ``origin/HEAD``."""
    work = _clone(origin, tmp_path / "work")
    _git(work, "switch", "-q", "-c", "develop")
    develop_sha = _commit(work, "dev.txt")

    repo = Repository(work, default_branch="develop")

    assert repo.default_branch_refs == ("develop",)
    assert develop_sha in repo.default_branch_shas
    # origin/main is deliberately *not* consulted once the override is set.
    assert "origin/main" not in repo.default_branch_refs


def test_configured_override_that_resolves_to_nothing(
    origin: Path, tmp_path: Path
) -> None:
    """An unresolvable override degrades to "cannot judge", never raises."""
    work = _clone(origin, tmp_path / "work")

    repo = Repository(work, default_branch="no-such-branch")

    assert repo.default_branch_refs == ()
    assert repo.default_branch_shas == frozenset()


def test_is_shallow(origin: Path, tmp_path: Path) -> None:
    """Case 7 — ``--depth=1`` is shallow; a full clone is not."""
    # `--depth` is ignored for a plain local-path clone; file:// forces the
    # real transport, which is the only way to get a genuinely shallow repo.
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth=1", origin.as_uri(), str(shallow)],
        check=True,
        capture_output=True,
    )
    full = _clone(origin, tmp_path / "full")

    assert Repository(shallow).is_shallow is True
    assert Repository(full).is_shallow is False
