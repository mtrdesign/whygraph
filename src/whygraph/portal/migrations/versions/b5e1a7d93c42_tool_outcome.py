"""tool outcome

Revision ID: b5e1a7d93c42
Revises: c9d4e2f7a815
Create Date: 2026-10-30 12:00:00

``agent_call_days`` gains three counters per row: ``errors`` (calls whose tool
reported a failure), ``internal_errors`` (calls where an exception escaped) and
``duration_ms`` (the sum of the timed calls' durations). Existing rows are
backfilled with 0; the server default is dropped after the backfill so the
models need not declare one.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b5e1a7d93c42"
down_revision: Union[str, Sequence[str], None] = "c9d4e2f7a815"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = ("errors", "internal_errors", "duration_ms")
_CHECK = (
    "errors >= 0 AND internal_errors >= 0 "
    "AND errors + internal_errors <= calls AND duration_ms >= 0"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "agent_call_days",
        sa.Column("errors", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "agent_call_days",
        sa.Column("internal_errors", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "agent_call_days",
        sa.Column("duration_ms", sa.BigInteger(), nullable=False, server_default="0"),
    )
    for column in _COLUMNS:
        op.alter_column("agent_call_days", column, server_default=None)
    op.create_check_constraint("ck_agent_call_days_outcomes", "agent_call_days", _CHECK)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("ck_agent_call_days_outcomes", "agent_call_days", type_="check")
    for column in reversed(_COLUMNS):
        op.drop_column("agent_call_days", column)
