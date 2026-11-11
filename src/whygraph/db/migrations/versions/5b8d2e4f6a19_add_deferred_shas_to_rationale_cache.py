"""add deferred_shas to rationale_cache

Revision ID: 5b8d2e4f6a19
Revises: 3a9f7c1d5e82
Create Date: 2026-11-11 12:00:00.000000

Adds ``rationale_cache.deferred_shas``, the space-joined SHAs a card was
written without a description for. Empty means final, so every row written
before this revision stays final. No data migration.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "5b8d2e4f6a19"
down_revision: Union[str, Sequence[str], None] = "3a9f7c1d5e82"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("rationale_cache", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "deferred_shas",
                sa.Text(),
                server_default=sa.text("''"),
                nullable=False,
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("rationale_cache", schema=None) as batch_op:
        batch_op.drop_column("deferred_shas")
