"""Tests for :mod:`whygraph.services.codegraph.bootstrap`.

The bootstrap helpers shell out to a local ``codegraph`` binary when one
is on ``PATH``, else fall back to ``docker run``. Every test here
monkeypatches :func:`shutil.which` and :func:`subprocess.run` so neither a
real CodeGraph install nor a Docker daemon is needed. Each test owns its
own ``tmp_path``-rooted "project" so the idempotency / DB-creation checks
operate on isolated files.
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
    ensure_codegraph_db,
    refresh_codegraph_index,
)
from whygraph.services.codegraph.exceptions import CodeGraphBootstrapError


def _make_existing_db(project_root: Path) -> Path:
    cg_dir = project_root / ".codegraph"
    cg_dir.mkdir(exist_ok=True)
    db = cg_dir / "codegraph.db"
    db.touch()
    return db


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

    monkeypatch.setattr(bootstrap.subprocess, "run", fail_run)

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
    monkeypatch.setattr(
        bootstrap.subprocess, "run", _capturing_run(captured, create_db_at=tmp_path)
    )

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
    monkeypatch.setattr(
        bootstrap.subprocess, "run", _capturing_run(captured, create_db_at=tmp_path)
    )

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
    monkeypatch.setattr(
        bootstrap.subprocess, "run", _capturing_run(captured, create_db_at=tmp_path)
    )

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
    monkeypatch.setattr(
        bootstrap.subprocess, "run", _capturing_run(captured, create_db_at=tmp_path)
    )

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

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)

    with pytest.raises(CodeGraphBootstrapError, match="exit 7"):
        ensure_codegraph_db(tmp_path)


def test_raises_when_exits_zero_but_db_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    captured: dict[str, object] = {}
    monkeypatch.setattr(bootstrap.subprocess, "run", _capturing_run(captured))

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
    monkeypatch.setattr(bootstrap.subprocess, "run", _capturing_run(captured))

    result = refresh_codegraph_index(tmp_path)

    assert result == db.resolve()
    assert captured["cmd"] == ["codegraph", "sync", "-q"]
    assert captured["cwd"] == tmp_path.resolve()


def test_refresh_falls_back_to_init_when_db_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        bootstrap.subprocess, "run", _capturing_run(captured, create_db_at=tmp_path)
    )

    result = refresh_codegraph_index(tmp_path)

    assert result == (tmp_path / ".codegraph" / "codegraph.db").resolve()
    assert captured["cmd"] == ["codegraph", "init", "-i"]


def test_refresh_sync_via_docker_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_existing_db(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", _which("docker"))
    captured: dict[str, object] = {}
    monkeypatch.setattr(bootstrap.subprocess, "run", _capturing_run(captured))

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
    monkeypatch.setattr(
        bootstrap.subprocess, "run", _status_run(calls, status=_status_json(23, 24))
    )

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
    monkeypatch.setattr(bootstrap.subprocess, "run", _status_run(calls, status=status))

    refresh_codegraph_index(tmp_path, capture=True)

    assert calls[-1] == ["codegraph", "sync", "-q"]
    assert ["codegraph", "index", "-q"] not in calls


def test_refresh_syncs_when_status_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_existing_db(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    calls: list[list[str]] = []
    monkeypatch.setattr(
        bootstrap.subprocess, "run", _status_run(calls, status_fails=True)
    )

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
    monkeypatch.setattr(bootstrap.subprocess, "run", _capturing_run(captured))

    refresh_codegraph_index(tmp_path, capture=True)

    assert captured["capture_output"] is True


def test_capture_default_streams_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_existing_db(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    captured: dict[str, object] = {}
    monkeypatch.setattr(bootstrap.subprocess, "run", _capturing_run(captured))

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

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)

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
    monkeypatch.setattr(bootstrap.subprocess, "run", fake)

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
    monkeypatch.setattr(bootstrap.subprocess, "run", _capturing_run(captured))

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
    monkeypatch.setattr(
        bootstrap.subprocess, "run", _status_run(calls, status=_status_json(23, 24))
    )

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

    monkeypatch.setattr(bootstrap.subprocess, "run", slow)

    with pytest.raises(CodeGraphBootstrapError, match="did not finish within 2 s"):
        refresh_codegraph_index(tmp_path, capture=True, allow_rebuild=False, timeout=2)
    assert seen["timeout"] == 2


def test_refresh_env_is_the_given_base_minus_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_existing_db(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", _which("codegraph"))
    captured: dict[str, object] = {}
    monkeypatch.setattr(bootstrap.subprocess, "run", _capturing_run(captured))
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

    assert captured["env"] == {"PATH": "/bin"}
