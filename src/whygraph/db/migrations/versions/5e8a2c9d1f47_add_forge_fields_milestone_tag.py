"""add forge fields, milestone and tag tables

Revision ID: 5e8a2c9d1f47
Revises: 22567728b72a
Create Date: 2026-10-29 09:00:00.000000

Phase 2 of the chat lead tools. ``pull_request`` gains the review and
planning fields (milestone number, assignees, review decision, latest
opinionated reviews, review requests, first review time, changed files and
line counts); ``issue`` gains milestone, assignees and state reason;
``forge_crawl_state`` gains ``fields_version`` so a crawl can tell that a
row was stored before these fields existed. JSON list columns are
``NOT NULL`` with a ``'[]'`` server default, the other new columns are
nullable (``fields_version`` is ``NOT NULL DEFAULT 0``). Two new tables,
``milestone`` and ``tag``, hold the forge's milestones and the repository's
tags.

Plain ``add_column`` calls: SQLite adds each column in place. Nothing is
populated here; the crawler backfills on its next run.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "5e8a2c9d1f47"
down_revision: Union[str, Sequence[str], None] = "22567728b72a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_col(name: str) -> sa.Column:
    """Return a ``NOT NULL`` JSON-list TEXT column defaulting to ``'[]'``."""
    return sa.Column(name, sa.Text(), nullable=False, server_default=sa.text("'[]'"))


def upgrade() -> None:
    """Upgrade schema (plain ``add_column`` calls, then two new tables)."""
    op.add_column("pull_request", sa.Column("milestone", sa.Integer(), nullable=True))
    op.add_column("pull_request", _json_col("assignees"))
    op.add_column(
        "pull_request", sa.Column("review_decision", sa.Text(), nullable=True)
    )
    op.add_column("pull_request", _json_col("latest_reviews"))
    op.add_column("pull_request", _json_col("review_requests"))
    op.add_column(
        "pull_request", sa.Column("first_review_at", sa.Text(), nullable=True)
    )
    op.add_column("pull_request", _json_col("files"))
    op.add_column("pull_request", sa.Column("additions", sa.Integer(), nullable=True))
    op.add_column("pull_request", sa.Column("deletions", sa.Integer(), nullable=True))
    op.add_column(
        "pull_request", sa.Column("changed_files", sa.Integer(), nullable=True)
    )
    op.add_column("issue", sa.Column("milestone", sa.Integer(), nullable=True))
    op.add_column("issue", _json_col("assignees"))
    op.add_column("issue", sa.Column("state_reason", sa.Text(), nullable=True))
    op.add_column(
        "forge_crawl_state",
        sa.Column(
            "fields_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_table(
        "milestone",
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("due_on", sa.Text(), nullable=True),
        sa.Column("closed_at", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("progress_pct", sa.REAL(), nullable=True),
        sa.Column("html_url", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("number"),
    )
    op.create_table(
        "tag",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("sha", sa.Text(), nullable=False),
        sa.Column("tagged_at", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    """Downgrade schema (drop the tables, then the columns)."""
    op.drop_table("tag")
    op.drop_table("milestone")
    op.drop_column("forge_crawl_state", "fields_version")
    op.drop_column("issue", "state_reason")
    op.drop_column("issue", "assignees")
    op.drop_column("issue", "milestone")
    op.drop_column("pull_request", "changed_files")
    op.drop_column("pull_request", "deletions")
    op.drop_column("pull_request", "additions")
    op.drop_column("pull_request", "files")
    op.drop_column("pull_request", "first_review_at")
    op.drop_column("pull_request", "review_requests")
    op.drop_column("pull_request", "latest_reviews")
    op.drop_column("pull_request", "review_decision")
    op.drop_column("pull_request", "assignees")
    op.drop_column("pull_request", "milestone")
