"""Tests for the one-time 2.0 ``portal.db`` import (:mod:`whygraph.portal.sqlite_import`).

Each test lays out a 2.0 data dir from the frozen fixture
(``tests/fixtures/portal_2_0_*``) over a fresh, migrated Postgres database
and runs :func:`import_legacy_sqlite` - the case table of plan section 4.4
is the contract. The last section drives the same import through the
portal's start-up.
"""

# ruff: noqa: F811 -- pytest fixtures (`env`) imported from test_portal_app

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlmodel import select

from conftest import (
    LEGACY_REVISION,
    LEGACY_ROW_COUNTS,
    LEGACY_SECRET_PLAINTEXTS,
    install_legacy_portal,
)
from test_portal_app import env, portal_client  # noqa: F401 -- `env` is a fixture
from whygraph.portal import db as portal_db
from whygraph.portal import secrets as pw_secrets
from whygraph.portal import sqlite_import
from whygraph.portal.models import LegacyImport, PortalBase, Project, ScanRun
from whygraph.portal.sqlite_import import (
    LEGACY_REVISIONS,
    TABLE_ORDER,
    LegacyImportError,
    import_legacy_sqlite,
)


@pytest.fixture(autouse=True)
def _propagate_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let ``caplog`` see the records after a CLI test configured ``whygraph`` logging."""
    monkeypatch.setattr(logging.getLogger("whygraph"), "propagate", True)


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, portal_database: str) -> Path:
    """A data dir over a fresh, migrated portal database."""
    monkeypatch.setenv("WHYGRAPH_DATA", str(tmp_path / "data"))
    return portal_db.data_dir()


@pytest.fixture
def legacy(data: Path) -> Path:
    """The seeded 2.0 ``portal.db`` (plus its ``secret.key``) in ``data``."""
    return install_legacy_portal(data)


def _pg_count(table: str) -> int:
    with portal_db.get_engine().connect() as conn:
        return conn.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()


def _pg_counts() -> dict[str, int]:
    return {t: _pg_count(t) for t in (*TABLE_ORDER, "legacy_import")}


EMPTY = dict.fromkeys((*TABLE_ORDER, "legacy_import"), 0)


def _sqlite_rows(path: Path, table: str) -> list[dict]:
    columns = [c.name for c in PortalBase.metadata.tables[table].columns]
    conn = sqlite3.connect(path)
    try:
        quoted = ", ".join(f'"{c}"' for c in columns)
        rows = conn.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
    finally:
        conn.close()
    return [dict(zip(columns, r)) for r in rows]


def _pg_rows(table: str) -> list[dict]:
    with portal_db.get_engine().connect() as conn:
        return [
            dict(r._mapping)
            for r in conn.execute(PortalBase.metadata.tables[table].select())
        ]


def _key(row: dict) -> tuple:
    return (row.get("id"), row.get("project_id"), row.get("agent"))


def _migrated(data: Path) -> list[Path]:
    return sorted(data.glob("portal.db.migrated-*"))


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_the_frozen_fixture_is_a_revision_2_1_imports() -> None:
    assert LEGACY_REVISION in LEGACY_REVISIONS
    assert set(TABLE_ORDER) == set(PortalBase.metadata.tables) - {"legacy_import"}


# ---------------------------------------------------------------------------
# The normal path
# ---------------------------------------------------------------------------


def test_import_keeps_every_row_and_id(data: Path, legacy: Path) -> None:
    expected = {t: _sqlite_rows(legacy, t) for t in TABLE_ORDER}

    report = import_legacy_sqlite(data)

    assert report is not None and report.rows == LEGACY_ROW_COUNTS
    assert not legacy.exists() and _migrated(data) == [report.source]
    for table in TABLE_ORDER:
        want = expected[table]
        for row in want:
            if table == "scan_runs":
                row["analyze"] = bool(row["analyze"])
            if table == "project_config":
                row["config"] = json.loads(row["config"])
        assert sorted(_pg_rows(table), key=_key) == sorted(want, key=_key), table
    with portal_db.get_session() as session:
        assert {r.id for r in session.exec(select(ScanRun)).all()} == {1, 2, 4}
        beta = session.exec(select(Project).where(Project.slug == "beta")).one()
        assert beta.created_by is None
        marker = session.get(LegacyImport, 1)
        assert marker is not None
        assert (marker.source_name, marker.source_revision) == (
            "portal.db",
            LEGACY_REVISION,
        )
        assert marker.rows == LEGACY_ROW_COUNTS
        # The sequences continue after the copied ids.
        run = ScanRun(project_id=beta.id, trigger="manual")
        session.add(run)
        session.flush()
        assert run.id == 5
    with portal_db.get_engine().connect() as conn:
        stored = dict(conn.execute(text("SELECT id, ciphertext FROM secrets")).all())
    assert {i: pw_secrets.decrypt(c) for i, c in stored.items()} == (
        LEGACY_SECRET_PLAINTEXTS
    )


def test_a_second_run_imports_nothing(data: Path, legacy: Path) -> None:
    assert import_legacy_sqlite(data) is not None
    assert import_legacy_sqlite(data) is None
    assert _pg_count("projects") == 2 and len(_migrated(data)) == 1


def test_no_portal_db_is_nothing_to_do(data: Path) -> None:
    assert import_legacy_sqlite(data) is None
    assert _pg_counts() == EMPTY


def test_the_import_logs_one_info_line(
    data: Path, legacy: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=sqlite_import.__name__):
        import_legacy_sqlite(data)
    records = [r for r in caplog.records if r.name == sqlite_import.__name__]
    assert len(records) == 1 and records[0].levelno == logging.INFO
    assert "projects=2" in records[0].getMessage()


# ---------------------------------------------------------------------------
# Refusals: nothing changes, the portal goes degraded
# ---------------------------------------------------------------------------


def test_a_symlinked_portal_db_is_refused(data: Path, tmp_path: Path) -> None:
    elsewhere = install_legacy_portal(tmp_path / "elsewhere")
    (data / "portal.db").symlink_to(elsewhere)
    with pytest.raises(LegacyImportError, match="symlink"):
        import_legacy_sqlite(data)
    assert elsewhere.exists() and (data / "portal.db").is_symlink()
    assert _pg_counts() == EMPTY


def test_a_portal_db_that_is_not_a_file_is_refused(data: Path) -> None:
    (data / "portal.db").mkdir()
    with pytest.raises(LegacyImportError, match="not a regular file"):
        import_legacy_sqlite(data)


def test_an_unknown_revision_is_refused(data: Path, legacy: Path) -> None:
    conn = sqlite3.connect(legacy)
    conn.execute("UPDATE alembic_version SET version_num = 'ffffffffffff'")
    conn.commit()
    conn.close()
    before = legacy.read_bytes()
    with pytest.raises(LegacyImportError, match="revision ffffffffffff") as exc:
        import_legacy_sqlite(data)
    assert "c5e8a1d2b3f4" in str(exc.value) and "remove the file" in str(exc.value)
    assert legacy.read_bytes() == before and _migrated(data) == []
    assert _pg_counts() == EMPTY


def test_a_file_that_is_not_sqlite_is_refused(data: Path) -> None:
    (data / "portal.db").write_bytes(b"not a database at all, just text" * 10)
    with pytest.raises(LegacyImportError, match="not a readable SQLite database"):
        import_legacy_sqlite(data)


def test_a_missing_secret_key_aborts_and_creates_none(data: Path, legacy: Path) -> None:
    (data / "secret.key").unlink()
    with pytest.raises(LegacyImportError, match="secret.key is missing"):
        import_legacy_sqlite(data)
    assert not (data / "secret.key").exists()  # never a fresh, wrong key
    assert legacy.exists() and _pg_counts() == EMPTY


def test_a_wrong_secret_key_aborts_with_postgres_empty(
    data: Path, legacy: Path
) -> None:
    (data / "secret.key").write_bytes(Fernet.generate_key())
    with pytest.raises(LegacyImportError, match="does not match portal.db"):
        import_legacy_sqlite(data)
    assert legacy.exists() and _migrated(data) == []
    assert _pg_counts() == EMPTY


def test_an_exception_mid_copy_rolls_back(
    data: Path, legacy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = sqlite_import._convert

    def convert(table: str, row: dict) -> dict:
        if table == "scan_runs":
            raise RuntimeError("disk on fire")
        return real(table, row)

    monkeypatch.setattr(sqlite_import, "_convert", convert)
    before = legacy.read_bytes()
    with pytest.raises(LegacyImportError, match="rolled back.*disk on fire"):
        import_legacy_sqlite(data)
    assert legacy.read_bytes() == before and _migrated(data) == []
    assert _pg_counts() == EMPTY


def test_a_count_mismatch_rolls_back(
    data: Path, legacy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = sqlite_import._count
    monkeypatch.setattr(
        sqlite_import,
        "_count",
        lambda conn, table: real(conn, table) - (table == "secrets"),
    )
    with pytest.raises(LegacyImportError, match="secrets has 3 rows"):
        import_legacy_sqlite(data)
    assert legacy.exists() and _pg_counts() == EMPTY


# ---------------------------------------------------------------------------
# Declines and recovery
# ---------------------------------------------------------------------------


def test_a_database_set_up_fresh_is_left_alone(
    data: Path, legacy: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with portal_db.get_session() as session:
        session.add(
            Project(slug="fresh", name="fresh", source="local", root="/r/fresh")
        )
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=sqlite_import.__name__):
        assert import_legacy_sqlite(data) is None
    records = [r for r in caplog.records if r.name == sqlite_import.__name__]
    assert [r.levelno for r in records] == [logging.WARNING]
    assert "already holds a portal" in records[0].getMessage()
    assert legacy.exists() and _migrated(data) == []  # not renamed
    assert _pg_count("projects") == 1 and _pg_count("legacy_import") == 0


def test_a_crash_before_the_rename_finishes_the_rename(
    data: Path, legacy: Path, caplog: pytest.LogCaptureFixture
) -> None:
    report = import_legacy_sqlite(data)
    assert report is not None
    report.source.rename(legacy)  # as if the process died before renaming
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=sqlite_import.__name__):
        assert import_legacy_sqlite(data) is None
    records = [r for r in caplog.records if r.name == sqlite_import.__name__]
    assert [r.levelno for r in records] == [logging.INFO]
    assert "finished renaming" in records[0].getMessage()
    assert not legacy.exists() and len(_migrated(data)) == 1
    assert _pg_count("projects") == 2  # imported once


def test_a_different_file_after_an_import_is_left_alone(
    data: Path, legacy: Path, caplog: pytest.LogCaptureFixture
) -> None:
    report = import_legacy_sqlite(data)
    assert report is not None
    report.source.rename(legacy)
    conn = sqlite3.connect(legacy)
    conn.execute("UPDATE projects SET name = 'Changed' WHERE id = 1")
    conn.commit()
    conn.close()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=sqlite_import.__name__):
        assert import_legacy_sqlite(data) is None
    records = [r for r in caplog.records if r.name == sqlite_import.__name__]
    assert [r.levelno for r in records] == [logging.WARNING]
    assert str(legacy) in records[0].getMessage()
    assert legacy.exists() and _migrated(data) == []
    with portal_db.get_session() as session:
        assert session.get(Project, 1).name == "Alpha"


def test_wal_and_shm_left_after_the_checkpoint_move_with_the_file(
    data: Path, legacy: Path
) -> None:
    holder = sqlite3.connect(legacy)  # another reader keeps the sidecars alive
    try:
        holder.execute("PRAGMA journal_mode = WAL")
        holder.execute("SELECT count(*) FROM projects").fetchone()
        assert (data / "portal.db-wal").exists() and (data / "portal.db-shm").exists()
        report = import_legacy_sqlite(data)
    finally:
        holder.close()
    assert report is not None
    assert not (data / "portal.db-wal").exists()
    assert not (data / "portal.db-shm").exists()
    for suffix in ("-wal", "-shm"):
        assert report.source.with_name(report.source.name + suffix).exists()
    assert _pg_count("projects") == 2


# ---------------------------------------------------------------------------
# Through the portal's start-up (plan section 7.2)
# ---------------------------------------------------------------------------


def test_the_portal_imports_a_2_0_data_dir_on_first_start(
    env: SimpleNamespace,
) -> None:
    install_legacy_portal(env.data)
    with portal_client() as client:
        state = client.get("/api/portal/state")
        assert state.status_code == 200 and "error" not in state.json()
        projects = client.get("/api/projects")
        assert projects.status_code == 200, projects.text
        slugs = {p["slug"] for p in projects.json()["projects"]}
    assert slugs == {"alpha", "beta"}
    assert not (env.data / "portal.db").exists()
    assert len(list(env.data.glob("portal.db.migrated-*"))) == 1
    with portal_client() as client:  # a second start imports nothing
        assert len(client.get("/api/projects").json()["projects"]) == 2


def test_an_unknown_revision_leaves_the_portal_degraded(
    env: SimpleNamespace,
) -> None:
    legacy = install_legacy_portal(env.data)
    conn = sqlite3.connect(legacy)
    conn.execute("UPDATE alembic_version SET version_num = 'ffffffffffff'")
    conn.commit()
    conn.close()
    with portal_client() as client:
        error = client.get("/api/portal/state").json()["error"]
        assert "revision ffffffffffff" in error
        assert client.get("/api/projects").status_code == 503
    assert legacy.exists()
