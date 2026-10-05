"""github projects

Revision ID: 73bf248ea6fd
Revises: b7d2c9a41e63
Create Date: 2026-10-04 18:00:00

M2d-2: production projects through the WhyGraph GitHub App. ``projects``
gains ``github_repo_id`` (GitHub's numeric repository id - the identity of
an imported repo; ``BIGINT``, ids pass 2^31), ``github_installation_id``,
``default_branch``, ``access_lost_at`` and ``access_lost_reason``. A partial
unique index ``uq_projects_org_github_repo (org_id, github_repo_id) WHERE
github_repo_id IS NOT NULL`` refuses a duplicate import per org, and a plain
index on ``github_repo_id`` serves the webhook's lookups across orgs.

A new table ``retired_org_slugs`` records the slugs of deleted
organizations, which are never handed out again.

Two new scan triggers, ``push`` (a webhook) and ``reconcile`` (the hourly
``ls-remote`` check): ``ck_scan_runs_trigger`` is dropped and re-created.

Every existing row fits: the new columns are nullable and there is no
backfill (a local ``github`` row keeps a ``NULL`` repo id).

``downgrade()`` refuses while the database holds anything the previous
schema cannot represent - an imported project (a ``github_repo_id``), a
retired slug, or a ``push`` / ``reconcile`` run. Otherwise it drops the
table, the indexes and the columns and restores the old trigger check.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "73bf248ea6fd"
down_revision: Union[str, Sequence[str], None] = "b7d2c9a41e63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_TRIGGERS = "trigger IN ('initial', 'manual', 'describe', 'hook', 'poll', 'sync')"
_NEW_TRIGGERS = (
    "trigger IN ('initial', 'manual', 'describe', 'hook', 'poll', 'sync', "
    "'push', 'reconcile')"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "projects", sa.Column("github_repo_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "projects", sa.Column("github_installation_id", sa.BigInteger(), nullable=True)
    )
    op.add_column("projects", sa.Column("default_branch", sa.Text(), nullable=True))
    op.add_column("projects", sa.Column("access_lost_at", sa.Text(), nullable=True))
    op.add_column("projects", sa.Column("access_lost_reason", sa.Text(), nullable=True))
    op.create_index(
        "uq_projects_org_github_repo",
        "projects",
        ["org_id", "github_repo_id"],
        unique=True,
        postgresql_where=sa.text("github_repo_id IS NOT NULL"),
    )
    op.create_index(
        "ix_projects_github_repo_id", "projects", ["github_repo_id"], unique=False
    )

    op.create_table(
        "retired_org_slugs",
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("retired_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("slug", name="pk_retired_org_slugs"),
    )

    op.drop_constraint("ck_scan_runs_trigger", "scan_runs", type_="check")
    op.create_check_constraint("ck_scan_runs_trigger", "scan_runs", _NEW_TRIGGERS)


def downgrade() -> None:
    """Downgrade schema (refused while M2d-2 data exists)."""
    bind = op.get_bind()
    blockers = {
        "imported GitHub projects": "SELECT count(*) FROM projects "
        "WHERE github_repo_id IS NOT NULL",
        "retired organization slugs": "SELECT count(*) FROM retired_org_slugs",
        "push / reconcile scan runs": "SELECT count(*) FROM scan_runs "
        "WHERE trigger IN ('push', 'reconcile')",
    }
    for what, sql in blockers.items():
        count = bind.execute(sa.text(sql)).scalar()
        if count:
            raise RuntimeError(
                f"cannot downgrade below the github projects revision: the database "
                f"holds {count} {what}, which the previous schema cannot represent"
            )

    op.drop_constraint("ck_scan_runs_trigger", "scan_runs", type_="check")
    op.create_check_constraint("ck_scan_runs_trigger", "scan_runs", _OLD_TRIGGERS)
    op.drop_table("retired_org_slugs")
    op.drop_index("ix_projects_github_repo_id", table_name="projects")
    op.drop_index("uq_projects_org_github_repo", table_name="projects")
    op.drop_column("projects", "access_lost_reason")
    op.drop_column("projects", "access_lost_at")
    op.drop_column("projects", "default_branch")
    op.drop_column("projects", "github_installation_id")
    op.drop_column("projects", "github_repo_id")
