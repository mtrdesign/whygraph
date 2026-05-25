"""add refactor_score to commit

Revision ID: 9c3d6e2af0b1
Revises: 7f2a8c1d4e3b
Create Date: 2026-05-26 11:00:00.000000

Adds the Phase 3 ``refactor_score`` column on ``commit``. Default 0
means every existing row is treated as "not boring" until the next scan
recomputes scores from ``commit_file_change`` rows.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9c3d6e2af0b1"
down_revision: Union[str, Sequence[str], None] = "7f2a8c1d4e3b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("commit", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "refactor_score",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("commit", schema=None) as batch_op:
        batch_op.drop_column("refactor_score")
