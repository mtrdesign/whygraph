from __future__ import annotations

import sqlite3
from pathlib import Path

from whygraph.db import open_whygraph_db


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_open_creates_tables(tmp_path: Path) -> None:
    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        names = _table_names(conn)
        assert {
            "schema_version",
            "evidence",
            "evidence_bundles",
            "rationale",
            "ingest_runs",
        }.issubset(names)
    finally:
        conn.close()


def test_open_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c" / "wg.db"
    conn = open_whygraph_db(nested)
    try:
        assert nested.exists()
    finally:
        conn.close()


def test_open_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "wg.db"
    conn1 = open_whygraph_db(db_path)
    conn1.close()
    conn2 = open_whygraph_db(db_path)
    try:
        rows = [tuple(r) for r in conn2.execute("SELECT version FROM schema_version")]
        assert rows == [(1,)]
    finally:
        conn2.close()


def test_open_seeds_schema_version_row_once(tmp_path: Path) -> None:
    db_path = tmp_path / "wg.db"
    open_whygraph_db(db_path).close()
    open_whygraph_db(db_path).close()
    conn = open_whygraph_db(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version=1"
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_evidence_bundles_has_head_at_collection_on_fresh_db(
    tmp_path: Path,
) -> None:
    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        assert "head_at_collection" in _column_names(conn, "evidence_bundles")
    finally:
        conn.close()


def test_migrate_adds_head_at_collection_to_legacy_db(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(str(db_path))
    legacy.executescript(
        """
        CREATE TABLE evidence_bundles (
            node_id TEXT PRIMARY KEY,
            bundle_hash TEXT NOT NULL,
            built_at INTEGER NOT NULL
        );
        """
    )
    legacy.commit()
    legacy.close()

    conn = open_whygraph_db(db_path)
    try:
        assert "head_at_collection" in _column_names(conn, "evidence_bundles")
    finally:
        conn.close()


def test_journal_mode_is_wal(tmp_path: Path) -> None:
    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_evidence_unique_constraint(tmp_path: Path) -> None:
    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        conn.execute(
            "INSERT INTO evidence(node_id, qualified_name, source, ref, payload, collected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("n1", "pkg.fn", "git_commit", "abc", "{}", 1),
        )
        try:
            conn.execute(
                "INSERT INTO evidence(node_id, qualified_name, source, ref, payload, collected_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("n1", "pkg.fn", "git_commit", "abc", "{}", 2),
            )
        except sqlite3.IntegrityError:
            return
        raise AssertionError("Expected UNIQUE(node_id, source, ref) violation")
    finally:
        conn.close()
