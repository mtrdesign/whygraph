"""add forge_crawl_state

Revision ID: 6b2e9d4f7a1c
Revises: d5a8e1f03b72
Create Date: 2026-10-26 12:00:00.000000

Adds ``forge_crawl_state``: one row per kind (``pull_request`` /
``issue``) holding the repository the last complete GitHub crawl read
and the newest ``updatedAt`` it saw. The GitHub crawler uses it to ask
GitHub only for what changed since, instead of re-paginating the whole
PR and issue history on every scan. There is no data migration: a
database without these rows simply gets one full walk, which then
writes them.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "6b2e9d4f7a1c"
down_revision: Union[str, Sequence[str], None] = "d5a8e1f03b72"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "forge_crawl_state",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("watermark", sa.Text(), nullable=True),
        sa.Column("crawled_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("kind"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("forge_crawl_state")
