"""add owner_uid to chat_session

Revision ID: d5a8e1f03b72
Revises: e4c1b9d72f3a
Create Date: 2026-10-04 12:00:00.000000

Adds ``chat_session.owner_uid``: the ``users.uid`` of the portal user who
started the session (M2d-1, per-user chat in production). NULL - the value
every existing row gets - means "before M2d, or local mode". The column
holds no foreign key because the portal's ``users`` table lives in another
database. Additive and nullable, so no data migration is needed.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d5a8e1f03b72"
down_revision: Union[str, Sequence[str], None] = "e4c1b9d72f3a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema (a plain ``add_column``, like ``e4c1b9d72f3a``)."""
    op.add_column("chat_session", sa.Column("owner_uid", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("chat_session", "owner_uid")
