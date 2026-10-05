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
from whygraph.services.git.credentials import (
    GITHUB_URL_ENV,
    TOKEN_ENV_VAR,
    GitHost,
    github_git_config,
    github_git_host,
    redact_tokens,
)

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


def test_parse_github_url_follows_the_configured_host():
    ghes = {GITHUB_URL_ENV: "https://GHE.example.com:8443/"}
    assert parse_github_url("https://ghe.example.com:8443/o/r.git", environ=ghes) == (
        "o",
        "r",
    )
    fake = {GITHUB_URL_ENV: "http://127.0.0.1:9999"}
    assert parse_github_url("http://127.0.0.1:9999/acme/api.git", environ=fake) == (
        "acme",
        "api",
    )
    for url, environ in [
        ("https://github.com/o/r", ghes),  # github.com is not the configured host
        ("https://ghe.example.com/o/r", ghes),  # the port is part of the host
        ("https://127.0.0.1:9999/acme/api", fake),  # the scheme is too
        ("http://127.0.0.1:9998/acme/api", fake),
    ]:
        with pytest.raises(InvalidRepoUrlError):
            parse_github_url(url, environ=environ)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, GitHost("https", "github.com")),
        ("", GitHost("https", "github.com")),
        ("https://github.com/", GitHost("https", "github.com")),
        ("https://github.com:443", GitHost("https", "github.com")),
        ("https://GHE.Example.com:8443", GitHost("https", "ghe.example.com:8443")),
        (
            "https://code.example.com/github",
            GitHost("https", "code.example.com", "/github"),
        ),
        ("http://127.0.0.1:9999", GitHost("http", "127.0.0.1:9999")),
        ("http://localhost", GitHost("http", "localhost")),
    ],
)
def test_github_git_host_reads_the_config(raw: str | None, expected: GitHost):
    assert github_git_host({} if raw is None else {GITHUB_URL_ENV: raw}) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://github.com",  # http only for a loopback host
        "http://ghe.example.com:8080",
        "http://10.0.0.1",
        "ftp://github.com",
        "https://user:pw@github.com",
        "https://github.com?x=1",
        "https://github.com#frag",
        "https://",
        "https://gith'ub.com",
        "https://github.com:99999",
    ],
)
def test_github_git_host_refuses_a_bad_url(raw: str):
    with pytest.raises(InvalidRepoUrlError, match=GITHUB_URL_ENV):
        github_git_host({GITHUB_URL_ENV: raw})


def test_http_is_allowed_only_for_a_loopback_host():
    https = github_git_config({})
    assert "protocol.https.allow=always" in https
    assert not any(a.startswith("protocol.http.") for a in https)
    ghes = github_git_config({GITHUB_URL_ENV: "https://ghe.example.com:8443"})
    assert not any(a.startswith("protocol.http.") for a in ghes)
    loopback = github_git_config({GITHUB_URL_ENV: "http://127.0.0.1:9999"})
    assert loopback[:6] == (
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        "-c",
        "protocol.http.allow=always",
    )
    with pytest.raises(InvalidRepoUrlError):
        github_git_config({GITHUB_URL_ENV: "http://ghe.example.com"})


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


def test_git_env_adds_token_and_disables_prompts_and_user_config():
    env = git_env(TOKEN, environ={"PATH": "/bin", "GH_TOKEN": "leak"})
    assert env == {
        "PATH": "/bin",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        TOKEN_ENV_VAR: TOKEN,
    }
    anonymous = git_env(None, environ={"PATH": "/bin"})
    assert TOKEN_ENV_VAR not in anonymous
    assert anonymous["GIT_CONFIG_GLOBAL"] == os.devnull
    assert anonymous["GIT_CONFIG_NOSYSTEM"] == "1"


def test_git_env_ignores_a_global_insteadof(tmp_path: Path):
    """Spike #6: a developer's ``insteadOf`` rewrote an https clone to ssh."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        '[url "git@github.com:"]\n\tinsteadOf = https://github.com/\n'
    )
    base = {"PATH": os.environ["PATH"], "HOME": str(home)}

    def rewritten(env: dict[str, str]) -> str:
        return subprocess.run(
            ["git", "ls-remote", "--get-url", URL],
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_path,
            check=True,
        ).stdout.strip()

    assert rewritten(base) == "git@github.com:octo/demo.git"  # the trap is set
    assert rewritten(git_env(None, environ=base)) == URL


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
    config = github_git_config()
    assert list(config) == argv[1 : 1 + len(config)]
    assert argv[-4:] == ["fetch", "--no-tags", "--", "origin"]


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


def _credential_fill(
    tmp_path: Path, request: str, token: str | None, github_url: str | None = None
):
    env = git_env(token, environ={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    config = github_git_config({GITHUB_URL_ENV: github_url} if github_url else {})
    return subprocess.run(
        ["git", *config, "credential", "fill"],
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


@pytest.mark.parametrize(
    ("github_url", "answered", "refused"),
    [
        (
            "https://ghe.example.com:8443",
            "protocol=https\nhost=ghe.example.com:8443\n\n",
            [
                "protocol=https\nhost=ghe.example.com\n\n",  # the port matters
                "protocol=https\nhost=github.com\n\n",
                "protocol=http\nhost=ghe.example.com:8443\n\n",
            ],
        ),
        (
            "http://127.0.0.1:9999",
            "protocol=http\nhost=127.0.0.1:9999\n\n",
            [
                "protocol=https\nhost=127.0.0.1:9999\n\n",  # the protocol matters
                "protocol=http\nhost=127.0.0.1:9998\n\n",
                "protocol=http\nhost=127.0.0.1\n\n",
                "protocol=https\nhost=github.com\n\n",
            ],
        ),
    ],
)
def test_helper_answers_for_the_configured_host_only(
    tmp_path: Path, github_url: str, answered: str, refused: list[str]
):
    res = _credential_fill(tmp_path, answered, TOKEN, github_url)
    assert res.returncode == 0, res.stderr
    assert f"password={TOKEN}" in res.stdout
    for request_text in refused:
        res = _credential_fill(tmp_path, request_text, TOKEN, github_url)
        assert res.returncode != 0, request_text
        assert TOKEN not in res.stdout and TOKEN not in res.stderr


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


def test_failure_message_scrubs_a_partly_masked_token(tmp_path: Path):
    """Spike #7: a tool prints most of an installation token, masking its tail."""
    minted = "ghs_12345_eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJJdjEifQ.c2lnbmF0dXJl"
    shown = minted[:40] + "*" * 12
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    fake.write_text(f"#!/bin/sh\necho 'fatal: token {shown} rejected' >&2\nexit 128\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    # Not the token in the env (a different one), so only the pattern can catch it.
    env = {"PATH": str(bin_dir), TOKEN_ENV_VAR: TOKEN}

    with pytest.raises(GitError) as info:
        Repository.clone(URL, tmp_path / "dest", env=env)

    message = str(info.value)
    assert minted[:12] not in message and "*" * 12 not in message
    assert "fatal: token ghs_*** rejected" in message


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("token ghp_abcdefgh1234 here", "token ghp_*** here"),
        ("ghs_1_abc.def-ghi_jkl****** done", "ghs_*** done"),
        ("gho_ABCDEFGH ghu_12345678 ghr_xyzxyzxy", "gho_*** ghu_*** ghr_***"),
        ("ghp_short and ghx_abcdefghij stay", "ghp_short and ghx_abcdefghij stay"),
    ],
)
def test_redact_tokens_matches_the_token_pattern(text: str, expected: str):
    assert redact_tokens(text) == expected


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
