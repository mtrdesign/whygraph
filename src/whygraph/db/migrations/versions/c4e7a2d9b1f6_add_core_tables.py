"""add the core-pass tables

Revision ID: c4e7a2d9b1f6
Revises: 5b8d2e4f6a19
Create Date: 2026-11-11 18:00:00.000000

Adds the five tables of the scan's "Core commits" phase (plan 49 section
6): ``core_state`` (one bookkeeping row), ``core_file`` and ``core_rename``
(per-file and per-rename blame caches), ``core_commit`` (the core set) and
``core_bulk_pair`` (the bulk-commit slices of the core files). All are
recomputed by the next scan, so there is no data migration.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4e7a2d9b1f6"
down_revision: Union[str, Sequence[str], None] = "5b8d2e4f6a19"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "core_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rules_key", sa.Text(), nullable=False),
        sa.Column("selection", sa.Text(), nullable=True),
        sa.Column("fallback_reason", sa.Text(), nullable=True),
        sa.Column("files_selected", sa.Integer(), nullable=True),
        sa.Column("files_total", sa.Integer(), nullable=False),
        sa.Column("files_done", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.Column("completed_head", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "core_file",
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("blob_sha", sa.Text(), nullable=False),
        sa.Column("computed_head", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("shas", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("path"),
    )
    op.create_table(
        "core_rename",
        sa.Column("rename_sha", sa.Text(), nullable=False),
        sa.Column("pred_path", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("shas", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("rename_sha", "pred_path"),
    )
    op.create_table(
        "core_commit",
        sa.Column("sha", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("sha"),
    )
    op.create_table(
        "core_bulk_pair",
        sa.Column("sha", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("sha", "path"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("core_bulk_pair")
    op.drop_table("core_commit")
    op.drop_table("core_rename")
    op.drop_table("core_file")
    op.drop_table("core_state")
