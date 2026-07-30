"""add provider and model to chat_message

Revision ID: f3582dfcc817
Revises: b18c23ad33e8
Create Date: 2026-07-29 12:44:48.729905

Records which provider and model produced each assistant row. The Chat
view lets the model be switched mid-conversation, so the session's own
provider/model only describes the *next* turn — without these columns a
transcript spanning two models would attribute every turn to whichever
was selected last. Nullable because user and tool rows have no model.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f3582dfcc817"
down_revision: Union[str, Sequence[str], None] = "b18c23ad33e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("chat_message", schema=None) as batch_op:
        batch_op.add_column(sa.Column("provider", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("model", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("chat_message", schema=None) as batch_op:
        batch_op.drop_column("model")
        batch_op.drop_column("provider")
