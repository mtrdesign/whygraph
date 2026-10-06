"""access

Revision ID: d8a31f6c07e5
Revises: c4e7a19b52d8
Create Date: 2026-10-06 12:00:00

M2f-1: project access. Four new tables, three new columns and one widened
check:

* ``organizations.default_project_role`` (``contributor`` / ``viewer`` /
  ``none``) and ``projects.restricted`` (boolean). Both are added with a
  server default so existing rows are filled, then the default is dropped
  (the models carry Python defaults only).
* ``project_grants``: a per-user override on one project, tied by composite
  foreign keys to the project and to the user's membership (a grant dies
  with either).
* ``invitations`` and ``invitation_grants``: GitHub-user invitations (by
  GitHub id) and the project grants they carry. One open invitation per
  ``(org_id, github_id)`` (``uq_invitations_open``, a partial index).
* ``audit_events``: the persisted audit log.
* ``platform_links.remote_project_role`` (nullable).
* ``ck_connection_tokens_revoked_reason`` gains ``'project_access_removed'``:
  dropped and re-created from literal strings.

Every existing row fits; there is no backfill.

``downgrade()`` refuses while the database holds anything the previous
schema cannot represent - dropping ``restricted`` silently would
un-restrict projects.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d8a31f6c07e5"
down_revision: Union[str, Sequence[str], None] = "c4e7a19b52d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_REVOKED_REASONS = (
    "revoked_reason IN ('user_revoked', 'admin_revoked', 'removed_locally', "
    "'member_removed', 'member_left', 'user_disabled', 'project_deleted', "
    "'org_deleted', 'idle')"
)
_NEW_REVOKED_REASONS = (
    "revoked_reason IN ('user_revoked', 'admin_revoked', 'removed_locally', "
    "'member_removed', 'member_left', 'user_disabled', 'project_deleted', "
    "'org_deleted', 'idle', 'project_access_removed')"
)
_DEFAULT_PROJECT_ROLES = "default_project_role IN ('contributor', 'viewer', 'none')"
_PROJECT_ROLE = "role IN ('admin', 'contributor', 'viewer')"
_ORG_ROLE = "role IN ('owner', 'admin', 'member')"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "organizations",
        sa.Column(
            "default_project_role",
            sa.Text(),
            nullable=False,
            server_default="contributor",
        ),
    )
    op.alter_column("organizations", "default_project_role", server_default=None)
    op.create_check_constraint(
        "ck_organizations_default_project_role",
        "organizations",
        _DEFAULT_PROJECT_ROLES,
    )
    op.add_column(
        "projects",
        sa.Column(
            "restricted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.alter_column("projects", "restricted", server_default=None)
    op.add_column(
        "platform_links", sa.Column("remote_project_role", sa.Text(), nullable=True)
    )

    op.create_table(
        "project_grants",
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("granted_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(_PROJECT_ROLE, name="ck_project_grants_role"),
        sa.ForeignKeyConstraint(
            ["org_id", "project_id"],
            ["projects.org_id", "projects.id"],
            name="fk_project_grants_project",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["memberships.org_id", "memberships.user_id"],
            name="fk_project_grants_membership",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by"],
            ["users.id"],
            name="fk_project_grants_granted_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("project_id", "user_id", name="pk_project_grants"),
    )
    op.create_index(
        "ix_project_grants_user_id", "project_grants", ["user_id"], unique=False
    )

    op.create_table(
        "invitations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uid", sa.Text(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("github_login", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("invited_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("redeemed_at", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.Text(), nullable=True),
        sa.Column("redeemed_by", sa.Integer(), nullable=True),
        sa.CheckConstraint(_ORG_ROLE, name="ck_invitations_role"),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_invitations_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by"],
            ["users.id"],
            name="fk_invitations_invited_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["redeemed_by"],
            ["users.id"],
            name="fk_invitations_redeemed_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_invitations"),
        sa.UniqueConstraint("uid", name="uq_invitations_uid"),
        sa.UniqueConstraint("org_id", "id", name="uq_invitations_org_id"),
    )
    op.create_index(
        "uq_invitations_open",
        "invitations",
        ["org_id", "github_id"],
        unique=True,
        postgresql_where=sa.text("redeemed_at IS NULL AND revoked_at IS NULL"),
    )

    op.create_table(
        "invitation_grants",
        sa.Column("invitation_id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.CheckConstraint(_PROJECT_ROLE, name="ck_invitation_grants_role"),
        sa.ForeignKeyConstraint(
            ["org_id", "invitation_id"],
            ["invitations.org_id", "invitations.id"],
            name="fk_invitation_grants_invitation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "project_id"],
            ["projects.org_id", "projects.id"],
            name="fk_invitation_grants_project",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "invitation_id", "project_id", name="pk_invitation_grants"
        ),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("org_slug", sa.Text(), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("actor_label", sa.Text(), nullable=True),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_audit_events_org",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name="fk_audit_events_actor",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
    )
    op.create_index(
        "ix_audit_events_org_id_id",
        "audit_events",
        ["org_id", sa.text("id DESC")],
        unique=False,
    )
    op.create_index("ix_audit_events_event", "audit_events", ["event"], unique=False)

    op.drop_constraint(
        "ck_connection_tokens_revoked_reason", "connection_tokens", type_="check"
    )
    op.create_check_constraint(
        "ck_connection_tokens_revoked_reason",
        "connection_tokens",
        _NEW_REVOKED_REASONS,
    )


def downgrade() -> None:
    """Downgrade schema (refused while M2f-1 data exists)."""
    bind = op.get_bind()
    blockers = {
        "project grants": "SELECT count(*) FROM project_grants",
        "invitations": "SELECT count(*) FROM invitations",
        "audit events": "SELECT count(*) FROM audit_events",
        "restricted projects": "SELECT count(*) FROM projects WHERE restricted",
        "organizations with a non-default project role": (
            "SELECT count(*) FROM organizations "
            "WHERE default_project_role <> 'contributor'"
        ),
        "platform links with a project role": (
            "SELECT count(*) FROM platform_links WHERE remote_project_role IS NOT NULL"
        ),
        "connection tokens revoked for lost project access": (
            "SELECT count(*) FROM connection_tokens "
            "WHERE revoked_reason = 'project_access_removed'"
        ),
    }
    for what, sql in blockers.items():
        count = bind.execute(sa.text(sql)).scalar()
        if count:
            raise RuntimeError(
                f"cannot downgrade below the access revision: the database "
                f"holds {count} {what}, which the previous schema cannot represent"
            )

    op.drop_constraint(
        "ck_connection_tokens_revoked_reason", "connection_tokens", type_="check"
    )
    op.create_check_constraint(
        "ck_connection_tokens_revoked_reason",
        "connection_tokens",
        _OLD_REVOKED_REASONS,
    )
    op.drop_index("ix_audit_events_event", table_name="audit_events")
    op.drop_index("ix_audit_events_org_id_id", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("invitation_grants")
    op.drop_index("uq_invitations_open", table_name="invitations")
    op.drop_table("invitations")
    op.drop_index("ix_project_grants_user_id", table_name="project_grants")
    op.drop_table("project_grants")
    op.drop_column("platform_links", "remote_project_role")
    op.drop_column("projects", "restricted")
    op.drop_constraint(
        "ck_organizations_default_project_role", "organizations", type_="check"
    )
    op.drop_column("organizations", "default_project_role")
