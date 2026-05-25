"""Smoke tests for the SQLModel + Alembic plumbing.

Covers the basics of the ``whygraph.db`` layer: the engine resolves the
configured SQLite path, and ``alembic upgrade head`` on an empty
database materializes exactly the SQLModel-owned tables.
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
from whygraph.db.bootstrap import alembic_config

SQLMODEL_TABLES = {
    "author",
    "commit",
    "commit_file_change",
    "issue",
    "pr_issue_link",
    "pull_request",
    "rationale_cache",
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
