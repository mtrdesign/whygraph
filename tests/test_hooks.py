"""Tests for :mod:`whygraph.hooks` — the auto-rescan git hooks.

Exercise :func:`sync_hooks` against a real (throwaway) git repo: the
managed dispatcher is sentinel-guarded, idempotent, never clobbers a
foreign hook, reconciles in **both** directions, and the generated shell
is syntactically valid. The ``post-checkout`` arg gate is tested by
running the helper under ``sh`` with git's real argument shapes, and the
portal request (section 4.11) against a recording ``curl`` stub with no
``whygraph`` on ``PATH``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from whygraph.hooks import (
    HELPER_RELPATH,
    HOOK_NAMES,
    SENTINEL,
    HooksError,
    resolve_hook_names,
    sync_hooks,
)


PORT = 8765


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo carrying a valid ``portal.env`` (the portal-managed state)."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".whygraph").mkdir()
    (root / ".whygraph" / "portal.env").write_text(f"slug=demo\nport={PORT}\n")
    return root


@pytest.fixture(autouse=True)
def fake_curl(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """Put a recording ``curl`` on PATH, and make sure no ``whygraph`` is.

    The helper POSTs to the portal with ``curl``; the stub writes its argv
    (one arg per line) to ``calls/<n>`` and exits with ``$FAKE_CURL_EXIT``
    (default 0), so no test ever reaches a real portal. PATH is only the
    stub dir, holding links to the tools the helper needs, so neither ``whygraph`` (GUI git clients have no shim either) nor the
    real ``curl`` is found.
    """
    bin_dir = tmp_path_factory.mktemp("stub-bin")
    calls = tmp_path_factory.mktemp("curl-calls")
    stub = bin_dir / "curl"
    stub.write_text(
        "#!/bin/sh\n"
        f'out="{calls}/$$"\n'
        'for a in "$@"; do printf \'%s\\n\' "$a"; done > "$out.tmp"\n'
        'mv "$out.tmp" "$out"\n'
        'exit "${FAKE_CURL_EXIT:-0}"\n'
    )
    stub.chmod(0o755)
    # Only the tools the helper (and git, and the tests) need - linked in,
    # so neither `whygraph` nor the real `curl` can be found.
    for tool in ("sh", "git", "sed", "head", "date", "mkdir", "mv", "touch"):
        found = shutil.which(tool)
        assert found, tool
        (bin_dir / tool).symlink_to(found)
    path = str(bin_dir)
    assert shutil.which("whygraph", path=path) is None
    monkeypatch.setenv("PATH", path)
    monkeypatch.delenv("FAKE_CURL_EXIT", raising=False)
    return SimpleNamespace(bin=bin_dir, calls=calls)


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

    The pending flag is written immediately before the detached POST, and
    the arg gate and the ``portal.env`` checks sit above it - so its
    existence is the observable signal that the gate let the call through.
    Only the flag counts (not ``.whygraph/logs``, which a rejected call
    also creates), so the negative arg-gate tests stay meaningful.
    """
    return (repo / ".whygraph" / "scan.pending").exists()


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


# --- the portal request (plan section 4.11) ----------------------------------


def _wait(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    return predicate()


def _curl_calls(fake_curl: SimpleNamespace) -> list[list[str]]:
    return [
        p.read_text().splitlines()
        for p in sorted(fake_curl.calls.iterdir())
        if not p.name.endswith(".tmp")
    ]


def _log_lines(repo: Path) -> list[str]:
    log = repo / ".whygraph" / "logs" / "hooks.log"
    return log.read_text().splitlines() if log.exists() else []


def _settle() -> None:
    """Give a detached subshell time to have acted, for negative checks."""
    time.sleep(0.3)


def test_the_hook_posts_to_the_portal_without_whygraph_on_path(
    repo: Path, fake_curl: SimpleNamespace
) -> None:
    _install_all(repo)

    result = _run_helper(repo)

    assert result.returncode == 0
    (argv,) = _wait(lambda: _curl_calls(fake_curl))
    assert argv == [
        "-fsS",
        "-m",
        "2",
        "--noproxy",
        "*",
        "-X",
        "POST",
        "-H",
        "X-WhyGraph-Client: 1",
        "-H",
        "Content-Type: application/json",
        "-d",
        '{"trigger":"hook"}',
        f"http://127.0.0.1:{PORT}/api/projects/demo/scans",
    ]
    _settle()
    assert _log_lines(repo) == []  # a successful request logs nothing


def test_no_local_scan_and_no_lock_ever(repo: Path, fake_curl: SimpleNamespace) -> None:
    # A `whygraph` that records any call: the helper must never run it.
    ran = fake_curl.bin.parent / "whygraph-ran"
    shim = fake_curl.bin / "whygraph"
    shim.write_text(f'#!/bin/sh\ntouch "{ran}"\n')
    shim.chmod(0o755)
    _install_all(repo)

    for _ in range(3):
        assert _run_helper(repo).returncode == 0
    _wait(lambda: len(_curl_calls(fake_curl)) == 3)
    _settle()

    assert not ran.exists()
    assert not (repo / ".whygraph" / "scan.lock").exists()
    assert "whygraph scan" not in (repo / HELPER_RELPATH).read_text()
    assert "scan.lock" not in (repo / HELPER_RELPATH).read_text()


def test_portal_down_logs_one_line_and_exits_zero(
    repo: Path, fake_curl: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CURL_EXIT", "7")  # curl: couldn't connect
    _install_all(repo)

    result = _run_helper(repo)

    assert result.returncode == 0
    lines = _wait(lambda: _log_lines(repo))
    _settle()
    assert len(_log_lines(repo)) == 1
    assert "portal not reachable on port 8765" in lines[0]


def test_curl_missing_logs_one_line(repo: Path, fake_curl: SimpleNamespace) -> None:
    (fake_curl.bin / "curl").unlink()
    _install_all(repo)

    assert _run_helper(repo).returncode == 0

    lines = _wait(lambda: _log_lines(repo))
    _settle()
    assert len(_log_lines(repo)) == 1 and "curl not found" in lines[0]


@pytest.mark.parametrize(
    "content",
    [
        "slug=Demo\nport=8765\n",  # upper case
        "slug=demo/x\nport=8765\n",
        "slug=demo\nport=87a5\n",
        "slug=demo\n",  # no port
        "port=8765\n",  # no slug
        "slug=demo \nport=8765\n",
    ],
)
def test_an_invalid_portal_env_sends_nothing_and_logs_one_line(
    repo: Path, fake_curl: SimpleNamespace, content: str
) -> None:
    (repo / ".whygraph" / "portal.env").write_text(content)
    _install_all(repo)

    assert _run_helper(repo).returncode == 0

    _settle()
    assert _curl_calls(fake_curl) == []
    lines = _log_lines(repo)
    assert len(lines) == 1 and "invalid" in lines[0]
    assert not _scan_was_armed(repo)


def test_portal_env_is_parsed_never_sourced(
    repo: Path, fake_curl: SimpleNamespace, tmp_path: Path
) -> None:
    pwned = tmp_path / "pwned"
    (repo / ".whygraph" / "portal.env").write_text(
        f"slug=$(touch {pwned})\nport=8765\n`touch {pwned}`\ntouch {pwned}\n"
    )
    _install_all(repo)

    assert _run_helper(repo).returncode == 0

    _settle()
    assert not pwned.exists()
    assert not (repo / "pwned").exists()
    assert _curl_calls(fake_curl) == []
    assert len(_log_lines(repo)) == 1


def test_a_missing_portal_env_logs_one_line(
    repo: Path, fake_curl: SimpleNamespace
) -> None:
    (repo / ".whygraph" / "portal.env").unlink()
    _install_all(repo)

    assert _run_helper(repo).returncode == 0

    _settle()
    assert _curl_calls(fake_curl) == []
    lines = _log_lines(repo)
    assert len(lines) == 1 and "no .whygraph/portal.env" in lines[0]


def test_a_git_tracked_portal_env_is_ignored(
    repo: Path, fake_curl: SimpleNamespace
) -> None:
    subprocess.run(["git", "add", "-f", ".whygraph/portal.env"], cwd=repo, check=True)
    _install_all(repo)

    assert _run_helper(repo).returncode == 0

    _settle()
    assert _curl_calls(fake_curl) == []
    lines = _log_lines(repo)
    assert len(lines) == 1 and "tracked by git" in lines[0]


def test_a_real_commit_fires_the_request(
    repo: Path, fake_curl: SimpleNamespace
) -> None:
    """End to end through git: the installed post-commit hook POSTs once."""
    _install_all(repo)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True, env=env)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "c"],
        cwd=repo,
        check=True,
        env=env,
    )

    calls = _wait(lambda: _curl_calls(fake_curl))
    assert len(calls) == 1
    assert calls[0][-1] == f"http://127.0.0.1:{PORT}/api/projects/demo/scans"
