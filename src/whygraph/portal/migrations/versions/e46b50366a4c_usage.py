"""usage

Revision ID: e46b50366a4c
Revises: bd0a25e6df74
Create Date: 2026-10-07 18:00:00

M2f-2: usage and cost. Four new tables, no change to an existing one:

* ``usage_events``: the usage ledger - one row per successful LLM provider
  round trip, with the tokens the provider reported, the served model, the
  key scope, the cost (provider-reported, estimated or unpriced) and the
  attribution (a member or the System actor, with label snapshots). The
  project, user, scan run and connection foreign keys are ``SET NULL`` so a
  row outlives them; the org's is ``CASCADE`` (deleting an org removes its
  usage history).
* ``budgets``: monthly budgets for the org, a project, the org-wide member
  default and per-member overrides. One row per target
  (``uq_budgets_target``, ``UNIQUE NULLS NOT DISTINCT``); composite foreign
  keys tie a project budget to the project and a member override to the
  membership.
* ``budget_alerts``: each threshold (50 / 75 / 100) crossed once per budget
  (and member, for the member default) per month
  (``uq_budget_alerts_once``, ``UNIQUE NULLS NOT DISTINCT``).
* ``price_overrides``: per-org model prices on top of the bundled table.

Money columns are ``NUMERIC``. There is no backfill.

``downgrade()`` refuses while any of the four tables holds a row - dropping
them silently would lose the org's spend history and budgets.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e46b50366a4c"
down_revision: Union[str, Sequence[str], None] = "bd0a25e6df74"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTOR_KIND = "actor_kind IN ('member', 'system')"
_SOURCE = "source IN ('scan', 'explorer', 'chat', 'mcp', 'agent')"
_TASK = "task IN ('analyze', 'rationale', 'chat')"
_KEY_SCOPE = "key_scope IN ('project', 'org', 'environment', 'none')"
_COST_SOURCE = "cost_source IN ('provider', 'estimated', 'unpriced')"
_ACTOR = "actor_kind <> 'system' OR user_id IS NULL"
_BUDGET_SCOPE = "scope IN ('org', 'project', 'member_default', 'member')"
_BUDGET_AMOUNT = "monthly_usd > 0 AND monthly_usd <= 1000000"
_BUDGET_SHAPE = (
    "(project_id IS NOT NULL) = (scope = 'project') "
    "AND (user_id IS NOT NULL) = (scope = 'member')"
)
_THRESHOLD = "threshold IN (50, 75, 100)"
_NONNEG = (
    "input_per_mtok >= 0 AND output_per_mtok >= 0 "
    "AND (cache_read_per_mtok IS NULL OR cache_read_per_mtok >= 0) "
    "AND (cache_write_per_mtok IS NULL OR cache_write_per_mtok >= 0)"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "usage_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("project_slug", sa.Text(), nullable=False),
        sa.Column("project_name", sa.Text(), nullable=False),
        sa.Column("actor_kind", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("actor_label", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("scan_run_id", sa.Integer(), nullable=True),
        sa.Column("chat_session_id", sa.Integer(), nullable=True),
        sa.Column("connection_id", sa.BigInteger(), nullable=True),
        sa.Column("client_name", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model_requested", sa.Text(), nullable=False),
        sa.Column("model_served", sa.Text(), nullable=True),
        sa.Column("key_scope", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=True),
        sa.Column("reasoning_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("cost_source", sa.Text(), nullable=False),
        sa.Column("price_version", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(_ACTOR_KIND, name="ck_usage_events_actor_kind"),
        sa.CheckConstraint(_SOURCE, name="ck_usage_events_source"),
        sa.CheckConstraint(_TASK, name="ck_usage_events_task"),
        sa.CheckConstraint(_KEY_SCOPE, name="ck_usage_events_key_scope"),
        sa.CheckConstraint(_COST_SOURCE, name="ck_usage_events_cost_source"),
        sa.CheckConstraint(_ACTOR, name="ck_usage_events_actor"),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_usage_events_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_usage_events_project",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_usage_events_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["scan_run_id"],
            ["scan_runs.id"],
            name="fk_usage_events_scan_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connection_tokens.id"],
            name="fk_usage_events_connection",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_usage_events"),
    )
    op.create_index(
        "ix_usage_events_org_time",
        "usage_events",
        ["org_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_usage_events_org_user_time",
        "usage_events",
        ["org_id", "user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_usage_events_org_project_time",
        "usage_events",
        ["org_id", "project_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_usage_events_project", "usage_events", ["project_id"], unique=False
    )
    op.create_index("ix_usage_events_user", "usage_events", ["user_id"], unique=False)
    op.create_index(
        "ix_usage_events_scan_run",
        "usage_events",
        ["scan_run_id"],
        unique=False,
        postgresql_where=sa.text("scan_run_id IS NOT NULL"),
    )
    op.create_index(
        "ix_usage_events_connection",
        "usage_events",
        ["connection_id"],
        unique=False,
        postgresql_where=sa.text("connection_id IS NOT NULL"),
    )

    op.create_table(
        "budgets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("monthly_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("hard_stop", sa.Boolean(), nullable=False),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(_BUDGET_SCOPE, name="ck_budgets_scope"),
        sa.CheckConstraint(_BUDGET_AMOUNT, name="ck_budgets_amount"),
        sa.CheckConstraint(_BUDGET_SHAPE, name="ck_budgets_shape"),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_budgets_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "project_id"],
            ["projects.org_id", "projects.id"],
            name="fk_budgets_project",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["memberships.org_id", "memberships.user_id"],
            name="fk_budgets_membership",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name="fk_budgets_updated_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_budgets"),
        sa.UniqueConstraint(
            "org_id",
            "scope",
            "project_id",
            "user_id",
            name="uq_budgets_target",
            postgresql_nulls_not_distinct=True,
        ),
    )

    op.create_table(
        "budget_alerts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("budget_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("month", sa.Text(), nullable=False),
        sa.Column("threshold", sa.SmallInteger(), nullable=False),
        sa.Column("crossed_at", sa.Text(), nullable=False),
        sa.Column("spent_usd", sa.Numeric(14, 6), nullable=False),
        sa.CheckConstraint(_THRESHOLD, name="ck_budget_alerts_threshold"),
        sa.ForeignKeyConstraint(
            ["budget_id"],
            ["budgets.id"],
            name="fk_budget_alerts_budget",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_budget_alerts_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_budget_alerts"),
        sa.UniqueConstraint(
            "budget_id",
            "user_id",
            "month",
            "threshold",
            name="uq_budget_alerts_once",
            postgresql_nulls_not_distinct=True,
        ),
    )

    op.create_table(
        "price_overrides",
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_per_mtok", sa.Numeric(12, 6), nullable=False),
        sa.Column("output_per_mtok", sa.Numeric(12, 6), nullable=False),
        sa.Column("cache_read_per_mtok", sa.Numeric(12, 6), nullable=True),
        sa.Column("cache_write_per_mtok", sa.Numeric(12, 6), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(_NONNEG, name="ck_price_overrides_nonneg"),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_price_overrides_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name="fk_price_overrides_updated_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint(
            "org_id", "provider", "model", name="pk_price_overrides"
        ),
    )


def downgrade() -> None:
    """Downgrade schema (refused while M2f-2 data exists)."""
    bind = op.get_bind()
    blockers = {
        "usage events": "SELECT count(*) FROM usage_events",
        "budgets": "SELECT count(*) FROM budgets",
        "budget alerts": "SELECT count(*) FROM budget_alerts",
        "price overrides": "SELECT count(*) FROM price_overrides",
    }
    for what, sql in blockers.items():
        count = bind.execute(sa.text(sql)).scalar()
        if count:
            raise RuntimeError(
                f"cannot downgrade below the usage revision: the database "
                f"holds {count} {what}, which the previous schema cannot represent"
            )

    op.drop_table("price_overrides")
    op.drop_table("budget_alerts")
    op.drop_table("budgets")
    op.drop_index("ix_usage_events_connection", table_name="usage_events")
    op.drop_index("ix_usage_events_scan_run", table_name="usage_events")
    op.drop_index("ix_usage_events_user", table_name="usage_events")
    op.drop_index("ix_usage_events_project", table_name="usage_events")
    op.drop_index("ix_usage_events_org_project_time", table_name="usage_events")
    op.drop_index("ix_usage_events_org_user_time", table_name="usage_events")
    op.drop_index("ix_usage_events_org_time", table_name="usage_events")
    op.drop_table("usage_events")
