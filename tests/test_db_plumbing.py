"""Smoke tests for the SQLModel + Alembic plumbing.

The most important test is :func:`test_alembic_leaves_scan_tables_alone`
— it proves that the ``include_object`` filter in
``whygraph/db/migrations/env.py`` keeps autogenerate from referencing
the hand-rolled tables owned by :mod:`whygraph.scan.db`. SQLModel-owned
tables (``author``, ``commit``, ``pull_request``, …) and the
hand-rolled scan tables (``authors``, ``commits``, ``pull_requests``,
…) share the same SQLite file but live in disjoint namespaces;
autogenerate is allowed to propose ``op.create_table`` for the
SQLModel ones, but must never touch the scan-owned ones.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest
from alembic import command

from whygraph import core
from whygraph.core.config import Config
from whygraph.db import engine as db_engine
from whygraph.db.bootstrap import alembic_config, ensure_initialized
from whygraph.scan.db import Database as ScanDatabase

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSIONS_DIR = REPO_ROOT / "src" / "whygraph" / "db" / "migrations" / "versions"

SQLMODEL_TABLES = {"author", "commit", "issue", "pr_issue_link", "pull_request"}
SCAN_TABLES = {
    "schema_version",
    "commits",
    "pull_requests",
    "issues",
    "pr_issue_links",
    "rationale_cache",
    "scan_state",
    "authors",
}


@pytest.fixture(autouse=True)
def _isolate_config_and_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Point WhyGraph at a per-test SQLite file and reset the engine cache.

    Yields the path the engine will use for that test, so individual
    tests can assert against it without re-deriving it.
    """
    db_path = tmp_path / "whygraph.db"
    monkeypatch.setattr(core, "_config", Config(whygraph_db=db_path))
    db_engine._reset_engine()
    try:
        yield db_path
    finally:
        db_engine._reset_engine()
        core._reset_config()


def _table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def test_engine_resolves_configured_path(_isolate_config_and_engine: Path) -> None:
    db_path = _isolate_config_and_engine
    engine = db_engine.get_engine()
    # SQLAlchemy URL .database is the path component of sqlite:///<path>
    assert engine.url.database == str(db_path)


def test_alembic_upgrade_on_empty_db(_isolate_config_and_engine: Path) -> None:
    db_path = _isolate_config_and_engine
    assert not db_path.exists()

    command.upgrade(alembic_config(), "head")

    assert db_path.exists()
    assert _table_names(db_path) == SQLMODEL_TABLES | {"alembic_version"}


def test_alembic_leaves_scan_tables_alone(
    _isolate_config_and_engine: Path,
) -> None:
    db_path = _isolate_config_and_engine

    # Materialize all 8 hand-rolled tables + schema_version into the
    # same SQLite file Alembic is about to touch.
    with ScanDatabase(db_path):
        pass
    before = _table_names(db_path)
    assert SCAN_TABLES <= before, (
        f"scan/db.py did not create the tables we expected: missing "
        f"{SCAN_TABLES - before}"
    )

    # The scan DB grabbed its own connection; drop it so Alembic can
    # open the file cleanly. Recreating the engine forces a fresh
    # SQLAlchemy connection too.
    db_engine._reset_engine()

    command.upgrade(alembic_config(), "head")

    after = _table_names(db_path)
    assert "alembic_version" in after, "alembic_version was not created"
    assert SCAN_TABLES <= after, (
        f"alembic upgrade dropped scan tables: missing "
        f"{SCAN_TABLES - after}"
    )

    # The critical assertion: autogenerate must never emit an op
    # referencing any of scan/db.py's tables by name. CREATE TABLE for
    # SQLModel tables (author, commit, …) is fine — those legitimately
    # belong to our metadata. A `drop_table('commits')` or similar
    # would be catastrophic.
    existing_revisions = set(VERSIONS_DIR.glob("*.py"))
    command.revision(
        alembic_config(), message="probe", autogenerate=True
    )
    new_revisions = set(VERSIONS_DIR.glob("*.py")) - existing_revisions
    assert len(new_revisions) == 1, (
        f"expected exactly one new revision file, got {new_revisions}"
    )
    probe_path = next(iter(new_revisions))
    try:
        body = probe_path.read_text()
        for scan_table in SCAN_TABLES:
            forbidden = f"'{scan_table}'"
            assert forbidden not in body, (
                f"autogenerate referenced scan-owned table {scan_table!r} — "
                f"include_object filter is misfiring. Full revision:\n{body}"
            )
    finally:
        probe_path.unlink()


def test_ensure_initialized_creates_both_layers(
    _isolate_config_and_engine: Path,
) -> None:
    db_path = _isolate_config_and_engine
    assert not db_path.exists()

    returned = ensure_initialized()

    assert returned == db_path
    tables = _table_names(db_path)
    assert SQLMODEL_TABLES <= tables, f"SQLModel tables missing: {SQLMODEL_TABLES - tables}"
    assert SCAN_TABLES <= tables, f"scan tables missing: {SCAN_TABLES - tables}"
    assert "alembic_version" in tables


def test_ensure_initialized_is_idempotent(
    _isolate_config_and_engine: Path,
) -> None:
    db_path = _isolate_config_and_engine

    def _counts() -> tuple[int, int]:
        conn = sqlite3.connect(db_path)
        try:
            scan = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
            alembic = conn.execute("SELECT COUNT(*) FROM alembic_version").fetchone()[0]
        finally:
            conn.close()
        return scan, alembic

    ensure_initialized()
    first = _counts()

    # scan/db's _migrate uses ``if v > current`` so re-running must not
    # apply already-applied migrations. Alembic's upgrade is a no-op
    # once at head. The second call must succeed without raising and
    # leave version-tracking row counts unchanged.
    db_engine._reset_engine()
    ensure_initialized()
    second = _counts()

    assert second == first
