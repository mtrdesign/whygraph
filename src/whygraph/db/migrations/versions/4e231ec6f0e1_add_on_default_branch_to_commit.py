"""add on_default_branch to commit

Revision ID: 4e231ec6f0e1
Revises: a1f7c2e9b4d8
Create Date: 2026-06-11 16:27:51.724319

Adds the ``on_default_branch`` discriminator on ``commit``. Default 1
means every existing row is treated as a default-branch (first-parent
walk) commit; PR-origin commits recovered from a squash-merged PR are
inserted with 0 so they stay out of the main-walk-only queries
(area-history, refactor-walk). Additive, server-default backfilled — safe
to re-run scans against an upgraded DB.

(Autogenerate also surfaced pre-existing server-default / index drift on
``commit.refactor_score`` and ``commit_file_change``; those are unrelated
to this change and intentionally omitted to keep the migration additive.)
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "4e231ec6f0e1"
down_revision: Union[str, Sequence[str], None] = "a1f7c2e9b4d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    A plain ``add_column`` (native ``ALTER TABLE ADD COLUMN``) rather than a
    batch recreate: SQLite supports adding a column with a constant server
    default in place, and recreating ``commit`` would trip the foreign key
    from ``commit_file_change`` on a populated DB.
    """
    op.add_column(
        "commit",
        sa.Column(
            "on_default_branch",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("commit", "on_default_branch")
