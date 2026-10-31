"""add project_purpose_run

Revision ID: 71460539b46f
Revises: b7e4c2a91d36
Create Date: 2026-10-31 12:00:00.000000

Adds ``project_purpose_run``: one row per run of the project-purpose
generator, holding the hash of the gathered inputs so an unchanged repo is
not regenerated, plus the audit fields (status, provider, model, token
counts, the proposal it made). ``note_id`` is a plain integer with no
foreign key. There is no data migration and no CHECK constraint; values
are validated in ``whygraph.analyze.project_purpose``.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "71460539b46f"
down_revision: Union[str, Sequence[str], None] = "b7e4c2a91d36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "project_purpose_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=True),
        sa.Column("input_bytes", sa.Integer(), nullable=True),
        sa.Column("sources", sa.Text(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("note_id", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("project_purpose_run")
