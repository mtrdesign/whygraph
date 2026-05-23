"""add rationale_cache

Revision ID: b4de974b9f54
Revises: 4ebdddf127cf
Create Date: 2026-05-23 19:59:51.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4de974b9f54'
down_revision: Union[str, Sequence[str], None] = '4ebdddf127cf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('rationale_cache',
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('line_start', sa.Integer(), nullable=False),
    sa.Column('line_end', sa.Integer(), nullable=False),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('evidence_fingerprint', sa.Text(), nullable=False),
    sa.Column('cached_at', sa.Text(), nullable=False),
    sa.Column('purpose', sa.Text(), nullable=False),
    sa.Column('why', sa.Text(), nullable=False),
    sa.Column('constraints', sa.Text(), nullable=False),
    sa.Column('tradeoffs', sa.Text(), nullable=False),
    sa.Column('risks', sa.Text(), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=True),
    sa.Column('output_tokens', sa.Integer(), nullable=True),
    sa.Column('actual_provider', sa.Text(), nullable=True),
    sa.Column('actual_model', sa.Text(), nullable=True),
    sa.Column('qualified_name', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('path', 'line_start', 'line_end', 'provider', 'model')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('rationale_cache')
