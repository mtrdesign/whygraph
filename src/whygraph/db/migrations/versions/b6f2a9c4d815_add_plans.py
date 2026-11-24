"""add plan and plan_link

Revision ID: b6f2a9c4d815
Revises: e9b3d6a1c5f8
Create Date: 2026-11-24 12:00:00.000000

Adds the shared-plans tables (plan 61): ``plan`` (one row per plan file:
redacted body, decision digest, outline, why index) and ``plan_link`` (links
from a plan to commits, pull requests and repo paths). ``plan_link.stem`` is
not a foreign key: a commit trailer may name a plan that is not indexed yet.
Both tables are rebuilt by the next scan. No data migration.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b6f2a9c4d815"
down_revision: Union[str, Sequence[str], None] = "e9b3d6a1c5f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "plan",
        sa.Column("stem", sa.String(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("blob_sha", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=True),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("outline", sa.Text(), nullable=False),
        sa.Column(
            "why_index", sa.Text(), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "redacted_lines", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "truncated", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("present", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("paths_head", sa.Text(), nullable=True),
        sa.Column("paths_hash", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("stem"),
    )
    op.create_table(
        "plan_link",
        sa.Column("stem", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("ref", sa.String(), nullable=False),
        sa.Column("sections", sa.Text(), nullable=True),
        sa.Column(
            "declared", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.PrimaryKeyConstraint("stem", "kind", "ref"),
    )
    op.create_index("ix_plan_link_kind_ref", "plan_link", ["kind", "ref"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_plan_link_kind_ref", table_name="plan_link")
    op.drop_table("plan_link")
    op.drop_table("plan")
