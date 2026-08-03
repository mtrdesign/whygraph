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
    "chat_message",
    "chat_session",
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


# ---------- chat tables (plan §6) -------------------------------------------


def test_chat_tables_exist_after_upgrade(_isolate_config_and_engine: Path) -> None:
    """Both chat tables and the session_id index land at head."""
    db_path = _isolate_config_and_engine
    command.upgrade(alembic_config(), "head")

    assert {"chat_session", "chat_message"} <= _table_names(db_path)

    conn = sqlite3.connect(db_path)
    try:
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert "ix_chat_message_session_id" in indexes


def test_ensure_initialized_is_idempotent(_isolate_config_and_engine: Path) -> None:
    """A second bootstrap over an already-migrated DB is a no-op, not an error.

    ``create_app`` calls this on every ``whygraph serve``, so re-running it
    against an existing DB has to be safe.
    """
    from whygraph.db.bootstrap import ensure_initialized

    ensure_initialized()
    before = _table_names(_isolate_config_and_engine)
    ensure_initialized()
    assert _table_names(_isolate_config_and_engine) == before


def test_chat_message_roundtrips_with_tool_calls_default(
    _isolate_config_and_engine: Path,
) -> None:
    """A tool-call-free row gets ``"[]"`` from the server default; FKs hold."""
    from sqlmodel import select

    from whygraph.db import get_session
    from whygraph.db.models import ChatMessage as ChatMessageRow
    from whygraph.db.models import ChatSession as ChatSessionRow

    command.upgrade(alembic_config(), "head")

    with get_session() as session:
        row = ChatSessionRow(
            title="New chat",
            provider="anthropic",
            model="claude-opus-4-7",
            created_at="2026-07-29T00:00:00Z",
            updated_at="2026-07-29T00:00:00Z",
        )
        session.add(row)
        session.commit()
        session_id = row.id

    with get_session() as session:
        session.add(
            ChatMessageRow(
                session_id=session_id,
                role="user",
                content="why is this here?",
                created_at="2026-07-29T00:00:01Z",
            )
        )
        session.commit()

    with get_session() as session:
        stored = session.exec(
            select(ChatMessageRow).where(ChatMessageRow.session_id == session_id)
        ).one()
        assert stored.role == "user"
        assert stored.tool_calls == "[]"
        assert stored.tool_call_id is None
        assert stored.input_tokens is None


def _column_names(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def test_chat_message_error_column_round_trips(
    _isolate_config_and_engine: Path,
) -> None:
    """``error`` lands at head and downgrades cleanly off a seeded DB.

    The column is nullable with no default rewrite, so an existing chat DB
    picks it up on the next ``whygraph serve``.
    """
    from whygraph.db import get_session
    from whygraph.db.models import ChatMessage as ChatMessageRow
    from whygraph.db.models import ChatSession as ChatSessionRow

    db_path = _isolate_config_and_engine
    command.upgrade(alembic_config(), "head")
    assert "error" in _column_names(db_path, "chat_message")

    with get_session() as session:
        row = ChatSessionRow(
            title="New chat",
            provider="anthropic",
            model="claude-opus-5",
            created_at="2026-07-30T00:00:00Z",
            updated_at="2026-07-30T00:00:00Z",
        )
        session.add(row)
        session.commit()
        session.add(
            ChatMessageRow(
                session_id=row.id,
                role="assistant",
                content="",
                error="401 unauthorized",
                created_at="2026-07-30T00:00:01Z",
            )
        )
        session.commit()

    db_engine._reset_engine()
    # Downgrade to the parent of the revision that adds `error`, not a bare
    # "-1" — that would only be the right target for as long as this stays
    # the head revision, which it no longer is.
    command.downgrade(alembic_config(), "f3582dfcc817")
    assert "error" not in _column_names(db_path, "chat_message")
    # The seeded rows survive the drop — only the column goes away.
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM chat_message").fetchone()[0] == 1
    finally:
        conn.close()


def test_chat_message_rejects_unknown_session(_isolate_config_and_engine: Path) -> None:
    """``foreign_keys=ON`` (engine.py) makes an orphan message fail."""
    from sqlalchemy.exc import IntegrityError

    from whygraph.db import get_session
    from whygraph.db.models import ChatMessage as ChatMessageRow

    command.upgrade(alembic_config(), "head")

    with pytest.raises(IntegrityError):
        with get_session() as session:
            session.add(
                ChatMessageRow(
                    session_id=999,
                    role="user",
                    content="orphan",
                    created_at="2026-07-29T00:00:00Z",
                )
            )
            session.commit()
