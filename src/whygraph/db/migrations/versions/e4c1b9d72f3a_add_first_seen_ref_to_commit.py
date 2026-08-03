"""add first_seen_ref to commit

Revision ID: e4c1b9d72f3a
Revises: c7d4a1e8b3f2
Create Date: 2026-08-03 10:00:00.000000

Adds the ``first_seen_ref`` provenance column on ``commit``. NULL — the
value every existing row gets — means "was on the default branch when
first scanned". A non-NULL value records the ref an off-default-branch
commit was first seen on: a local branch name for work scanned off a
feature branch, or ``refs/pull/<N>/head`` for a commit recovered by the
squash-origin enricher. Additive and nullable, so no data migration is
needed; the first post-upgrade scan's reconcile pass recomputes every
``on_default_branch`` value in one sweep.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e4c1b9d72f3a"
down_revision: Union[str, Sequence[str], None] = "c7d4a1e8b3f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    A plain ``add_column`` (native ``ALTER TABLE ADD COLUMN``) rather than a
    batch recreate: SQLite adds a nullable column in place, and recreating
    ``commit`` would trip the foreign key from ``commit_file_change`` on a
    populated DB.
    """
    op.add_column("commit", sa.Column("first_seen_ref", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("commit", "first_seen_ref")
