"""add error to chat_message

Revision ID: c7d4a1e8b3f2
Revises: f3582dfcc817
Create Date: 2026-07-30 10:12:03.441027

Records why an assistant turn failed. A provider error (or a user abort)
used to exist only as an in-band SSE frame, so it vanished on refresh and
— when the failure landed before the first token — left the user message
with no reply row at all. Nullable because a successful turn has no error.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c7d4a1e8b3f2"
down_revision: Union[str, Sequence[str], None] = "f3582dfcc817"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("chat_message", schema=None) as batch_op:
        batch_op.add_column(sa.Column("error", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("chat_message", schema=None) as batch_op:
        batch_op.drop_column("error")
