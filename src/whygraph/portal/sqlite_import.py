"""The one-time import of a 2.0 portal (``<data dir>/portal.db``) into Postgres.

WhyGraph 2.0 kept the portal's tables in SQLite; 2.1 keeps them in
Postgres. On the first start of 2.1 the portal copies the old file into
the (empty) database in **one transaction**, keeping every id, so
``runs/<id>.*`` still match their rows and stored secrets still decrypt
with the data dir's ``secret.key``. It then renames the file to
``portal.db.migrated-<UTC>``, which is how a user goes back to 2.0.

The copy transaction also writes the :class:`~whygraph.portal.models.LegacyImport`
marker (the file's SHA-256 and revision) and verifies the copy (row counts,
and that a secret decrypts) **before** it commits. The rename happens only
after the commit, so a crash between the two is recoverable: the next start
sees the marker and a file with the same hash, and finishes the rename.

:func:`import_legacy_sqlite` handles every case of the plan's table
(section 4.4); refusals raise :class:`LegacyImportError`, which the
portal's startup turns into degraded mode with the message shown.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import InvalidToken, MultiFernet
from sqlalchemy import insert, select, text
from sqlalchemy.engine import Connection, Engine

from whygraph.core.safe_paths import UnsafePathError, check_inside

from . import db as portal_db
from .models import LegacyImport, PortalBase, _now
from .secrets import KEY_FILE_NAME, load_existing_keyring

_log = logging.getLogger(__name__)

LEGACY_REVISIONS: frozenset[str] = frozenset({"c5e8a1d2b3f4"})
"""Portal Alembic heads a released 2.0.x ships; a 2.0.x patch adding one adds it here."""

TABLE_ORDER: tuple[str, ...] = (
    "users",
    "settings",
    "projects",
    "project_agents",
    "project_config",
    "secrets",
    "scan_runs",
)
"""The 2.0 tables, in foreign-key order."""

_SIDECARS = ("-wal", "-shm")


@dataclass(frozen=True)
class ImportReport:
    """What one import copied.

    Attributes
    ----------
    source : Path
        The renamed file (``portal.db.migrated-<UTC>``).
    rows : dict[str, int]
        Rows copied, per table.
    """

    source: Path
    rows: dict[str, int]


class LegacyImportError(RuntimeError):
    """The 2.0 ``portal.db`` cannot be imported; nothing was changed."""


def import_legacy_sqlite(data_dir: Path) -> ImportReport | None:
    """Import ``<data_dir>/portal.db`` into the empty portal database, once.

    Parameters
    ----------
    data_dir : Path
        The portal data directory (holds ``portal.db`` and ``secret.key``).

    Returns
    -------
    ImportReport or None
        The report of an import that ran, or ``None`` when there was
        nothing to import (no file, an import already done, or a database
        that already holds a portal).

    Raises
    ------
    LegacyImportError
        When the file is a symlink, not a regular file or outside the data
        dir, is not a 2.0 portal database, ``secret.key`` is missing or
        does not match, or the copy failed (rolled back; the file is
        untouched and the next start retries).
    """
    path = data_dir / portal_db.LEGACY_DB_FILE_NAME
    if not path.is_symlink() and not path.exists():
        return None
    _check_file(data_dir, path)
    revision = _checkpoint(path)
    digest = _sha256(path)

    engine = portal_db.migration_engine()
    try:
        with engine.connect() as conn:
            marker = conn.execute(
                select(LegacyImport.__table__).where(LegacyImport.__table__.c.id == 1)
            ).first()
            occupied = marker is None and _holds_a_portal(conn)
        if marker is not None:
            if marker.source_sha256 == digest:
                renamed = _rename(path)
                _log.info(
                    "portal.db was imported before; finished renaming it to %s",
                    renamed.name,
                )
            else:
                _log.warning(
                    "%s differs from the portal.db imported on %s; leaving it as "
                    "it is (remove it, or keep it as a backup of 2.0 data)",
                    path,
                    marker.imported_at,
                )
            return None
        if occupied:
            _log.warning(
                "%s was not imported: the portal database already holds a portal "
                "(set up fresh before the file appeared); leaving the file as it is",
                path,
            )
            return None
        if revision not in LEGACY_REVISIONS:
            expected = ", ".join(sorted(LEGACY_REVISIONS))
            raise LegacyImportError(
                f"portal.db is at revision {revision or '<none>'}; 2.1 imports a "
                f"2.0.x portal ({expected}) - start the matching 2.0 release once "
                "to migrate it, or remove the file to start empty"
            )
        keyring = _keyring(data_dir)
        rows = _copy(engine, path, keyring, digest, revision)
    finally:
        engine.dispose()

    renamed = _rename(path)
    counts = ", ".join(f"{t}={n}" for t, n in rows.items())
    _log.info("imported the 2.0 portal.db (%s); kept it as %s", counts, renamed.name)
    return ImportReport(source=renamed, rows=rows)


def _check_file(data_dir: Path, path: Path) -> None:
    """Refuse a symlink, a non-regular file, or a path that leaves the data dir."""
    if path.is_symlink():
        raise LegacyImportError(f"{path} is a symlink; refusing to import it")
    if not path.is_file():
        raise LegacyImportError(f"{path} is not a regular file; refusing to import it")
    try:
        check_inside(data_dir, path)
    except UnsafePathError as exc:
        raise LegacyImportError(f"refusing to import {exc}") from None


def _checkpoint(path: Path) -> str | None:
    """Fold any WAL into the file and return its portal revision."""
    try:
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            try:
                row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            except sqlite3.OperationalError:  # no alembic_version table
                row = None
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise LegacyImportError(
            f"{path} is not a readable SQLite database ({exc}); remove it to start "
            "empty"
        ) from None
    return row[0] if row else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _holds_a_portal(conn: Connection) -> bool:
    """Whether Postgres was set up already (a ``settings``, user or project row)."""
    for table in ("settings", "users", "projects"):
        if conn.execute(text(f'SELECT 1 FROM "{table}" LIMIT 1')).first():
            return True
    return False


def _keyring(data_dir: Path) -> MultiFernet:
    try:
        return load_existing_keyring(data_dir / KEY_FILE_NAME)
    except FileNotFoundError:
        raise LegacyImportError(
            "secret.key is missing - restore it from your backup of this data dir"
        ) from None
    except ValueError:
        raise LegacyImportError(
            "secret.key is not a valid key - restore the secret.key that belongs "
            "to this data dir"
        ) from None


def _convert(table: str, row: dict[str, Any]) -> dict[str, Any]:
    """Turn sqlite3's ``int`` / ``str`` into what the Postgres columns take."""
    if table == "scan_runs":
        row["analyze"] = bool(row["analyze"])
    elif table == "project_config":
        row["config"] = json.loads(row["config"])
    return row


def _count(conn: Connection, table: str) -> int:
    return conn.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()


def _copy(
    engine: Engine, path: Path, keyring: MultiFernet, digest: str, revision: str
) -> dict[str, int]:
    """Copy every table, verify, write the marker, commit - or roll back."""
    rows: dict[str, int] = {}
    source = sqlite3.connect(path)
    try:
        with engine.begin() as conn:
            for name in TABLE_ORDER:
                table = PortalBase.metadata.tables[name]
                columns = [c.name for c in table.columns]
                order = ", ".join(f'"{c.name}"' for c in table.primary_key.columns)
                quoted = ", ".join(f'"{c}"' for c in columns)
                fetched = source.execute(
                    f'SELECT {quoted} FROM "{name}" ORDER BY {order}'
                ).fetchall()
                batch = [_convert(name, dict(zip(columns, r))) for r in fetched]
                if batch:
                    conn.execute(insert(table), batch)
                rows[name] = len(batch)
            for name in TABLE_ORDER:
                if "id" in PortalBase.metadata.tables[name].c:
                    conn.execute(
                        text(
                            f"SELECT setval(pg_get_serial_sequence('{name}', 'id'), "
                            f'coalesce(max(id), 1), max(id) IS NOT NULL) FROM "{name}"'
                        )
                    )
            _verify(conn, rows, keyring)
            conn.execute(
                insert(LegacyImport.__table__).values(
                    id=1,
                    source_name=path.name,
                    source_sha256=digest,
                    source_revision=revision,
                    rows=rows,
                    imported_at=_now(),
                )
            )
    except LegacyImportError:
        raise
    except Exception as exc:
        raise LegacyImportError(
            f"importing portal.db failed and was rolled back; the file is "
            f"untouched and the next start retries: {exc}"
        ) from exc
    finally:
        source.close()
    return rows


def _verify(conn: Connection, rows: dict[str, int], keyring: MultiFernet) -> None:
    """Row counts match and the first secret decrypts; raising rolls back."""
    for name, expected in rows.items():
        got = _count(conn, name)
        if got != expected:
            raise LegacyImportError(
                f"importing portal.db was rolled back: {name} has {got} rows in "
                f"Postgres but {expected} in the file"
            )
    first = conn.execute(text("SELECT ciphertext FROM secrets ORDER BY id LIMIT 1"))
    ciphertext = first.scalar()
    if ciphertext is None:
        return
    try:
        keyring.decrypt(ciphertext.encode("ascii"))
    except InvalidToken:
        raise LegacyImportError(
            "secret.key does not match portal.db - restore the secret.key that "
            "belongs to this data dir"
        ) from None


def _rename(path: Path) -> Path:
    """Rename the file (and any ``-wal`` / ``-shm`` left) to ``.migrated-<UTC>``."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.migrated-{stamp}")
    for suffix in _SIDECARS:
        sidecar = path.with_name(path.name + suffix)
        if sidecar.is_file() and not sidecar.is_symlink():
            sidecar.rename(target.with_name(target.name + suffix))
    path.rename(target)
    return target


__all__ = [
    "ImportReport",
    "LEGACY_REVISIONS",
    "LegacyImportError",
    "TABLE_ORDER",
    "import_legacy_sqlite",
]
