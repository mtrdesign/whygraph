"""Tests for :mod:`whygraph.hooks` — the auto-rescan git hooks.

Exercise :func:`sync_hooks` against a real (throwaway) git repo: the
managed dispatcher is sentinel-guarded, idempotent, never clobbers a
foreign hook, reconciles in **both** directions, and the generated shell
is syntactically valid. The ``post-checkout`` arg gate is tested by
running the helper under ``sh`` with git's real argument shapes.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from whygraph.hooks import (
    HELPER_RELPATH,
    HOOK_NAMES,
    SENTINEL,
    HooksError,
    resolve_hook_names,
    sync_hooks,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


@pytest.fixture(autouse=True)
def _stub_whygraph_on_path(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Put a no-op ``whygraph`` on PATH for the helper's own guard.

    The helper exits early unless ``command -v whygraph`` succeeds, so the
    arg-gate tests need *a* binary — but not the real one, which would
    fork a detached scan into pytest's tmp dir and outlive the test.
    """
    bin_dir = tmp_path_factory.mktemp("stub-bin")
    stub = bin_dir / "whygraph"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def _hook(repo: Path, name: str) -> Path:
    return repo / ".git" / "hooks" / name


def _install_all(repo: Path):
    return sync_hooks(repo, HOOK_NAMES)


# --- ported from the retired tests/test_cli_hooks.py -------------------------


def test_install_creates_helper_and_hooks(repo: Path) -> None:
    result = _install_all(repo)

    helper = repo / HELPER_RELPATH
    assert result.helper == helper
    assert helper.exists()
    assert os.access(helper, os.X_OK)

    for name in HOOK_NAMES:
        hook = _hook(repo, name)
        assert hook.exists(), name
        assert SENTINEL in hook.read_text()
        assert os.access(hook, os.X_OK)


def test_install_is_idempotent(repo: Path) -> None:
    _install_all(repo)
    _install_all(repo)  # second run must not stack blocks

    for name in HOOK_NAMES:
        assert _hook(repo, name).read_text().count(SENTINEL) == 1, name


def test_install_appends_to_foreign_hook(repo: Path) -> None:
    foreign = _hook(repo, "post-commit")
    foreign.write_text("#!/bin/sh\necho custom-hook\n")

    _install_all(repo)

    text = foreign.read_text()
    assert "echo custom-hook" in text  # foreign content preserved
    assert SENTINEL in text  # ours appended


def test_uninstall_removes_ours_keeps_foreign(repo: Path) -> None:
    """Case 26 — ``sync_hooks(root, ())`` *is* the uninstall."""
    foreign = _hook(repo, "post-commit")
    foreign.write_text("#!/bin/sh\necho custom-hook\n")
    _install_all(repo)

    result = sync_hooks(repo, ())

    text = foreign.read_text()
    assert "echo custom-hook" in text
    assert SENTINEL not in text
    # Hooks WhyGraph created outright are removed, as is the helper.
    assert not _hook(repo, "post-merge").exists()
    assert not (repo / HELPER_RELPATH).exists()
    assert result.helper is None
    assert set(result.removed) == set(HOOK_NAMES)


def test_states_are_reported_per_hook(repo: Path) -> None:
    """The direct-inspection equivalent of the retired ``status`` command."""
    before = sync_hooks(repo, ())
    assert set(before.actions.values()) == {"absent"}

    after = _install_all(repo)
    assert set(after.actions.values()) == {"created"}
    assert set(after.installed) == set(HOOK_NAMES)


def test_not_a_git_repo_raises_hooks_error(tmp_path: Path) -> None:
    """Case 34 — a ``HooksError``, never a ``ClickException``."""
    with pytest.raises(HooksError, match="not a git repository"):
        sync_hooks(tmp_path, HOOK_NAMES)


def test_generated_shell_is_valid(repo: Path) -> None:
    """Case 33 — ``sh -n`` parses without executing."""
    _install_all(repo)

    for path in [
        repo / HELPER_RELPATH,
        *(_hook(repo, n) for n in HOOK_NAMES),
    ]:
        check = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
        assert check.returncode == 0, f"{path}: {check.stderr}"


# --- new: the four-hook set and D7's two-directional reconcile ---------------


def test_all_four_hooks_are_managed(repo: Path) -> None:
    """Case 23 — ``post-checkout`` joined the set."""
    _install_all(repo)

    assert "post-checkout" in HOOK_NAMES
    assert SENTINEL in _hook(repo, "post-checkout").read_text()


def test_shrinking_the_list_removes_dropped_hooks(repo: Path) -> None:
    """Case 24 (D7 shrink) — the half that is easy to forget."""
    _install_all(repo)

    result = sync_hooks(repo, ("post-commit", "post-merge"))

    assert SENTINEL in _hook(repo, "post-commit").read_text()
    assert SENTINEL in _hook(repo, "post-merge").read_text()
    assert not _hook(repo, "post-rewrite").exists()
    assert not _hook(repo, "post-checkout").exists()
    # The helper stays — two hooks still dispatch to it.
    assert (repo / HELPER_RELPATH).exists()
    assert set(result.removed) == {"post-rewrite", "post-checkout"}


def test_growing_the_list_restores_hooks(repo: Path) -> None:
    """Case 25 (D7 grow) — the reverse direction."""
    sync_hooks(repo, ("post-commit",))
    assert not _hook(repo, "post-rewrite").exists()

    sync_hooks(repo, HOOK_NAMES)

    for name in HOOK_NAMES:
        assert SENTINEL in _hook(repo, name).read_text(), name


def test_shrink_preserves_foreign_content_in_a_dropped_hook(repo: Path) -> None:
    """Case 27 — only the managed block goes."""
    foreign = _hook(repo, "post-rewrite")
    foreign.write_text("#!/bin/sh\necho mine\n")
    _install_all(repo)

    sync_hooks(repo, ("post-commit",))

    text = foreign.read_text()
    assert "echo mine" in text
    assert SENTINEL not in text


def test_resolve_hook_names(repo: Path) -> None:
    """Case 28 — the bool-or-list shape, and a typo'd name."""
    assert resolve_hook_names(True) == HOOK_NAMES
    assert resolve_hook_names(False) == ()
    assert resolve_hook_names(()) == ()
    assert resolve_hook_names(["post-commit"]) == ("post-commit",)
    # Normalized to HOOK_NAMES order regardless of how the config listed them.
    assert resolve_hook_names(["post-merge", "post-commit"]) == (
        "post-commit",
        "post-merge",
    )

    with pytest.raises(HooksError, match="post-comit"):
        resolve_hook_names(["post-comit"])


# --- the post-checkout arg gate ----------------------------------------------


def test_dispatcher_forwards_arguments(repo: Path) -> None:
    """Case 29 — without ``"$@"`` the helper could not tell the cases apart."""
    _install_all(repo)

    assert '"$helper" "$@"' in _hook(repo, "post-checkout").read_text()


def _run_helper(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the helper with git's post-checkout argument shape."""
    return subprocess.run(
        ["sh", str(repo / HELPER_RELPATH), *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def _scan_was_armed(repo: Path) -> bool:
    """Whether the helper got as far as arming a scan.

    The pending flag is written immediately before the detached subshell,
    and the arg gate sits above it — so its existence (or the log the
    subshell creates) is the observable signal that the gate let the call
    through, without depending on a `whygraph` binary being on PATH.
    """
    return (repo / ".whygraph" / "logs").exists() or (
        repo / ".whygraph" / "scan.pending"
    ).exists()


def test_file_checkout_is_skipped(repo: Path) -> None:
    """Case 30 — ``git checkout -- path`` passes ``0`` as the third arg."""
    _install_all(repo)

    result = _run_helper(repo, "a" * 40, "b" * 40, "0")

    assert result.returncode == 0
    assert not _scan_was_armed(repo)


def test_same_point_branch_creation_is_skipped(repo: Path) -> None:
    """Case 31 — ``git switch -c`` at the same commit: identical tree."""
    _install_all(repo)
    sha = "c" * 40

    result = _run_helper(repo, sha, sha, "1")

    assert result.returncode == 0
    assert not _scan_was_armed(repo)


def test_real_branch_switch_proceeds(repo: Path) -> None:
    """Case 32 — a genuine branch switch passes the gate."""
    _install_all(repo)

    result = _run_helper(repo, "a" * 40, "b" * 40, "1")

    assert result.returncode == 0
    assert _scan_was_armed(repo)


def test_argless_hooks_proceed(repo: Path) -> None:
    """post-commit passes no arguments; the gate must ignore it entirely."""
    _install_all(repo)

    result = _run_helper(repo)

    assert result.returncode == 0
    assert _scan_was_armed(repo)
