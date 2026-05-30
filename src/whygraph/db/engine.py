"""SQLAlchemy engine and session factory for WhyGraph's SQLModel layer.

Module-level lazy engine bound to the WhyGraph SQLite database
(``.whygraph/whygraph.db`` by default, overridable via
``whygraph.toml``'s ``whygraph_db`` key — see
:class:`whygraph.core.Config`).

The engine sits alongside the hand-rolled :mod:`whygraph.scan.db` layer
in the same SQLite file; the two layers coexist without interfering
because Alembic's ``include_object`` filter (see
``whygraph/db/migrations/env.py``) scopes migrations to tables registered
on :data:`whygraph.db.base.metadata`. The legacy layer continues to own
its tables and its own ``schema_version`` row; SQLModel-managed tables
live under ``alembic_version`` instead.

Notes
-----
``sqlmodel.Session`` is *not* thread-safe. Each thread (e.g. a scan
worker pulled from :class:`concurrent.futures.ThreadPoolExecutor`) must
open its own session via :func:`get_session`. Never share a session
across threads.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from whygraph.core import get_config

# Path constants duplicated locally to honor the "leave scan/db.py
# alone" constraint of the initial DB-layer plumbing PR. If the
# duplication ever drifts from scan/db.py, lift these (and
# ``default_db_path``) into ``whygraph.core.config`` in a focused
# follow-up.
_DB_DIR_NAME = ".whygraph"
_DB_FILE_NAME = "whygraph.db"

_engine: Engine | None = None


def _project_root() -> Path:
    """Git repo root containing ``cwd``, falling back to ``cwd`` itself.

    Walks up to the nearest ``.git`` marker. Mirrors the resolution used
    by :func:`whygraph.core.get_config` so the default DB path tracks the
    same notion of "project root" as the rest of the package.
    """
    start = Path.cwd().resolve()
    for candidate in [start, *start.parents]:
        if (candidate / ".git").exists():
            return candidate
    return Path.cwd()


def _resolved_db_path() -> Path:
    """Return the configured DB path or the project-relative default."""
    override = get_config().whygraph_db
    if override is not None:
        return override
    return _project_root() / _DB_DIR_NAME / _DB_FILE_NAME


def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
    """SQLAlchemy ``connect`` listener: enable WAL, foreign keys, busy timeout.

    Runs on the raw DBAPI connection (``sqlite3.Connection``) — not the
    SQLAlchemy ``Connection`` wrapper — so the PRAGMAs are scoped to the
    underlying file handle for the entire lifetime of that connection.
    Mirrors the behavior of :mod:`whygraph.scan.db`.

    ``busy_timeout`` makes a second writer wait (up to 5s) instead of
    failing immediately with ``SQLITE_BUSY`` — relevant once a background
    git-hook rescan can overlap a manual ``whygraph scan``. WAL already
    lets a reader (e.g. a live MCP container) run alongside the writer.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 5000")
    finally:
        cursor.close()


def _build_engine(path: Path) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", _set_sqlite_pragmas)
    return engine


def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy :class:`Engine`, building it lazily.

    The engine is bound to the path returned by
    :func:`whygraph.core.get_config` (``whygraph_db`` override, else the
    project-relative default ``.whygraph/whygraph.db``). The PRAGMA
    listener (WAL + foreign keys) is registered before the engine is
    returned, so the very first checkout already has them applied.

    Returns
    -------
    Engine
        The shared engine. Repeated calls return the same instance.
    """
    global _engine
    if _engine is None:
        _engine = _build_engine(_resolved_db_path())
    return _engine


def _reset_engine() -> None:
    """Drop the cached engine. Test-only — not part of the public API."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


@contextmanager
def get_session() -> Iterator[Session]:
    """Yield a :class:`sqlmodel.Session` bound to :func:`get_engine`.

    On normal exit the session is committed; on exception it is rolled
    back. The session is always closed.

    Yields
    ------
    Session
        A fresh session — never reuse one across threads.

    Examples
    --------
    >>> with get_session() as session:
    ...     session.add(some_model_instance)
    """
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = ["get_engine", "get_session"]
