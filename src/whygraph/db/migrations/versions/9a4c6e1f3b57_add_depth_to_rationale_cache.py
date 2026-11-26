"""add depth, context_shas and coverage to rationale_cache

Revision ID: 9a4c6e1f3b57
Revises: d2c8e4a7f1b3
Create Date: 2026-11-27 09:00:00.000000

Plan 62 (Part B, section 13.5): ``rationale_cache`` gains ``depth`` (the
card's depth level, 0 ``low`` .. 3 ``xhigh``; ``NULL`` marks a row written
before this revision, whose fingerprint is the old whole-bundle hash),
``context_shas`` (the space-joined SHAs of the context commits the card
rendered; ``NOT NULL DEFAULT ''``, like ``deferred_shas``) and ``coverage``
(the card's coverage object as JSON; ``NULL`` when not recorded).

Plain ``add_column`` calls: SQLite adds each column in place. Nothing is
populated here: every existing row stays a legacy row and keeps being served
exactly as long as it would have before.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9a4c6e1f3b57"
down_revision: Union[str, Sequence[str], None] = "d2c8e4a7f1b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema (plain ``add_column`` calls)."""
    op.add_column("rationale_cache", sa.Column("depth", sa.Integer(), nullable=True))
    op.add_column(
        "rationale_cache",
        sa.Column(
            "context_shas",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column("rationale_cache", sa.Column("coverage", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema (drop the columns)."""
    op.drop_column("rationale_cache", "coverage")
    op.drop_column("rationale_cache", "context_shas")
    op.drop_column("rationale_cache", "depth")
