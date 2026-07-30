"""add chat_session and chat_message

Revision ID: b18c23ad33e8
Revises: 4e231ec6f0e1
Create Date: 2026-07-29 10:49:47.492596

Adds the two tables backing the ``whygraph serve`` Chat view: one row per
conversation (``chat_session``, carrying the provider + model it was
started with) and one row per message (``chat_message``, including tool
rows so a reloaded transcript replays the tool activity the user saw
live). Only ``session_id`` is indexed — every read is "all messages for
one session", and a single-user local tool never accumulates enough
sessions for a second index to matter.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b18c23ad33e8"
down_revision: Union[str, Sequence[str], None] = "4e231ec6f0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "chat_session",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "chat_message",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "tool_calls",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("tool_call_id", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chat_session.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("chat_message", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_chat_message_session_id"),
            ["session_id"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("chat_message", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_chat_message_session_id"))
    op.drop_table("chat_message")
    op.drop_table("chat_session")
