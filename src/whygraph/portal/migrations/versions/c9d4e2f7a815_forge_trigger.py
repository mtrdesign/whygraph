"""forge trigger

Revision ID: c9d4e2f7a815
Revises: f2b8d4a60c13
Create Date: 2026-10-29 12:00:00

A new scan trigger, ``forge``: a GitHub-only run (``--github-only``) of a
production GitHub project, which refreshes the pull requests, issues,
milestones and tags but neither the commits nor the code index.
``ck_scan_runs_trigger`` is dropped and re-created.

``downgrade()`` refuses while a ``forge`` run exists: the previous
constraint cannot represent it.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c9d4e2f7a815"
down_revision: Union[str, Sequence[str], None] = "f2b8d4a60c13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_TRIGGERS = (
    "trigger IN ('initial', 'manual', 'describe', 'hook', 'poll', 'sync', "
    "'push', 'reconcile')"
)
_NEW_TRIGGERS = (
    "trigger IN ('initial', 'manual', 'describe', 'hook', 'poll', 'sync', "
    "'push', 'reconcile', 'forge')"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint("ck_scan_runs_trigger", "scan_runs", type_="check")
    op.create_check_constraint("ck_scan_runs_trigger", "scan_runs", _NEW_TRIGGERS)


def downgrade() -> None:
    """Downgrade schema (refused while forge runs exist)."""
    count = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM scan_runs WHERE trigger = 'forge'"))
        .scalar()
    )
    if count:
        raise RuntimeError(
            f"cannot downgrade below the forge trigger revision: the database "
            f"holds {count} forge scan runs, which the previous schema cannot "
            f"represent"
        )
    op.drop_constraint("ck_scan_runs_trigger", "scan_runs", type_="check")
    op.create_check_constraint("ck_scan_runs_trigger", "scan_runs", _OLD_TRIGGERS)
