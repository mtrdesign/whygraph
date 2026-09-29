"""Tests for ``GitHubClient.check_auth(env=)`` and the repo access probe."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from whygraph.services.github import (
    GitHubClient,
    GitHubError,
    RepoAccessError,
    check_repo_access,
    github_env,
)

TOKEN = "ghp_SECRETTOKEN0123456789abcdefghijkl"


class _StubShell:
    """Returns a canned ``CompletedProcess`` and records the call."""

    def __init__(self, returncode=0, stdout="", stderr="", raises=None):
        self.result = subprocess.CompletedProcess([], returncode, stdout, stderr)
        self.raises = raises
        self.calls: list[tuple[list[str], dict | None]] = []

    def run(self, cmd, *, check=True, env=None, **_):
        self.calls.append((list(cmd), env))
        if self.raises:
            raise self.raises
        return self.result


def _probe(shell: _StubShell):
    return check_repo_access("octo", "demo", TOKEN, shell=shell)  # type: ignore[arg-type]


def test_probe_success_reports_the_repository():
    shell = _StubShell(
        stdout=json.dumps(
            {"full_name": "octo/Demo", "private": True, "default_branch": "trunk"}
        )
    )
    access = _probe(shell)
    assert (access.full_name, access.private, access.default_branch) == (
        "octo/Demo",
        True,
        "trunk",
    )
    argv, env = shell.calls[0]
    assert argv == ["gh", "api", "repos/octo/demo"]
    assert env["GH_TOKEN"] == TOKEN
    assert TOKEN not in " ".join(argv)


@pytest.mark.parametrize(
    "stderr, code",
    [
        ("gh: Bad credentials (HTTP 401)", "bad_token"),
        (
            "gh: Resource not accessible by personal access token (HTTP 403)",
            "no_access",
        ),
        (
            "gh: Resource protected by organization SAML enforcement (HTTP 403)",
            "no_access",
        ),
        ("gh: Not Found (HTTP 404)", "not_found"),
        (
            "To get started with GitHub CLI, please run:  gh auth login",
            "bad_token",
        ),
    ],
)
def test_probe_maps_failures_to_codes(stderr: str, code: str):
    with pytest.raises(RepoAccessError) as info:
        _probe(_StubShell(returncode=1, stderr=stderr))
    assert info.value.code == code


@pytest.mark.parametrize(
    "shell",
    [
        _StubShell(
            returncode=1, stderr="gh: API rate limit exceeded for user (HTTP 403)"
        ),
        _StubShell(returncode=1, stderr="gh: Server Error (HTTP 502)"),
        _StubShell(returncode=1, stderr="dial tcp: no such host"),
        _StubShell(stdout="not json"),
        _StubShell(raises=FileNotFoundError("gh")),
        _StubShell(raises=subprocess.TimeoutExpired(["gh"], 30)),
    ],
)
def test_probe_other_failures_are_plain_github_errors(shell: _StubShell):
    with pytest.raises(GitHubError) as info:
        _probe(shell)
    assert not isinstance(info.value, RepoAccessError)


def test_probe_error_text_never_contains_the_token():
    shell = _StubShell(returncode=1, stderr=f"weird failure echoing {TOKEN}")
    with pytest.raises(GitHubError) as info:
        _probe(shell)
    assert TOKEN not in str(info.value)


def test_github_env_never_inherits_ambient_tokens(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GH_TOKEN", "ambient")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient")
    env = github_env(TOKEN)
    assert env["GH_TOKEN"] == TOKEN
    assert "GITHUB_TOKEN" not in env and "OPENAI_API_KEY" not in env
    assert "GH_TOKEN" not in github_env(None)


def test_probe_real_subprocess_passes_token_by_env_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A fake ``gh`` on PATH records argv and env; the token is env-only."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    fake = bin_dir / "gh"
    fake.write_text(
        f'#!/bin/sh\necho "argv: $*" >> {log}\necho "token: $GH_TOKEN" >> {log}\n'
        'echo \'{"full_name":"octo/demo","private":false,"default_branch":"main"}\'\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_TOKEN", "ambient-should-not-leak")

    access = check_repo_access("octo", "demo", TOKEN)

    assert access.default_branch == "main"
    lines = log.read_text().splitlines()
    assert lines[0] == "argv: api repos/octo/demo"
    assert lines[1] == f"token: {TOKEN}"


def test_check_auth_uses_the_supplied_env(monkeypatch: pytest.MonkeyPatch):
    seen: list[dict | None] = []

    class FakeShell:
        def run(self, cmd, *, check=True, env=None, **_):
            seen.append(env)
            return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("whygraph.services.github.client.Shell", FakeShell)
    env = github_env(TOKEN)
    GitHubClient.check_auth(env=env)
    GitHubClient.check_auth()  # legacy call shape still inherits (env=None)
    assert seen == [env, None]


def test_check_auth_env_failure_raises_github_error(monkeypatch: pytest.MonkeyPatch):
    class FakeShell:
        def run(self, cmd, *, check=True, env=None, **_):
            return subprocess.CompletedProcess(cmd, 1, "", "invalid token")

    monkeypatch.setattr("whygraph.services.github.client.Shell", FakeShell)
    with pytest.raises(GitHubError, match="not authenticated"):
        GitHubClient.check_auth(env=github_env(TOKEN))
