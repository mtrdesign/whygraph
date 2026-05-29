"""add per-file llm descriptions to commit_file_change

Revision ID: a1f7c2e9b4d8
Revises: 9c3d6e2af0b1
Create Date: 2026-05-29 10:00:00.000000

Adds ``llm_description`` / ``llm_description_model`` columns on
``commit_file_change``. They cache the lazily-generated per-file
description for *bulk* commits (more files than
``analyze.large_commit_file_count``), keyed by ``(commit_sha, path)``.
Both are nullable and default ``NULL``; normal commits never populate
them and keep using the whole-diff ``commit.llm_description``.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1f7c2e9b4d8"
down_revision: Union[str, Sequence[str], None] = "9c3d6e2af0b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("commit_file_change", schema=None) as batch_op:
        batch_op.add_column(sa.Column("llm_description", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("llm_description_model", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("commit_file_change", schema=None) as batch_op:
        batch_op.drop_column("llm_description_model")
        batch_op.drop_column("llm_description")
