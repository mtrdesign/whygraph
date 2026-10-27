"""add chat branching

Revision ID: a3c7e91b4d25
Revises: 4b774c7b092d
Create Date: 2026-10-27 15:00:00.000000

Turns the chat transcript into a parent-pointer tree. ``chat_message`` gains
``parent_id`` (the parent row in the same session; NULL for a root; parents
always have lower ids; indexed, with no foreign key because SQLite's
``ADD COLUMN`` cannot add one), ``finish_reason`` (the provider's raw stop
reason for the round), ``effort`` (the effort level the turn ran at) and
``reasoning`` (reasoning summary text for display; NULL until it is
filled). ``chat_session`` gains ``active_leaf_id`` (the row the visible
transcript ends at; NULL for an empty session) and ``effort`` (NULL means
the ``[chat].effort`` default).

Every existing transcript is linear, so the backfill points each message at
the previous message of its session and each session at its last message.
All new columns are nullable, so no other data migration is needed.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a3c7e91b4d25"
down_revision: Union[str, Sequence[str], None] = "4b774c7b092d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema (plain ``add_column`` calls, then a linear backfill)."""
    op.add_column("chat_message", sa.Column("parent_id", sa.Integer(), nullable=True))
    op.add_column("chat_message", sa.Column("finish_reason", sa.Text(), nullable=True))
    op.add_column("chat_message", sa.Column("effort", sa.Text(), nullable=True))
    op.add_column("chat_message", sa.Column("reasoning", sa.Text(), nullable=True))
    op.create_index("ix_chat_message_parent_id", "chat_message", ["parent_id"])
    op.add_column(
        "chat_session", sa.Column("active_leaf_id", sa.Integer(), nullable=True)
    )
    op.add_column("chat_session", sa.Column("effort", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE chat_message SET parent_id = (
            SELECT MAX(m2.id) FROM chat_message m2
            WHERE m2.session_id = chat_message.session_id
              AND m2.id < chat_message.id
        )
        """
    )
    op.execute(
        """
        UPDATE chat_session SET active_leaf_id = (
            SELECT MAX(id) FROM chat_message
            WHERE chat_message.session_id = chat_session.id
        )
        """
    )


def downgrade() -> None:
    """Downgrade schema (the index first, then the plain ``drop_column`` calls)."""
    op.drop_index("ix_chat_message_parent_id", "chat_message")
    op.drop_column("chat_message", "reasoning")
    op.drop_column("chat_message", "effort")
    op.drop_column("chat_message", "finish_reason")
    op.drop_column("chat_message", "parent_id")
    op.drop_column("chat_session", "effort")
    op.drop_column("chat_session", "active_leaf_id")
