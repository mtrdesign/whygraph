"""github members

Revision ID: b7d2c9a41e63
Revises: e3a055b4530a
Create Date: 2026-10-04 12:00:00

M2d-1: GitHub sign-in. ``users`` gains ``github_id`` (the identity; unique),
``github_login`` (display and lookup; unique case-insensitively through a
functional index on ``lower(github_login)``), ``avatar_url`` and
``disabled_at``. Two check constraints: a user holds a password **or** a
GitHub id, never both, and a login needs an id.

Both constraints hold for every existing row (all have ``github_id`` NULL);
there is no backfill.

``downgrade()`` refuses when ``settings.mode`` is ``production``: the
previous schema cannot represent a GitHub account, so it cannot run one. In
local mode it drops the constraints, the index and the columns.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b7d2c9a41e63"
down_revision: Union[str, Sequence[str], None] = "e3a055b4530a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("users", sa.Column("github_id", sa.BigInteger(), nullable=True))
    op.add_column("users", sa.Column("github_login", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("avatar_url", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("disabled_at", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_users_github_id", "users", ["github_id"])
    op.create_index(
        "uq_users_github_login_lower",
        "users",
        [sa.text("lower(github_login)")],
        unique=True,
    )
    op.create_check_constraint(
        "ck_users_one_credential",
        "users",
        "NOT (password_hash IS NOT NULL AND github_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_users_login_needs_id",
        "users",
        "github_login IS NULL OR github_id IS NOT NULL",
    )


def downgrade() -> None:
    """Downgrade schema (refused in production mode)."""
    bind = op.get_bind()
    mode = bind.execute(sa.text("SELECT mode FROM settings WHERE id = 1")).scalar()
    if mode == "production":
        raise RuntimeError(
            "cannot downgrade below the github members revision: this portal runs "
            "in production mode, and the previous schema has no GitHub accounts"
        )

    op.drop_constraint("ck_users_login_needs_id", "users", type_="check")
    op.drop_constraint("ck_users_one_credential", "users", type_="check")
    op.drop_index("uq_users_github_login_lower", table_name="users")
    op.drop_constraint("uq_users_github_id", "users", type_="unique")
    op.drop_column("users", "disabled_at")
    op.drop_column("users", "avatar_url")
    op.drop_column("users", "github_login")
    op.drop_column("users", "github_id")
