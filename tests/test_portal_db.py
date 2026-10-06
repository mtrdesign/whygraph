"""Tests for the portal DB, models, migrations, secrets and context builder.

Covers plan step 3: a fresh migrate, the separate ``MetaData`` (both
directions), the ``NULLS NOT DISTINCT`` unique / cascade constraints, the
Fernet key file and keyring, the secret store's API shape, and building a
``ProjectContext`` under the config policy (rules 2 and 3 of section 4.2.1).
Also the Postgres connection plumbing (URL resolution, the pooled and
migration engines, the wait, the instance lock), and M2b's tenancy revision
(backfill, downgrade) and organization constraints.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
import warnings
from pathlib import Path
from typing import Iterator

import anyio
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, select

from conftest import builtin_org_id
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
    Membership,
    Organization,
    PortalBase,
    Project,
    ProjectAgent,
    ProjectConfig,
    ScanRun,
    Secret,
    Setting,
    User,
)
from whygraph.portal.orgs import add_member, create_org, ensure_builtin_org
from whygraph.portal.projects import (
    is_valid_slug,
    slugify,
    unique_slug,
    validate_slug,
)

PORTAL_TABLES = {
    "settings",
    "organizations",
    "memberships",
    "users",
    "projects",
    "project_agents",
    "project_config",
    "secrets",
    "scan_runs",
    "sessions",
    "password_resets",
    "retired_org_slugs",
    "connection_tokens",
    "platform_links",
    "project_grants",
    "invitations",
    "invitation_grants",
    "audit_events",
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


@pytest.fixture
def org(data: Path) -> int:
    """The built-in org's id (created, as the portal's start would)."""
    with portal_db.get_session() as s:
        return builtin_org_id(s)


def _project(session, slug: str = "demo", root: str | None = None, **kw) -> Project:
    org_id = kw.pop("org_id", None)
    project = Project(
        org_id=builtin_org_id(session) if org_id is None else org_id,
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
    """An empty database migrates to head with no rows - no org either."""
    portal_db.ensure_initialized()
    with portal_db.get_engine().connect() as conn:
        counts = {
            t: conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar_one()
            for t in PORTAL_TABLES
        }
    assert counts == dict.fromkeys(PORTAL_TABLES, 0)


# ---------------------------------------------------------------------------
# The tenancy revision (M2b): backfill, constraints, downgrade
# ---------------------------------------------------------------------------

BASELINE = "86e839432e62"
TENANCY = "4ebfd8b89904"
GITHUB = "b7d2c9a41e63"
PROJECTS = "73bf248ea6fd"
CONNECTIONS = "c4e7a19b52d8"
ACCESS = "d8a31f6c07e5"
HEAD = ACCESS


def _seed_m2a(conn) -> dict[str, int]:  # noqa: ANN001
    """Rows with the M2a baseline's columns: what a dev install holds today."""
    conn.execute(
        text("INSERT INTO settings (id, mode, created_at) VALUES (1, 'local', 'now')")
    )
    user = conn.execute(
        text(
            "INSERT INTO users (uid, display_name, role, created_at) "
            "VALUES ('u-alice', 'Alice', 'owner', 'now') RETURNING id"
        )
    ).scalar_one()
    project = conn.execute(
        text(
            "INSERT INTO projects (slug, name, source, root, created_by, created_at) "
            "VALUES ('api', 'API', 'local', '/repos/api', :u, 'now') RETURNING id"
        ),
        {"u": user},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO project_config (project_id, config, updated_at) VALUES "
            '(NULL, \'{"llm": {"model": "anthropic/a"}}\', \'now\'), '
            "(:p, '{\"analyze\": {\"max_workers\": 2}}', 'now')"
        ),
        {"p": project},
    )
    conn.execute(
        text(
            "INSERT INTO secrets (project_id, kind, provider, ciphertext, hint, "
            "created_at) VALUES "
            "(NULL, 'llm_api_key', 'anthropic', 'c1', '…1111', 'now'), "
            "(:p, 'github_token', NULL, 'c2', '…2222', 'now')"
        ),
        {"p": project},
    )
    return {"user": user, "project": project}


def _columns(table: str) -> set[str]:
    return {c["name"] for c in inspect(portal_db.get_engine()).get_columns(table)}


def test_tenancy_upgrade_backfills_an_m2a_database(
    empty_portal_database: str,
) -> None:
    command.upgrade(portal_db.alembic_config(), BASELINE)
    with portal_db.get_engine().begin() as conn:
        ids = _seed_m2a(conn)
    portal_db._reset_engine()
    command.upgrade(portal_db.alembic_config(), "head")

    with portal_db.get_engine().connect() as conn:
        orgs = conn.execute(text("SELECT id, slug, name, uid FROM organizations")).all()
        assert [(o.slug, o.name) for o in orgs] == [("local", "Local")]
        (org,) = orgs
        assert len(org.uid) == 36
        assert conn.execute(
            text("SELECT builtin_org_id FROM settings")
        ).scalar_one() == (org.id)
        assert conn.execute(
            text("SELECT org_id, user_id, role FROM memberships")
        ).all() == [(org.id, ids["user"], "owner")]
        for table in ("projects", "project_config", "secrets"):
            assert set(
                conn.execute(text(f"SELECT DISTINCT org_id FROM {table}")).scalars()
            ) == {org.id}, table
        assert conn.execute(text("SELECT count(*) FROM project_config")).scalar() == 2
        assert conn.execute(text("SELECT count(*) FROM secrets")).scalar() == 2
        constraints = set(
            conn.execute(
                text(
                    "SELECT conname FROM pg_constraint c JOIN pg_namespace n "
                    "ON n.oid = c.connamespace WHERE n.nspname = 'public'"
                )
            ).scalars()
        )
    assert {
        "uq_projects_org_slug",
        "uq_projects_org_id",
        "fk_projects_org",
        "fk_project_config_org",
        "fk_project_config_project",
        "fk_secrets_org",
        "fk_secrets_project",
        "fk_settings_builtin_org",
        "ck_organizations_slug",
        "ck_memberships_role",
    } <= constraints
    assert "uq_projects_slug" not in constraints
    assert "role" not in _columns("users")
    assert "legacy_import" not in inspect(portal_db.get_engine()).get_table_names()

    # The backfilled rows read back through the org-scoped helpers.
    with portal_db.get_session() as s:
        assert load_layer(s, None, org_id=org.id) == {"llm": {"model": "anthropic/a"}}
        assert load_layer(s, ids["project"], org_id=org.id) == {
            "analyze": {"max_workers": 2}
        }


def test_tenancy_scope_constraints_are_nulls_not_distinct(
    empty_portal_database: str,
) -> None:
    portal_db.ensure_initialized()
    with portal_db.get_engine().connect() as conn:
        defs = dict(
            conn.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE indexname IN "
                    "('uq_project_config_scope', 'uq_secrets_scope')"
                )
            ).all()
        )
    assert "(org_id, project_id) NULLS NOT DISTINCT" in defs["uq_project_config_scope"]
    assert (
        "(org_id, project_id, kind, provider) NULLS NOT DISTINCT"
        in defs["uq_secrets_scope"]
    )


def test_tenancy_downgrade_restores_the_m2a_rows(empty_portal_database: str) -> None:
    command.upgrade(portal_db.alembic_config(), BASELINE)
    with portal_db.get_engine().begin() as conn:
        _seed_m2a(conn)
    command.upgrade(portal_db.alembic_config(), "head")
    portal_db._reset_engine()
    command.downgrade(portal_db.alembic_config(), BASELINE)
    portal_db._reset_engine()

    tables = set(inspect(portal_db.get_engine()).get_table_names())
    assert "legacy_import" in tables
    assert not tables & {"organizations", "memberships"}
    for table in ("projects", "project_config", "secrets", "settings"):
        assert not _columns(table) & {"org_id", "builtin_org_id"}, table
    with portal_db.get_engine().connect() as conn:
        assert conn.execute(text("SELECT role FROM users")).scalars().all() == ["owner"]
        assert conn.execute(text("SELECT slug FROM projects")).scalars().all() == [
            "api"
        ]
        assert conn.execute(text("SELECT count(*) FROM project_config")).scalar() == 2
        assert conn.execute(text("SELECT count(*) FROM secrets")).scalar() == 2
        assert conn.execute(text("SELECT count(*) FROM legacy_import")).scalar() == 0
        uniques = set(
            conn.execute(
                text("SELECT conname FROM pg_constraint WHERE contype = 'u'")
            ).scalars()
        )
    assert "uq_projects_slug" in uniques
    # And back up again: the round trip is clean.
    portal_db._reset_engine()
    command.upgrade(portal_db.alembic_config(), "head")


def test_tenancy_downgrade_refuses_with_two_orgs(empty_portal_database: str) -> None:
    portal_db.ensure_initialized()
    with portal_db.get_session() as s:
        builtin_org_id(s)
        create_org(s, slug="bravo", name="Beta")
    portal_db._reset_engine()
    with pytest.raises(RuntimeError, match="2 organizations"):
        command.downgrade(portal_db.alembic_config(), BASELINE)
    portal_db._reset_engine()
    with portal_db.get_engine().connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar() == (HEAD)
        assert conn.execute(text("SELECT count(*) FROM organizations")).scalar() == 2


# ---------------------------------------------------------------------------
# The identity revision (M2c): users, sessions, password resets
# ---------------------------------------------------------------------------


def test_identity_upgrade_keeps_existing_users(empty_portal_database: str) -> None:
    command.upgrade(portal_db.alembic_config(), TENANCY)
    with portal_db.get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (uid, display_name, username, password_hash, "
                "created_at) VALUES ('u-1', 'Alice', 'alice', 'h', 'then')"
            )
        )
    portal_db._reset_engine()
    command.upgrade(portal_db.alembic_config(), "head")

    assert not _columns("users") & {"username"}
    assert {"email", "is_instance_admin", "password_changed_at"} <= _columns("users")
    with portal_db.get_engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT uid, display_name, password_hash, created_at, email, "
                "is_instance_admin, password_changed_at FROM users"
            )
        ).one()
    assert tuple(row) == ("u-1", "Alice", "h", "then", None, False, None)


def test_identity_upgrade_on_an_empty_database(empty_portal_database: str) -> None:
    command.upgrade(portal_db.alembic_config(), TENANCY)
    portal_db._reset_engine()
    command.upgrade(portal_db.alembic_config(), "head")
    tables = set(inspect(portal_db.get_engine()).get_table_names())
    assert {"sessions", "password_resets"} <= tables
    with portal_db.get_engine().connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar() == (HEAD)


def test_identity_downgrade_in_local_mode(empty_portal_database: str) -> None:
    portal_db.ensure_initialized()
    portal_db._reset_engine()
    command.downgrade(portal_db.alembic_config(), TENANCY)
    portal_db._reset_engine()

    tables = set(inspect(portal_db.get_engine()).get_table_names())
    assert not tables & {"sessions", "password_resets"}
    cols = _columns("users")
    assert "username" in cols
    assert not cols & {"email", "is_instance_admin", "password_changed_at"}
    with portal_db.get_engine().connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar() == (TENANCY)
    # And back up again: the round trip is clean.
    command.upgrade(portal_db.alembic_config(), "head")


def test_identity_downgrade_refuses_in_production(empty_portal_database: str) -> None:
    portal_db.ensure_initialized()
    with portal_db.get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO settings (id, mode, created_at) "
                "VALUES (1, 'production', 'now') "
                "ON CONFLICT (id) DO UPDATE SET mode = 'production'"
            )
        )
    portal_db._reset_engine()
    with pytest.raises(RuntimeError, match="production mode"):
        command.downgrade(portal_db.alembic_config(), TENANCY)
    portal_db._reset_engine()
    with portal_db.get_engine().connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar() == (HEAD)
    assert "email" in _columns("users")


def test_identity_email_constraints(empty_portal_database: str) -> None:
    portal_db.ensure_initialized()
    insert = text(
        "INSERT INTO users (uid, display_name, email, is_instance_admin, "
        "created_at) VALUES (:uid, 'x', :email, false, 'now')"
    )
    with portal_db.get_engine().begin() as conn:
        # Many email-less users fit: the unique constraint treats NULLs as distinct.
        conn.execute(insert, {"uid": "a", "email": None})
        conn.execute(insert, {"uid": "b", "email": None})
        conn.execute(insert, {"uid": "c", "email": "x@example.com"})
    with pytest.raises(IntegrityError, match="uq_users_email"):
        with portal_db.get_engine().begin() as conn:
            conn.execute(insert, {"uid": "d", "email": "x@example.com"})
    with pytest.raises(IntegrityError, match="ck_users_email_lower"):
        with portal_db.get_engine().begin() as conn:
            conn.execute(insert, {"uid": "e", "email": "Y@Example.com"})


def test_github_identity_constraints_and_index(empty_portal_database: str) -> None:
    portal_db.ensure_initialized()
    assert {"github_id", "github_login", "avatar_url", "disabled_at"} <= _columns(
        "users"
    )
    insert = text(
        "INSERT INTO users (uid, display_name, password_hash, github_id, "
        "github_login, is_instance_admin, created_at) "
        "VALUES (:uid, 'x', :hash, :gid, :login, false, 'now')"
    )

    def add(uid: str, *, hash=None, gid=None, login=None) -> None:  # noqa: ANN001
        with portal_db.get_engine().begin() as conn:
            conn.execute(insert, {"uid": uid, "hash": hash, "gid": gid, "login": login})

    add("a", gid=1, login="Ben")
    add("b")  # a user with neither credential (local mode's) still fits
    add("c", hash="h")
    with pytest.raises(IntegrityError, match="uq_users_github_id"):
        add("d", gid=1)
    with pytest.raises(IntegrityError, match="uq_users_github_login_lower"):
        add("e", gid=2, login="bEN")
    with pytest.raises(IntegrityError, match="ck_users_one_credential"):
        add("f", hash="h", gid=3)
    with pytest.raises(IntegrityError, match="ck_users_login_needs_id"):
        add("g", login="orphan")


# ---------------------------------------------------------------------------
# The github projects revision (M2d-2): repo ids, retired slugs, triggers
# ---------------------------------------------------------------------------

_INSERT_PROJECT = text(
    "INSERT INTO projects (org_id, slug, name, source, root, github_repo_id, "
    "created_at, restricted) VALUES (:org, :slug, :slug, 'github', :root, :repo, "
    "'now', false)"
)


def _insert_run(conn, project: int, trigger: str) -> None:  # noqa: ANN001
    conn.execute(
        text(
            'INSERT INTO scan_runs (project_id, kind, trigger, "analyze", status) '
            "VALUES (:p, 'sync', :t, false, 'queued')"
        ),
        {"p": project, "t": trigger},
    )


def test_github_projects_columns_index_and_triggers(
    empty_portal_database: str,
) -> None:
    portal_db.ensure_initialized()
    assert {
        "github_repo_id",
        "github_installation_id",
        "default_branch",
        "access_lost_at",
        "access_lost_reason",
    } <= _columns("projects")
    with portal_db.get_session() as s:
        one = builtin_org_id(s)
        two = create_org(s, slug="bravo", name="Bravo").id
        s.commit()
    big = 2**31 + 5  # repository ids pass 2^31 (spike #9): BIGINT
    with portal_db.get_engine().begin() as conn:
        conn.execute(
            _INSERT_PROJECT, {"org": one, "slug": "a", "root": "r/a", "repo": big}
        )
        # The same repo in another org fits (the index is per org) ...
        conn.execute(
            _INSERT_PROJECT, {"org": two, "slug": "a", "root": "r/b", "repo": big}
        )
        # ... and many repo-less projects in one org.
        conn.execute(
            _INSERT_PROJECT, {"org": one, "slug": "b", "root": "r/c", "repo": None}
        )
        conn.execute(
            _INSERT_PROJECT, {"org": one, "slug": "c", "root": "r/d", "repo": None}
        )
        project = conn.execute(
            text("SELECT id FROM projects WHERE root = 'r/a'")
        ).scalar()
        assert (
            conn.execute(
                text("SELECT github_repo_id FROM projects WHERE id = :p"),
                {"p": project},
            ).scalar()
            == big
        )
        for trigger in ("push", "reconcile"):
            _insert_run(conn, project, trigger)
        defs = dict(
            conn.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE indexname IN "
                    "('uq_projects_org_github_repo', 'ix_projects_github_repo_id')"
                )
            ).all()
        )
    assert "WHERE (github_repo_id IS NOT NULL)" in defs["uq_projects_org_github_repo"]
    assert "UNIQUE" in defs["uq_projects_org_github_repo"]
    assert "UNIQUE" not in defs["ix_projects_github_repo_id"]
    with pytest.raises(IntegrityError, match="uq_projects_org_github_repo"):
        with portal_db.get_engine().begin() as conn:
            conn.execute(
                _INSERT_PROJECT, {"org": one, "slug": "d", "root": "r/e", "repo": big}
            )
    with pytest.raises(IntegrityError, match="ck_scan_runs_trigger"):
        with portal_db.get_engine().begin() as conn:
            _insert_run(conn, project, "webhook")


def test_retired_org_slugs_table(empty_portal_database: str) -> None:
    from whygraph.portal.models import RetiredOrgSlug

    portal_db.ensure_initialized()
    with portal_db.get_session() as s:
        s.add(RetiredOrgSlug(slug="acme"))
        s.commit()
        (row,) = s.exec(select(RetiredOrgSlug)).all()
        assert row.slug == "acme" and row.retired_at
    with pytest.raises(IntegrityError):
        with portal_db.get_session() as s:
            s.add(RetiredOrgSlug(slug="acme"))
            s.commit()


def test_github_projects_upgrade_keeps_existing_projects(
    empty_portal_database: str,
) -> None:
    portal_db.ensure_initialized()
    with portal_db.get_session() as s:
        org = builtin_org_id(s)
        s.commit()
    portal_db._reset_engine()
    command.downgrade(portal_db.alembic_config(), GITHUB)
    with portal_db.get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO projects (org_id, slug, name, source, root, created_at) "
                "VALUES (:org, 'api', 'API', 'github', 'repos/api', 'now')"
            ),
            {"org": org},
        )
    portal_db._reset_engine()
    command.upgrade(portal_db.alembic_config(), "head")
    with portal_db.get_engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT slug, github_repo_id, github_installation_id, default_branch, "
                "access_lost_at, access_lost_reason FROM projects"
            )
        ).one()
    assert tuple(row) == ("api", None, None, None, None, None)


def test_github_projects_downgrade_round_trip(empty_portal_database: str) -> None:
    portal_db.ensure_initialized()
    portal_db._reset_engine()
    command.downgrade(portal_db.alembic_config(), GITHUB)
    portal_db._reset_engine()
    assert "retired_org_slugs" not in inspect(portal_db.get_engine()).get_table_names()
    assert not _columns("projects") & {"github_repo_id", "access_lost_at"}
    with portal_db.get_engine().connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar() == (GITHUB)
        check = conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_scan_runs_trigger'"
            )
        ).scalar_one()
    assert "push" not in check and "poll" in check
    # And back up again: the round trip is clean.
    command.upgrade(portal_db.alembic_config(), "head")
    portal_db._reset_engine()
    assert "retired_org_slugs" in inspect(portal_db.get_engine()).get_table_names()


@pytest.mark.parametrize(
    "seed, match",
    [
        (
            "INSERT INTO retired_org_slugs (slug, retired_at) VALUES ('x', 'now')",
            "slug",
        ),
        (
            "INSERT INTO projects (org_id, slug, name, source, root, github_repo_id, "
            "created_at, restricted) VALUES (:org, 'gh', 'gh', 'github', "
            "'repos/gh', 7, 'now', false)",
            "imported GitHub projects",
        ),
        (
            'INSERT INTO scan_runs (project_id, kind, trigger, "analyze", status) '
            "VALUES (:project, 'sync', 'push', false, 'ok')",
            "push / reconcile",
        ),
    ],
)
def test_github_projects_downgrade_refuses_m2d2_data(
    empty_portal_database: str, seed: str, match: str
) -> None:
    portal_db.ensure_initialized()
    with portal_db.get_session() as s:
        org = builtin_org_id(s)
        s.commit()
    with portal_db.get_engine().begin() as conn:
        project = conn.execute(
            text(
                "INSERT INTO projects (org_id, slug, name, source, root, created_at, restricted) "
                "VALUES (:org, 'api', 'API', 'local', '/r/api', 'now', false) RETURNING id"
            ),
            {"org": org},
        ).scalar_one()
        conn.execute(text(seed), {"org": org, "project": project})
    portal_db._reset_engine()
    with pytest.raises(RuntimeError, match=match):
        command.downgrade(portal_db.alembic_config(), GITHUB)
    portal_db._reset_engine()
    with portal_db.get_engine().connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar() == (HEAD)


@pytest.mark.parametrize(
    "seed",
    [
        "UPDATE projects SET restricted = true",
        "UPDATE organizations SET default_project_role = 'viewer'",
        "INSERT INTO invitations (uid, org_id, github_id, github_login, role, "
        "created_at, expires_at) VALUES ('i-1', :org, 1, 'octo', 'member', "
        "'now', 'later')",
    ],
)
def test_access_downgrade_refuses_m2f1_data(
    empty_portal_database: str, seed: str
) -> None:
    portal_db.ensure_initialized()
    with portal_db.get_session() as s:
        org = builtin_org_id(s)
        s.commit()
    with portal_db.get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO projects (org_id, slug, name, source, root, created_at, restricted) "
                "VALUES (:org, 'api', 'API', 'local', '/r/api', 'now', false)"
            ),
            {"org": org},
        )
        conn.execute(text(seed), {"org": org})
    portal_db._reset_engine()
    with pytest.raises(RuntimeError, match="access revision"):
        command.downgrade(portal_db.alembic_config(), CONNECTIONS)
    portal_db._reset_engine()
    with portal_db.get_engine().connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar() == (HEAD)


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


def test_ensure_initialized_returns_none(data: Path) -> None:
    assert portal_db.ensure_initialized() is None


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


def test_saving_global_config_twice_leaves_one_row(org: int) -> None:
    with portal_db.get_session() as s:
        save_layer(s, None, {"llm": {"model": "anthropic/a"}}, org_id=org)
    with portal_db.get_session() as s:
        save_layer(s, None, {"llm": {"model": "anthropic/b"}}, org_id=org)
    with portal_db.get_session() as s:
        rows = s.exec(
            select(ProjectConfig).where(ProjectConfig.project_id.is_(None))
        ).all()
        assert len(rows) == 1
        assert rows[0].config == {"llm": {"model": "anthropic/b"}}
        assert load_layer(s, None, org_id=org) == {"llm": {"model": "anthropic/b"}}


def test_saving_project_config_twice_leaves_one_row(org: int) -> None:
    with portal_db.get_session() as s:
        p = _project(s)
        save_layer(s, p.id, {"analyze": {"max_workers": 2}}, org_id=org)
        save_layer(s, p.id, {"analyze": {"max_workers": 3}}, org_id=org)
    with portal_db.get_session() as s:
        assert len(s.exec(select(ProjectConfig)).all()) == 1


def test_duplicate_global_rows_are_rejected_by_the_index(org: int) -> None:
    with pytest.raises(IntegrityError, match="uq_project_config_scope"):
        with portal_db.get_session() as s:
            s.add(ProjectConfig(org_id=org, project_id=None, config={}))
            s.flush()
            s.add(ProjectConfig(org_id=org, project_id=None, config={"a": 1}))
            s.flush()


def test_saving_a_global_secret_twice_leaves_one_row(org: int) -> None:
    for value in ("sk-first-1111", "sk-second-2222"):
        with portal_db.get_session() as s:
            pw_secrets.put_secret(
                s, kind="llm_api_key", provider="anthropic", value=value, org_id=org
            )
    with portal_db.get_session() as s:
        assert len(s.exec(select(Secret)).all()) == 1
        assert (
            pw_secrets.read_secret(
                s, kind="llm_api_key", provider="anthropic", org_id=org
            )
            == "sk-second-2222"
        )


def test_github_token_uniqueness_holds_despite_null_provider(org: int) -> None:
    """A NULL provider must not let duplicate GitHub tokens slip past UNIQUE."""
    with pytest.raises(IntegrityError, match="uq_secrets_scope"):
        with portal_db.get_session() as s:
            for _ in range(2):
                s.add(Secret(org_id=org, kind="github_token", ciphertext="x", hint="x"))
                s.flush()


def test_project_secret_uniqueness_holds_despite_null_provider(org: int) -> None:
    with portal_db.get_session() as s:
        p = _project(s)
        pid = p.id
    with pytest.raises(IntegrityError, match="uq_secrets_scope"):
        with portal_db.get_session() as s:
            for _ in range(2):
                s.add(
                    Secret(
                        org_id=org,
                        project_id=pid,
                        kind="github_token",
                        ciphertext="x",
                        hint="x",
                    )
                )
                s.flush()


@pytest.mark.parametrize(
    ("kind", "provider", "constraint"),
    [
        ("nonsense", None, "ck_secrets_kind"),
        ("llm_api_key", None, "ck_secrets_provider"),
        ("github_token", "anthropic", "ck_secrets_provider"),
    ],
)
def test_secret_kind_and_provider_are_checked(
    org: int, kind: str, provider: str | None, constraint: str
) -> None:
    bad = Secret(org_id=org, kind=kind, provider=provider, ciphertext="x", hint="x")
    with pytest.raises(IntegrityError, match=constraint):
        with portal_db.get_session() as s:
            s.add(bad)
            s.flush()


def test_deleting_a_project_cascades(org: int) -> None:
    with portal_db.get_session() as s:
        p = _project(s)
        other = _project(s, "other")
        pid, oid = p.id, other.id
        save_layer(s, pid, {"analyze": {"max_workers": 2}}, org_id=org)
        save_layer(s, oid, {"analyze": {"max_workers": 4}}, org_id=org)
        pw_secrets.put_secret(
            s,
            kind="llm_api_key",
            provider="openai",
            value="sk-abcd",
            project_id=pid,
            org_id=org,
        )
        pw_secrets.put_secret(
            s, kind="github_token", value="ghp_xxxx", project_id=oid, org_id=org
        )
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
        assert a.email is None and a.password_hash is None


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
    with pytest.raises(IntegrityError, match="uq_projects_org_slug"):
        with portal_db.get_session() as s:
            _project(s, "a", root="/r/other")
    with pytest.raises(IntegrityError, match="uq_projects_root"):
        with portal_db.get_session() as s:
            _project(s, "b", root="/r/a")


def test_m2_columns_exist(data: Path) -> None:
    insp = inspect(portal_db.get_engine())
    cols = {
        t: {c["name"] for c in insp.get_columns(t)}
        for t in ("users", "projects", "scan_runs")
    }
    assert {"uid", "email", "password_hash"} <= cols["users"]
    assert "username" not in cols["users"]  # replaced by email (M2c)
    assert "role" not in cols["users"]  # per org, in memberships (M2b)
    assert {"created_by", "last_scanned_head", "initialized_at"} <= cols["projects"]
    assert {"requested_by", "analyze", "trigger", "kind"} <= cols["scan_runs"]


# ---------------------------------------------------------------------------
# Organizations: slugs, the built-in org, memberships, cross-org constraints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", ["Bad", "-x", "x-", "a b", "api", "www", "a" * 41])
def test_org_slug_is_validated_on_insert(data: Path, slug: str) -> None:
    with pytest.raises(ValueError):
        with portal_db.get_session() as s:
            s.add(Organization(slug=slug, name="x"))
            s.flush()


def test_org_slug_format_is_checked_in_the_database(data: Path) -> None:
    with pytest.raises(IntegrityError, match="ck_organizations_slug"):
        with portal_db.get_engine().begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO organizations (uid, slug, name, created_at, "
                    "default_project_role) VALUES ('u1', 'Bad Slug', 'x', 'now', "
                    "'contributor')"
                )
            )


def test_org_slug_is_immutable_and_the_builtin_org_is_kept(org: int) -> None:
    with portal_db.get_session() as s:
        s.get(Organization, org).name = "Renamed"  # the name is renamable
    with pytest.raises(ValueError, match="immutable"):
        with portal_db.get_session() as s:
            s.get(Organization, org).slug = "changed"
            s.flush()
    with pytest.raises(IntegrityError, match="fk_settings_builtin_org"):
        with portal_db.get_session() as s:
            s.delete(s.get(Organization, org))
            s.flush()
    with portal_db.get_session() as s:
        row = s.get(Organization, org)
        assert (row.slug, row.name) == ("local", "Renamed")


def test_project_org_is_immutable(org: int) -> None:
    with portal_db.get_session() as s:
        beta = create_org(s, slug="bravo", name="Beta").id
        pid = _project(s).id
    with pytest.raises(ValueError, match="organization is immutable"):
        with portal_db.get_session() as s:
            s.get(Project, pid).org_id = beta
            s.flush()
    with portal_db.get_session() as s:
        _project(s, "x", org_id=beta)
    # Removing a project has effects outside the DB, so an org never cascades.
    with pytest.raises(IntegrityError, match="fk_projects_org"):
        with portal_db.get_session() as s:
            s.delete(s.get(Organization, beta))
            s.flush()


def test_ensure_builtin_org_is_idempotent(data: Path) -> None:
    with portal_db.get_session() as s:
        setting = Setting(id=1, mode="local")
        s.add(setting)  # only added, not flushed: what the portal's start passes
        first = ensure_builtin_org(s, setting)
        assert (first.slug, first.name) == ("local", "Local")
        assert setting.builtin_org_id == first.id
        first_id = first.id
    with portal_db.get_session() as s:
        assert ensure_builtin_org(s, s.get(Setting, 1)).id == first_id
        s.get(Setting, 1).builtin_org_id = None  # a lost link is repaired
    with portal_db.get_session() as s:
        assert ensure_builtin_org(s, s.get(Setting, 1)).id == first_id
    with portal_db.get_session() as s:
        assert s.get(Setting, 1).builtin_org_id == first_id
        assert len(s.exec(select(Organization)).all()) == 1


def test_memberships_check_the_role_and_follow_the_user(org: int) -> None:
    with portal_db.get_session() as s:
        user = User(display_name="Ada")
        s.add(user)
        s.flush()
        uid = user.id
        member = add_member(s, org_id=org, user_id=uid, role="admin")
        assert member.role == "admin"
        with pytest.raises(ValueError):
            add_member(s, org_id=org, user_id=uid, role="boss")
    with pytest.raises(IntegrityError, match="ck_memberships_role"):
        with portal_db.get_engine().begin() as conn:
            conn.execute(text("UPDATE memberships SET role = 'boss'"))
    with pytest.raises(IntegrityError, match="pk_memberships"):
        with portal_db.get_session() as s:
            add_member(s, org_id=org, user_id=uid, role="member")
    with portal_db.get_session() as s:
        s.delete(s.get(User, uid))
    with portal_db.get_session() as s:
        assert s.exec(select(Membership)).all() == []


def test_project_rows_must_share_their_projects_org(org: int) -> None:
    with portal_db.get_session() as s:
        beta = create_org(s, slug="bravo", name="Beta").id
        pid = _project(s).id
    with pytest.raises(IntegrityError, match="fk_secrets_project"):
        with portal_db.get_session() as s:
            s.add(
                Secret(
                    org_id=beta,
                    project_id=pid,
                    kind="github_token",
                    ciphertext="x",
                    hint="x",
                )
            )
            s.flush()
    with pytest.raises(IntegrityError, match="fk_project_config_project"):
        with portal_db.get_session() as s:
            s.add(ProjectConfig(org_id=beta, project_id=pid, config={}))
            s.flush()


def test_orgs_hold_the_same_slug_and_their_own_defaults(org: int) -> None:
    with portal_db.get_session() as s:
        beta = create_org(s, slug="bravo", name="Beta").id
        _project(s, "api", root="/r/a")
        _project(s, "api", root="/r/b", org_id=beta)
        save_layer(s, None, {"llm": {"model": "anthropic/a"}}, org_id=org)
        save_layer(s, None, {"llm": {"model": "anthropic/b"}}, org_id=beta)
        assert unique_slug(s, "api", org_id=beta) == "api-2"
        assert unique_slug(s, "web", org_id=beta) == "web"
    with portal_db.get_session() as s:
        assert load_layer(s, None, org_id=org) == {"llm": {"model": "anthropic/a"}}
        assert load_layer(s, None, org_id=beta) == {"llm": {"model": "anthropic/b"}}
        assert len(s.exec(select(ProjectConfig)).all()) == 2


def test_context_and_endpoint_clearing_stay_within_the_org(org: int) -> None:
    with portal_db.get_session() as s:
        beta = create_org(s, slug="bravo", name="Beta").id
        mine, theirs = _project(s, "a"), _project(s, "b", org_id=beta)
        for org_id, value in ((org, "sk-local-1111"), (beta, "sk-beta-2222")):
            save_layer(
                s,
                None,
                {"llm": {"openai": {"base_url": "https://a/v1"}}},
                org_id=org_id,
            )
            pw_secrets.put_secret(
                s, kind="llm_api_key", provider="openai", value=value, org_id=org_id
            )
        pw_secrets.put_secret(
            s,
            kind="llm_api_key",
            provider="openai",
            value="sk-beta-proj",
            project_id=theirs.id,
            org_id=beta,
        )
        assert _build(s, mine).config.llm.openai.api_key == "sk-local-1111"
        assert _build(s, theirs).config.llm.openai.api_key == "sk-beta-proj"
        # local's endpoint change clears only local's keys (rule 3, per org)
        cleared = save_layer(
            s, None, {"llm": {"openai": {"base_url": "https://b/v1"}}}, org_id=org
        )
        assert cleared == []
        assert pw_secrets.secret_status(
            s, kind="llm_api_key", provider="openai", org_id=beta
        )["set"]
        assert pw_secrets.secret_status(
            s,
            kind="llm_api_key",
            provider="openai",
            project_id=theirs.id,
            org_id=beta,
        )["set"]
        assert _build(s, mine).config.llm.openai.api_key is None


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


def test_unique_slug_suffixes_on_collision(org: int) -> None:
    with portal_db.get_session() as s:
        assert unique_slug(s, "Whygraph", org_id=org) == "whygraph"
        _project(s, "whygraph")
        assert unique_slug(s, "Whygraph", org_id=org) == "whygraph-2"
        _project(s, "whygraph-2")
        assert unique_slug(s, "whygraph", org_id=org) == "whygraph-3"
        long = "x" * 63
        _project(s, long)
        assert is_valid_slug(unique_slug(s, long, org_id=org))


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


def test_secrets_round_trip_and_ciphertext_differs_per_write(org: int) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="anthropic", value="sk-ant-a1b2", org_id=org
        )
        first = s.exec(select(Secret)).one().ciphertext
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="anthropic", value="sk-ant-a1b2", org_id=org
        )
        second = s.exec(select(Secret)).one().ciphertext
        assert (
            pw_secrets.read_secret(
                s, kind="llm_api_key", provider="anthropic", org_id=org
            )
            == "sk-ant-a1b2"
        )
    assert first != second
    with portal_db.get_engine().connect() as raw:
        stored = raw.execute(text("SELECT ciphertext, hint FROM secrets")).one()
    assert "sk-ant" not in stored[0] and "sk-ant" not in stored[1]


def test_secret_status_exposes_only_set_and_hint(org: int) -> None:
    with portal_db.get_session() as s:
        assert pw_secrets.secret_status(s, kind="github_token", org_id=org) == {
            "set": False,
            "hint": None,
        }
        pw_secrets.put_secret(
            s, kind="github_token", value="ghp_abcdefa1b2", org_id=org
        )
        status = pw_secrets.secret_status(s, kind="github_token", org_id=org)
    assert status == {"set": True, "hint": "…a1b2"}
    assert "ghp_" not in repr(status)


def test_unreadable_secret_is_flagged_not_fatal(org: int) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="github_token", value="ghp_abcdefa1b2", org_id=org
        )
        s.exec(select(Secret)).one().ciphertext = "not-a-fernet-token"
    with portal_db.get_session() as s:
        status = pw_secrets.secret_status(s, kind="github_token", org_id=org)
    assert status == {"set": True, "hint": "…a1b2", "unreadable": True}


def test_secret_store_validates_scope(org: int) -> None:
    with portal_db.get_session() as s:
        for kw in (
            dict(kind="llm_api_key", provider="ollama", value="x"),
            dict(kind="llm_api_key", provider=None, value="x"),
            dict(kind="github_token", provider="anthropic", value="x"),
            dict(kind="weird", value="x"),
            dict(kind="github_token", value="   "),
        ):
            with pytest.raises(ValueError):
                pw_secrets.put_secret(s, **kw, org_id=org)


def test_delete_secret(org: int) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(s, kind="github_token", value="ghp_1234", org_id=org)
        assert pw_secrets.delete_secret(s, kind="github_token", org_id=org) is True
        assert pw_secrets.delete_secret(s, kind="github_token", org_id=org) is False


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


def test_secrets_in_config_dict_are_rejected(org: int) -> None:
    bad = {"llm": {"anthropic": {"api_key": "sk-x"}}, "scan": {"token": "ghp_x"}}
    assert find_secret_paths(bad) == ["llm.anthropic.api_key", "scan.token"]
    with portal_db.get_session() as s:
        with pytest.raises(ConfigPolicyError) as exc:
            save_layer(s, None, bad, org_id=org)
        assert "sk-x" not in str(exc.value) and "ghp_x" not in str(exc.value)
        assert s.exec(select(ProjectConfig)).all() == []


def test_changing_an_endpoint_clears_that_scopes_key_only(org: int) -> None:
    with portal_db.get_session() as s:
        pid = _project(s).id
        save_layer(
            s,
            None,
            {"llm": {"openai": {"base_url": "https://a.example/v1"}}},
            org_id=org,
        )
        save_layer(s, pid, {}, org_id=org)
        for scope in (None, pid):
            pw_secrets.put_secret(
                s,
                kind="llm_api_key",
                provider="openai",
                value="sk-1111",
                project_id=scope,
                org_id=org,
            )
            pw_secrets.put_secret(
                s,
                kind="llm_api_key",
                provider="anthropic",
                value="sk-2222",
                project_id=scope,
                org_id=org,
            )
        # unrelated edit: keys survive
        save_layer(
            s,
            None,
            {
                "llm": {"openai": {"base_url": "https://a.example/v1"}},
                "analyze": {"max_workers": 2},
            },
            org_id=org,
        )
        assert pw_secrets.secret_status(
            s, kind="llm_api_key", provider="openai", org_id=org
        )["set"]
        # global endpoint edit clears the global openai key, and the openai
        # key of every project that inherits the endpoint (security fix 1,
        # finding 6) - never another provider's
        cleared = save_layer(
            s,
            None,
            {"llm": {"openai": {"base_url": "https://evil.example/v1"}}},
            org_id=org,
        )
        assert cleared == [(pid, "openai")]
        assert not pw_secrets.secret_status(
            s, kind="llm_api_key", provider="openai", org_id=org
        )["set"]
        assert pw_secrets.secret_status(
            s, kind="llm_api_key", provider="anthropic", org_id=org
        )["set"]
        assert not pw_secrets.secret_status(
            s, kind="llm_api_key", provider="openai", project_id=pid, org_id=org
        )["set"]
        pw_secrets.put_secret(
            s,
            kind="llm_api_key",
            provider="openai",
            value="sk-3333",
            project_id=pid,
            org_id=org,
        )
        # a project-scope endpoint edit clears that project's key
        save_layer(
            s,
            pid,
            {"llm": {"openai": {"base_url": "https://p.example/v1"}}},
            org_id=org,
        )
        assert not pw_secrets.secret_status(
            s, kind="llm_api_key", provider="openai", project_id=pid, org_id=org
        )["set"]
        assert pw_secrets.secret_status(
            s, kind="llm_api_key", provider="anthropic", project_id=pid, org_id=org
        )["set"]


def test_removing_an_endpoint_also_clears_the_key(org: int) -> None:
    with portal_db.get_session() as s:
        save_layer(s, None, {"llm": {"ollama": {"host": "http://h:1"}}}, org_id=org)
        save_layer(
            s, None, {"llm": {"openai": {"base_url": "https://a/v1"}}}, org_id=org
        )
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="openai", value="sk-1111", org_id=org
        )
        save_layer(s, None, {}, org_id=org)
        assert not pw_secrets.secret_status(
            s, kind="llm_api_key", provider="openai", org_id=org
        )["set"]


# ---------------------------------------------------------------------------
# Building a ProjectContext
# ---------------------------------------------------------------------------


def _build(session, project: Project) -> ProjectContext:
    return build_project_context(session, project)


def test_context_merges_layers_project_wins(org: int) -> None:
    with portal_db.get_session() as s:
        save_layer(
            s,
            None,
            {
                "llm": {"model": "anthropic/claude-x"},
                "analyze": {"max_workers": 2},
                "scan": {"max_workers": 9},  # 1.x alias, normalized per layer
            },
            org_id=org,
        )
        p = _project(s)
        save_layer(
            s,
            p.id,
            {"analyze": {"max_workers": 6}, "scan": {"forge": "github"}},
            org_id=org,
        )
        ctx = _build(s, p)
    assert ctx.slug == "demo" and ctx.root == Path("/repos/demo")
    assert ctx.config.model_for("analyze") == ("anthropic", "claude-x")
    assert ctx.config.analyze.max_workers == 6
    assert ctx.config.scan_forge == "github"


def test_context_null_resets_a_key(org: int) -> None:
    with portal_db.get_session() as s:
        save_layer(s, None, {"analyze": {"max_workers": 2}}, org_id=org)
        p = _project(s)
        save_layer(s, p.id, {"analyze": {"max_workers": None}}, org_id=org)
        assert _build(s, p).config.analyze.max_workers == Config().analyze.max_workers


def test_db_paths_in_a_project_row_are_ignored(
    data: Path, org: int, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    with portal_db.get_session() as s:
        save_layer(
            s,
            None,
            {"whygraph_db": "/etc/global.db", "codegraph_db": str(data / "portal.db")},
            org_id=org,
        )
        p = _project(s, root=str(root))
        save_layer(
            s,
            p.id,
            {
                "whygraph_db": str(tmp_path / "other" / "whygraph.db"),
                "codegraph_db": "../x.db",
            },
            org_id=org,
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


def test_context_injects_secrets_in_memory_only(org: int) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s,
            kind="llm_api_key",
            provider="anthropic",
            value="sk-glob-1111",
            org_id=org,
        )
        pw_secrets.put_secret(
            s,
            kind="llm_api_key",
            provider="claude-cli",
            value="sk-cli-2222",
            org_id=org,
        )
        pw_secrets.put_secret(s, kind="github_token", value="ghp_glob3333", org_id=org)
        p = _project(s)
        ctx = _build(s, p)
        stored = [c.config for c in s.exec(select(ProjectConfig)).all()]
    assert ctx.config.llm.anthropic.api_key == "sk-glob-1111"
    assert ctx.config.llm.claude_cli.api_key == "sk-cli-2222"
    assert ctx.config.scan_token == "ghp_glob3333"
    assert stored == []
    for secret in ("sk-glob-1111", "sk-cli-2222", "ghp_glob3333"):
        assert secret not in repr(ctx.config) and secret not in repr(ctx)


def test_project_secret_overrides_global_for_that_project_only(org: int) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s,
            kind="llm_api_key",
            provider="anthropic",
            value="sk-glob-0000",
            org_id=org,
        )
        pw_secrets.put_secret(s, kind="github_token", value="ghp_glob0000", org_id=org)
        a, b = _project(s, "a"), _project(s, "b")
        pw_secrets.put_secret(
            s,
            kind="llm_api_key",
            provider="anthropic",
            value="sk-proj-a111",
            project_id=a.id,
            org_id=org,
        )
        pw_secrets.put_secret(
            s, kind="github_token", value="ghp_proj-a11", project_id=a.id, org_id=org
        )
        ca, cb = _build(s, a), _build(s, b)
    assert ca.config.llm.anthropic.api_key == "sk-proj-a111"
    assert ca.config.scan_token == "ghp_proj-a11"
    assert cb.config.llm.anthropic.api_key == "sk-glob-0000"
    assert cb.config.scan_token == "ghp_glob0000"


def test_project_overriding_base_url_gets_no_global_key(org: int) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="openai", value="sk-glob-1111", org_id=org
        )
        pw_secrets.put_secret(
            s,
            kind="llm_api_key",
            provider="anthropic",
            value="sk-glob-2222",
            org_id=org,
        )
        plain, overriding = _project(s, "plain"), _project(s, "over")
        save_layer(
            s,
            overriding.id,
            {"llm": {"openai": {"base_url": "https://attacker.example/v1"}}},
            org_id=org,
        )
        c_plain, c_over = _build(s, plain), _build(s, overriding)
    assert c_plain.config.llm.openai.api_key == "sk-glob-1111"
    assert c_over.config.llm.openai.base_url == "https://attacker.example/v1"
    assert c_over.config.llm.openai.api_key is None  # never sent to the override
    assert (
        c_over.config.llm.anthropic.api_key == "sk-glob-2222"
    )  # other providers unaffected


def test_project_own_key_is_kept_with_its_own_endpoint(org: int) -> None:
    with portal_db.get_session() as s:
        p = _project(s)
        save_layer(
            s,
            p.id,
            {"llm": {"openai": {"base_url": "https://gw.example/v1"}}},
            org_id=org,
        )
        pw_secrets.put_secret(
            s,
            kind="llm_api_key",
            provider="openai",
            value="sk-mine-1111",
            project_id=p.id,
            org_id=org,
        )
        assert _build(s, p).config.llm.openai.api_key == "sk-mine-1111"


def test_null_endpoint_is_not_an_override(org: int) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="llm_api_key", provider="openai", value="sk-glob-1111", org_id=org
        )
        p = _project(s)
        save_layer(s, p.id, {"llm": {"openai": {"base_url": None}}}, org_id=org)
        assert _build(s, p).config.llm.openai.api_key == "sk-glob-1111"


def test_unreadable_secret_does_not_fail_the_build_or_fall_back(org: int) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s,
            kind="llm_api_key",
            provider="anthropic",
            value="sk-glob-0000",
            org_id=org,
        )
        p = _project(s)
        pw_secrets.put_secret(
            s,
            kind="llm_api_key",
            provider="anthropic",
            value="sk-proj-1111",
            project_id=p.id,
            org_id=org,
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


def test_cache_builds_once_and_rebuilds_after_invalidate(org: int) -> None:
    cache = ContextCache()
    with portal_db.get_session() as s:
        pid = _project(s).id
        save_layer(s, pid, {"analyze": {"max_workers": 2}}, org_id=org)
    first = cache.get(pid)
    assert cache.get(pid) is first
    with portal_db.get_session() as s:
        save_layer(s, pid, {"analyze": {"max_workers": 5}}, org_id=org)
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
    org: int,
) -> None:
    with portal_db.get_session() as s:
        pw_secrets.put_secret(
            s, kind="claude_oauth_token", value="sk-ant-oat-glob1", org_id=org
        )
        a, b = _project(s, "a"), _project(s, "b")
        pw_secrets.put_secret(
            s,
            kind="claude_oauth_token",
            value="sk-ant-oat-proja",
            project_id=a.id,
            org_id=org,
        )
        ca, cb = _build(s, a), _build(s, b)
        with pytest.raises(ValueError, match="has no provider"):
            pw_secrets.put_secret(
                s,
                kind="claude_oauth_token",
                provider="claude-cli",
                value="x",
                org_id=org,
            )
        stored = [c.config for c in s.exec(select(ProjectConfig)).all()]
    assert ca.config.llm.claude_cli.oauth_token == "sk-ant-oat-proja"
    assert cb.config.llm.claude_cli.oauth_token == "sk-ant-oat-glob1"
    assert stored == []  # a secret never lands in a config layer
    for secret in ("sk-ant-oat-glob1", "sk-ant-oat-proja"):
        assert secret not in repr(ca.config) and secret not in repr(cb)
