"""admin metrics

Revision ID: 3c8f1b6d4e92
Revises: 7a2e5c9d1b48
Create Date: 2026-11-19 10:00:00

Plan 59 phase 3 schema, read by nothing yet: ``storage_samples`` (§4.10) and
the three indexes the dashboard statements need (P8): ``ix_usage_events_time``,
``ix_scan_runs_queued_at`` and ``ix_agent_call_days_day``.

The downgrade drops them all. Sampled storage figures are lost on a
downgrade; that is acceptable for a test path.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "3c8f1b6d4e92"
down_revision: Union[str, Sequence[str], None] = "7a2e5c9d1b48"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DAY = "day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "storage_samples",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Text(), nullable=False),
        sa.Column("history_bytes", sa.BigInteger(), nullable=False),
        sa.Column("git_bytes", sa.BigInteger(), nullable=False),
        sa.Column("codegraph_bytes", sa.BigInteger(), nullable=False),
        sa.Column("worktree_bytes", sa.BigInteger(), nullable=False),
        sa.Column("runs_bytes", sa.BigInteger(), nullable=False),
        sa.Column("measured_at", sa.Text(), nullable=False),
        sa.CheckConstraint(_DAY, name="ck_storage_samples_day"),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_storage_samples_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "project_id"],
            ["projects.org_id", "projects.id"],
            name="fk_storage_samples_project",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_storage_samples"),
        sa.UniqueConstraint("project_id", "day", name="uq_storage_samples_project_day"),
    )
    op.create_index("ix_storage_samples_org_day", "storage_samples", ["org_id", "day"])
    op.create_index("ix_usage_events_time", "usage_events", ["created_at"])
    op.create_index("ix_scan_runs_queued_at", "scan_runs", ["queued_at"])
    op.create_index("ix_agent_call_days_day", "agent_call_days", ["day"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_agent_call_days_day", table_name="agent_call_days")
    op.drop_index("ix_scan_runs_queued_at", table_name="scan_runs")
    op.drop_index("ix_usage_events_time", table_name="usage_events")
    op.drop_index("ix_storage_samples_org_day", table_name="storage_samples")
    op.drop_table("storage_samples")
