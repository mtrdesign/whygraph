from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Callable, Iterable

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
    kind TEXT NOT NULL
);
CREATE TABLE files (
    path TEXT PRIMARY KEY,
    language TEXT
);
"""


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


def _insert_edges(
    conn: sqlite3.Connection, edges: Iterable[tuple[str, str, str]]
) -> None:
    conn.executemany(
        "INSERT INTO edges(source, target, kind) VALUES (?, ?, ?)",
        list(edges),
    )


def build_fake_codegraph_db(
    path: Path,
    *,
    nodes: list[dict] | None = None,
    edges: list[tuple[str, str, str]] | None = None,
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
        edges: list[tuple[str, str, str]] | None = None,
    ) -> Path:
        counter["n"] += 1
        path = tmp_path / f"codegraph_{counter['n']}.db"
        return build_fake_codegraph_db(path, nodes=nodes, edges=edges)

    return _factory


def _git_available() -> bool:
    return shutil.which("git") is not None


def _run(cmd: list[str], cwd: Path, env: dict | None = None) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, env=env)


@pytest.fixture
def init_git_repo(tmp_path: Path) -> Callable[..., Path]:
    """Factory: create an isolated git repo with deterministic identity."""
    if not _git_available():
        pytest.skip("git not available on PATH")

    counter = {"n": 0}

    def _factory(*, name: str | None = None) -> Path:
        counter["n"] += 1
        repo = tmp_path / (name or f"repo_{counter['n']}")
        repo.mkdir()
        env = {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
        }
        _run(["git", "init", "-q", "-b", "main"], cwd=repo, env=env)
        _run(["git", "config", "user.email", "test@example.com"], cwd=repo, env=env)
        _run(["git", "config", "user.name", "Test"], cwd=repo, env=env)
        _run(["git", "config", "commit.gpgsign", "false"], cwd=repo, env=env)
        return repo

    return _factory


@pytest.fixture
def git_commit() -> Callable[..., str]:
    """Factory: write a file in a repo and commit it. Returns the new HEAD sha."""

    def _commit(
        repo: Path,
        file_path: str,
        content: str,
        *,
        message: str = "wip",
    ) -> str:
        full = repo / file_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        env = {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(repo.parent),
        }
        _run(["git", "add", file_path], cwd=repo, env=env)
        _run(["git", "commit", "-q", "-m", message], cwd=repo, env=env)
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        return out.stdout.strip()

    return _commit
