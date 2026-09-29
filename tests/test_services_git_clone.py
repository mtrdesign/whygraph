"""Tests for :meth:`Repository.clone` / ``fetch_default`` / ``fast_forward``.

Everything runs the real ``git`` binary against local bare repos. The
production argv only allows https, so a clone of a "GitHub" URL is redirected
to a local bare repo with a test-only ``url.<x>.insteadOf`` plus
``protocol.file.allow=always`` injected through ``GIT_CONFIG_*`` env vars.
The production argv is asserted (and run against ``file://``) separately.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
from pathlib import Path

import pytest

from whygraph.core import Shell, ShellError
from whygraph.services.git import (
    GitError,
    InvalidRepoUrlError,
    Repository,
    git_env,
    parse_github_url,
    pass_through_env,
    strip_userinfo,
)
from whygraph.services.git.commands import GitCloneCmd, GitFetchDefaultCmd
from whygraph.services.git.credentials import GITHUB_GIT_CONFIG, TOKEN_ENV_VAR

TOKEN = "ghp_SECRETTOKEN0123456789abcdefghijkl"
URL = "https://github.com/octo/demo.git"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _commit(root: Path, name: str, content: str) -> None:
    (root / name).write_text(content)
    _git(root, "add", name)
    _git(
        root,
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=T",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        f"add {name}",
    )


@pytest.fixture
def bare(tmp_path: Path) -> Path:
    """A bare repo named ``demo.git`` with one commit on ``main``."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _commit(seed, "a.txt", "one\n")
    target = tmp_path / "demo.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(target)],
        check=True,
        capture_output=True,
    )
    return target


def _env(tmp_path: Path, bare: Path, *, token: str | None = TOKEN) -> dict[str, str]:
    """Hermetic allowlisted env that maps ``URL`` onto the local bare repo."""
    base = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    return git_env(
        token,
        environ=base,
        extra={
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "protocol.file.allow",
            "GIT_CONFIG_VALUE_0": "always",
            "GIT_CONFIG_KEY_1": f"url.{bare.parent}/demo.insteadOf",
            "GIT_CONFIG_VALUE_1": "https://github.com/octo/demo",
        },
    )


class _SpyRun:
    """Records every ``subprocess.run`` the shell makes (argv + env)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []
        real = subprocess.run

        def spy(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append((list(cmd), kwargs.get("env")))
            return real(cmd, *args, **kwargs)

        monkeypatch.setattr("whygraph.core.shell.subprocess.run", spy)


# --------------------------------------------------------------------------
# URL validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/octo/demo", ("octo", "demo")),
        ("https://github.com/octo/demo.git", ("octo", "demo")),
        ("https://github.com/o-c-t-o/my.repo_name-2", ("o-c-t-o", "my.repo_name-2")),
    ],
)
def test_parse_github_url_accepts_plain_urls(url: str, expected: tuple[str, str]):
    assert parse_github_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pw@github.com/octo/demo",
        "https://x-access-token:ghp_abc@github.com/octo/demo.git",
        "https://token@github.com/octo/demo",
        "https://gitlab.com/octo/demo",
        "https://github.com.evil.example/octo/demo",
        "https://github.com:8443/octo/demo",
        "http://github.com/octo/demo",
        "git@github.com:octo/demo.git",
        "ssh://git@github.com/octo/demo",
        "file:///tmp/repo",
        "ext::sh -c 'touch /tmp/pwned'",
        "/tmp/repo",
        "--upload-pack=touch /tmp/x",
        "https://github.com/octo/demo/",
        "https://github.com/octo/demo/tree/main",
        "https://github.com/octo/demo?x=1",
        "https://github.com/octo/..",
        "https://github.com/octo/demo\n",
        "https://github.com/octo",
        "",
    ],
)
def test_parse_github_url_rejects_everything_else(url: str):
    with pytest.raises(InvalidRepoUrlError):
        parse_github_url(url)


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://x-access-token:ghp_abc@github.com/o/r.git",
            "https://github.com/o/r.git",
        ),
        ("https://user@github.com/o/r", "https://github.com/o/r"),
        ("https://github.com/o/r.git", "https://github.com/o/r.git"),
        ("git@github.com:o/r.git", "git@github.com:o/r.git"),
        ("ssh://git@github.com/o/r", "ssh://git@github.com/o/r"),
    ],
)
def test_strip_userinfo(url: str, expected: str):
    assert strip_userinfo(url) == expected


def test_clone_rejects_bad_url_before_running_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError("git must not run for an invalid URL")

    monkeypatch.setattr("whygraph.core.shell.subprocess.run", boom)
    for bad in ("file:///etc", "https://u:p@github.com/o/r", "https://evil.test/o/r"):
        with pytest.raises(InvalidRepoUrlError):
            Repository.clone(bad, tmp_path / "dest")
    assert not (tmp_path / "dest").exists()


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------


def test_pass_through_env_is_an_allowlist():
    src = {
        "PATH": "/bin",
        "HOME": "/h",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "SSL_CERT_FILE": "/c",
        "https_proxy": "p",
        "NO_PROXY": "x",
        "GH_TOKEN": "g",
        "GITHUB_TOKEN": "g",
        "ANTHROPIC_API_KEY": "k",
        "OPENAI_API_KEY": "k",
        "WHYGRAPH_GIT_TOKEN": "t",
        "SECRET_THING": "s",
    }
    got = pass_through_env(src)
    assert set(got) == {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "SSL_CERT_FILE",
        "https_proxy",
        "NO_PROXY",
    }


def test_git_env_adds_token_and_disables_prompts():
    env = git_env(TOKEN, environ={"PATH": "/bin", "GH_TOKEN": "leak"})
    assert env == {
        "PATH": "/bin",
        "GIT_TERMINAL_PROMPT": "0",
        TOKEN_ENV_VAR: TOKEN,
    }
    assert TOKEN_ENV_VAR not in git_env(None, environ={"PATH": "/bin"})


# --------------------------------------------------------------------------
# Production argv
# --------------------------------------------------------------------------


def test_clone_argv_is_https_only_with_reset_then_inline_helper():
    argv = GitCloneCmd(URL, Path("/data/x")).argv()
    assert argv[0] == "git"
    assert argv[argv.index("clone") - 1] != "-c"  # config precedes the subcommand
    joined = argv[: argv.index("clone")]
    assert "protocol.allow=never" in joined
    assert "protocol.https.allow=always" in joined
    helpers = [a for a in joined if a.startswith("credential.helper=")]
    assert helpers[0] == "credential.helper="  # reset comes first
    assert len(helpers) == 2
    assert "github.com" in helpers[1]
    # `--` separates options from the URL and destination.
    assert argv[-3:] == ["--", URL, "/data/x"]
    assert argv[-4] == "clone"
    assert TOKEN not in " ".join(argv)


def test_fetch_argv_carries_the_same_config():
    argv = GitFetchDefaultCmd().argv()
    assert list(GITHUB_GIT_CONFIG) == argv[1 : 1 + len(GITHUB_GIT_CONFIG)]
    assert argv[-3:] == ["fetch", "--no-tags", "origin"]


@pytest.mark.parametrize("scheme", ["file", "path"])
def test_production_argv_refuses_file_transport(
    tmp_path: Path, bare: Path, scheme: str
):
    """No test override here: ``protocol.allow=never`` must block a local clone."""
    url = f"file://{bare}" if scheme == "file" else str(bare)
    env = git_env(None, environ={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    with pytest.raises(ShellError) as info:
        Shell().run(GitCloneCmd(url, tmp_path / "dest"), env=env)
    assert "not allowed" in info.value.stderr
    assert not (tmp_path / "dest").exists()


def test_production_argv_refuses_ext_transport(tmp_path: Path):
    marker = tmp_path / "pwned"
    env = git_env(None, environ={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    with pytest.raises(ShellError):
        Shell().run(
            GitCloneCmd(f"ext::sh -c 'touch {marker}'", tmp_path / "d"), env=env
        )
    assert not marker.exists()


# --------------------------------------------------------------------------
# Credential helper
# --------------------------------------------------------------------------


def _credential_fill(tmp_path: Path, request: str, token: str | None):
    env = git_env(token, environ={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    return subprocess.run(
        ["git", *GITHUB_GIT_CONFIG, "credential", "fill"],
        input=request,
        capture_output=True,
        text=True,
        env=env,
    )


def test_helper_answers_for_github_https(tmp_path: Path):
    res = _credential_fill(tmp_path, "protocol=https\nhost=github.com\n\n", TOKEN)
    assert res.returncode == 0
    assert "username=x-access-token" in res.stdout
    assert f"password={TOKEN}" in res.stdout


@pytest.mark.parametrize(
    "request_text",
    [
        "protocol=https\nhost=example.com\n\n",
        "protocol=https\nhost=github.com.evil.example\n\n",
        "protocol=https\nhost=gist.github.com\n\n",
        "protocol=http\nhost=github.com\n\n",
    ],
)
def test_helper_returns_nothing_for_other_hosts_or_protocols(
    tmp_path: Path, request_text: str
):
    res = _credential_fill(tmp_path, request_text, TOKEN)
    # Git found no credential and prompts are disabled, so it fails.
    assert res.returncode != 0
    assert TOKEN not in res.stdout
    assert TOKEN not in res.stderr


def test_helper_returns_nothing_without_a_token(tmp_path: Path):
    res = _credential_fill(tmp_path, "protocol=https\nhost=github.com\n\n", None)
    assert res.returncode != 0
    assert "password=" not in res.stdout


# --------------------------------------------------------------------------
# Clone / fetch / fast-forward against a local bare repo
# --------------------------------------------------------------------------


def test_clone_succeeds_and_leaks_no_token(
    tmp_path: Path,
    bare: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    spy = _SpyRun(monkeypatch)
    caplog.set_level(logging.DEBUG)
    dest = tmp_path / "checkout"

    repo = Repository.clone(URL, dest, env=_env(tmp_path, bare))

    assert (dest / "a.txt").read_text() == "one\n"
    assert repo.root == dest
    # The remote is stored as the plain URL, exactly as given.
    assert f"url = {URL}" in (dest / ".git" / "config").read_text()
    # The token reached the child only as an env var...
    assert spy.calls
    for argv, env in spy.calls:
        assert TOKEN not in " ".join(argv)
        assert env is not None and env[TOKEN_ENV_VAR] == TOKEN
        assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env
    # ...and is nowhere on disk or in the logs.
    assert TOKEN not in (dest / ".git" / "config").read_text()
    for path in (dest / ".git").rglob("*"):
        if path.is_file() and path.stat().st_size < 1_000_000:
            assert TOKEN.encode() not in path.read_bytes(), path
    assert TOKEN not in caplog.text
    assert "x-access-token" not in (dest / ".git" / "config").read_text()


def test_clone_without_env_is_anonymous_and_fails_cleanly_offline(
    tmp_path: Path, bare: Path
):
    # No insteadOf here, so this would reach the network; an env whose PATH
    # lacks git proves the failure path maps to GitError without touching it.
    env = {"PATH": str(tmp_path)}
    with pytest.raises(GitError, match="git is not installed"):
        Repository.clone(URL, tmp_path / "dest", env=env)


def test_clone_timeout_surfaces_as_git_error(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    with pytest.raises(GitError, match="timed out after 1s"):
        Repository.clone(
            URL,
            tmp_path / "dest",
            env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
            timeout=1,
        )


def test_failure_message_scrubs_the_token(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    fake.write_text(f"#!/bin/sh\necho 'fatal: bad {TOKEN} thing' >&2\nexit 128\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {"PATH": str(bin_dir), TOKEN_ENV_VAR: TOKEN}

    with pytest.raises(GitError) as info:
        Repository.clone(URL, tmp_path / "dest", env=env)

    assert TOKEN not in str(info.value)
    assert "***" in str(info.value)


def test_fetch_default_and_fast_forward(tmp_path: Path, bare: Path):
    env = _env(tmp_path, bare)
    dest = tmp_path / "checkout"
    repo = Repository.clone(URL, dest, env=env)
    hook = dest / ".git" / "hooks" / "post-merge"
    marker = tmp_path / "hook-ran"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)

    # Nothing new upstream: a fetch is a no-op and HEAD does not move.
    repo.fetch_default(env=env)
    assert repo.fast_forward() is False

    # Push a commit to the bare remote from a second clone.
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(other)], check=True, capture_output=True
    )
    _commit(other, "b.txt", "two\n")
    _git(other, "push", "-q", "origin", "main")

    before = _git(dest, "rev-parse", "HEAD")
    repo.fetch_default(env=env)
    assert _git(dest, "rev-parse", "HEAD") == before  # fetch alone moves nothing
    assert repo.fast_forward() is True
    assert (dest / "b.txt").exists()
    assert _git(dest, "rev-parse", "HEAD") != before
    assert not marker.exists()  # post-merge hook did not fire


def test_fast_forward_refuses_a_diverged_branch(tmp_path: Path, bare: Path):
    env = _env(tmp_path, bare)
    dest = tmp_path / "checkout"
    repo = Repository.clone(URL, dest, env=env)
    _commit(dest, "local.txt", "local\n")

    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(other)], check=True, capture_output=True
    )
    _commit(other, "remote.txt", "remote\n")
    _git(other, "push", "-q", "origin", "main")

    repo.fetch_default(env=env)
    with pytest.raises(GitError):
        repo.fast_forward()


def test_fetch_default_failure_is_a_git_error(tmp_path: Path):
    repo_dir = tmp_path / "r"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-q", "-b", "main")  # no origin remote
    env = git_env(None, environ={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    with pytest.raises(GitError, match="fetch in"):
        Repository(repo_dir).fetch_default(env=env)
