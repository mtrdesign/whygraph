"""add commit_file_change

Revision ID: 7f2a8c1d4e3b
Revises: 4bde3eda78f2
Create Date: 2026-05-26 10:00:00.000000

Adds the per-commit, per-file change index that backs WhyGraph's area-
history queries (Phase 2 of the layered evidence pipeline). One row per
(commit, path-at-that-commit); rename edges live in ``renamed_from``
and are walked recursively at query time.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7f2a8c1d4e3b"
down_revision: Union[str, Sequence[str], None] = "4bde3eda78f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "commit_file_change",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("commit_sha", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("change_type", sa.Text(), nullable=False),
        sa.Column("renamed_from", sa.Text(), nullable=True),
        sa.Column("similarity", sa.Integer(), nullable=True),
        sa.Column(
            "lines_added", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "lines_deleted", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.ForeignKeyConstraint(["commit_sha"], ["commit.sha"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("commit_file_change", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_commit_file_change_commit_sha"),
            ["commit_sha"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_commit_file_change_path"),
            ["path"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_commit_file_change_renamed_from"),
            ["renamed_from"],
            unique=False,
        )
        # Composite for the hot path "commits for this path, newest first" —
        # the area-history query joins on commit_sha after filtering by path.
        batch_op.create_index(
            "ix_commit_file_change_path_commit_sha",
            ["path", "commit_sha"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("commit_file_change", schema=None) as batch_op:
        batch_op.drop_index("ix_commit_file_change_path_commit_sha")
        batch_op.drop_index(batch_op.f("ix_commit_file_change_renamed_from"))
        batch_op.drop_index(batch_op.f("ix_commit_file_change_path"))
        batch_op.drop_index(batch_op.f("ix_commit_file_change_commit_sha"))
    op.drop_table("commit_file_change")
