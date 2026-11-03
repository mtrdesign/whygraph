"""update check

Revision ID: 496953df26b4
Revises: b5e1a7d93c42
Create Date: 2026-11-03 12:00:00

``settings`` gains ``update_check``: whether the portal's daily release check
is on (local mode's Settings switch). Existing rows are backfilled with true;
the server default is dropped afterwards so the model need not declare one.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "496953df26b4"
down_revision: Union[str, Sequence[str], None] = "b5e1a7d93c42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "settings",
        sa.Column(
            "update_check", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.alter_column("settings", "update_check", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("settings", "update_check")
