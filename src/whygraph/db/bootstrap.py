"""Bring up the WhyGraph DB schema via Alembic.

Wraps the SQLModel-via-Alembic schema management behind one idempotent
``ensure_initialized()`` entry point.

Used by the ``whygraph init`` CLI command and reusable from tests that
need a fully-materialized DB on disk.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig

import whygraph.db as _db_pkg
from whygraph.db import engine as _engine


def alembic_config() -> AlembicConfig:
    """Build an :class:`AlembicConfig` pointed at the packaged migrations dir.

    Does *not* depend on the repo-root ``alembic.ini`` — that file is
    not shipped in the wheel (packaging is ``packages = ["src/whygraph"]``).
    The ``script_location`` is resolved at runtime from the installed
    package location, so this works both in-tree and after
    ``uv tool install``.

    Returns
    -------
    AlembicConfig
        A config the rest of the Alembic API will accept. ``env.py``
        sources the database URL via :func:`whygraph.db.get_engine`, so
        the ``sqlalchemy.url`` placeholder set here is intentionally
        ignored.
    """
    migrations_dir = Path(_db_pkg.__file__).parent / "migrations"
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(migrations_dir))
    cfg.set_main_option("sqlalchemy.url", "sqlite:///placeholder")
    return cfg


def ensure_initialized() -> Path:
    """Idempotently bring the WhyGraph DB schema up to head.

    Creates the parent directory of the configured DB path if needed,
    then runs ``alembic upgrade head``. Re-running on an
    already-initialized DB is a no-op — Alembic tracks the current
    revision in ``alembic_version`` and skips applied migrations.

    Returns
    -------
    Path
        The path to the initialized SQLite file.
    """
    db_path = _engine._resolved_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(), "head")
    return db_path


__all__ = ["alembic_config", "ensure_initialized"]
