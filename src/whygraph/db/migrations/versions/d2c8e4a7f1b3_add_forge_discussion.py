"""add forge discussion: review summaries, review threads, comment totals

Revision ID: d2c8e4a7f1b3
Revises: b6f2a9c4d815
Create Date: 2026-11-26 21:00:00.000000

Plan 62 (Part A): ``pull_request`` gains ``review_summaries`` and
``review_threads`` (JSON lists, human comments only), GitHub's
``review_threads_total`` and ``comments_total``, and the thread pass's
``review_threads_synced`` flag; ``issue`` gains ``comments`` (JSON list) and
``comments_total``. JSON list columns are ``NOT NULL`` with a ``'[]'``
server default, totals are nullable (``NULL`` until the next GitHub crawl
re-reads the row), and ``review_threads_synced`` is ``NOT NULL DEFAULT 0``.

Plain ``add_column`` calls: SQLite adds each column in place. Nothing is
populated here; ``FORGE_FIELDS_VERSION`` 2 makes the next crawl re-read and
rewrite every row, and the thread pass fills ``review_threads``.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d2c8e4a7f1b3"
down_revision: Union[str, Sequence[str], None] = "b6f2a9c4d815"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_col(name: str) -> sa.Column:
    """Return a ``NOT NULL`` JSON-list TEXT column defaulting to ``'[]'``."""
    return sa.Column(name, sa.Text(), nullable=False, server_default=sa.text("'[]'"))


def upgrade() -> None:
    """Upgrade schema (plain ``add_column`` calls)."""
    op.add_column("pull_request", _json_col("review_summaries"))
    op.add_column("pull_request", _json_col("review_threads"))
    op.add_column(
        "pull_request", sa.Column("review_threads_total", sa.Integer(), nullable=True)
    )
    op.add_column(
        "pull_request",
        sa.Column(
            "review_threads_synced",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "pull_request", sa.Column("comments_total", sa.Integer(), nullable=True)
    )
    op.add_column("issue", _json_col("comments"))
    op.add_column("issue", sa.Column("comments_total", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema (drop the columns)."""
    op.drop_column("issue", "comments_total")
    op.drop_column("issue", "comments")
    op.drop_column("pull_request", "comments_total")
    op.drop_column("pull_request", "review_threads_synced")
    op.drop_column("pull_request", "review_threads_total")
    op.drop_column("pull_request", "review_threads")
    op.drop_column("pull_request", "review_summaries")
