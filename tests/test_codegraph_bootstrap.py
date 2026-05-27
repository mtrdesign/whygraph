"""Tests for :mod:`whygraph.services.codegraph.bootstrap`.

The bootstrap helper shells out to ``docker run``; every test here
monkeypatches :func:`shutil.which` and :func:`subprocess.run` so no real
Docker daemon is needed. Each test owns its own ``tmp_path``-rooted
"project" so the idempotency / DB-creation checks operate on isolated
files.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from whygraph.services.codegraph import bootstrap
from whygraph.services.codegraph.bootstrap import (
    DEFAULT_CODEGRAPH_IMAGE,
    ensure_codegraph_db,
)
from whygraph.services.codegraph.exceptions import CodeGraphBootstrapError


def _make_existing_db(project_root: Path) -> Path:
    cg_dir = project_root / ".codegraph"
    cg_dir.mkdir()
    db = cg_dir / "codegraph.db"
    db.touch()
    return db


def test_returns_immediately_when_db_exists(tmp_path: Path) -> None:
    db = _make_existing_db(tmp_path)

    result = ensure_codegraph_db(tmp_path)

    assert result == db.resolve()


def test_does_not_invoke_docker_when_db_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_existing_db(tmp_path)

    def fail_run(*args: object, **kwargs: object) -> object:
        raise AssertionError("subprocess.run must not be invoked when DB exists")

    monkeypatch.setattr(bootstrap.subprocess, "run", fail_run)

    ensure_codegraph_db(tmp_path)


def test_raises_when_docker_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: None)

    with pytest.raises(CodeGraphBootstrapError, match="docker is not on PATH"):
        ensure_codegraph_db(tmp_path)


def test_raises_when_container_exits_zero_but_db_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: "/usr/bin/docker")

    def fake_run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)

    with pytest.raises(CodeGraphBootstrapError, match="was not created"):
        ensure_codegraph_db(tmp_path)


def test_raises_when_container_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: "/usr/bin/docker")

    def fake_run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        raise subprocess.CalledProcessError(returncode=7, cmd=cmd)

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)

    with pytest.raises(CodeGraphBootstrapError, match="exit 7"):
        ensure_codegraph_db(tmp_path)


def test_happy_path_invokes_docker_and_returns_db_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: "/usr/bin/docker")
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        captured["cmd"] = cmd
        # Simulate codegraph init creating the DB.
        _make_existing_db(tmp_path)
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)

    result = ensure_codegraph_db(tmp_path)

    assert result == (tmp_path / ".codegraph" / "codegraph.db").resolve()
    assert captured["cmd"][0] == "docker"
    assert "run" in captured["cmd"]
    assert "init" in captured["cmd"]
    assert "-i" in captured["cmd"]
    assert DEFAULT_CODEGRAPH_IMAGE in captured["cmd"]


def test_custom_image_arg_threads_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: "/usr/bin/docker")
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        captured["cmd"] = cmd
        _make_existing_db(tmp_path)
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)

    ensure_codegraph_db(tmp_path, image="ghcr.io/example/cg:v9.9.9")

    assert "ghcr.io/example/cg:v9.9.9" in captured["cmd"]
    assert DEFAULT_CODEGRAPH_IMAGE not in captured["cmd"]


def test_bind_mounts_resolved_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: "/usr/bin/docker")
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        captured["cmd"] = cmd
        _make_existing_db(tmp_path)
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)

    ensure_codegraph_db(tmp_path)

    expected_mount = f"{tmp_path.resolve()}:/workspace"
    assert expected_mount in captured["cmd"]
