"""Who migrates a project's ``whygraph.db`` inside the portal, and when.

``whygraph serve`` migrated its one DB at startup; the portal serves many
projects and may meet a 1.x database at any revision. So the first
request that needs a project's data (a data route, ``/mcp/<slug>``, a
scan) runs :meth:`ProjectMigrations.ensure` once for that project:

* under :data:`MIGRATION_LOCK`, one process-wide lock - Alembic's
  ``context`` / ``op`` proxies are module globals, so two upgrades must
  never run at the same time, in any thread;
* when the DB is not at head, it is first copied to
  ``.whygraph/backups/whygraph-<revision>.db`` (SQLite's online backup,
  so a WAL database is copied consistently), so a 1.x DB migrated in
  place can be restored;
* memoized per DB path; :meth:`ProjectMigrations.forget` makes the next
  request check again (after a scan child exits, or when a project is
  removed);
* never through a symlink: :func:`whygraph.portal.paths.check_project_paths`
  runs first on every call, so a committed ``.whygraph/whygraph.db`` link
  can neither migrate nor back up another project's DB or ``portal.db``.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from alembic.script import ScriptDirectory

from whygraph.core.context import ProjectContext, use_project
from whygraph.core.safe_paths import check_inside
from whygraph.db import bootstrap

from .paths import check_project_paths

MIGRATION_LOCK = threading.Lock()
"""Serializes every Alembic run in the process (project and portal chains)."""


def _current_revision(db_path: Path) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.OperationalError:  # no alembic_version table yet
        return None
    finally:
        conn.close()
    return row[0] if row else None


def _backup(db_path: Path, revision: str | None) -> Path:
    backups = db_path.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    dest = backups / f"whygraph-{revision or 'base'}.db"
    if dest.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest = backups / f"whygraph-{revision or 'base'}.{stamp}.db"
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


class ProjectMigrations:
    """Memoized, serialized, backed-up ``alembic upgrade head`` per project DB."""

    def __init__(self) -> None:
        self._done: set[Path] = set()
        self._lock = threading.Lock()

    def ensure(self, ctx: ProjectContext) -> Path | None:
        """Bring ``ctx``'s project DB to head once per process (blocking).

        Parameters
        ----------
        ctx : ProjectContext
            The project; its ``config.whygraph_db`` is the DB migrated.

        Returns
        -------
        Path or None
            The backup written before a migration, or ``None`` when the
            DB was already at head (or already checked).

        Raises
        ------
        whygraph.core.safe_paths.UnsafePathError
            When the DB path (or ``.whygraph/``, ``.whygraph/backups/``,
            ``.codegraph/``) is a symlink or resolves outside the root.
            Checked on every call, memoized or not.
        """
        db_path = Path(ctx.config.whygraph_db or ctx.root / ".whygraph/whygraph.db")
        check_project_paths(ctx.root)
        check_inside(ctx.root, db_path)
        key = db_path.resolve()
        with self._lock:
            if key in self._done:
                return None
        backup: Path | None = None
        with MIGRATION_LOCK, use_project(ctx):
            if db_path.exists():
                head = ScriptDirectory.from_config(
                    bootstrap.alembic_config()
                ).get_current_head()
                current = _current_revision(db_path)
                if current != head:
                    backup = _backup(db_path, current)
            bootstrap.ensure_initialized()
        with self._lock:
            self._done.add(key)
        return backup

    def forget(self, db_path: Path) -> None:
        """Make the next :meth:`ensure` for ``db_path`` check the revision again."""
        with self._lock:
            self._done.discard(Path(db_path).resolve())


__all__ = ["MIGRATION_LOCK", "ProjectMigrations"]
