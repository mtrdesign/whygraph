"""add replay to chat_message

Revision ID: 4b774c7b092d
Revises: 8d3f1a5c2e7b
Create Date: 2026-10-27 12:00:00.000000

Adds ``chat_message.replay``: a JSON object holding the provider reasoning
payload an assistant row must send back on later turns (DeepSeek's
``reasoning_content``; only the keys in
``serve.chat.PERSISTED_REPLAY_KEYS`` are ever written). NULL - the value
every existing row gets - means "nothing to replay". Additive and nullable,
so no data migration is needed.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "4b774c7b092d"
down_revision: Union[str, Sequence[str], None] = "8d3f1a5c2e7b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema (a plain ``add_column``, like ``d5a8e1f03b72``)."""
    op.add_column("chat_message", sa.Column("replay", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("chat_message", "replay")
