"""instance settings

Revision ID: 5d9e2a7c3f18
Revises: 3c8f1b6d4e92
Create Date: 2026-11-19 12:00:00

Plan 59 phase 4 schema (§4.13): the singleton ``instance_settings`` table and
the ``signup_allowlist``. **No row is seeded**: the loader returns the defaults
while the row is missing and the first ``PUT`` writes it.

The downgrade drops both tables; the configured policy is lost, which is
acceptable for a test path.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "5d9e2a7c3f18"
down_revision: Union[str, Sequence[str], None] = "3c8f1b6d4e92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "instance_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("signup_policy", sa.Text(), server_default="open", nullable=False),
        sa.Column("org_creation", sa.Text(), server_default="everyone", nullable=False),
        sa.Column("max_orgs_per_user", sa.Integer(), nullable=True),
        sa.Column("max_projects_per_org", sa.Integer(), nullable=True),
        sa.Column("banner_text", sa.Text(), nullable=True),
        sa.Column("banner_level", sa.Text(), server_default="info", nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_instance_settings_singleton"),
        sa.CheckConstraint(
            "signup_policy IN ('open', 'invite_only', 'closed')",
            name="ck_instance_settings_signup_policy",
        ),
        sa.CheckConstraint(
            "org_creation IN ('everyone', 'admins')",
            name="ck_instance_settings_org_creation",
        ),
        sa.CheckConstraint(
            "max_orgs_per_user >= 1", name="ck_instance_settings_max_orgs"
        ),
        sa.CheckConstraint(
            "max_projects_per_org >= 1", name="ck_instance_settings_max_projects"
        ),
        sa.CheckConstraint(
            "char_length(banner_text) <= 500", name="ck_instance_settings_banner_text"
        ),
        sa.CheckConstraint(
            "banner_level IN ('info', 'warning')",
            name="ck_instance_settings_banner_level",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name="fk_instance_settings_updated_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_instance_settings"),
    )
    op.create_table(
        "signup_allowlist",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("github_login", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("used_at", sa.Text(), nullable=True),
        sa.CheckConstraint("char_length(note) <= 200", name="ck_signup_allowlist_note"),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_signup_allowlist_created_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_signup_allowlist"),
        sa.UniqueConstraint("github_id", name="uq_signup_allowlist_github_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("signup_allowlist")
    op.drop_table("instance_settings")
