"""chat tool counter

Revision ID: f2b8d4a60c13
Revises: a7c3e91f5b20
Create Date: 2026-10-28 12:00:00

The Chat assistant's own tool calls are counted in ``agent_call_days`` with
``source = 'chat'`` (names and counts only, no member). The source CHECK
widens from ``('mcp', 'agent')`` to ``('mcp', 'agent', 'chat')``.

``downgrade()`` refuses while a ``chat`` row exists: the previous constraint
cannot represent it.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f2b8d4a60c13"
down_revision: Union[str, Sequence[str], None] = "a7c3e91f5b20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_SOURCE = "source IN ('mcp', 'agent')"
_NEW_SOURCE = "source IN ('mcp', 'agent', 'chat')"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint("ck_agent_call_days_source", "agent_call_days", type_="check")
    op.create_check_constraint(
        "ck_agent_call_days_source", "agent_call_days", _NEW_SOURCE
    )


def downgrade() -> None:
    """Downgrade schema (refused while chat tool counters exist)."""
    count = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM agent_call_days WHERE source = 'chat'"))
        .scalar()
    )
    if count:
        raise RuntimeError(
            f"cannot downgrade below the chat tool counter revision: the "
            f"database holds {count} chat tool counter rows, which the "
            f"previous schema cannot represent"
        )
    op.drop_constraint("ck_agent_call_days_source", "agent_call_days", type_="check")
    op.create_check_constraint(
        "ck_agent_call_days_source", "agent_call_days", _OLD_SOURCE
    )
