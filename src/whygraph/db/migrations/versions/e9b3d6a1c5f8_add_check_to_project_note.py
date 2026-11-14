"""add the check columns to project_note

Revision ID: e9b3d6a1c5f8
Revises: c4e7a2d9b1f6
Create Date: 2026-11-14 12:00:00.000000

Adds ``project_note.check_kind``, ``check_text`` and ``check_glob``: the
optional check of a constraint note (plan 54 section 5.8), a fixed text that
must be absent from, or present in, the files of one glob. All three are
nullable and set together; every note written before this revision has no
check. No data migration and no CHECK constraint; values are validated in
``whygraph.mcp.notes``.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e9b3d6a1c5f8"
down_revision: Union[str, Sequence[str], None] = "c4e7a2d9b1f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Plain ``add_column`` calls (native ``ALTER TABLE``), never a batch
    recreate: recreating ``project_note`` would trip its ``supersedes_id``
    foreign key on a populated DB.
    """
    op.add_column("project_note", sa.Column("check_kind", sa.Text(), nullable=True))
    op.add_column("project_note", sa.Column("check_text", sa.Text(), nullable=True))
    op.add_column("project_note", sa.Column("check_glob", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema (native ``DROP COLUMN``, for the same reason)."""
    op.drop_column("project_note", "check_glob")
    op.drop_column("project_note", "check_text")
    op.drop_column("project_note", "check_kind")
