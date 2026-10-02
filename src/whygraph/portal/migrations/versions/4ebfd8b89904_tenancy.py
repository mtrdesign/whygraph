"""tenancy

Revision ID: 4ebfd8b89904
Revises: 86e839432e62
Create Date: 2026-10-02 11:56:46

M2b: organizations and memberships, and an ``org_id`` on projects, config
layers and secrets. Project slugs become unique per org, and composite
foreign keys ``(org_id, project_id) -> projects (org_id, id)`` keep a
project's config and secrets in its own org. ``users.role`` (the role now
lives in ``memberships``) and the SQLite importer's ``legacy_import`` go.

A database that already holds a ``settings`` row (a portal that started at
least once; M2a only allowed ``local``) is backfilled into one built-in
org, ``local``, linked from ``settings.builtin_org_id``, with every user as
its owner. An empty database gets its org at the portal's first start.
Everything runs in one transaction, so a failed backfill leaves the M2a
schema untouched.

``downgrade()`` refuses when more than one org exists: the old global
``uq_projects_slug`` cannot hold two orgs' projects of the same slug.
"""

from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "4ebfd8b89904"
down_revision: Union[str, Sequence[str], None] = "86e839432e62"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ORG_TABLES = ("projects", "project_config", "secrets")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upgrade() -> None:
    """Upgrade schema."""
    # 1. The new tables and the built-in org marker.
    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uid", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$'",
            name="ck_organizations_slug",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_organizations_slug"),
        sa.UniqueConstraint("uid", name="uq_organizations_uid"),
    )
    op.create_table(
        "memberships",
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'member')", name="ck_memberships_role"
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_memberships_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_memberships_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("org_id", "user_id", name="pk_memberships"),
    )
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"], unique=False)
    op.add_column("settings", sa.Column("builtin_org_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_settings_builtin_org",
        "settings",
        "organizations",
        ["builtin_org_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # 2. org_id, nullable until the backfill ran.
    for table in _ORG_TABLES:
        op.add_column(table, sa.Column("org_id", sa.Integer(), nullable=True))

    # 3. Backfill the built-in org, only for a portal that has started.
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM settings WHERE id = 1")).first():
        now = _now()
        org_id = bind.execute(
            sa.text(
                "INSERT INTO organizations (uid, slug, name, created_at) "
                "VALUES (gen_random_uuid()::text, 'local', 'Local', :now) "
                "RETURNING id"
            ),
            {"now": now},
        ).scalar_one()
        bind.execute(
            sa.text("UPDATE settings SET builtin_org_id = :org WHERE id = 1"),
            {"org": org_id},
        )
        bind.execute(
            sa.text(
                "INSERT INTO memberships (org_id, user_id, role, created_at) "
                "SELECT :org, id, 'owner', :now FROM users"
            ),
            {"org": org_id, "now": now},
        )
        for table in _ORG_TABLES:
            bind.execute(sa.text(f"UPDATE {table} SET org_id = :org"), {"org": org_id})

    # 4. NOT NULL, and each row's org.
    for table in _ORG_TABLES:
        op.alter_column(table, "org_id", existing_type=sa.Integer(), nullable=False)
    op.create_foreign_key(
        "fk_projects_org",
        "projects",
        "organizations",
        ["org_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    for table in ("project_config", "secrets"):
        op.create_foreign_key(
            f"fk_{table}_org",
            table,
            "organizations",
            ["org_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # 5. Project slugs unique per org, plus the composite foreign key target.
    op.drop_constraint("uq_projects_slug", "projects", type_="unique")
    op.create_unique_constraint("uq_projects_org_slug", "projects", ["org_id", "slug"])
    op.create_unique_constraint("uq_projects_org_id", "projects", ["org_id", "id"])

    # 6. The project foreign keys become composite.
    for table in ("project_config", "secrets"):
        name = f"fk_{table}_project"
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            name,
            table,
            "projects",
            ["org_id", "project_id"],
            ["org_id", "id"],
            ondelete="CASCADE",
        )

    # 7. The scope constraints include org_id.
    op.drop_constraint("uq_project_config_scope", "project_config", type_="unique")
    op.create_unique_constraint(
        "uq_project_config_scope",
        "project_config",
        ["org_id", "project_id"],
        postgresql_nulls_not_distinct=True,
    )
    op.drop_constraint("uq_secrets_scope", "secrets", type_="unique")
    op.create_unique_constraint(
        "uq_secrets_scope",
        "secrets",
        ["org_id", "project_id", "kind", "provider"],
        postgresql_nulls_not_distinct=True,
    )

    # 8. What M2b removes.
    op.drop_column("users", "role")
    op.drop_table("legacy_import")


def downgrade() -> None:
    """Downgrade schema (refused when more than one org exists)."""
    bind = op.get_bind()
    orgs = bind.execute(sa.text("SELECT count(*) FROM organizations")).scalar_one()
    if orgs > 1:
        raise RuntimeError(
            f"cannot downgrade below the tenancy revision: {orgs} organizations "
            "exist, and the previous schema holds the projects of one only"
        )

    op.create_table(
        "legacy_import",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.Text(), nullable=False),
        sa.Column("rows", sa.JSON(), nullable=False),
        sa.Column("imported_at", sa.Text(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_legacy_import_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column(
        "users",
        sa.Column("role", sa.Text(), nullable=False, server_default="owner"),
    )
    op.alter_column("users", "role", existing_type=sa.Text(), server_default=None)

    op.drop_constraint("uq_secrets_scope", "secrets", type_="unique")
    op.create_unique_constraint(
        "uq_secrets_scope",
        "secrets",
        ["project_id", "kind", "provider"],
        postgresql_nulls_not_distinct=True,
    )
    op.drop_constraint("uq_project_config_scope", "project_config", type_="unique")
    op.create_unique_constraint(
        "uq_project_config_scope",
        "project_config",
        ["project_id"],
        postgresql_nulls_not_distinct=True,
    )

    for table in ("project_config", "secrets"):
        name = f"fk_{table}_project"
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            name, table, "projects", ["project_id"], ["id"], ondelete="CASCADE"
        )
        op.drop_constraint(f"fk_{table}_org", table, type_="foreignkey")

    op.drop_constraint("uq_projects_org_id", "projects", type_="unique")
    op.drop_constraint("uq_projects_org_slug", "projects", type_="unique")
    op.create_unique_constraint("uq_projects_slug", "projects", ["slug"])
    op.drop_constraint("fk_projects_org", "projects", type_="foreignkey")

    for table in _ORG_TABLES:
        op.drop_column(table, "org_id")

    op.drop_constraint("fk_settings_builtin_org", "settings", type_="foreignkey")
    op.drop_column("settings", "builtin_org_id")
    op.drop_index("ix_memberships_user_id", table_name="memberships")
    op.drop_table("memberships")
    op.drop_table("organizations")
