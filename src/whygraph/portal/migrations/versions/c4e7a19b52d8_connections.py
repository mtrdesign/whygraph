"""connections

Revision ID: c4e7a19b52d8
Revises: 73bf248ea6fd
Create Date: 2026-10-05 12:00:00

M2e: the connected portal. Two new tables and one widened check:

* ``connection_tokens`` (production): a local portal acting as one user on
  one project. ``uid`` (a UUID4, what the lists and audit name),
  ``user_id`` (``ON DELETE CASCADE``), ``org_id`` / ``project_id`` (``ON
  DELETE SET NULL``, so a deleted project's token still answers why),
  ``token_hash`` (SHA-256 hex, unique), ``client_name``, ``created_at``,
  ``last_used_at`` (``NULL`` = never used), ``revoked_at`` and
  ``revoked_reason`` (``ck_connection_tokens_revoked_reason``). An index on
  ``user_id`` and a partial index on ``project_id WHERE revoked_at IS
  NULL`` (the live tokens of a project).
* ``platform_links`` (local mode): the link of a ``platform`` project, one
  row per project (``ON DELETE CASCADE``), its token encrypted;
  ``ck_platform_links_status``.
* ``ck_projects_source`` gains ``'platform'``: dropped and re-created.

Every existing row fits; there is no backfill.

``downgrade()`` refuses while the database holds anything the previous
schema cannot represent - a ``platform`` project, a platform link or any
connection token. Otherwise it drops both tables and restores the old
source check.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4e7a19b52d8"
down_revision: Union[str, Sequence[str], None] = "73bf248ea6fd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_SOURCES = "source IN ('local', 'github')"
_NEW_SOURCES = "source IN ('local', 'github', 'platform')"
_REVOKED_REASONS = (
    "revoked_reason IN ('user_revoked', 'admin_revoked', 'removed_locally', "
    "'member_removed', 'member_left', 'user_disabled', 'project_deleted', "
    "'org_deleted', 'idle')"
)
_LINK_STATUSES = (
    "status IN ('ok', 'unreachable', 'revoked', 'removed', 'access_lost', "
    "'update_required')"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "connection_tokens",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("uid", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("client_name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("last_used_at", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.Text(), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            _REVOKED_REASONS, name="ck_connection_tokens_revoked_reason"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_connection_tokens_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_connection_tokens_org",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_connection_tokens_project",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_connection_tokens"),
        sa.UniqueConstraint("uid", name="uq_connection_tokens_uid"),
        sa.UniqueConstraint("token_hash", name="uq_connection_tokens_token_hash"),
    )
    op.create_index(
        "ix_connection_tokens_user_id", "connection_tokens", ["user_id"], unique=False
    )
    op.create_index(
        "ix_connection_tokens_live_project_id",
        "connection_tokens",
        ["project_id"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "platform_links",
        sa.Column("project_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("platform_origin", sa.Text(), nullable=False),
        sa.Column("api_origin", sa.Text(), nullable=False),
        sa.Column("org_slug", sa.Text(), nullable=False),
        sa.Column("remote_slug", sa.Text(), nullable=False),
        sa.Column("remote_name", sa.Text(), nullable=False),
        sa.Column("clone_url", sa.Text(), nullable=False),
        sa.Column("default_branch", sa.Text(), nullable=False),
        sa.Column("token_ciphertext", sa.Text(), nullable=False),
        sa.Column("token_hint", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("status_at", sa.Text(), nullable=False),
        sa.Column("last_platform_head", sa.Text(), nullable=True),
        sa.CheckConstraint(_LINK_STATUSES, name="ck_platform_links_status"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_platform_links_project",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("project_id", name="pk_platform_links"),
    )

    op.drop_constraint("ck_projects_source", "projects", type_="check")
    op.create_check_constraint("ck_projects_source", "projects", _NEW_SOURCES)


def downgrade() -> None:
    """Downgrade schema (refused while M2e data exists)."""
    bind = op.get_bind()
    blockers = {
        "platform projects": "SELECT count(*) FROM projects WHERE source = 'platform'",
        "platform links": "SELECT count(*) FROM platform_links",
        "connection tokens": "SELECT count(*) FROM connection_tokens",
    }
    for what, sql in blockers.items():
        count = bind.execute(sa.text(sql)).scalar()
        if count:
            raise RuntimeError(
                f"cannot downgrade below the connections revision: the database "
                f"holds {count} {what}, which the previous schema cannot represent"
            )

    op.drop_constraint("ck_projects_source", "projects", type_="check")
    op.create_check_constraint("ck_projects_source", "projects", _OLD_SOURCES)
    op.drop_table("platform_links")
    op.drop_index(
        "ix_connection_tokens_live_project_id", table_name="connection_tokens"
    )
    op.drop_index("ix_connection_tokens_user_id", table_name="connection_tokens")
    op.drop_table("connection_tokens")
