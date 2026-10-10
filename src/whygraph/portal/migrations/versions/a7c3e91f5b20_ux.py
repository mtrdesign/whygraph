"""ux

Revision ID: a7c3e91f5b20
Revises: e46b50366a4c
Create Date: 2026-10-09 12:00:00

M2f-3: the UX pass. Three columns, one table and one index:

* ``scan_runs.queued_at``: when the run was queued (``NULL`` on older rows).
* ``scan_runs.cancelled_by``: the member who cancelled the run (``SET NULL``
  with the user; ``NULL`` for a budget stop, a system cancel or an old row).
* ``memberships.welcome_pending``: set when a member joins, cleared when they
  dismiss the welcome banner. Existing members are not pending.
* ``agent_call_days``: one counter row per (project, day, source, actor,
  connection, kind) - the agent-activity numbers on the Overview and the
  onboarding "any agent call" check. ``user_id`` and ``connection_id`` carry
  no foreign key on purpose: the counters outlive both.
* ``ix_usage_events_key_last_used``: serves a key's "last used" lookup.

There is no backfill. ``downgrade()`` refuses while ``agent_call_days`` holds
a row - dropping it would lose the agent-activity history.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a7c3e91f5b20"
down_revision: Union[str, Sequence[str], None] = "e46b50366a4c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DAY = "day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'"
_SOURCE = "source IN ('mcp', 'agent')"
_KIND = "length(kind) BETWEEN 1 AND 64"
_CALLS = "calls > 0"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("scan_runs", sa.Column("queued_at", sa.Text(), nullable=True))
    op.add_column("scan_runs", sa.Column("cancelled_by", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_scan_runs_cancelled_by",
        "scan_runs",
        "users",
        ["cancelled_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "memberships",
        sa.Column(
            "welcome_pending", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.alter_column("memberships", "welcome_pending", server_default=None)

    op.create_table(
        "agent_call_days",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("connection_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("calls", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(_DAY, name="ck_agent_call_days_day"),
        sa.CheckConstraint(_SOURCE, name="ck_agent_call_days_source"),
        sa.CheckConstraint(_KIND, name="ck_agent_call_days_kind"),
        sa.CheckConstraint(_CALLS, name="ck_agent_call_days_calls"),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_agent_call_days_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "project_id"],
            ["projects.org_id", "projects.id"],
            name="fk_agent_call_days_project",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_call_days"),
        sa.UniqueConstraint(
            "project_id",
            "day",
            "source",
            "user_id",
            "connection_id",
            "kind",
            name="uq_agent_call_days_key",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_agent_call_days_org_day",
        "agent_call_days",
        ["org_id", "day"],
        unique=False,
    )
    op.create_index(
        "ix_usage_events_key_last_used",
        "usage_events",
        ["org_id", "provider", "key_scope", "project_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema (refused while agent activity counters exist)."""
    count = (
        op.get_bind().execute(sa.text("SELECT count(*) FROM agent_call_days")).scalar()
    )
    if count:
        raise RuntimeError(
            f"cannot downgrade below the ux revision: the database holds "
            f"{count} agent call counter rows, which the previous schema "
            f"cannot represent"
        )

    op.drop_index("ix_usage_events_key_last_used", table_name="usage_events")
    op.drop_table("agent_call_days")
    op.drop_column("memberships", "welcome_pending")
    op.drop_constraint("fk_scan_runs_cancelled_by", "scan_runs", type_="foreignkey")
    op.drop_column("scan_runs", "cancelled_by")
    op.drop_column("scan_runs", "queued_at")
