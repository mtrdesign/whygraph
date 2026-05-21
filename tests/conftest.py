from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

import pytest

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
