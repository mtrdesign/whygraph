"""identity

Revision ID: e3a055b4530a
Revises: 4ebfd8b89904
Create Date: 2026-10-03 12:00:00

M2c: sign-in for production mode. ``users`` gains ``email`` (the login name,
stored lowercased; a plain unique, so local mode's email-less user and any
number of ``NULL`` emails fit), ``is_instance_admin`` and
``password_changed_at``, and loses the never-read ``username``. Two new
tables: ``sessions`` (only the SHA-256 of the cookie token is stored) and
``password_resets`` (admin-issued one-time links).

Existing rows keep their data: every user gets ``is_instance_admin = false``
and a ``NULL`` email.

``downgrade()`` refuses when ``settings.mode`` is ``production``: the
previous schema has no way to sign a user in, so it cannot run one. In local
mode it drops the new tables and columns and re-adds ``username`` as an empty
nullable column.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e3a055b4530a"
down_revision: Union[str, Sequence[str], None] = "4ebfd8b89904"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("users", sa.Column("email", sa.Text(), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "is_instance_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column(
        "users", "is_instance_admin", existing_type=sa.Boolean(), server_default=None
    )
    op.add_column("users", sa.Column("password_changed_at", sa.Text(), nullable=True))
    op.drop_column("users", "username")
    op.create_unique_constraint("uq_users_email", "users", ["email"])
    op.create_check_constraint("ck_users_email_lower", "users", "email = lower(email)")

    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_sessions_user", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)

    op.create_table(
        "password_resets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("used_at", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_password_resets_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_password_resets_created_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_password_resets"),
        sa.UniqueConstraint("token_hash", name="uq_password_resets_token_hash"),
    )
    op.create_index(
        "ix_password_resets_user_id", "password_resets", ["user_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema (refused in production mode)."""
    bind = op.get_bind()
    mode = bind.execute(sa.text("SELECT mode FROM settings WHERE id = 1")).scalar()
    if mode == "production":
        raise RuntimeError(
            "cannot downgrade below the identity revision: this portal runs in "
            "production mode, and the previous schema has no sign-in"
        )

    op.drop_index("ix_password_resets_user_id", table_name="password_resets")
    op.drop_table("password_resets")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")

    op.drop_constraint("ck_users_email_lower", "users", type_="check")
    op.drop_constraint("uq_users_email", "users", type_="unique")
    op.add_column("users", sa.Column("username", sa.Text(), nullable=True))
    op.drop_column("users", "password_changed_at")
    op.drop_column("users", "is_instance_admin")
    op.drop_column("users", "email")
