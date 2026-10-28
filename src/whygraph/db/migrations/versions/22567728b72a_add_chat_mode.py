"""add chat mode and grounding

Revision ID: 22567728b72a
Revises: a3c7e91b4d25
Create Date: 2026-10-28 09:00:00.000000

Adds the chat-mode and grounding columns, all nullable TEXT so no data
migration is needed. ``chat_session.mode`` is the session's chat mode
(NULL means the default engineer mode). ``chat_message.mode`` records the
mode a turn ran under. ``chat_message.grounding`` stores the References
block shown for an assistant turn (NULL when the turn had none).

Plain ``add_column`` calls: SQLite adds a nullable column in place.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "22567728b72a"
down_revision: Union[str, Sequence[str], None] = "a3c7e91b4d25"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema (plain ``add_column`` calls)."""
    op.add_column("chat_session", sa.Column("mode", sa.Text(), nullable=True))
    op.add_column("chat_message", sa.Column("mode", sa.Text(), nullable=True))
    op.add_column("chat_message", sa.Column("grounding", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema (plain ``drop_column`` calls)."""
    op.drop_column("chat_message", "grounding")
    op.drop_column("chat_message", "mode")
    op.drop_column("chat_session", "mode")
