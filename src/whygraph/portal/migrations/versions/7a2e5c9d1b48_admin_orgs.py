"""admin orgs

Revision ID: 7a2e5c9d1b48
Revises: df854cdd4f03
Create Date: 2026-11-18 14:00:00

Plan 59 phase 2 schema, read by nothing yet: ``reader_grants`` (D3);
``organizations.suspended_*`` (D6); ``deleted_at`` / ``deleted_by`` on
``users``, ``organizations`` and ``projects`` plus ``users.deleted_memberships``
and ``users.erased_at`` (D12-D14); the audit indexes on ``actor_id`` and
``target`` (D52); and ``user_deleted`` as a connection-token revoke reason.

The downgrade rewrites ``user_deleted`` to ``admin_revoked`` and drops
everything added here. Soft-deleted and suspended rows become live again on
a downgrade; that is acceptable for a test path.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7a2e5c9d1b48"
down_revision: Union[str, Sequence[str], None] = "df854cdd4f03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_REVOKED_REASONS = (
    "revoked_reason IN ('user_revoked', 'admin_revoked', 'removed_locally', "
    "'member_removed', 'member_left', 'user_disabled', 'project_deleted', "
    "'org_deleted', 'idle', 'project_access_removed')"
)
_NEW_REVOKED_REASONS = (
    "revoked_reason IN ('user_revoked', 'admin_revoked', 'removed_locally', "
    "'member_removed', 'member_left', 'user_disabled', 'project_deleted', "
    "'org_deleted', 'idle', 'project_access_removed', 'user_deleted')"
)


def _text(name: str) -> sa.Column:
    return sa.Column(name, sa.Text(), nullable=True)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "reader_grants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("revoked_at", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_reader_grants"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_reader_grants_user", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_reader_grants_org",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_reader_grants_user_org", "reader_grants", ["user_id", "org_id"])

    # users
    op.add_column("users", _text("deleted_at"))
    op.add_column("users", sa.Column("deleted_by", sa.Integer(), nullable=True))
    op.add_column("users", _text("deleted_memberships"))
    op.add_column("users", _text("erased_at"))
    op.create_foreign_key(
        "fk_users_deleted_by",
        "users",
        "users",
        ["deleted_by"],
        ["id"],
        ondelete="SET NULL",
    )

    # organizations
    op.add_column("organizations", _text("suspended_at"))
    op.add_column("organizations", _text("suspended_reason"))
    op.add_column(
        "organizations", sa.Column("suspended_by", sa.Integer(), nullable=True)
    )
    op.add_column("organizations", _text("deleted_at"))
    op.add_column("organizations", sa.Column("deleted_by", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_organizations_suspended_reason",
        "organizations",
        "suspended_reason IS NULL OR char_length(suspended_reason) <= 500",
    )
    op.create_foreign_key(
        "fk_organizations_suspended_by",
        "organizations",
        "users",
        ["suspended_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_organizations_deleted_by",
        "organizations",
        "users",
        ["deleted_by"],
        ["id"],
        ondelete="SET NULL",
    )

    # projects
    op.add_column("projects", _text("deleted_at"))
    op.add_column("projects", sa.Column("deleted_by", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_projects_deleted_by",
        "projects",
        "users",
        ["deleted_by"],
        ["id"],
        ondelete="SET NULL",
    )

    # audit_events (D52)
    op.create_index("ix_audit_events_actor", "audit_events", ["actor_id"])
    op.create_index("ix_audit_events_target", "audit_events", ["target"])

    op.drop_constraint(
        "ck_connection_tokens_revoked_reason", "connection_tokens", type_="check"
    )
    op.create_check_constraint(
        "ck_connection_tokens_revoked_reason",
        "connection_tokens",
        _NEW_REVOKED_REASONS,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "UPDATE connection_tokens SET revoked_reason = 'admin_revoked' "
        "WHERE revoked_reason = 'user_deleted'"
    )
    op.drop_constraint(
        "ck_connection_tokens_revoked_reason", "connection_tokens", type_="check"
    )
    op.create_check_constraint(
        "ck_connection_tokens_revoked_reason",
        "connection_tokens",
        _OLD_REVOKED_REASONS,
    )

    op.drop_index("ix_audit_events_target", table_name="audit_events")
    op.drop_index("ix_audit_events_actor", table_name="audit_events")

    op.drop_constraint("fk_projects_deleted_by", "projects", type_="foreignkey")
    op.drop_column("projects", "deleted_by")
    op.drop_column("projects", "deleted_at")

    op.drop_constraint(
        "fk_organizations_deleted_by", "organizations", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_organizations_suspended_by", "organizations", type_="foreignkey"
    )
    op.drop_constraint(
        "ck_organizations_suspended_reason", "organizations", type_="check"
    )
    op.drop_column("organizations", "deleted_by")
    op.drop_column("organizations", "deleted_at")
    op.drop_column("organizations", "suspended_by")
    op.drop_column("organizations", "suspended_reason")
    op.drop_column("organizations", "suspended_at")

    op.drop_constraint("fk_users_deleted_by", "users", type_="foreignkey")
    op.drop_column("users", "erased_at")
    op.drop_column("users", "deleted_memberships")
    op.drop_column("users", "deleted_by")
    op.drop_column("users", "deleted_at")

    op.drop_index("ix_reader_grants_user_org", table_name="reader_grants")
    op.drop_table("reader_grants")
