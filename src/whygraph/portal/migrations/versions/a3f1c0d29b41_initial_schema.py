"""initial portal schema

Revision ID: a3f1c0d29b41
Revises:
Create Date: 2026-09-30 03:00:49.565592

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a3f1c0d29b41"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=True),
        sa.CheckConstraint("mode IN ('local', 'production')", name="ck_settings_mode"),
        sa.CheckConstraint("id = 1", name="ck_settings_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uid", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uid", name="uq_users_uid"),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("root", sa.Text(), nullable=False),
        sa.Column("remote_url", sa.Text(), nullable=True),
        sa.Column("initialized_at", sa.Text(), nullable=True),
        sa.Column("last_scan_at", sa.Text(), nullable=True),
        sa.Column("last_scanned_head", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("source IN ('local', 'github')", name="ck_projects_source"),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_projects_created_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("root", name="uq_projects_root"),
        sa.UniqueConstraint("slug", name="uq_projects_slug"),
    )
    op.create_table(
        "project_agents",
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("agent", sa.Text(), nullable=False),
        sa.Column("configured_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "agent IN ('claude', 'cursor', 'vscode', 'codex')",
            name="ck_project_agents_agent",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_project_agents_project",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("project_id", "agent", name="pk_project_agents"),
    )
    op.create_table(
        "project_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_project_config_project",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("project_config", schema=None) as batch_op:
        batch_op.create_index(
            "uq_project_config_project",
            ["project_id"],
            unique=True,
            sqlite_where=sa.text("project_id IS NOT NULL"),
        )

    # Expression-based partial indexes: Alembic cannot autogenerate or compare
    # these on SQLite, so they are written by hand. `ON ((1)) WHERE project_id
    # IS NULL` keeps a single global row (NULLs are distinct in a UNIQUE).
    op.create_index(
        "uq_project_config_global",
        "project_config",
        [sa.text("(1)")],
        unique=True,
        sqlite_where=sa.text("project_id IS NULL"),
    )

    op.create_table(
        "scan_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("analyze", sa.Boolean(), nullable=False),
        sa.Column("requested_by", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.Column("events_path", sa.Text(), nullable=True),
        sa.Column("log_path", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('scan', 'sync')", name="ck_scan_runs_kind"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'ok', 'failed', 'interrupted', 'cancelled')",
            name="ck_scan_runs_status",
        ),
        sa.CheckConstraint(
            "trigger IN ('initial', 'manual', 'describe', 'hook', 'poll', 'sync')",
            name="ck_scan_runs_trigger",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_scan_runs_project",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name="fk_scan_runs_requested_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("scan_runs", schema=None) as batch_op:
        batch_op.create_index("ix_scan_runs_project_id", ["project_id"], unique=False)

    op.create_table(
        "secrets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("hint", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "(kind = 'llm_api_key') = (provider IS NOT NULL)",
            name="ck_secrets_provider",
        ),
        sa.CheckConstraint(
            "kind IN ('llm_api_key', 'github_token')", name="ck_secrets_kind"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_secrets_project",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # `provider` is NULL for a GitHub token, so the uniqueness runs over
    # coalesce(provider, '') - one index per scope.
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


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_secrets_global", table_name="secrets")
    op.drop_index("uq_secrets_project", table_name="secrets")
    op.drop_table("secrets")
    with op.batch_alter_table("scan_runs", schema=None) as batch_op:
        batch_op.drop_index("ix_scan_runs_project_id")

    op.drop_table("scan_runs")
    op.drop_index("uq_project_config_global", table_name="project_config")
    with op.batch_alter_table("project_config", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_project_config_project", sqlite_where=sa.text("project_id IS NOT NULL")
        )

    op.drop_table("project_config")
    op.drop_table("project_agents")
    op.drop_table("projects")
    op.drop_table("users")
    op.drop_table("settings")
