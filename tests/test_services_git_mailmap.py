"""Tests for ``GitCheckMailmapCmd`` and :meth:`Repository.check_mailmap`.

The command's argv shape is pinned directly (argv batching, **not**
``--stdin`` — ``Shell.run`` has no stdin support), and the repository
method is exercised against a real ``git`` binary with a real
``.mailmap`` on disk, matching ``test_services_git_fetch_metadata.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from whygraph.services.git import GitError, Repository
from whygraph.services.git import repository as repo_mod
from whygraph.services.git.commands import GitCheckMailmapCmd


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


def _completed(stdout: str) -> CompletedProcess[str]:
    return CompletedProcess(args=["git"], returncode=0, stdout=stdout, stderr="")


# --- the command ------------------------------------------------------------


def test_argv_batches_contacts_and_never_uses_stdin() -> None:
    contacts = ("a <a@x>", "b <b@y>", "c <c@z>")

    argv = GitCheckMailmapCmd(*contacts).argv()

    assert argv == ["git", "check-mailmap", *contacts]
    assert "--stdin" not in argv


def test_parse_preserves_line_order() -> None:
    out = "A <a@x>\nB <b@y>\nC <c@z>\n"

    parsed = GitCheckMailmapCmd().parse(_completed(out))

    assert parsed == ("A <a@x>", "B <b@y>", "C <c@z>")


# --- the repository method --------------------------------------------------


def test_check_mailmap_empty_never_spawns_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("git must not be invoked for an empty contact list")

    repo = Repository(tmp_path)
    monkeypatch.setattr(repo._shell, "run", _boom)

    assert repo.check_mailmap([]) == ()


def test_check_mailmap_canonicalizes_name_and_email(tmp_path: Path) -> None:
    """A real mailmap folds two aliases onto one proper contact — including
    rewriting the display name, which is what makes ``primary_name``
    implementable."""
    root = tmp_path / "repo"
    _init(root)
    (root / ".mailmap").write_text(
        "Real Person <real@example.com> <alias@example.com>\n"
        "Real Person <real@example.com> <12345+person@users.noreply.github.com>\n"
    )

    out = Repository(root).check_mailmap(
        [
            "whatever <alias@example.com>",
            "x <12345+person@users.noreply.github.com>",
            "y <stranger@example.com>",
        ]
    )

    assert out[0] == "Real Person <real@example.com>"
    assert out[1] == "Real Person <real@example.com>"
    # An unknown contact passes through untouched — worst case is a no-op.
    assert out[2] == "y <stranger@example.com>"


def test_check_mailmap_chunks_large_input_preserving_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1,200 contacts become 3 invocations of <= 500, concatenated in input
    order — so argv can never approach ARG_MAX."""
    monkeypatch.setattr(repo_mod, "_MAILMAP_CHUNK", 500)
    contacts = [f"n{i} <u{i}@example.com>" for i in range(1200)]
    calls: list[int] = []

    def _fake_run(cmd: GitCheckMailmapCmd, **_kw: object) -> tuple[str, ...]:
        calls.append(len(cmd.contacts))
        return tuple(cmd.contacts)

    repo = Repository(tmp_path)
    monkeypatch.setattr(repo._shell, "run", _fake_run)

    out = repo.check_mailmap(contacts)

    assert calls == [500, 500, 200]
    assert list(out) == contacts


def test_check_mailmap_git_failure_raises_git_error(tmp_path: Path) -> None:
    """A non-git directory surfaces as GitError, not ShellError — matching
    every sibling method on Repository."""
    with pytest.raises(GitError):
        Repository(tmp_path).check_mailmap(["x <x@example.com>"])
