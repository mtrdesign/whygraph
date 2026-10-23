"""Tests for :mod:`whygraph.services.codegraph.bootstrap`.

The bootstrap helpers shell out to a local ``codegraph`` binary when one
is on ``PATH``, else fall back to ``docker run``. Every test here
monkeypatches :func:`shutil.which` and :func:`subprocess.run` so neither a
real CodeGraph install nor a Docker daemon is needed. Each test owns its
own ``tmp_path``-rooted "project" so the idempotency / DB-creation checks
operate on isolated files.

The symbolic-link guard (``escaping_symlinks``, which runs git) is stubbed
to "nothing escapes" for every test here: ``tests/test_codegraph_symlinks.py``
covers it against real repositories. The guard tests at the end put it
back with an answer of their own.
"""

from __future__ import annotations

import json

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from whygraph.services.codegraph import bootstrap
from whygraph.services.codegraph.bootstrap import (
    DEFAULT_CODEGRAPH_IMAGE,
    GUARD_VERSION,
    ensure_codegraph_db,
    index_predates_guard,
    refresh_codegraph_index,
)
from whygraph.services.codegraph.exceptions import (
    CodeGraphBootstrapError,
    CodeGraphUnsafeSymlinkError,
)
from whygraph.services.codegraph.paths import GUARD_STAMP_RELPATH


@pytest.fixture(autouse=True)
def _no_escaping_links(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard finds nothing (it shells out to git, which these tests fake)."""
    monkeypatch.setattr(bootstrap, "escaping_symlinks", lambda root: ())


def _make_existing_db(project_root: Path, *, guarded: bool = True) -> Path:
    """An index; ``guarded`` stamps it as built behind the symbolic-link guard."""
    cg_dir = project_root / ".codegraph"
    cg_dir.mkdir(exist_ok=True)
    db = cg_dir / "codegraph.db"
    db.touch()
    if guarded:
        (project_root / GUARD_STAMP_RELPATH).write_text(GUARD_VERSION + "\n")
    return db


def _fake_popen(
    monkeypatch: pytest.MonkeyPatch, run: Callable[..., subprocess.CompletedProcess]
) -> None:
    """Make ``bootstrap.subprocess.Popen`` answer through a ``subprocess.run``-shaped fake.

    ``run(cmd, check=, cwd=, capture_output=, text=, env=, timeout=)`` returns a
    ``CompletedProcess`` or raises ``TimeoutExpired`` / ``CalledProcessError``,
    which the fake process reports from ``communicate`` and ``returncode``.
    """

    class FakePopen:
        pid = 2**22 + 1

        def __init__(self, cmd: list[str], **kw: object) -> None:
            self._kw = {
                "check": True,
                "cwd": kw.get("cwd"),
                "capture_output": kw.get("stdout") == subprocess.PIPE,
                "text": kw.get("text", False),
                "env": kw.get("env"),
            }
            assert kw.get("start_new_session") is True
            self._cmd = cmd
            self.returncode: int | None = None
            self._killed = False

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            if self._killed:  # the reap after the group kill
                self.returncode = -15
                return "", ""
            try:
                done = run(self._cmd, **self._kw, timeout=timeout)
            except subprocess.TimeoutExpired:
                self._killed = True
                raise
            except subprocess.CalledProcessError as exc:
                self.returncode = exc.returncode
                return exc.stdout or exc.output, exc.stderr
            self.returncode = done.returncode
            return done.stdout, done.stderr

    monkeypatch.setattr(bootstrap.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(bootstrap.os, "killpg", lambda pid, sig: None)


def _which(*available: str) -> Callable[[str], str | None]:
    """Fake ``shutil.which`` that resolves only the named tools."""
    avail = set(available)
    return lambda name: f"/usr/bin/{name}" if name in avail else None


def _capturing_run(
    captured: dict[str, object], *, create_db_at: Path | None = None
) -> Callable[..., subprocess.CompletedProcess]:
    """Fake ``subprocess.run`` recording ``cmd`` / ``cwd`` and faking success."""

    def fake_run(
        cmd: list[str],
        *,
        check: bool = True,
        cwd: Path | None = None,
        capture_output: bool = False,
        text: bool = False,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["capture_output"] = capture_output
        captured["env"] = env
        if create_db_at is not None:
            _make_existing_db(create_db_at)
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    return fake_run


# --------------------------------------------------------------------------- #
# ensure_codegraph_db — idempotency
# --------------------------------------------------------------------------- #


def test_returns_immediately_when_db_exists(tmp_path: Path) -> None:
    db = _make_existing_db(tmp_path)

    result = ensure_codegraph_db(tmp_path)

    assert result == db.resolve()


def test_does_not_invoke_subprocess_when_db_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_existing_db(tmp_path)

    def fail_run(*args: object, **kwargs: object) -> object:
        raise AssertionError("subprocess.run must not be invoked when DB exists")

    _fake_popen(monkeypatch, fail_run)

    ensure_codegraph_db(tmp_path)


def test_raises_when_neither_codegraph_nor_docker_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", _which())

    with pytest.raises(CodeGraphBootstrapError, match="neither `codegraph`"):
        ensure_codegraph_db(tmp_path)


# --------------------------------------------------------------------------- #
# ensure_codegraph_db — local binary path (the container path)
# --------------------------------------------------------------------------- #


def test_local_binary_preferred_over_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph", "docker"))
    captured: dict[str, object] = {}
    _fake_popen(monkeypatch, _capturing_run(captured, create_db_at=tmp_path))

    result = ensure_codegraph_db(tmp_path)

    assert result == (tmp_path / ".codegraph" / "codegraph.db").resolve()
    assert captured["cmd"] == ["codegraph", "init", "-i"]
    # Local invocation runs in the project root (no bind mount, no Docker).
    assert captured["cwd"] == tmp_path.resolve()


# --------------------------------------------------------------------------- #
# ensure_codegraph_db — docker fallback
# --------------------------------------------------------------------------- #


def test_docker_fallback_when_codegraph_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", _which("docker"))
    captured: dict[str, object] = {}
    _fake_popen(monkeypatch, _capturing_run(captured, create_db_at=tmp_path))

    result = ensure_codegraph_db(tmp_path)

    assert result == (tmp_path / ".codegraph" / "codegraph.db").resolve()
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd[0] == "docker"
    assert "run" in cmd
    # The WhyGraph image has no ENTRYPOINT, so the binary is named explicitly.
    assert "codegraph" in cmd
    assert "init" in cmd and "-i" in cmd
    assert DEFAULT_CODEGRAPH_IMAGE in cmd
    # Non-interactive: no TTY flag, so it works under `docker exec` / CI.
    assert "-it" not in cmd
    assert "-t" not in cmd
    assert captured["cwd"] is None


def test_custom_image_arg_threads_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", _which("docker"))
    captured: dict[str, object] = {}
    _fake_popen(monkeypatch, _capturing_run(captured, create_db_at=tmp_path))

    ensure_codegraph_db(tmp_path, image="ghcr.io/example/cg:v9.9.9")

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert "ghcr.io/example/cg:v9.9.9" in cmd
    assert DEFAULT_CODEGRAPH_IMAGE not in cmd


def test_bind_mounts_resolved_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", _which("docker"))
    captured: dict[str, object] = {}
    _fake_popen(monkeypatch, _capturing_run(captured, create_db_at=tmp_path))

    ensure_codegraph_db(tmp_path)

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert f"{tmp_path.resolve()}:/workspace" in cmd


# --------------------------------------------------------------------------- #
# ensure_codegraph_db — failure surfaces
# --------------------------------------------------------------------------- #


def test_raises_when_command_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))

    def fake_run(
        cmd: list[str],
        *,
        check: bool = True,
        cwd: Path | None = None,
        capture_output: bool = False,
        text: bool = False,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        raise subprocess.CalledProcessError(returncode=7, cmd=cmd)

    _fake_popen(monkeypatch, fake_run)

    with pytest.raises(CodeGraphBootstrapError, match="exit 7"):
        ensure_codegraph_db(tmp_path)


def test_raises_when_exits_zero_but_db_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    captured: dict[str, object] = {}
    _fake_popen(monkeypatch, _capturing_run(captured))

    with pytest.raises(CodeGraphBootstrapError, match="was not created"):
        ensure_codegraph_db(tmp_path)


# --------------------------------------------------------------------------- #
# refresh_codegraph_index — sync when present, init when missing
# --------------------------------------------------------------------------- #


def test_refresh_runs_sync_when_db_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _make_existing_db(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    captured: dict[str, object] = {}
    _fake_popen(monkeypatch, _capturing_run(captured))

    result = refresh_codegraph_index(tmp_path)

    assert result == db.resolve()
    assert captured["cmd"] == ["codegraph", "sync", "-q"]
    assert captured["cwd"] == tmp_path.resolve()


def test_refresh_falls_back_to_init_when_db_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    captured: dict[str, object] = {}
    _fake_popen(monkeypatch, _capturing_run(captured, create_db_at=tmp_path))

    result = refresh_codegraph_index(tmp_path)

    assert result == (tmp_path / ".codegraph" / "codegraph.db").resolve()
    assert captured["cmd"] == ["codegraph", "init", "-i"]


def test_refresh_sync_via_docker_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_existing_db(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", _which("docker"))
    captured: dict[str, object] = {}
    _fake_popen(monkeypatch, _capturing_run(captured))

    refresh_codegraph_index(tmp_path)

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd[0] == "docker"
    assert "codegraph" in cmd
    assert "sync" in cmd and "-q" in cmd
    assert DEFAULT_CODEGRAPH_IMAGE in cmd


def _status_run(
    calls: list[list[str]], *, status: str | None = None, status_fails: bool = False
) -> Callable[..., subprocess.CompletedProcess]:
    """Fake ``subprocess.run`` answering ``status --json`` with ``status``."""

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(cmd)
        if cmd[-2:] == ["status", "--json"]:
            if status_fails:
                raise subprocess.CalledProcessError(1, cmd, output="", stderr="boom")
            return subprocess.CompletedProcess(cmd, 0, stdout=status, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return fake_run


def _status_json(built: object, current: object) -> str:
    # Real output can carry a banner before the JSON; the parser skips it.
    return "note: whatever\n" + json.dumps(
        {
            "initialized": True,
            "index": {
                "builtWithVersion": "1.3.0",
                "builtWithExtractionVersion": built,
                "currentExtractionVersion": current,
                "reindexRecommended": built != current,
            },
        }
    )


def test_refresh_rebuilds_an_index_from_another_extraction_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`codegraph sync` keeps such an index ("Already up to date") - verified
    against CodeGraph 1.5.0 - so a full `codegraph index` runs instead."""
    _make_existing_db(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    calls: list[list[str]] = []
    _fake_popen(monkeypatch, _status_run(calls, status=_status_json(23, 24)))

    refresh_codegraph_index(tmp_path, capture=True)

    assert calls == [
        ["codegraph", "status", "--json"],
        ["codegraph", "index", "-q"],
    ]


@pytest.mark.parametrize(
    "status",
    [
        _status_json(24, 24),  # same extractor, maybe a newer CodeGraph: sync
        _status_json(None, 24),  # an older CodeGraph without the fields
        '{"initialized": true}',
        "not json at all",
    ],
)
def test_refresh_syncs_when_the_extractor_matches_or_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    _make_existing_db(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    calls: list[list[str]] = []
    _fake_popen(monkeypatch, _status_run(calls, status=status))

    refresh_codegraph_index(tmp_path, capture=True)

    assert calls[-1] == ["codegraph", "sync", "-q"]
    assert ["codegraph", "index", "-q"] not in calls


def test_refresh_syncs_when_status_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_existing_db(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    calls: list[list[str]] = []
    _fake_popen(monkeypatch, _status_run(calls, status_fails=True))

    refresh_codegraph_index(tmp_path, capture=True)

    assert calls[-1] == ["codegraph", "sync", "-q"]


# --------------------------------------------------------------------------- #
# capture=True — used by the concurrent CodeGraphCrawler under rich.Progress
# --------------------------------------------------------------------------- #


def test_capture_true_passes_capture_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_existing_db(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    captured: dict[str, object] = {}
    _fake_popen(monkeypatch, _capturing_run(captured))

    refresh_codegraph_index(tmp_path, capture=True)

    assert captured["capture_output"] is True


def test_capture_default_streams_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_existing_db(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    captured: dict[str, object] = {}
    _fake_popen(monkeypatch, _capturing_run(captured))

    refresh_codegraph_index(tmp_path)

    assert captured["capture_output"] is False


def test_capture_true_folds_stderr_into_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_existing_db(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))

    def fake_run(
        cmd: list[str],
        *,
        check: bool = True,
        cwd: Path | None = None,
        capture_output: bool = False,
        text: bool = False,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        raise subprocess.CalledProcessError(
            returncode=3, cmd=cmd, stderr="boom: index corrupt"
        )

    _fake_popen(monkeypatch, fake_run)

    with pytest.raises(CodeGraphBootstrapError, match="boom: index corrupt"):
        refresh_codegraph_index(tmp_path, capture=True)


# --------------------------------------------------------------------------- #
# credential stripping - the codegraph subprocess never sees secrets
# --------------------------------------------------------------------------- #


_SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-secret",
    "OPENAI_API_KEY": "sk-openai-secret",
    "OPENROUTER_API_KEY": "sk-or-secret",
    "DEEPSEEK_API_KEY": "sk-ds-secret",
    "GH_TOKEN": "ghp_secret",
    "WHYGRAPH_GIT_TOKEN": "gitsecret",
    "WHYGRAPH_GITHUB_TOKEN_FILE": "/data/runs/1.token",
    "WHYGRAPH_DATABASE_URL": "postgresql+psycopg://whygraph@db/whygraph",
    "WHYGRAPH_DATABASE_PASSWORD_FILE": "/data/postgres.password",
}


@pytest.mark.parametrize("db_exists", [True, False])
def test_codegraph_subprocess_env_has_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_exists: bool
) -> None:
    for name, value in _SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GITHUB_TOKEN", "unrelated-but-kept")
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    captured: dict[str, object] = {}
    if db_exists:
        _make_existing_db(tmp_path)
        fake = _capturing_run(captured)
    else:
        fake = _capturing_run(captured, create_db_at=tmp_path)
    _fake_popen(monkeypatch, fake)

    refresh_codegraph_index(tmp_path)

    env = captured["env"]
    assert isinstance(env, dict)
    for name in _SECRETS:
        assert name not in env
    assert "PATH" in env
    assert env["GITHUB_TOKEN"] == "unrelated-but-kept"


def test_codegraph_docker_fallback_env_has_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_existing_db(tmp_path)
    for name, value in _SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(bootstrap.shutil, "which", _which("docker"))
    captured: dict[str, object] = {}
    _fake_popen(monkeypatch, _capturing_run(captured))

    refresh_codegraph_index(tmp_path)

    env = captured["env"]
    assert isinstance(env, dict)
    assert not set(_SECRETS) & set(env)


def test_refresh_without_rebuild_skips_status_and_only_syncs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP read path never runs a full `codegraph index` (it can take minutes)."""
    _make_existing_db(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    calls: list[list[str]] = []
    _fake_popen(monkeypatch, _status_run(calls, status=_status_json(23, 24)))

    refresh_codegraph_index(tmp_path, capture=True, allow_rebuild=False)

    assert calls == [["codegraph", "sync", "-q"]]


def test_refresh_timeout_becomes_a_bootstrap_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_existing_db(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    seen: dict[str, object] = {}

    def slow(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    _fake_popen(monkeypatch, slow)

    with pytest.raises(CodeGraphBootstrapError, match="did not finish within 2 s"):
        refresh_codegraph_index(tmp_path, capture=True, allow_rebuild=False, timeout=2)
    assert seen["timeout"] == 2


def test_refresh_env_is_the_given_base_minus_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_existing_db(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    captured: dict[str, object] = {}
    _fake_popen(monkeypatch, _capturing_run(captured))
    monkeypatch.setenv("AMBIENT_ONLY", "1")

    refresh_codegraph_index(
        tmp_path,
        allow_rebuild=False,
        env={
            "PATH": "/bin",
            "GH_TOKEN": "t",
            "OPENAI_API_KEY": "k",
            "CLAUDE_CODE_OAUTH_TOKEN": "c",
        },
    )

    assert captured["env"] == {"PATH": "/bin", **bootstrap._SWITCHES}


# --------------------------------------------------------------------------- #
# The symbolic-link guard and the pre-guard rebuild (plan Step 1, R16)
# --------------------------------------------------------------------------- #


def _refusing_guard(
    monkeypatch: pytest.MonkeyPatch, paths: tuple[str, ...]
) -> list[Path]:
    """Make the guard report ``paths``; the roots it was asked about."""
    asked: list[Path] = []

    def guard(root: Path) -> tuple[str, ...]:
        asked.append(root)
        return paths

    monkeypatch.setattr(bootstrap, "escaping_symlinks", guard)
    return asked


@pytest.mark.parametrize("existing", [False, True])
def test_the_guard_refuses_before_codegraph_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    if existing:
        _make_existing_db(tmp_path)
    links = tuple(f"app/link{i}.py" for i in range(7))
    asked = _refusing_guard(monkeypatch, links)
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    calls: list[list[str]] = []
    _fake_popen(monkeypatch, _status_run(calls))

    with pytest.raises(CodeGraphUnsafeSymlinkError) as raised:
        refresh_codegraph_index(tmp_path, capture=True)

    assert calls == []  # not even `codegraph status`
    assert asked == [tmp_path.resolve()]
    assert raised.value.paths == links
    message = str(raised.value)
    assert "app/link0.py" in message and "app/link4.py" in message
    assert "app/link5.py" not in message and "and 2 more" in message
    # A non-strict scan swallows it as a bootstrap failure.
    assert isinstance(raised.value, CodeGraphBootstrapError)


def test_ensure_refuses_before_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refusing_guard(monkeypatch, ("evil.py",))
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    captured: dict[str, object] = {}
    _fake_popen(monkeypatch, _capturing_run(captured))

    with pytest.raises(CodeGraphUnsafeSymlinkError, match="1 symbolic link leads out"):
        ensure_codegraph_db(tmp_path)
    assert "cmd" not in captured
    assert not (tmp_path / ".codegraph").exists()


def test_the_read_path_resync_is_refused_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mcp/targets.py` re-syncs with allow_rebuild=False: still guarded."""
    _make_existing_db(tmp_path)
    _refusing_guard(monkeypatch, ("evil.py",))
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    calls: list[list[str]] = []
    _fake_popen(monkeypatch, _status_run(calls))

    with pytest.raises(CodeGraphUnsafeSymlinkError):
        refresh_codegraph_index(tmp_path, allow_rebuild=False)
    assert calls == []


def test_a_first_index_is_stamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    captured: dict[str, object] = {}
    _fake_popen(monkeypatch, _capturing_run(captured, create_db_at=tmp_path))
    (tmp_path / ".codegraph").mkdir()

    refresh_codegraph_index(tmp_path)

    assert captured["cmd"] == ["codegraph", "init", "-i"]
    assert (tmp_path / GUARD_STAMP_RELPATH).read_text().strip() == GUARD_VERSION
    assert index_predates_guard(tmp_path) is False


def test_an_index_from_before_the_guard_is_rebuilt_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_existing_db(tmp_path, guarded=False)
    assert index_predates_guard(tmp_path) is True
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    calls: list[list[str]] = []
    _fake_popen(monkeypatch, _status_run(calls, status=_status_json(24, 24)))

    refresh_codegraph_index(tmp_path, capture=True)
    assert calls == [["codegraph", "index", "-q"]]  # no status check needed
    assert index_predates_guard(tmp_path) is False

    calls.clear()
    refresh_codegraph_index(tmp_path, capture=True)
    assert calls[-1] == ["codegraph", "sync", "-q"]
    assert ["codegraph", "index", "-q"] not in calls


def test_the_read_path_never_rebuilds_an_unstamped_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_existing_db(tmp_path, guarded=False)
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    calls: list[list[str]] = []
    _fake_popen(monkeypatch, _status_run(calls))

    refresh_codegraph_index(tmp_path, allow_rebuild=False)

    assert calls == [["codegraph", "sync", "-q"]]
    assert index_predates_guard(tmp_path) is True  # a sync never stamps


def test_a_refused_rebuild_keeps_the_index_unstamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first scan after the links are gone rebuilds it (R16)."""
    _make_existing_db(tmp_path, guarded=False)
    _refusing_guard(monkeypatch, ("evil.py",))
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    calls: list[list[str]] = []
    _fake_popen(monkeypatch, _status_run(calls))

    with pytest.raises(CodeGraphUnsafeSymlinkError):
        refresh_codegraph_index(tmp_path)
    assert index_predates_guard(tmp_path) is True

    monkeypatch.setattr(bootstrap, "escaping_symlinks", lambda root: ())
    refresh_codegraph_index(tmp_path)
    assert calls == [["codegraph", "index", "-q"]]
    assert index_predates_guard(tmp_path) is False


@pytest.mark.parametrize("stamp", [None, "0\n", ""])
def test_index_predates_guard_reads_the_stamp(
    tmp_path: Path, stamp: str | None
) -> None:
    assert index_predates_guard(tmp_path) is False  # no index at all
    _make_existing_db(tmp_path, guarded=False)
    if stamp is not None:
        (tmp_path / GUARD_STAMP_RELPATH).write_text(stamp)
    assert index_predates_guard(tmp_path) is True


def test_index_predates_guard_never_reads_through_a_linked_codegraph_dir(
    tmp_path: Path,
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "codegraph.db").touch()
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".codegraph").symlink_to(elsewhere)
    assert index_predates_guard(root) is False


# --------------------------------------------------------------------------- #
# Environment rules and process control (plan Step 2, section 4.4)
# --------------------------------------------------------------------------- #


_RUN_SWITCHES = {
    "DO_NOT_TRACK": "1",
    "CODEGRAPH_TELEMETRY": "0",
    "CODEGRAPH_NO_UPDATE_CHECK": "1",
    "CODEGRAPH_NO_DOWNLOAD": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
}


def test_codegraph_env_has_the_privacy_and_git_switches() -> None:
    env = bootstrap._codegraph_env({"PATH": "/bin", "HOME": "/tmp"})

    assert {k: env[k] for k in _RUN_SWITCHES} == _RUN_SWITCHES
    assert env["PATH"] == "/bin"


def test_scan_env_never_disables_the_watcher_and_init_has_no_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    monkeypatch.setenv("CODEGRAPH_NO_WATCH", "1")
    monkeypatch.setenv("CODEGRAPH_MCP_TOOLS", "all")
    captured: dict[str, object] = {}
    _fake_popen(monkeypatch, _capturing_run(captured, create_db_at=tmp_path))

    ensure_codegraph_db(tmp_path)

    # The ambient value is passed through only when the caller says so; the
    # bootstrap itself never sets either switch (R18: under NO_WATCH, `init`
    # offers CodeGraph's own git hooks).
    clean = bootstrap._codegraph_env({"PATH": "/bin"})
    assert "CODEGRAPH_NO_WATCH" not in clean
    assert "CODEGRAPH_NO_DAEMON" not in clean
    assert "CODEGRAPH_MCP_TOOLS" not in clean
    assert captured["cmd"] == ["codegraph", "init", "-i"]
    assert "-y" not in captured["cmd"] and "--yes" not in captured["cmd"]


def test_hosted_env_has_the_switches_and_the_pinned_tools(tmp_path: Path) -> None:
    home = tmp_path / "codegraph-home"
    base = {
        "PATH": "/bin",
        "HOME": "/root",
        "WHYGRAPH_DATA": "/data",
        "WHYGRAPH_DATABASE_URL": "postgresql://x",
        "GH_TOKEN": "t",
        "OPENAI_API_KEY": "k",
        "CODEGRAPH_MCP_DEBUG": "1",
    }

    env = bootstrap.hosted_env(home, base)

    assert {k: env[k] for k in _RUN_SWITCHES} == _RUN_SWITCHES
    assert env["CODEGRAPH_NO_WATCH"] == "1"
    assert env["CODEGRAPH_NO_DAEMON"] == "1"
    assert env["CODEGRAPH_MCP_TOOLS"] == "explore,node,search,impact"
    assert env["HOME"] == str(home)
    assert env["PATH"] == "/bin"
    for name in ("GH_TOKEN", "OPENAI_API_KEY", "WHYGRAPH_DATABASE_URL"):
        assert name not in env


def test_hosted_env_defaults_to_the_pass_through_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHYGRAPH_DATA", "/data")
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("CODEGRAPH_MCP_DEBUG", "1")

    env = bootstrap.hosted_env(tmp_path)

    assert not any(k.startswith("WHYGRAPH_") for k in env)
    assert "GH_TOKEN" not in env
    assert "CODEGRAPH_MCP_DEBUG" not in env


def test_a_timeout_kills_the_forked_grandchild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The npm-shim shape: the child forks a Node-like grandchild that outlives it."""
    import os
    import time

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pid_file = tmp_path / "grandchild.pid"
    script = bin_dir / "codegraph"
    script.write_text(f'#!/bin/sh\nsleep 60 &\necho $! > "{pid_file}"\nwait\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    _make_existing_db(tmp_path)

    # 3 s, not 1: a loaded machine must still get to fork and write the pid.
    with pytest.raises(CodeGraphBootstrapError, match="did not finish"):
        refresh_codegraph_index(tmp_path, allow_rebuild=False, timeout=3)

    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pid, 9)
    pytest.fail("the grandchild survived the timeout")
