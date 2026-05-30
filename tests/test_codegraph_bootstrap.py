"""Tests for :mod:`whygraph.services.codegraph.bootstrap`.

The bootstrap helpers shell out to a local ``codegraph`` binary when one
is on ``PATH``, else fall back to ``docker run``. Every test here
monkeypatches :func:`shutil.which` and :func:`subprocess.run` so neither a
real CodeGraph install nor a Docker daemon is needed. Each test owns its
own ``tmp_path``-rooted "project" so the idempotency / DB-creation checks
operate on isolated files.
"""

from __future__ import annotations

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
        cmd: list[str], *, check: bool = True, cwd: Path | None = None
    ) -> subprocess.CompletedProcess:
        captured["cmd"] = cmd
        captured["cwd"] = cwd
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
        cmd: list[str], *, check: bool = True, cwd: Path | None = None
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
    assert "sync" in cmd and "-q" in cmd
    assert DEFAULT_CODEGRAPH_IMAGE in cmd
