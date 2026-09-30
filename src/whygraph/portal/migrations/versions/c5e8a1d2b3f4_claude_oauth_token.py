"""secrets: allow the claude_oauth_token kind

Revision ID: c5e8a1d2b3f4
Revises: a3f1c0d29b41
Create Date: 2026-09-30 19:00:00.000000

A Claude subscription token (``claude setup-token``) is its own secret kind:
it is not an API key (it bills the subscription), and it has no provider.
SQLite cannot alter a CHECK constraint in place, so the table is rebuilt
(batch mode); the two partial unique indexes are recreated after it.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c5e8a1d2b3f4"
down_revision: Union[str, Sequence[str], None] = "a3f1c0d29b41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "kind IN ('llm_api_key', 'github_token')"
_NEW = "kind IN ('llm_api_key', 'github_token', 'claude_oauth_token')"


def _rebuild(check: str) -> None:
    op.drop_index("uq_secrets_project", table_name="secrets")
    op.drop_index("uq_secrets_global", table_name="secrets")
    with op.batch_alter_table("secrets", recreate="always") as batch:
        batch.drop_constraint("ck_secrets_kind", type_="check")
        batch.create_check_constraint("ck_secrets_kind", check)
    op.create_index(
        "uq_secrets_project",
        "secrets",
        ["project_id", "kind", sa.text("coalesce(provider, '')")],
        unique=True,
        sqlite_where=sa.text("project_id IS NOT NULL"),
    )
    op.create_index(
        "uq_secrets_global",
        "secrets",
        ["kind", sa.text("coalesce(provider, '')")],
        unique=True,
        sqlite_where=sa.text("project_id IS NULL"),
    )


def upgrade() -> None:
    """Upgrade schema."""
    _rebuild(_NEW)


def downgrade() -> None:
    """Downgrade schema (drops any claude_oauth_token rows first)."""
    op.execute("DELETE FROM secrets WHERE kind = 'claude_oauth_token'")
    _rebuild(_OLD)
