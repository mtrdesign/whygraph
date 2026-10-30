"""add project_note

Revision ID: b7e4c2a91d36
Revises: 6f1b3d8a2c94
Create Date: 2026-10-30 12:00:00.000000

Adds ``project_note``: code-anchored team notes (decisions, constraints,
gotchas) with a proposed / confirmed / retired lifecycle. There is no data
migration and no CHECK constraint; values are validated in
``whygraph.mcp.notes``.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b7e4c2a91d36"
down_revision: Union[str, Sequence[str], None] = "6f1b3d8a2c94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "project_note",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("proposed_body", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("anchor_kind", sa.Text(), nullable=False),
        sa.Column("anchor_path", sa.Text(), nullable=True),
        sa.Column("line_start", sa.Integer(), nullable=True),
        sa.Column("line_end", sa.Integer(), nullable=True),
        sa.Column("qualified_name", sa.Text(), nullable=True),
        sa.Column("area", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=True),
        sa.Column("refs", sa.Text(), nullable=True),
        sa.Column("supersedes_id", sa.Integer(), nullable=True),
        sa.Column("proposed_by", sa.Text(), nullable=False),
        sa.Column("proposed_at", sa.Text(), nullable=False),
        sa.Column("confirmed_by", sa.Text(), nullable=True),
        sa.Column("confirmed_at", sa.Text(), nullable=True),
        sa.Column("reconfirmed_by", sa.Text(), nullable=True),
        sa.Column("reconfirmed_at", sa.Text(), nullable=True),
        sa.Column("retired_by", sa.Text(), nullable=True),
        sa.Column("retired_at", sa.Text(), nullable=True),
        sa.Column("retire_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["supersedes_id"], ["project_note.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_project_note_status", "project_note", ["status"])
    op.create_index("ix_project_note_anchor_path", "project_note", ["anchor_path"])
    op.create_index(
        "ix_project_note_qualified_name", "project_note", ["qualified_name"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_project_note_qualified_name", table_name="project_note")
    op.drop_index("ix_project_note_anchor_path", table_name="project_note")
    op.drop_index("ix_project_note_status", table_name="project_note")
    op.drop_table("project_note")
