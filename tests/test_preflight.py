"""Tests for :mod:`whygraph.cli.preflight`.

The probes shell out to ``shutil.which`` and ``subprocess.run``; every
test monkeypatches those so the suite runs hermetic regardless of what
the host has installed. ``ANTHROPIC_API_KEY`` is managed via
``monkeypatch.setenv`` / ``delenv`` so test order can't matter.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

import pytest

from whygraph.cli import preflight
from whygraph.cli.preflight import PreflightError, run_preflight


def _git_repo(tmp_path: Path, *, remote_url: str | None) -> Path:
    """Materialise a ``.git/config`` so the GitHub-remote probe sees it."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    body = ""
    if remote_url is not None:
        body = (
            '[remote "origin"]\n'
            f"    url = {remote_url}\n"
            "    fetch = +refs/heads/*:refs/remotes/origin/*\n"
        )
    (git_dir / "config").write_text(body)
    return tmp_path


def _patch_which(monkeypatch: pytest.MonkeyPatch, missing: set[str]) -> None:
    """Make ``shutil.which`` report ``None`` for names in ``missing``, else a stub path."""

    def fake_which(name: str) -> str | None:
        return None if name in missing else f"/usr/bin/{name}"

    monkeypatch.setattr(preflight.shutil, "which", fake_which)


def _patch_gh_auth_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)


def test_happy_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _git_repo(tmp_path, remote_url="git@github.com:org/repo.git")
    _patch_which(monkeypatch, missing=set())
    _patch_gh_auth_ok(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    # Must not raise.
    run_preflight(tmp_path)


def test_git_missing_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _git_repo(tmp_path, remote_url=None)
    _patch_which(monkeypatch, missing={"git"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    with pytest.raises(PreflightError, match="git"):
        run_preflight(tmp_path)


def test_docker_absence_is_irrelevant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _git_repo(tmp_path, remote_url=None)
    _patch_which(monkeypatch, missing={"docker"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    # Docker is no longer a preflight concern — init doesn't index CodeGraph,
    # and `whygraph scan` runs the in-image binary. Must not raise.
    run_preflight(tmp_path)


def test_gh_missing_on_github_repo_is_soft_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _git_repo(tmp_path, remote_url="git@github.com:org/repo.git")
    _patch_which(monkeypatch, missing={"gh"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    # Soft — must not raise.
    run_preflight(tmp_path)


def test_gh_probe_skipped_on_non_github_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _git_repo(tmp_path, remote_url="git@gitlab.com:org/repo.git")

    calls: dict[str, int] = {}

    def counting_which(name: str) -> str | None:
        calls[name] = calls.get(name, 0) + 1
        return f"/usr/bin/{name}"

    monkeypatch.setattr(preflight.shutil, "which", counting_which)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    run_preflight(tmp_path)

    assert "gh" not in calls, "gh probe must not run on non-GitHub repos"


def test_gh_auth_status_nonzero_is_soft_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _git_repo(tmp_path, remote_url="git@github.com:org/repo.git")
    _patch_which(monkeypatch, missing=set())

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="not logged in"
        )

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    # Soft — must not raise.
    run_preflight(tmp_path)


def test_llm_missing_is_soft_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _git_repo(tmp_path, remote_url=None)
    _patch_which(monkeypatch, missing={"claude"})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    run_preflight(tmp_path)


def test_llm_ok_via_env_var_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _git_repo(tmp_path, remote_url=None)
    _patch_which(monkeypatch, missing={"claude"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-key")

    # Env var alone satisfies the LLM check even without `claude` on PATH.
    run_preflight(tmp_path)


def test_llm_ok_via_claude_cli_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _git_repo(tmp_path, remote_url=None)
    _patch_which(monkeypatch, missing=set())
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # `claude` on PATH alone satisfies the LLM check.
    run_preflight(tmp_path)


def test_hard_missing_reported_in_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _git_repo(tmp_path, remote_url=None)
    _patch_which(monkeypatch, missing={"git"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    with pytest.raises(PreflightError) as exc_info:
        run_preflight(tmp_path)

    assert "git" in str(exc_info.value)
