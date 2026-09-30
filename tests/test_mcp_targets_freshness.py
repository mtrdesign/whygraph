"""A ``qualified_name`` target re-syncs a stale CodeGraph index before use.

Before this, ``resolve_target`` read a symbol's line range straight from the
index. After an edit that shifted a function, blame then ran on the wrong
lines until the next scan. The unit tests drive the check with a fake index
and a stubbed ``codegraph sync``; the last test runs the real binary.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import build_fake_codegraph_db
from whygraph.mcp import targets
from whygraph.services.codegraph import CodeGraphBootstrapError

A_SOURCE = "def a():\n    return 1\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo root with ``src/pkg/a.py`` indexed (content_hash recorded)."""
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "a.py").write_text(A_SOURCE)
    (root / ".codegraph").mkdir()
    db = build_fake_codegraph_db(root / ".codegraph" / "codegraph.db")
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE files ADD COLUMN content_hash TEXT")
        conn.execute(
            "INSERT INTO files (path, language, content_hash) VALUES (?, 'python', ?)",
            ("src/pkg/a.py", _sha(A_SOURCE)),
        )
    monkeypatch.setattr(targets, "repo_root", lambda: root)
    monkeypatch.setattr(
        targets, "get_config", lambda: SimpleNamespace(codegraph_db=None)
    )
    return root


def _resolve() -> targets.Target:
    return targets.resolve_target(
        path=None, line_start=None, line_end=None, qualified_name="pkg.a"
    )


def _fake_sync(root: Path, *, start: int, end: int, calls: list[Path]):
    """A stand-in for ``codegraph sync``: re-index a.py at a new range."""

    def _refresh(project_root: Path, **_kw: object) -> Path:
        calls.append(project_root)
        db = root / ".codegraph" / "codegraph.db"
        text = (root / "src" / "pkg" / "a.py").read_text()
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE nodes SET start_line = ?, end_line = ? WHERE id = 'n_a'",
                (start, end),
            )
            conn.execute(
                "UPDATE files SET content_hash = ? WHERE path = 'src/pkg/a.py'",
                (_sha(text),),
            )
        return db

    return _refresh


def test_fresh_file_uses_the_index_without_syncing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(
        targets, "refresh_codegraph_index", lambda *a, **k: calls.append(a)
    )

    target = _resolve()

    assert (target.line_start, target.line_end, target.index_stale) == (1, 5, False)
    assert calls == []


def test_changed_file_is_resynced_and_looked_up_again(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "src" / "pkg" / "a.py").write_text("# moved\n# down\n" + A_SOURCE)
    calls: list[Path] = []
    monkeypatch.setattr(
        targets,
        "refresh_codegraph_index",
        _fake_sync(repo, start=3, end=7, calls=calls),
    )

    target = _resolve()

    assert (target.line_start, target.line_end, target.index_stale) == (3, 7, False)
    assert calls == [repo]
    assert "index_stale" not in targets.target_dict(target)


def test_failed_resync_keeps_the_old_range_and_flags_it(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "src" / "pkg" / "a.py").write_text("# moved\n" + A_SOURCE)

    def _boom(*_a: object, **_k: object) -> Path:
        raise CodeGraphBootstrapError("`codegraph sync -q` failed (exit 1)")

    monkeypatch.setattr(targets, "refresh_codegraph_index", _boom)

    target = _resolve()

    assert (target.line_start, target.line_end) == (1, 5)
    assert target.index_stale is True
    assert targets.target_dict(target)["index_stale"] is True


def test_deleted_file_counts_as_stale(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "src" / "pkg" / "a.py").unlink()
    calls: list[object] = []

    def _refresh(*a: object, **_k: object) -> Path:
        calls.append(a)
        raise CodeGraphBootstrapError("gone")

    monkeypatch.setattr(targets, "refresh_codegraph_index", _refresh)

    assert _resolve().index_stale is True
    assert len(calls) == 1


def test_symlinked_file_is_never_read_or_resynced(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("secret\n")
    a = repo / "src" / "pkg" / "a.py"
    a.unlink()
    a.symlink_to(outside)
    calls: list[object] = []
    monkeypatch.setattr(
        targets, "refresh_codegraph_index", lambda *a, **k: calls.append(a)
    )

    target = _resolve()

    assert target.index_stale is False
    assert calls == []


def test_index_without_content_hash_is_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "a.py").write_text("changed\n")
    (root / ".codegraph").mkdir()
    build_fake_codegraph_db(root / ".codegraph" / "codegraph.db")  # no content_hash
    monkeypatch.setattr(targets, "repo_root", lambda: root)
    monkeypatch.setattr(
        targets, "get_config", lambda: SimpleNamespace(codegraph_db=None)
    )
    calls: list[object] = []
    monkeypatch.setattr(
        targets, "refresh_codegraph_index", lambda *a, **k: calls.append(a)
    )

    assert _resolve().index_stale is False
    assert calls == []


def test_explicit_range_is_never_stale() -> None:
    target = targets.resolve_target(
        path="src/x.py", line_start=2, line_end=4, qualified_name=None
    )
    assert target.index_stale is False


@pytest.mark.skipif(shutil.which("codegraph") is None, reason="needs the codegraph CLI")
def test_real_codegraph_follows_a_shifted_function(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end with the real indexer: lines inserted above move the range."""
    import subprocess

    from whygraph.services.codegraph import ensure_codegraph_db

    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    source = root / "src" / "calc.py"
    source.write_text("def shifted_target(a, b):\n    return a + b\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    ensure_codegraph_db(root, capture=True)
    monkeypatch.setattr(targets, "repo_root", lambda: root)
    monkeypatch.setattr(
        targets, "get_config", lambda: SimpleNamespace(codegraph_db=None)
    )

    def resolve() -> targets.Target:
        return targets.resolve_target(
            path=None, line_start=None, line_end=None, qualified_name="shifted_target"
        )

    before = resolve()
    source.write_text("import os\n\n\n# comment\n" + source.read_text())
    after = resolve()

    assert before.line_start == 1
    assert after.line_start == before.line_start + 4
    assert after.index_stale is False
