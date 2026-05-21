from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from typing import Iterable, Iterator

import pytest

from whygraph import core
from whygraph.core.config import Config
from whygraph.db import ensure_initialized
from whygraph.db import engine as db_engine

_CODEGRAPH_SCHEMA = """\
CREATE TABLE nodes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    language TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    docstring TEXT,
    signature TEXT
);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    kind TEXT NOT NULL,
    line INTEGER
);
CREATE TABLE files (
    path TEXT PRIMARY KEY,
    language TEXT
);
"""


# An edge fixture row: (source, target, kind), or (source, target, kind, line)
# when a test needs to exercise the edge's recorded line.
EdgeRow = tuple[str, str, str] | tuple[str, str, str, int | None]


_NODE_FIELDS = (
    "id",
    "kind",
    "name",
    "qualified_name",
    "file_path",
    "language",
    "start_line",
    "end_line",
    "docstring",
    "signature",
)


def _insert_nodes(conn: sqlite3.Connection, nodes: Iterable[dict]) -> None:
    placeholders = ",".join("?" * len(_NODE_FIELDS))
    conn.executemany(
        f"INSERT INTO nodes({', '.join(_NODE_FIELDS)}) VALUES ({placeholders})",
        [tuple(n.get(f) for f in _NODE_FIELDS) for n in nodes],
    )


def _insert_edges(conn: sqlite3.Connection, edges: Iterable[EdgeRow]) -> None:
    rows = [(e[0], e[1], e[2], e[3] if len(e) > 3 else None) for e in edges]
    conn.executemany(
        "INSERT INTO edges(source, target, kind, line) VALUES (?, ?, ?, ?)",
        rows,
    )


def build_fake_codegraph_db(
    path: Path,
    *,
    nodes: list[dict] | None = None,
    edges: list[EdgeRow] | None = None,
) -> Path:
    """Create a minimal CodeGraph-shaped SQLite DB for tests.

    Default fixture: three nodes (a, b, c) where a calls b and b calls c.
    """
    if nodes is None:
        nodes = [
            {
                "id": "n_a",
                "kind": "function",
                "name": "a",
                "qualified_name": "pkg.a",
                "file_path": "src/pkg/a.py",
                "language": "python",
                "start_line": 1,
                "end_line": 5,
                "docstring": "doc-a",
                "signature": "def a()",
            },
            {
                "id": "n_b",
                "kind": "function",
                "name": "b",
                "qualified_name": "pkg.b",
                "file_path": "src/pkg/b.py",
                "language": "python",
                "start_line": 1,
                "end_line": 5,
                "docstring": None,
                "signature": "def b()",
            },
            {
                "id": "n_c",
                "kind": "function",
                "name": "c",
                "qualified_name": "pkg.c",
                "file_path": "src/pkg/c.py",
                "language": "python",
                "start_line": 1,
                "end_line": 5,
                "docstring": None,
                "signature": "def c()",
            },
        ]
    if edges is None:
        edges = [("n_a", "n_b", "calls"), ("n_b", "n_c", "calls")]

    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_CODEGRAPH_SCHEMA)
        _insert_nodes(conn, nodes)
        _insert_edges(conn, edges)
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def fake_codegraph_db(tmp_path: Path) -> Path:
    return build_fake_codegraph_db(tmp_path / "codegraph.db")


@pytest.fixture
def codegraph_db_factory(tmp_path: Path):
    counter = {"n": 0}

    def _factory(
        *,
        nodes: list[dict] | None = None,
        edges: list[EdgeRow] | None = None,
    ) -> Path:
        counter["n"] += 1
        path = tmp_path / f"codegraph_{counter['n']}.db"
        return build_fake_codegraph_db(path, nodes=nodes, edges=edges)

    return _factory


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one file committed across two commits.

    ``sample.py`` ends with three lines: the first two land in the initial
    commit, the third in the second commit — so a blame of lines 1-3
    yields two distinct commits.
    """
    root = tmp_path / "repo"
    root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True
        )

    _git("init")
    _git("config", "user.email", "tester@example.com")
    _git("config", "user.name", "Test User")
    _git("config", "commit.gpgsign", "false")
    sample = root / "sample.py"
    sample.write_text("line one\nline two\n")
    _git("add", "sample.py")
    _git("commit", "-m", "first commit")
    sample.write_text("line one\nline two\nline three\n")
    _git("add", "sample.py")
    _git("commit", "-m", "second commit\n\nAdds the third line for context.")
    return root


@pytest.fixture
def whygraph_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Point WhyGraph's DB layer at an isolated, empty per-test SQLite file.

    Yields the path. The schema is *not* created — use
    :func:`whygraph_db_initialized` for a migrated database.
    """
    db_path = tmp_path / "whygraph.db"
    monkeypatch.setattr(core, "_config", Config(whygraph_db=db_path))
    db_engine._reset_engine()
    try:
        yield db_path
    finally:
        db_engine._reset_engine()
        core._reset_config()


@pytest.fixture
def whygraph_db_initialized(whygraph_db: Path) -> Path:
    """An isolated WhyGraph DB with the schema migrated to head."""
    ensure_initialized()
    return whygraph_db
