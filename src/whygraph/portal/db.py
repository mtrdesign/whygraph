"""The portal database: data directory, engine, and Alembic bootstrap.

The portal keeps one SQLite file, ``<data dir>/portal.db``, with its own
tables (:mod:`whygraph.portal.models`) and its own Alembic chain
(``portal/migrations``). It is deliberately separate from every project's
``.whygraph/whygraph.db``: the two have unrelated lifecycles, and a
project database must stay usable by the headless CLI without a portal.

The data directory is ``$WHYGRAPH_DATA`` when set, else
``~/.local/share/whygraph``. It also holds ``secret.key``
(:mod:`whygraph.portal.secrets`) and, in later steps, cloned repositories
and scan run files, which is why the paths stored in the database are
relative to it.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.engine import Engine
from sqlmodel import Session

from whygraph.db.engine import _build_engine

DATA_ENV_VAR = "WHYGRAPH_DATA"
"""Environment variable that overrides the data directory."""

DB_FILE_NAME = "portal.db"

_engines: dict[Path, Engine] = {}
_engines_lock = threading.Lock()


def data_dir() -> Path:
    """Return the portal data directory, creating it (mode 700) if missing.

    Returns
    -------
    Path
        ``$WHYGRAPH_DATA`` when set and non-empty, else
        ``~/.local/share/whygraph``. An existing directory keeps its mode.
    """
    override = os.environ.get(DATA_ENV_VAR)
    path = Path(override).expanduser() if override else _default_data_dir()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _default_data_dir() -> Path:
    return Path.home() / ".local" / "share" / "whygraph"


def portal_db_path() -> Path:
    """Return the path of the portal SQLite file inside :func:`data_dir`."""
    return data_dir() / DB_FILE_NAME


def get_engine() -> Engine:
    """Return the cached engine for the portal database.

    The engine gets the same PRAGMAs as a project engine (WAL, foreign
    keys ON, ``busy_timeout``), registered before the first checkout, so
    the ``ON DELETE CASCADE`` constraints are enforced.

    Returns
    -------
    Engine
        One engine per resolved DB path; repeated calls return it.
    """
    path = portal_db_path()
    key = path.resolve()
    engine = _engines.get(key)
    if engine is None:
        with _engines_lock:
            engine = _engines.get(key)
            if engine is None:
                engine = _engines[key] = _build_engine(path)
    return engine


def _reset_engine() -> None:
    """Dispose and drop every cached engine. Test-only - not public API."""
    with _engines_lock:
        engines = list(_engines.values())
        _engines.clear()
    for engine in engines:
        engine.dispose()


@contextmanager
def get_session() -> Iterator[Session]:
    """Yield a :class:`sqlmodel.Session` on the portal DB.

    Commits on normal exit, rolls back on an exception, always closes.

    Yields
    ------
    Session
        A fresh session - never share one across threads.
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


def alembic_config() -> AlembicConfig:
    """Build an :class:`AlembicConfig` for the packaged portal migrations.

    Mirrors :func:`whygraph.db.bootstrap.alembic_config`: the script
    location is resolved from the installed package, and ``env.py``
    sources the URL from :func:`get_engine`, so the placeholder URL here
    is ignored.

    Returns
    -------
    AlembicConfig
        A config for the portal's own migration chain.
    """
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", "sqlite:///placeholder")
    return cfg


def ensure_initialized() -> Path:
    """Idempotently bring the portal DB schema up to head.

    Returns
    -------
    Path
        The path of the (now migrated) portal SQLite file.
    """
    db_path = portal_db_path()
    command.upgrade(alembic_config(), "head")
    return db_path


__all__ = [
    "alembic_config",
    "data_dir",
    "ensure_initialized",
    "get_engine",
    "get_session",
    "portal_db_path",
]
