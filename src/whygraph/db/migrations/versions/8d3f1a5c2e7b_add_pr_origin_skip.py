"""add pr_origin_skip

Revision ID: 8d3f1a5c2e7b
Revises: 6b2e9d4f7a1c
Create Date: 2026-10-26 13:00:00.000000

Adds ``pr_origin_skip``: one row per squash-merged PR whose original
commits could not be recovered (its ``refs/pull/<N>/head`` is gone
upstream, or a commit stayed unreadable after a good fetch), keyed by the
PR's ``updated_at`` at that moment. The PR-origin enricher skips such a PR
until it changes, so one missing ref no longer blocks every other
candidate on every scan. There is no data migration: no row means "never
gave up", which is the previous behaviour.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "8d3f1a5c2e7b"
down_revision: Union[str, Sequence[str], None] = "6b2e9d4f7a1c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "pr_origin_skip",
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("pr_updated_at", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("pr_number"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("pr_origin_skip")
