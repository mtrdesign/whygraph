"""Tests for the portal DB, models, migrations, secrets and context builder.

Covers plan step 3: a fresh migrate, the separate ``MetaData`` (both
directions), the ``NULLS NOT DISTINCT`` unique / cascade constraints, the
Fernet key file and keyring, the secret store's API shape, and building a
``ProjectContext`` under the config policy (rules 2 and 3 of section 4.2.1).
Also the Postgres connection plumbing (URL resolution, the pooled and
migration engines, the wait, the instance lock) and the frozen 2.0 fixture.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import time
import warnings
from pathlib import Path
from typing import Iterator

import anyio
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, select

from conftest import (
    LEGACY_REVISION,
    LEGACY_ROW_COUNTS,
    LEGACY_SECRET_KEY,
    LEGACY_SECRET_PLAINTEXTS,
    build_legacy_portal_db,
)
from whygraph.core.config import Config
from whygraph.core.context import ProjectContext
from whygraph.portal import db as portal_db
from whygraph.portal import secrets as pw_secrets
from whygraph.portal.config_layers import (
    ConfigPolicyError,
    find_secret_paths,
    load_layer,
    save_layer,
)
from whygraph.portal.context import (
    ContextCache,
    ProjectNotFound,
    build_project_context,
)
from whygraph.portal.models import (
    PortalBase,
    Project,
    ProjectAgent,
    ProjectConfig,
    ScanRun,
    Secret,
    Setting,
    User,
)
from whygraph.portal.projects import (
    is_valid_slug,
    slugify,
    unique_slug,
    validate_slug,
)

PORTAL_TABLES = {
    "settings",
    "users",
    "projects",
    "project_agents",
    "project_config",
    "secrets",
    "scan_runs",
    "legacy_import",
}


@pytest.fixture
def data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, portal_database: str
) -> Iterator[Path]:
    """An isolated portal data dir over a fresh, migrated portal database."""
    monkeypatch.setenv("WHYGRAPH_DATA", str(tmp_path / "data"))
    path = portal_db.data_dir()
    portal_db._reset_engine()
    try:
        yield path
    finally:
        portal_db._reset_engine()


def _project(session, slug: str = "demo", root: str | None = None, **kw) -> Project:
    project = Project(
        slug=slug,
        name=slug,
        source=kw.pop("source", "local"),
        root=root or f"/repos/{slug}",
        **kw,
    )
    session.add(project)
    session.flush()
    return project


# ---------------------------------------------------------------------------
# Migrations and metadata
# ---------------------------------------------------------------------------


def test_fresh_migrate_creates_portal_tables(data: Path) -> None:
    names = set(inspect(portal_db.get_engine()).get_table_names())
    assert names == PORTAL_TABLES | {"alembic_version"}


def test_data_dir_is_private(data: Path) -> None:
    assert stat.S_IMODE(data.stat().st_mode) & 0o077 == 0


def test_migrate_is_idempotent(data: Path) -> None:
    portal_db.ensure_initialized()
    portal_db.ensure_initialized()
    with portal_db.get_session() as s:
        assert s.exec(select(Setting)).all() == []


def test_metadata_is_separate_in_both_directions() -> None:
    from whygraph.db import models as _project_models  # noqa: F401

    portal = set(PortalBase.metadata.tables)
    project = set(SQLModel.metadata.tables)
    assert portal == PORTAL_TABLES
    assert not portal & project
    assert "chat_session" not in portal
    assert "projects" not in project


def _diff_table_names(metadata, *, include_object=None) -> set[str]:
    """Table names an autogenerate against an *empty* DB would create."""
    engine = create_engine("sqlite://")
    with engine.connect() as conn, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ctx = MigrationContext.configure(
            conn, opts={"include_object": include_object} if include_object else {}
        )
        diff = compare_metadata(ctx, metadata)
    return {op[1].name for op in diff if op[0] == "add_table"}


def test_autogenerate_never_crosses_the_chains() -> None:
    from whygraph.db import models as _project_models  # noqa: F401

    def project_include(obj, name, type_, reflected, compare_to):  # noqa: ANN001
        # the same filter whygraph/db/migrations/env.py applies
        return not (type_ == "table" and name not in SQLModel.metadata.tables)

    project_tables = _diff_table_names(
        SQLModel.metadata, include_object=project_include
    )
    portal_tables = _diff_table_names(PortalBase.metadata)
    assert project_tables and not project_tables & PORTAL_TABLES
    assert portal_tables == PORTAL_TABLES


def test_migration_matches_models(empty_portal_database: str) -> None:
    """No drift between the baseline and the models (the in-suite ``alembic check``)."""
    portal_db.ensure_initialized()
    with portal_db.get_engine().connect() as conn, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "compare_server_default": True}
        )
        diff = compare_metadata(ctx, PortalBase.metadata)
    assert diff == []


def test_baseline_constraints_are_nulls_not_distinct(
    empty_portal_database: str,
) -> None:
    portal_db.ensure_initialized()
    with portal_db.get_engine().connect() as conn:
        defs = dict(
            conn.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE indexname IN ('uq_project_config_scope', 'uq_secrets_scope')"
                )
            ).all()
        )
    assert set(defs) == {"uq_project_config_scope", "uq_secrets_scope"}
    assert all("NULLS NOT DISTINCT" in d for d in defs.values()), defs


def test_baseline_inserts_no_rows(empty_portal_database: str) -> None:
    portal_db.ensure_initialized()
    with portal_db.get_engine().connect() as conn:
        counts = {
            t: conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar_one()
            for t in PORTAL_TABLES
        }
    assert counts == dict.fromkeys(PORTAL_TABLES, 0)


def test_legacy_2_0_fixture_is_consistent(tmp_path: Path) -> None:
    """The frozen 2.0 portal the importer reads: its head, its rows, its key."""
    db = build_legacy_portal_db(tmp_path / "portal.db")
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [
            (LEGACY_REVISION,)
        ]
        counts = {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in LEGACY_ROW_COUNTS
        }
        ciphertexts = dict(conn.execute("SELECT id, ciphertext FROM secrets"))
        run_ids = [r[0] for r in conn.execute("SELECT id FROM scan_runs ORDER BY id")]
        analyze = {r[0] for r in conn.execute("SELECT analyze FROM scan_runs")}
        null_creator = conn.execute(
            "SELECT count(*) FROM projects WHERE created_by IS NULL"
        ).fetchone()[0]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    assert counts == LEGACY_ROW_COUNTS
    assert run_ids == [1, 2, 4] and analyze == {0, 1} and null_creator == 1
    keyring = pw_secrets.MultiFernet(
        [pw_secrets.Fernet(LEGACY_SECRET_KEY.read_bytes().strip())]
    )
    assert {
        i: keyring.decrypt(c.encode()).decode() for i, c in ciphertexts.items()
    } == LEGACY_SECRET_PLAINTEXTS


# ---------------------------------------------------------------------------
# Connection plumbing: URL, engines, wait, instance lock
# ---------------------------------------------------------------------------


@pytest.fixture
def no_db_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.delenv(portal_db.DATABASE_URL_ENV, raising=False)
    monkeypatch.delenv(portal_db.DATABASE_PASSWORD_FILE_ENV, raising=False)
    return monkeypatch


@pytest.mark.parametrize("value", [None, "", "   "])
def test_database_url_unset_is_not_configured(
    no_db_env: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is not None:
        no_db_env.setenv(portal_db.DATABASE_URL_ENV, value)
    with pytest.raises(portal_db.PortalDatabaseNotConfigured, match="is not set"):
        portal_db.database_url()


@pytest.mark.parametrize(
    "value", ["sqlite:///portal.db", "mysql://u:p@h/db", "not a url"]
)
def test_database_url_must_be_postgres(
    no_db_env: pytest.MonkeyPatch, value: str
) -> None:
    no_db_env.setenv(portal_db.DATABASE_URL_ENV, value)
    with pytest.raises(portal_db.PortalDatabaseNotConfigured):
        portal_db.database_url()


@pytest.mark.parametrize(
    "value",
    [
        "postgres://u:pw@db:5433/whygraph",
        "postgresql://u:pw@db:5433/whygraph",
        "postgresql+psycopg2://u:pw@db:5433/whygraph",
        "postgresql+psycopg://u:pw@db:5433/whygraph",
    ],
)
def test_database_url_is_normalised_to_psycopg(
    no_db_env: pytest.MonkeyPatch, value: str
) -> None:
    no_db_env.setenv(portal_db.DATABASE_URL_ENV, value)
    url = portal_db.database_url()
    assert url.drivername == "postgresql+psycopg"
    assert (url.username, url.password, url.host, url.port, url.database) == (
        "u",
        "pw",
        "db",
        5433,
        "whygraph",
    )
    assert portal_db.database_target(url) == "db:5433/whygraph"


def test_password_file_wins_and_is_stripped(
    no_db_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = tmp_path / "postgres.password"
    secret.write_text("  s3cr3t-pw\n\n")
    no_db_env.setenv(portal_db.DATABASE_URL_ENV, "postgresql://u:url-pw@db/whygraph")
    no_db_env.setenv(portal_db.DATABASE_PASSWORD_FILE_ENV, str(secret))
    assert portal_db.database_url().password == "s3cr3t-pw"
    no_db_env.setenv(portal_db.DATABASE_URL_ENV, "postgresql://u@db/whygraph")
    assert portal_db.database_url().password == "s3cr3t-pw"


def test_missing_or_unreadable_password_file_is_not_configured(
    no_db_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    no_db_env.setenv(portal_db.DATABASE_URL_ENV, "postgresql://u@db/whygraph")
    no_db_env.setenv(portal_db.DATABASE_PASSWORD_FILE_ENV, str(tmp_path / "missing"))
    with pytest.raises(portal_db.PortalDatabaseNotConfigured, match="cannot read"):
        portal_db.database_url()
    locked = tmp_path / "locked"
    locked.write_text("pw")
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):  # running as root: chmod cannot deny it
            return
        no_db_env.setenv(portal_db.DATABASE_PASSWORD_FILE_ENV, str(locked))
        with pytest.raises(portal_db.PortalDatabaseNotConfigured, match="cannot read"):
            portal_db.database_url()
    finally:
        locked.chmod(0o600)


def test_alembic_config_has_no_placeholder_url() -> None:
    assert portal_db.alembic_config().get_main_option("sqlalchemy.url") is None


def test_ensure_initialized_returns_none_and_legacy_path(data: Path) -> None:
    assert portal_db.ensure_initialized() is None
    assert portal_db.legacy_db_path() == data / "portal.db"
    assert not portal_db.legacy_db_path().exists()


def test_postgres_scheme_and_password_file_authenticate(
    portal_database: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    url = make_url(portal_database)
    secret = tmp_path / "postgres.password"
    secret.write_text(f"{url.password or ''}\n")
    bare = url.set(drivername="postgres", password=None)
    monkeypatch.setenv(
        portal_db.DATABASE_URL_ENV, bare.render_as_string(hide_password=False)
    )
    monkeypatch.setenv(portal_db.DATABASE_PASSWORD_FILE_ENV, str(secret))
    assert portal_db.database_url().password == (url.password or "")
    with portal_db.get_engine().connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar_one() == 1


def _show(conn, name: str) -> str:  # noqa: ANN001
    return conn.execute(text(f"SHOW {name}")).scalar_one()


def test_only_pooled_connections_carry_the_server_timeouts(
    portal_database: str,
) -> None:
    with portal_db.get_engine().connect() as conn:
        assert _show(conn, "statement_timeout") == "30s"
        assert _show(conn, "idle_in_transaction_session_timeout") == "1min"
    engine = portal_db.migration_engine()
    try:
        with engine.connect() as conn:
            assert _show(conn, "statement_timeout") == "0"
            assert _show(conn, "idle_in_transaction_session_timeout") == "0"
    finally:
        engine.dispose()
    lock = portal_db.InstanceLock()
    assert lock.acquire()
    try:
        conn = lock._conn
        assert conn is not None
        assert _show(conn, "statement_timeout") == "0"
        assert _show(conn, "idle_in_transaction_session_timeout") == "0"
    finally:
        lock.release()


def _closed_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_wait_for_database_returns_when_reachable(portal_database: str) -> None:
    portal_db.wait_for_database(timeout=5)


def test_wait_for_database_times_out_within_its_budget(
    no_db_env: pytest.MonkeyPatch,
) -> None:
    port = _closed_port()
    no_db_env.setenv(
        portal_db.DATABASE_URL_ENV, f"postgresql://u:hunter2@127.0.0.1:{port}/wg"
    )
    no_db_env.setenv("WHYGRAPH_DATABASE_WAIT_SEC", "1")
    started = time.monotonic()
    with pytest.raises(portal_db.PortalDatabaseUnreachable) as exc:
        portal_db.wait_for_database()
    assert time.monotonic() - started < 4
    assert exc.value.target == f"127.0.0.1:{port}/wg"
    assert "hunter2" not in str(exc.value)


def test_instance_lock_is_exclusive_per_database(portal_database: str) -> None:
    first, second = portal_db.InstanceLock(), portal_db.InstanceLock()
    assert not first.is_held()
    assert first.acquire() and first.is_held()
    with pytest.raises(RuntimeError):
        first.acquire()  # once per process: advisory locks are re-entrant
    assert second.acquire() is False and not second.is_held()
    first.release()
    assert not first.is_held()
    first.release()  # a no-op once released
    assert second.acquire()
    second.release()


def test_instance_lock_is_lost_with_its_session(portal_database: str) -> None:
    lock = portal_db.InstanceLock()
    assert lock.acquire()
    try:
        with portal_db.get_engine().connect() as conn:
            pids = (
                conn.execute(
                    text(
                        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND "
                        "database = (SELECT oid FROM pg_database "
                        "WHERE datname = current_database())"
                    )
                )
                .scalars()
                .all()
            )
            assert len(pids) == 1
            conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pids[0]})
        deadline = time.monotonic() + 5
        while lock.is_held() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not lock.is_held()
        successor = portal_db.InstanceLock()
        assert successor.acquire()  # free for the next portal
        successor.release()
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


def test_saving_global_config_twice_leaves_one_row(data: Path) -> None:
    with portal_db.get_session() as s:
        save_layer(s, None, {"llm": {"model": "anthropic/a"}})
    with portal_db.get_session() as s:
        save_layer(s, None, {"llm": {"model": "anthropic/b"}})
    with portal_db.get_session() as s:
        rows = s.exec(
            select(ProjectConfig).where(ProjectConfig.project_id.is_(None))
        ).all()
        assert len(rows) == 1
        assert rows[0].config == {"llm": {"model": "anthropic/b"}}
        assert load_layer(s, None) == {"llm": {"model": "anthropic/b"}}


def test_saving_project_config_twice_leaves_one_row(data: Path) -> None:
    with portal_db.get_session() as s:
        p = _project(s)
        save_layer(s, p.id, {"analyze": {"max_workers": 2}})
        save_layer(s, p.id, {"analyze": {"max_workers": 3}})
    with portal_db.get_session() as s:
        assert len(s.exec(select(ProjectConfig)).all()) == 1


def test_duplicate_global_rows_are_rejected_by_the_index(data: Path) -> None:
    with pytest.raises(IntegrityError):
        with portal_db.get_session() as s:
            s.add(ProjectConfig(project_id=None, config={}))
            s.flush()
            s.add(ProjectConfig(project_id=None, config={"a": 1}))
            s.flush()


def test_saving_a_global_secret_twice_leaves_one_row(data: Path) -> None:
    for value in ("sk-first-1111", "sk-second-2222"):
        with portal_db.get_session() as s:
            pw_secrets.put_secret(
                s, kind="llm_api_key", provider="anthropic", value=value
            )
    with portal_db.get_session() as s:
        assert len(s.exec(select(Secret)).all()) == 1
        assert (
            pw_secrets.read_secret(s, kind="llm_api_key", provider="anthropic")
            == "sk-second-2222"
        )


def test_github_token_uniqueness_holds_despite_null_provider(data: Path) -> None:
    """A NULL provider must not let duplicate GitHub tokens slip past UNIQUE."""
    with pytest.raises(IntegrityError):
        with portal_db.get_session() as s:
            for _ in range(2):
                s.add(Secret(kind="github_token", ciphertext="x", hint="x"))
                s.flush()


def test_project_secret_uniqueness_holds_despite_null_provider(data: Path) -> None:
    with portal_db.get_session() as s:
        p = _project(s)
        pid = p.id
    with pytest.raises(IntegrityError):
        with portal_db.get_session() as s:
            for _ in range(2):
                s.add(
                    Secret(
                        project_id=pid, kind="github_token", ciphertext="x", hint="x"
                    )
                )
                s.flush()


def test_secret_kind_and_provider_are_checked(data: Path) -> None:
    for bad in (
        Secret(kind="nonsense", ciphertext="x", hint="x"),
        Secret(kind="llm_api_key", provider=None, ciphertext="x", hint="x"),
        Secret(kind="github_token", provider="anthropic", ciphertext="x", hint="x"),
    ):
        with pytest.raises(IntegrityError):
            with portal_db.get_session() as s:
                s.add(bad)
                s.flush()


def test_deleting_a_project_cascades(data: Path) -> None:
    with portal_db.get_session() as s:
        p = _project(s)
        other = _project(s, "other")
        pid, oid = p.id, other.id
        save_layer(s, pid, {"analyze": {"max_workers": 2}})
        save_layer(s, oid, {"analyze": {"max_workers": 4}})
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="openai", value="sk-abcd", project_id=pid
        )
        pw_secrets.put_secret(s, kind="github_token", value="ghp_xxxx", project_id=oid)
        s.add(ProjectAgent(project_id=pid, agent="claude"))
        s.add(ScanRun(project_id=pid, trigger="manual"))
        s.add(ScanRun(project_id=oid, trigger="hook"))
    with portal_db.get_session() as s:
        s.delete(s.get(Project, pid))
    with portal_db.get_session() as s:
        assert [r.project_id for r in s.exec(select(ProjectConfig)).all()] == [oid]
        assert [r.project_id for r in s.exec(select(Secret)).all()] == [oid]
        assert s.exec(select(ProjectAgent)).all() == []
        assert [r.project_id for r in s.exec(select(ScanRun)).all()] == [oid]


def test_deleting_a_user_nulls_attribution(data: Path) -> None:
    with portal_db.get_session() as s:
        u = User(display_name="Ada")
        s.add(u)
        s.flush()
        p = _project(s, created_by=u.id)
        s.add(ScanRun(project_id=p.id, trigger="manual", requested_by=u.id))
        uid = u.id
    with portal_db.get_session() as s:
        s.delete(s.get(User, uid))
    with portal_db.get_session() as s:
        assert s.exec(select(Project)).one().created_by is None
        assert s.exec(select(ScanRun)).one().requested_by is None


def test_user_has_a_stable_uuid(data: Path) -> None:
    with portal_db.get_session() as s:
        a, b = User(display_name="a"), User(display_name="b")
        s.add(a)
        s.add(b)
        s.flush()
        assert len(a.uid) == 36 and a.uid != b.uid
        assert a.role == "owner" and a.username is None and a.password_hash is None


def test_project_agents_rejects_unknown_agent_and_duplicates(data: Path) -> None:
    with portal_db.get_session() as s:
        pid = _project(s).id
        s.add(ProjectAgent(project_id=pid, agent="claude"))
    with pytest.raises(IntegrityError):
        with portal_db.get_session() as s:
            s.add(ProjectAgent(project_id=pid, agent="emacs"))
            s.flush()
    with pytest.raises(IntegrityError):
        with portal_db.get_session() as s:
            s.add(ProjectAgent(project_id=pid, agent="claude"))
            s.flush()
    with portal_db.get_session() as s:
        for agent in ("cursor", "vscode", "codex"):
            s.add(ProjectAgent(project_id=pid, agent=agent))
    with portal_db.get_session() as s:
        assert len(s.exec(select(ProjectAgent)).all()) == 4


def test_settings_is_a_singleton(data: Path) -> None:
    with portal_db.get_session() as s:
        s.add(Setting(mode="local"))
    with pytest.raises(IntegrityError):
        with portal_db.get_session() as s:
            s.add(Setting(id=2, mode="local"))
            s.flush()
    with pytest.raises(IntegrityError):
        with portal_db.get_session() as s:
            s.execute(text("UPDATE settings SET mode = 'bogus'"))


def test_project_slug_and_root_are_unique(data: Path) -> None:
    with portal_db.get_session() as s:
        _project(s, "a", root="/r/a")
    with pytest.raises(IntegrityError):
        with portal_db.get_session() as s:
            _project(s, "a", root="/r/other")
    with pytest.raises(IntegrityError):
        with portal_db.get_session() as s:
            _project(s, "b", root="/r/a")


def test_m2_columns_exist(data: Path) -> None:
    insp = inspect(portal_db.get_engine())
    cols = {
        t: {c["name"] for c in insp.get_columns(t)}
        for t in ("users", "projects", "scan_runs")
    }
    assert {"uid", "username", "password_hash", "role"} <= cols["users"]
    assert {"created_by", "last_scanned_head", "initialized_at"} <= cols["projects"]
    assert {"requested_by", "analyze", "trigger", "kind"} <= cols["scan_runs"]


# ---------------------------------------------------------------------------
# Slugs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", ["a", "whygraph", "my-repo-2", "0abc", "a" * 63])
def test_valid_slugs(slug: str) -> None:
    assert is_valid_slug(slug) and validate_slug(slug) == slug


@pytest.mark.parametrize(
    "slug", ["", "-a", "A", "a b", "a_b", "a/b", "a.b", "a" * 64, "a\n", "../x", "é"]
)
def test_invalid_slugs(slug: str) -> None:
    assert not is_valid_slug(slug)
    with pytest.raises(ValueError):
        validate_slug(slug)


def test_slugify() -> None:
    assert slugify("My Repo_v2") == "my-repo-v2"
    assert slugify("--Hello!!World--") == "hello-world"
    assert slugify("???") == "project"
    assert is_valid_slug(slugify("x" * 200))


def test_unique_slug_suffixes_on_collision(data: Path) -> None:
    with portal_db.get_session() as s:
        assert unique_slug(s, "Whygraph") == "whygraph"
        _project(s, "whygraph")
        assert unique_slug(s, "Whygraph") == "whygraph-2"
        _project(s, "whygraph-2")
        assert unique_slug(s, "whygraph") == "whygraph-3"
        long = "x" * 63
        _project(s, long)
        assert is_valid_slug(unique_slug(s, long))


def test_slug_is_validated_on_insert_and_immutable(data: Path) -> None:
    with pytest.raises(ValueError):
        with portal_db.get_session() as s:
            _project(s, "Bad Slug")
    with portal_db.get_session() as s:
        _project(s, "keep")
    with portal_db.get_session() as s:
        project = s.exec(select(Project)).one()
        project.name = "Renamed"  # only the name is renamable
    with pytest.raises(ValueError, match="immutable"):
        with portal_db.get_session() as s:
            s.exec(select(Project)).one().slug = "changed"
            s.flush()
    with portal_db.get_session() as s:
        project = s.exec(select(Project)).one()
        assert (project.slug, project.name) == ("keep", "Renamed")


# ---------------------------------------------------------------------------
# Key file, keyring, secret store
# ---------------------------------------------------------------------------


def test_key_file_is_private_and_stable(data: Path) -> None:
    key = pw_secrets.ensure_key()
    path = data / "secret.key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert pw_secrets.ensure_key() == key
    assert list(data.glob("secret.key.*.tmp")) == []


def test_keyring_round_trips_and_ciphertext_differs_per_write(data: Path) -> None:
    a, b = pw_secrets.encrypt("sk-secret"), pw_secrets.encrypt("sk-secret")
    assert a != b and "sk-secret" not in a
    assert pw_secrets.decrypt(a) == pw_secrets.decrypt(b) == "sk-secret"
    assert len(pw_secrets.load_keyring()._fernets) == 1  # M1: one key, MultiFernet


def test_secrets_round_trip_and_ciphertext_differs_per_write(data: Path) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="anthropic", value="sk-ant-a1b2"
        )
        first = s.exec(select(Secret)).one().ciphertext
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="anthropic", value="sk-ant-a1b2"
        )
        second = s.exec(select(Secret)).one().ciphertext
        assert (
            pw_secrets.read_secret(s, kind="llm_api_key", provider="anthropic")
            == "sk-ant-a1b2"
        )
    assert first != second
    with portal_db.get_engine().connect() as raw:
        stored = raw.execute(text("SELECT ciphertext, hint FROM secrets")).one()
    assert "sk-ant" not in stored[0] and "sk-ant" not in stored[1]


def test_secret_status_exposes_only_set_and_hint(data: Path) -> None:
    with portal_db.get_session() as s:
        assert pw_secrets.secret_status(s, kind="github_token") == {
            "set": False,
            "hint": None,
        }
        pw_secrets.put_secret(s, kind="github_token", value="ghp_abcdefa1b2")
        status = pw_secrets.secret_status(s, kind="github_token")
    assert status == {"set": True, "hint": "…a1b2"}
    assert "ghp_" not in repr(status)


def test_unreadable_secret_is_flagged_not_fatal(data: Path) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(s, kind="github_token", value="ghp_abcdefa1b2")
        s.exec(select(Secret)).one().ciphertext = "not-a-fernet-token"
    with portal_db.get_session() as s:
        status = pw_secrets.secret_status(s, kind="github_token")
    assert status == {"set": True, "hint": "…a1b2", "unreadable": True}


def test_secret_store_validates_scope(data: Path) -> None:
    with portal_db.get_session() as s:
        for kw in (
            dict(kind="llm_api_key", provider="ollama", value="x"),
            dict(kind="llm_api_key", provider=None, value="x"),
            dict(kind="github_token", provider="anthropic", value="x"),
            dict(kind="weird", value="x"),
            dict(kind="github_token", value="   "),
        ):
            with pytest.raises(ValueError):
                pw_secrets.put_secret(s, **kw)


def test_delete_secret(data: Path) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(s, kind="github_token", value="ghp_1234")
        assert pw_secrets.delete_secret(s, kind="github_token") is True
        assert pw_secrets.delete_secret(s, kind="github_token") is False


_RACER = """
import os, sys, time
os.environ["WHYGRAPH_DATA"] = sys.argv[1]
go = sys.argv[2]
while not os.path.exists(go):
    time.sleep(0.001)
from whygraph.portal import secrets
print(secrets.ensure_key().decode())
"""


def test_two_processes_racing_end_with_one_complete_key(tmp_path: Path) -> None:
    data = tmp_path / "race-data"
    data.mkdir()
    go = tmp_path / "go"
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _RACER, str(data), str(go)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    time.sleep(1.0)  # let every child import whygraph and reach the barrier
    go.touch()
    outs = [p.communicate(timeout=60) for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    keys = {out.strip() for out, _ in outs}
    assert len(keys) == 1
    on_disk = (data / "secret.key").read_bytes().strip().decode()
    assert keys == {on_disk} and len(on_disk) == 44
    assert list(data.glob("secret.key.*.tmp")) == []


def test_key_race_loser_reads_the_winners_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHYGRAPH_DATA", str(tmp_path / "d"))
    winner = pw_secrets.Fernet.generate_key()
    real_link = os.link

    def racing_link(src, dst, *a, **kw):  # noqa: ANN001
        Path(dst).write_bytes(winner)  # the other process publishes first
        return real_link(src, dst, *a, **kw)  # -> FileExistsError

    monkeypatch.setattr(os, "link", racing_link)
    assert pw_secrets.ensure_key() == winner
    assert Path(tmp_path / "d" / "secret.key").read_bytes() == winner
    assert list((tmp_path / "d").glob("secret.key.*.tmp")) == []


def test_stale_tmp_from_a_reused_pid_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHYGRAPH_DATA", str(tmp_path / "d"))
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / f"secret.key.{os.getpid()}.tmp").write_text("stale")
    key = pw_secrets.ensure_key()
    assert (tmp_path / "d" / "secret.key").read_bytes() == key


def test_corrupt_key_file_is_an_error_not_a_silent_regenerate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHYGRAPH_DATA", str(tmp_path / "d"))
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "secret.key").write_text("garbage")
    with pytest.raises(ValueError):
        pw_secrets.ensure_key()


# ---------------------------------------------------------------------------
# Config policy
# ---------------------------------------------------------------------------


def test_secrets_in_config_dict_are_rejected(data: Path) -> None:
    bad = {"llm": {"anthropic": {"api_key": "sk-x"}}, "scan": {"token": "ghp_x"}}
    assert find_secret_paths(bad) == ["llm.anthropic.api_key", "scan.token"]
    with portal_db.get_session() as s:
        with pytest.raises(ConfigPolicyError) as exc:
            save_layer(s, None, bad)
        assert "sk-x" not in str(exc.value) and "ghp_x" not in str(exc.value)
        assert s.exec(select(ProjectConfig)).all() == []


def test_changing_an_endpoint_clears_that_scopes_key_only(data: Path) -> None:
    with portal_db.get_session() as s:
        pid = _project(s).id
        save_layer(s, None, {"llm": {"openai": {"base_url": "https://a.example/v1"}}})
        save_layer(s, pid, {})
        for scope in (None, pid):
            pw_secrets.put_secret(
                s,
                kind="llm_api_key",
                provider="openai",
                value="sk-1111",
                project_id=scope,
            )
            pw_secrets.put_secret(
                s,
                kind="llm_api_key",
                provider="anthropic",
                value="sk-2222",
                project_id=scope,
            )
        # unrelated edit: keys survive
        save_layer(
            s,
            None,
            {
                "llm": {"openai": {"base_url": "https://a.example/v1"}},
                "analyze": {"max_workers": 2},
            },
        )
        assert pw_secrets.secret_status(s, kind="llm_api_key", provider="openai")["set"]
        # global endpoint edit clears the global openai key, and the openai
        # key of every project that inherits the endpoint (security fix 1,
        # finding 6) - never another provider's
        cleared = save_layer(
            s, None, {"llm": {"openai": {"base_url": "https://evil.example/v1"}}}
        )
        assert cleared == [(pid, "openai")]
        assert not pw_secrets.secret_status(s, kind="llm_api_key", provider="openai")[
            "set"
        ]
        assert pw_secrets.secret_status(s, kind="llm_api_key", provider="anthropic")[
            "set"
        ]
        assert not pw_secrets.secret_status(
            s, kind="llm_api_key", provider="openai", project_id=pid
        )["set"]
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="openai", value="sk-3333", project_id=pid
        )
        # a project-scope endpoint edit clears that project's key
        save_layer(s, pid, {"llm": {"openai": {"base_url": "https://p.example/v1"}}})
        assert not pw_secrets.secret_status(
            s, kind="llm_api_key", provider="openai", project_id=pid
        )["set"]
        assert pw_secrets.secret_status(
            s, kind="llm_api_key", provider="anthropic", project_id=pid
        )["set"]


def test_removing_an_endpoint_also_clears_the_key(data: Path) -> None:
    with portal_db.get_session() as s:
        save_layer(s, None, {"llm": {"ollama": {"host": "http://h:1"}}})
        save_layer(s, None, {"llm": {"openai": {"base_url": "https://a/v1"}}})
        pw_secrets.put_secret(s, kind="llm_api_key", provider="openai", value="sk-1111")
        save_layer(s, None, {})
        assert not pw_secrets.secret_status(s, kind="llm_api_key", provider="openai")[
            "set"
        ]


# ---------------------------------------------------------------------------
# Building a ProjectContext
# ---------------------------------------------------------------------------


def _build(session, project: Project) -> ProjectContext:
    return build_project_context(session, project)


def test_context_merges_layers_project_wins(data: Path) -> None:
    with portal_db.get_session() as s:
        save_layer(
            s,
            None,
            {
                "llm": {"model": "anthropic/claude-x"},
                "analyze": {"max_workers": 2},
                "scan": {"max_workers": 9},  # 1.x alias, normalized per layer
            },
        )
        p = _project(s)
        save_layer(
            s, p.id, {"analyze": {"max_workers": 6}, "scan": {"forge": "github"}}
        )
        ctx = _build(s, p)
    assert ctx.slug == "demo" and ctx.root == Path("/repos/demo")
    assert ctx.config.model_for("analyze") == ("anthropic", "claude-x")
    assert ctx.config.analyze.max_workers == 6
    assert ctx.config.scan_forge == "github"


def test_context_null_resets_a_key(data: Path) -> None:
    with portal_db.get_session() as s:
        save_layer(s, None, {"analyze": {"max_workers": 2}})
        p = _project(s)
        save_layer(s, p.id, {"analyze": {"max_workers": None}})
        assert _build(s, p).config.analyze.max_workers == Config().analyze.max_workers


def test_db_paths_in_a_project_row_are_ignored(data: Path, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    with portal_db.get_session() as s:
        save_layer(
            s,
            None,
            {"whygraph_db": "/etc/global.db", "codegraph_db": str(data / "portal.db")},
        )
        p = _project(s, root=str(root))
        save_layer(
            s,
            p.id,
            {
                "whygraph_db": str(tmp_path / "other" / "whygraph.db"),
                "codegraph_db": "../x.db",
            },
        )
        ctx = _build(s, p)
    assert ctx.config.whygraph_db == root / ".whygraph" / "whygraph.db"
    assert ctx.config.codegraph_db == root / ".codegraph" / "codegraph.db"


def test_github_clone_root_resolves_under_the_data_dir(data: Path) -> None:
    with portal_db.get_session() as s:
        p = _project(s, "cloned", root="repos/cloned", source="github")
        ctx = _build(s, p)
    assert ctx.root == data / "repos" / "cloned"
    assert (
        ctx.config.whygraph_db
        == data / "repos" / "cloned" / ".whygraph" / "whygraph.db"
    )


def test_context_injects_secrets_in_memory_only(data: Path) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="anthropic", value="sk-glob-1111"
        )
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="claude-cli", value="sk-cli-2222"
        )
        pw_secrets.put_secret(s, kind="github_token", value="ghp_glob3333")
        p = _project(s)
        ctx = _build(s, p)
        stored = [c.config for c in s.exec(select(ProjectConfig)).all()]
    assert ctx.config.llm.anthropic.api_key == "sk-glob-1111"
    assert ctx.config.llm.claude_cli.api_key == "sk-cli-2222"
    assert ctx.config.scan_token == "ghp_glob3333"
    assert stored == []
    for secret in ("sk-glob-1111", "sk-cli-2222", "ghp_glob3333"):
        assert secret not in repr(ctx.config) and secret not in repr(ctx)


def test_project_secret_overrides_global_for_that_project_only(data: Path) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="anthropic", value="sk-glob-0000"
        )
        pw_secrets.put_secret(s, kind="github_token", value="ghp_glob0000")
        a, b = _project(s, "a"), _project(s, "b")
        pw_secrets.put_secret(
            s,
            kind="llm_api_key",
            provider="anthropic",
            value="sk-proj-a111",
            project_id=a.id,
        )
        pw_secrets.put_secret(
            s, kind="github_token", value="ghp_proj-a11", project_id=a.id
        )
        ca, cb = _build(s, a), _build(s, b)
    assert ca.config.llm.anthropic.api_key == "sk-proj-a111"
    assert ca.config.scan_token == "ghp_proj-a11"
    assert cb.config.llm.anthropic.api_key == "sk-glob-0000"
    assert cb.config.scan_token == "ghp_glob0000"


def test_project_overriding_base_url_gets_no_global_key(data: Path) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="openai", value="sk-glob-1111"
        )
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="anthropic", value="sk-glob-2222"
        )
        plain, overriding = _project(s, "plain"), _project(s, "over")
        save_layer(
            s,
            overriding.id,
            {"llm": {"openai": {"base_url": "https://attacker.example/v1"}}},
        )
        c_plain, c_over = _build(s, plain), _build(s, overriding)
    assert c_plain.config.llm.openai.api_key == "sk-glob-1111"
    assert c_over.config.llm.openai.base_url == "https://attacker.example/v1"
    assert c_over.config.llm.openai.api_key is None  # never sent to the override
    assert (
        c_over.config.llm.anthropic.api_key == "sk-glob-2222"
    )  # other providers unaffected


def test_project_own_key_is_kept_with_its_own_endpoint(data: Path) -> None:
    with portal_db.get_session() as s:
        p = _project(s)
        save_layer(s, p.id, {"llm": {"openai": {"base_url": "https://gw.example/v1"}}})
        pw_secrets.put_secret(
            s,
            kind="llm_api_key",
            provider="openai",
            value="sk-mine-1111",
            project_id=p.id,
        )
        assert _build(s, p).config.llm.openai.api_key == "sk-mine-1111"


def test_null_endpoint_is_not_an_override(data: Path) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="openai", value="sk-glob-1111"
        )
        p = _project(s)
        save_layer(s, p.id, {"llm": {"openai": {"base_url": None}}})
        assert _build(s, p).config.llm.openai.api_key == "sk-glob-1111"


def test_unreadable_secret_does_not_fail_the_build_or_fall_back(data: Path) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="anthropic", value="sk-glob-0000"
        )
        p = _project(s)
        pw_secrets.put_secret(
            s,
            kind="llm_api_key",
            provider="anthropic",
            value="sk-proj-1111",
            project_id=p.id,
        )
        for row in s.exec(select(Secret).where(Secret.project_id == p.id)).all():
            row.ciphertext = "garbage"
        s.flush()
        ctx = _build(s, p)
    # the broken project key is skipped; the global one is NOT silently used instead
    assert ctx.config.llm.anthropic.api_key is None


# ---------------------------------------------------------------------------
# ContextCache
# ---------------------------------------------------------------------------


def test_cache_builds_once_and_rebuilds_after_invalidate(data: Path) -> None:
    cache = ContextCache()
    with portal_db.get_session() as s:
        pid = _project(s).id
        save_layer(s, pid, {"analyze": {"max_workers": 2}})
    first = cache.get(pid)
    assert cache.get(pid) is first
    with portal_db.get_session() as s:
        save_layer(s, pid, {"analyze": {"max_workers": 5}})
    assert cache.get(pid) is first  # stale until invalidated, by design
    cache.invalidate(pid)
    assert cache.get(pid).config.analyze.max_workers == 5
    cache.invalidate()
    assert cache.get(pid) is not first


def test_cache_async_miss_and_unknown_project(data: Path) -> None:
    cache = ContextCache()
    with portal_db.get_session() as s:
        pid = _project(s).id

    async def go() -> None:
        ctx = await cache.aget(pid)
        assert ctx.slug == "demo"
        assert await cache.aget(pid) is ctx
        with pytest.raises(ProjectNotFound):
            await cache.aget(999)

    anyio.run(go)


def test_config_repr_hides_secrets() -> None:
    cfg = Config.from_dict(
        {
            "scan": {"token": "ghp_visible"},
            "llm": {"openai": {"api_key": "sk-visible"}},
        },
        Path("."),
    )
    assert "ghp_visible" not in repr(cfg) and "sk-visible" not in repr(cfg)
    assert cfg.scan_token == "ghp_visible" and cfg.llm.openai.api_key == "sk-visible"


def test_claude_oauth_token_is_a_providerless_secret_project_over_global(
    data: Path,
) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(s, kind="claude_oauth_token", value="sk-ant-oat-glob1")
        a, b = _project(s, "a"), _project(s, "b")
        pw_secrets.put_secret(
            s, kind="claude_oauth_token", value="sk-ant-oat-proja", project_id=a.id
        )
        ca, cb = _build(s, a), _build(s, b)
        with pytest.raises(ValueError, match="has no provider"):
            pw_secrets.put_secret(
                s, kind="claude_oauth_token", provider="claude-cli", value="x"
            )
        stored = [c.config for c in s.exec(select(ProjectConfig)).all()]
    assert ca.config.llm.claude_cli.oauth_token == "sk-ant-oat-proja"
    assert cb.config.llm.claude_cli.oauth_token == "sk-ant-oat-glob1"
    assert stored == []  # a secret never lands in a config layer
    for secret in ("sk-ant-oat-glob1", "sk-ant-oat-proja"):
        assert secret not in repr(ca.config) and secret not in repr(cb)
