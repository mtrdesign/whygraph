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
    "forge_crawl_state",
    "issue",
    "milestone",
    "pr_issue_link",
    "pr_origin_skip",
    "project_note",
    "project_purpose_run",
    "pull_request",
    "rationale_cache",
    "tag",
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


def test_no_configured_path_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from whygraph.core.context import ProjectContextError
    from whygraph.db.bootstrap import ensure_initialized

    monkeypatch.setattr(core, "_config", Config())
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ProjectContextError, match="no WhyGraph database"):
        db_engine.get_engine()
    with pytest.raises(ProjectContextError, match="no WhyGraph database"):
        ensure_initialized()
    assert not (tmp_path / ".whygraph").exists()


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


def test_forge_and_pr_origin_tables_upgrade_and_downgrade(
    _isolate_config_and_engine: Path,
) -> None:
    """Both revisions create their table; explicit targets drop them in turn."""
    db_path = _isolate_config_and_engine
    command.upgrade(alembic_config(), "head")

    assert {"forge_crawl_state", "pr_origin_skip"} <= _table_names(db_path)
    db_engine._reset_engine()
    command.downgrade(alembic_config(), "6b2e9d4f7a1c")
    assert "pr_origin_skip" not in _table_names(db_path)
    assert "forge_crawl_state" in _table_names(db_path)
    db_engine._reset_engine()
    command.downgrade(alembic_config(), "d5a8e1f03b72")
    assert "forge_crawl_state" not in _table_names(db_path)


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


def test_chat_message_replay_column_round_trips(
    _isolate_config_and_engine: Path,
) -> None:
    """``replay`` lands at head and downgrades cleanly off a seeded DB."""
    from whygraph.db import get_session
    from whygraph.db.models import ChatMessage as ChatMessageRow
    from whygraph.db.models import ChatSession as ChatSessionRow

    db_path = _isolate_config_and_engine
    command.upgrade(alembic_config(), "head")
    assert "replay" in _column_names(db_path, "chat_message")

    with get_session() as session:
        row = ChatSessionRow(
            title="New chat",
            provider="deepseek",
            model="deepseek-flash",
            created_at="2026-10-27T00:00:00Z",
            updated_at="2026-10-27T00:00:00Z",
        )
        session.add(row)
        session.commit()
        session.add(
            ChatMessageRow(
                session_id=row.id,
                role="assistant",
                content="answer",
                replay='{"reasoning_content": "thought"}',
                created_at="2026-10-27T00:00:01Z",
            )
        )
        session.commit()

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT replay FROM chat_message").fetchone() == (
            '{"reasoning_content": "thought"}',
        )
    finally:
        conn.close()

    db_engine._reset_engine()
    command.downgrade(alembic_config(), "8d3f1a5c2e7b")
    assert "replay" not in _column_names(db_path, "chat_message")
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM chat_message").fetchone()[0] == 1
    finally:
        conn.close()


def test_chat_mode_and_grounding_columns_round_trip(
    _isolate_config_and_engine: Path,
) -> None:
    """The mode and grounding columns land at the new head and downgrade cleanly."""
    db_path = _isolate_config_and_engine

    command.upgrade(alembic_config(), "head")
    assert "mode" in _column_names(db_path, "chat_session")
    assert {"mode", "grounding"} <= _column_names(db_path, "chat_message")

    db_engine._reset_engine()
    command.downgrade(alembic_config(), "a3c7e91b4d25")
    assert "mode" not in _column_names(db_path, "chat_session")
    assert not {"mode", "grounding"} & _column_names(db_path, "chat_message")


def test_chat_message_feedback_and_is_error_columns_round_trip(
    _isolate_config_and_engine: Path,
) -> None:
    """The rating and ``is_error`` columns land at head and downgrade cleanly."""
    from alembic.script import ScriptDirectory

    db_path = _isolate_config_and_engine
    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_revision("6f1b3d8a2c94").nextrev

    columns = {"is_error", "rating", "rating_reason", "rating_comment", "rated_at"}
    command.upgrade(alembic_config(), "5e8a2c9d1f47")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO chat_session (title, provider, model, created_at, "
            "updated_at) VALUES ('t', 'deepseek', 'm', 'x', 'x')"
        )
        conn.execute(
            "INSERT INTO chat_message (session_id, role, content, tool_calls, "
            "created_at) VALUES (1, 'user', 'q', '[]', 'x')"
        )
        conn.commit()
    finally:
        conn.close()
    assert not columns & _column_names(db_path, "chat_message")

    db_engine._reset_engine()
    command.upgrade(alembic_config(), "head")
    assert columns <= _column_names(db_path, "chat_message")

    db_engine._reset_engine()
    command.downgrade(alembic_config(), "5e8a2c9d1f47")
    assert not columns & _column_names(db_path, "chat_message")
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM chat_message").fetchone()[0] == 1
    finally:
        conn.close()


def test_project_note_table_round_trip(_isolate_config_and_engine: Path) -> None:
    """``project_note`` lands at head and downgrades cleanly, keeping other data."""
    from alembic.script import ScriptDirectory

    db_path = _isolate_config_and_engine
    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    assert script.get_revision("b7e4c2a91d36").down_revision == "6f1b3d8a2c94"

    command.upgrade(alembic_config(), "6f1b3d8a2c94")
    assert "project_note" not in _table_names(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO chat_session (title, provider, model, created_at, "
            "updated_at) VALUES ('t', 'deepseek', 'm', 'x', 'x')"
        )
        conn.commit()
    finally:
        conn.close()

    db_engine._reset_engine()
    command.upgrade(alembic_config(), "head")
    assert "project_note" in _table_names(db_path)
    insert = (
        "INSERT INTO project_note (kind, body, status, anchor_kind, anchor_path, "
        "source, proposed_by, proposed_at, supersedes_id) "
        "VALUES ('decision', ?, 'confirmed', 'file', 'a.py', 'human', 'u', 'x', ?)"
    )
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(insert, ("one", None))
        conn.execute(insert, ("two", 1))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM project_note").fetchone()[0] == 2
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(project_note)")}
    finally:
        conn.close()
    assert {
        "ix_project_note_status",
        "ix_project_note_anchor_path",
        "ix_project_note_qualified_name",
    } <= indexes

    db_engine._reset_engine()
    command.downgrade(alembic_config(), "6f1b3d8a2c94")
    assert "project_note" not in _table_names(db_path)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM chat_session").fetchone()[0] == 1
    finally:
        conn.close()


def test_project_purpose_run_table_round_trip(
    _isolate_config_and_engine: Path,
) -> None:
    """``project_purpose_run`` lands at head and downgrades cleanly, keeping other data."""
    from alembic.script import ScriptDirectory

    db_path = _isolate_config_and_engine
    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    assert script.get_revision(script.get_current_head()).down_revision == (
        "b7e4c2a91d36"
    )

    command.upgrade(alembic_config(), "b7e4c2a91d36")
    assert "project_purpose_run" not in _table_names(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO chat_session (title, provider, model, created_at, "
            "updated_at) VALUES ('t', 'deepseek', 'm', 'x', 'x')"
        )
        conn.commit()
    finally:
        conn.close()

    db_engine._reset_engine()
    command.upgrade(alembic_config(), "head")
    assert "project_purpose_run" in _table_names(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO project_purpose_run (requested_by, started_at, "
            "finished_at, status, input_hash, input_bytes, sources, provider, "
            "model, tokens_in, tokens_out, note_id, error_code) "
            "VALUES ('u', 'x', 'x', 'ok', 'h', 10, '[]', 'deepseek', 'm', 1, 2, "
            "7, NULL)"
        )
        conn.commit()
        assert (
            conn.execute("SELECT COUNT(*) FROM project_purpose_run").fetchone()[0] == 1
        )
    finally:
        conn.close()

    db_engine._reset_engine()
    command.downgrade(alembic_config(), "b7e4c2a91d36")
    assert "project_purpose_run" not in _table_names(db_path)
    assert "project_note" in _table_names(db_path)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM chat_session").fetchone()[0] == 1
    finally:
        conn.close()


def test_forge_fields_milestone_tag_round_trip(
    _isolate_config_and_engine: Path,
) -> None:
    """The phase 2 forge columns and tables land at head and downgrade cleanly."""
    db_path = _isolate_config_and_engine
    command.upgrade(alembic_config(), "head")
    pr_cols = _column_names(db_path, "pull_request")
    assert {
        "milestone",
        "assignees",
        "review_decision",
        "latest_reviews",
        "review_requests",
        "first_review_at",
        "files",
        "additions",
        "deletions",
        "changed_files",
    } <= pr_cols
    assert {"milestone", "assignees", "state_reason"} <= _column_names(db_path, "issue")
    assert "fields_version" in _column_names(db_path, "forge_crawl_state")

    conn = sqlite3.connect(db_path)
    try:
        defaults = {
            (t, r[1]): r[4]
            for t in ("pull_request", "issue")
            for r in conn.execute(f"PRAGMA table_info({t})")
        }
        for t, c in (
            ("pull_request", "assignees"),
            ("pull_request", "latest_reviews"),
            ("pull_request", "review_requests"),
            ("pull_request", "files"),
            ("issue", "assignees"),
        ):
            assert defaults[(t, c)] == "'[]'"
    finally:
        conn.close()
    assert {"milestone", "tag"} <= _table_names(db_path)

    db_engine._reset_engine()
    command.downgrade(alembic_config(), "22567728b72a")
    assert not {"milestone", "tag"} & _table_names(db_path)
    assert "files" not in _column_names(db_path, "pull_request")
    assert "state_reason" not in _column_names(db_path, "issue")
    assert "fields_version" not in _column_names(db_path, "forge_crawl_state")


def test_chat_branching_backfills_parents_and_leaf(
    _isolate_config_and_engine: Path,
) -> None:
    """The branching migration links each linear transcript and downgrades."""
    db_path = _isolate_config_and_engine
    prev = "4b774c7b092d"
    command.upgrade(alembic_config(), prev)

    # Raw SQL: the head ORM models carry columns this revision lacks.
    conn = sqlite3.connect(db_path)
    try:
        ts = "2026-10-27T00:00:00Z"
        for title in ("A", "B", "empty"):
            conn.execute(
                "INSERT INTO chat_session (title, provider, model, created_at, "
                "updated_at) VALUES (?, 'deepseek', 'm', ?, ?)",
                (title, ts, ts),
            )
        sessions = {
            title: sid
            for sid, title in conn.execute("SELECT id, title FROM chat_session")
        }
        ids: dict[str, int] = {}
        # Interleaved across sessions so "same session" is exercised.
        for name, session, role in (
            ("A1", "A", "user"),
            ("B1", "B", "user"),
            ("A2", "A", "assistant"),
            ("B2", "B", "assistant"),
            ("A3", "A", "user"),
        ):
            cur = conn.execute(
                "INSERT INTO chat_message (session_id, role, content, tool_calls, "
                "created_at) VALUES (?, ?, 'x', '[]', ?)",
                (sessions[session], role, ts),
            )
            ids[name] = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    command.upgrade(alembic_config(), "head")

    new_message_cols = {"parent_id", "finish_reason", "effort", "reasoning"}
    new_session_cols = {"active_leaf_id", "effort"}
    assert new_message_cols <= _column_names(db_path, "chat_message")
    assert new_session_cols <= _column_names(db_path, "chat_session")

    conn = sqlite3.connect(db_path)
    try:
        parents = dict(conn.execute("SELECT id, parent_id FROM chat_message"))
        assert parents == {
            ids["A1"]: None,
            ids["A2"]: ids["A1"],
            ids["A3"]: ids["A2"],
            ids["B1"]: None,
            ids["B2"]: ids["B1"],
        }
        leaves = dict(conn.execute("SELECT title, active_leaf_id FROM chat_session"))
        assert leaves == {"A": ids["A3"], "B": ids["B2"], "empty": None}
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(chat_message)")}
        assert "ix_chat_message_parent_id" in indexes
    finally:
        conn.close()

    db_engine._reset_engine()
    command.downgrade(alembic_config(), prev)
    assert not new_message_cols & _column_names(db_path, "chat_message")
    assert not new_session_cols & _column_names(db_path, "chat_session")
    conn = sqlite3.connect(db_path)
    try:
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(chat_message)")}
        assert "ix_chat_message_parent_id" not in indexes
        assert conn.execute("SELECT COUNT(*) FROM chat_message").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM chat_session").fetchone()[0] == 3
    finally:
        conn.close()
