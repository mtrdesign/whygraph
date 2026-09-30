"""End-to-end tests for the git-hook reconcile in ``initialize_project``.

Drives :func:`whygraph.project_setup.initialize_project` (what the portal's
Initialize runs) against a throwaway git repo. The hook list comes from the
call - the portal reads ``[scan].hooks`` from the project config and passes
it in - so these tests are about hook reconciliation and nothing else.
(They used to drive the 1.x ``whygraph init`` command, removed in 2.0.0.)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from whygraph.hooks import HELPER_RELPATH, HOOK_NAMES, SENTINEL
from whygraph.project_setup import HttpMcp, InitializeResult, initialize_project


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def _init(root: Path, hooks: bool | list[str]) -> InitializeResult:
    return initialize_project(
        root, agents=[], hooks=hooks, mcp=HttpMcp(slug="demo"), marker=None
    )


def _managed(repo: Path) -> set[str]:
    """Hook names currently carrying the managed block."""
    hooks_dir = repo / ".git" / "hooks"
    return {
        name
        for name in HOOK_NAMES
        if (hooks_dir / name).exists() and SENTINEL in (hooks_dir / name).read_text()
    }


def test_default_installs_all_four_hooks(repo: Path) -> None:
    """Case 35."""
    result = _init(repo, True)

    assert _managed(repo) == set(HOOK_NAMES)
    assert (repo / HELPER_RELPATH).exists()
    assert result.hooks is not None
    assert set(result.hooks.installed) == set(HOOK_NAMES)


def test_opt_out_installs_nothing(repo: Path) -> None:
    """Case 36 - ``hooks = false`` installs no hook and no helper."""
    _init(repo, False)

    assert _managed(repo) == set()
    assert not (repo / HELPER_RELPATH).exists()


def test_flipping_to_false_removes_hooks_and_helper(repo: Path) -> None:
    """Case 37 - the reconciler uninstalls too."""
    _init(repo, True)
    assert _managed(repo) == set(HOOK_NAMES)

    result = _init(repo, False)

    assert _managed(repo) == set()
    assert not (repo / HELPER_RELPATH).exists()
    assert result.hooks is not None
    assert set(result.hooks.removed) == set(HOOK_NAMES)


def test_shrinking_the_list_drops_the_others(repo: Path) -> None:
    """Case 38 (D7 end-to-end) - the removal half."""
    _init(repo, True)

    _init(repo, ["post-commit"])

    assert _managed(repo) == {"post-commit"}
    # The helper stays - post-commit still dispatches to it.
    assert (repo / HELPER_RELPATH).exists()


def test_typo_in_hook_name_warns_and_installs_nothing(repo: Path) -> None:
    """Case 14 / 41 - a bad name is a warning, not a failed initialize."""
    result = _init(repo, ["post-comit"])

    assert result.hooks is None
    assert "post-comit" in (result.hooks_error or "")
    assert _managed(repo) == set()
    # The rest of initialize still completed.
    assert (repo / ".gitignore").exists()


def test_unwritable_hooks_dir_warns_but_initialize_succeeds(repo: Path) -> None:
    """Case 16 / 42 - best-effort (section 4.6 property 1)."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hooks_dir.chmod(0o500)
    try:
        result = _init(repo, True)
    finally:
        hooks_dir.chmod(0o700)

    assert result.hooks_error and "cannot write git hooks" in result.hooks_error
    # The gitignore work completed regardless.
    assert (repo / ".gitignore").exists()


def test_not_a_git_repo_warns_but_initialize_succeeds(tmp_path: Path) -> None:
    """Outside a repo the other steps still run."""
    plain = tmp_path / "plain"
    plain.mkdir()

    result = _init(plain, True)

    assert result.hooks_error and "not a git repository" in result.hooks_error
    assert (plain / ".gitignore").exists()
