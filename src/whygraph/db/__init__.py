"""SQLModel + Alembic layer for WhyGraph-owned tables.

This package is a *parallel* DB layer to :mod:`whygraph.scan.db`. Both
operate on the same SQLite file (``.whygraph/whygraph.db`` by default),
but they manage disjoint sets of tables:

* :mod:`whygraph.scan.db` — hand-rolled schema for scan/evidence data
  (``commits``, ``pull_requests``, ``issues``, ``pr_issue_links``,
  ``rationale_cache``, ``scan_state``, ``authors``). Tracks its own
  versions in ``schema_version``.
* :mod:`whygraph.db` — SQLModel-defined tables (none yet; first one will
  land alongside its own Alembic migration). Tracked in
  ``alembic_version``.

The Alembic ``include_object`` filter in
``whygraph/db/migrations/env.py`` keeps autogenerate from emitting
``DROP TABLE`` for the hand-rolled tables it doesn't know about. The two
version tables are intentional and isolated; do not "unify" them without
also migrating every legacy table off :mod:`whygraph.scan.db`.

Public API
----------
* :func:`get_engine` — process-wide :class:`sqlalchemy.engine.Engine`.
* :func:`get_session` — context-managed :class:`sqlmodel.Session`.
"""

from __future__ import annotations

from .bootstrap import ensure_initialized
from .engine import get_engine, get_session

__all__ = ["get_engine", "get_session", "ensure_initialized"]
