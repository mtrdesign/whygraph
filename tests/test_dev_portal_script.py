"""Tests for the pure helpers of ``scripts/dev_portal.py`` (the dev wrapper).

``scripts/`` is not a package and is not collected, so the module is loaded
by path. Only the change detection and argument parsing are tested here; the
process handling is exercised by running ``make dev-local``.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "dev_portal.py"
_spec = importlib.util.spec_from_file_location("dev_portal", _PATH)
assert _spec is not None and _spec.loader is not None
dev_portal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dev_portal)


def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    os.utime(path, (mtime, mtime))


def test_snapshot_watches_sources_and_skips_caches_and_static(tmp_path: Path) -> None:
    _touch(tmp_path / "a.py", 1)
    _touch(tmp_path / "prompts" / "p.md", 1)
    _touch(tmp_path / "x.toml", 1)
    _touch(tmp_path / "__pycache__" / "a.cpython-312.pyc", 1)
    _touch(tmp_path / "__pycache__" / "b.py", 1)
    _touch(tmp_path / "serve" / "static" / "index.js", 1)
    _touch(tmp_path / "serve" / "static" / "shim.py", 1)
    _touch(tmp_path / "notes.txt", 1)

    names = {p.relative_to(tmp_path).as_posix() for p in dev_portal.snapshot(tmp_path)}

    assert names == {"a.py", "prompts/p.md", "x.toml"}


def test_first_change_detects_modify_add_and_delete(tmp_path: Path) -> None:
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    _touch(a, 1)
    before = dev_portal.snapshot(tmp_path)
    assert dev_portal.first_change(before, dev_portal.snapshot(tmp_path)) is None

    _touch(a, 2)
    assert dev_portal.first_change(before, dev_portal.snapshot(tmp_path)) == a

    _touch(a, 1)
    _touch(b, 1)
    assert dev_portal.first_change(before, dev_portal.snapshot(tmp_path)) == b

    b.unlink()
    a.unlink()
    assert dev_portal.first_change(before, dev_portal.snapshot(tmp_path)) == a


@pytest.mark.parametrize(
    ("args", "env", "port"),
    [
        (["--data", "/d", "--port", "8777"], {}, 8777),
        (["--port=9001"], {}, 9001),
        ([], {"WHYGRAPH_PORT": "8800"}, 8800),
        ([], {}, 8765),
    ],
)
def test_portal_port(
    args: list[str], env: dict[str, str], port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WHYGRAPH_PORT", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert dev_portal.portal_port(args) == port
