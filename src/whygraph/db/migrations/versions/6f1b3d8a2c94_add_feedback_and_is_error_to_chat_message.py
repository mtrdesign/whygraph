"""add is_error and rating columns to chat_message

Revision ID: 6f1b3d8a2c94
Revises: 5e8a2c9d1f47
Create Date: 2026-10-30 12:00:00.000000

Adds ``chat_message.is_error`` (a tool row's error flag, so it survives a
reload and a fork) and the four answer-rating columns (``rating``,
``rating_reason``, ``rating_comment``, ``rated_at``). All additive and
nullable, so no data migration is needed; NULL ``is_error`` reads as false.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "6f1b3d8a2c94"
down_revision: Union[str, Sequence[str], None] = "5e8a2c9d1f47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema (plain ``add_column`` calls, never a batch recreate)."""
    op.add_column("chat_message", sa.Column("is_error", sa.Boolean(), nullable=True))
    op.add_column("chat_message", sa.Column("rating", sa.Integer(), nullable=True))
    op.add_column("chat_message", sa.Column("rating_reason", sa.Text(), nullable=True))
    op.add_column("chat_message", sa.Column("rating_comment", sa.Text(), nullable=True))
    op.add_column("chat_message", sa.Column("rated_at", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("chat_message", "rated_at")
    op.drop_column("chat_message", "rating_comment")
    op.drop_column("chat_message", "rating_reason")
    op.drop_column("chat_message", "rating")
    op.drop_column("chat_message", "is_error")
