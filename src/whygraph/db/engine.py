"""SQLAlchemy engine and session factory for WhyGraph's SQLModel layer.

Module-level lazy engines bound to the WhyGraph SQLite database
(``.whygraph/whygraph.db`` by default, overridable via
``whygraph.toml``'s ``whygraph_db`` key — see
:class:`whygraph.core.Config`). One engine is cached per resolved DB
path, so a process serving several projects through
:func:`whygraph.core.context.use_project` gets one engine per project;
a CLI process only ever resolves one path and so still has one engine.

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

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from whygraph.core import _resolve_root, get_config

# Path constants duplicated locally to honor the "leave scan/db.py
# alone" constraint of the initial DB-layer plumbing PR. If the
# duplication ever drifts from scan/db.py, lift these (and
# ``default_db_path``) into ``whygraph.core.config`` in a focused
# follow-up.
_DB_DIR_NAME = ".whygraph"
_DB_FILE_NAME = "whygraph.db"

_engines: dict[Path, Engine] = {}
_engines_lock = threading.Lock()


def _resolved_db_path() -> Path:
    """Return the configured DB path or the project-relative default."""
    override = get_config().whygraph_db
    if override is not None:
        return override
    return _resolve_root() / _DB_DIR_NAME / _DB_FILE_NAME


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
    """Return the SQLAlchemy :class:`Engine` for the current project's DB.

    The engine is bound to the path derived from
    :func:`whygraph.core.get_config` (``whygraph_db`` override, else the
    project-relative default ``.whygraph/whygraph.db``). Both lookups
    honour a bound :func:`whygraph.core.context.use_project` context, so
    the path is the bound project's DB when there is one. The PRAGMA
    listener (WAL + foreign keys) is registered before the engine is
    returned, so the very first checkout already has them applied.

    Returns
    -------
    Engine
        The cached engine for the resolved DB path. Repeated calls for
        the same path return the same instance.

    Raises
    ------
    whygraph.core.context.ProjectContextError
        If strict mode is on and no project context is bound.
    """
    path = _resolved_db_path()
    key = path.resolve()
    engine = _engines.get(key)
    if engine is None:
        with _engines_lock:
            engine = _engines.get(key)
            if engine is None:
                engine = _engines[key] = _build_engine(path)
    return engine


def _reset_engine() -> None:
    """Dispose and drop every cached engine. Test-only — not public API."""
    with _engines_lock:
        engines = list(_engines.values())
        _engines.clear()
    for engine in engines:
        engine.dispose()


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
