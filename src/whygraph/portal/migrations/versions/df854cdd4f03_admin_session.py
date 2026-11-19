"""admin session

Revision ID: df854cdd4f03
Revises: 496953df26b4
Create Date: 2026-11-18 12:00:00

``sessions`` gains ``kind`` (``user`` / ``admin``, plan 59 D2) and
``paired_session_id``: the everyday session an admin session was started
beside (D51; ``ON DELETE SET NULL``, so a sweep never ends an admin session).
Existing rows are backfilled as ``user`` sessions; the server default is
dropped afterwards so the model need not declare one.

Password accounts become admin-only (D4): their everyday sessions are
deleted and their live connection tokens revoked (``admin_revoked``).
"""

from datetime import UTC, datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "df854cdd4f03"
down_revision: Union[str, Sequence[str], None] = "496953df26b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "sessions",
        sa.Column("kind", sa.Text(), nullable=False, server_default="user"),
    )
    op.alter_column("sessions", "kind", server_default=None)
    op.create_check_constraint(
        "ck_sessions_kind", "sessions", "kind IN ('user', 'admin')"
    )
    # D51: the everyday session an admin session was started beside (NULL when
    # there was none).
    op.add_column(
        "sessions", sa.Column("paired_session_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "fk_sessions_paired",
        "sessions",
        "sessions",
        ["paired_session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Postgres does not index a referencing column, and every DELETE on
    # sessions (the sweep, revoke) must find the rows that point at the
    # deleted one.
    op.create_index("ix_sessions_paired_session_id", "sessions", ["paired_session_id"])
    # D4: password accounts are admin-only from now on - end their everyday
    # sessions ...
    op.execute(
        "DELETE FROM sessions WHERE kind = 'user' AND user_id IN "
        "(SELECT id FROM users WHERE password_hash IS NOT NULL)"
    )
    # ... and their connection tokens (connections.lookup never looked at
    # password_hash).
    op.execute(
        sa.text(
            "UPDATE connection_tokens SET revoked_at = :now, "
            "revoked_reason = 'admin_revoked' "
            "WHERE revoked_at IS NULL AND user_id IN "
            "(SELECT id FROM users WHERE password_hash IS NOT NULL)"
        ).bindparams(
            # the format models._now() writes
            now=datetime.now(UTC).isoformat(timespec="seconds")
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    # An admin row must never become a user row.
    op.execute("DELETE FROM sessions WHERE kind = 'admin'")
    op.drop_index("ix_sessions_paired_session_id", table_name="sessions")
    op.drop_constraint("fk_sessions_paired", "sessions", type_="foreignkey")
    op.drop_column("sessions", "paired_session_id")
    op.drop_constraint("ck_sessions_kind", "sessions", type_="check")
    op.drop_column("sessions", "kind")
    # Revoked tokens and deleted sessions are not brought back.
