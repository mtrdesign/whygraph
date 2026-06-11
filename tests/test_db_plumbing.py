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


def _insert_commit(sha: str, **overrides: object) -> None:
    """Insert one minimal ``commit`` row, applying any field overrides."""
    from whygraph.db import get_session
    from whygraph.db.models import Commit

    fields: dict[str, object] = dict(
        sha=sha,
        parent_shas="[]",
        author_name="Jane",
        author_email="jane@example.com",
        authored_at="2026-01-01T00:00:00Z",
        committed_at="2026-01-01T00:00:00Z",
        subject="s",
        body="",
        files_changed=1,
        insertions=1,
        deletions=0,
        scanned_at="2026-01-02T00:00:00Z",
    )
    fields.update(overrides)
    with get_session() as session:
        session.add(Commit(**fields))
        session.commit()


def test_commit_on_default_branch_default_and_explicit(
    _isolate_config_and_engine: Path,
) -> None:
    """The column defaults to 1; an explicit 0 (PR-origin commit) persists."""
    from sqlmodel import select

    from whygraph.db import get_session
    from whygraph.db.models import Commit

    command.upgrade(alembic_config(), "head")
    _insert_commit("default_sha")  # no on_default_branch → server default
    _insert_commit("origin_sha", on_default_branch=0)

    with get_session() as session:
        # Read scalars inside the session: the default value is server-set, so
        # accessing it on a detached row would trigger a refresh load and raise
        # DetachedInstanceError.
        default_on_main = session.get(Commit, "default_sha").on_default_branch
        origin_on_main = session.get(Commit, "origin_sha").on_default_branch
        on_main = set(
            session.exec(select(Commit.sha).where(Commit.on_default_branch == 1)).all()
        )

    assert default_on_main == 1
    assert origin_on_main == 0
    assert on_main == {"default_sha"}


def test_connect_pragmas_applied(_isolate_config_and_engine: Path) -> None:
    """The connect listener enables WAL + a busy timeout for concurrent writers."""
    engine = db_engine.get_engine()
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
