"""postgres baseline

Revision ID: 86e839432e62
Revises:
Create Date: 2026-10-01 17:38:26.293883

The single Postgres baseline of the portal chain (2.0). It replaces the two
SQLite revisions the portal had before it (``a3f1c0d29b41``, ``c5e8a1d2b3f4``), which no
Postgres database ever ran.

The two "one global row + one per project" rules are ``UNIQUE ... NULLS NOT
DISTINCT`` constraints (``uq_project_config_scope``, ``uq_secrets_scope``),
which replace the four SQLite partial indexes. This revision inserts no rows:
``settings`` is written by the portal's startup, after the import.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "86e839432e62"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "legacy_import",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.Text(), nullable=False),
        sa.Column("rows", sa.JSON(), nullable=False),
        sa.Column("imported_at", sa.Text(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_legacy_import_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
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
        sa.UniqueConstraint(
            "project_id",
            name="uq_project_config_scope",
            postgresql_nulls_not_distinct=True,
        ),
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
    op.create_index(
        "ix_scan_runs_project_id", "scan_runs", ["project_id"], unique=False
    )
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
            "kind IN ('llm_api_key', 'github_token', 'claude_oauth_token')",
            name="ck_secrets_kind",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_secrets_project",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "kind",
            "provider",
            name="uq_secrets_scope",
            postgresql_nulls_not_distinct=True,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("secrets")
    op.drop_index("ix_scan_runs_project_id", table_name="scan_runs")
    op.drop_table("scan_runs")
    op.drop_table("project_config")
    op.drop_table("project_agents")
    op.drop_table("projects")
    op.drop_table("users")
    op.drop_table("settings")
    op.drop_table("legacy_import")
