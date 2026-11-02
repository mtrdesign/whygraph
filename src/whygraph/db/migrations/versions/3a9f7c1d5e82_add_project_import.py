"""add project_import, imported_actor, imported_chat

Revision ID: 3a9f7c1d5e82
Revises: 71460539b46f
Create Date: 2026-11-01 12:00:00.000000

Adds the three project-transfer tables: ``project_import`` (a log of applied
imports), ``imported_actor`` (display names for ``imported:<uid>`` actors)
and ``imported_chat`` (origin key to imported session), plus
``rationale_cache.import_id``, a plain nullable integer with no foreign key
(like ``chat_message.parent_id``). No data migration.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "3a9f7c1d5e82"
down_revision: Union[str, Sequence[str], None] = "71460539b46f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "project_import",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bundle_sha256", sa.Text(), nullable=False),
        sa.Column("imported_by", sa.Text(), nullable=False),
        sa.Column("imported_at", sa.Text(), nullable=False),
        sa.Column("source_mode", sa.Text(), nullable=False),
        sa.Column("source_label", sa.Text(), nullable=False),
        sa.Column("whygraph_version", sa.Text(), nullable=False),
        sa.Column("counts", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_project_import_bundle_sha256"),
        "project_import",
        ["bundle_sha256"],
        unique=False,
    )
    op.create_table(
        "imported_actor",
        sa.Column("uid", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("import_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["import_id"], ["project_import.id"]),
        sa.PrimaryKeyConstraint("uid"),
    )
    op.create_table(
        "imported_chat",
        sa.Column("origin_key", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("import_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["import_id"], ["project_import.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["chat_session.id"]),
        sa.PrimaryKeyConstraint("origin_key"),
    )
    with op.batch_alter_table("rationale_cache", schema=None) as batch_op:
        batch_op.add_column(sa.Column("import_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("rationale_cache", schema=None) as batch_op:
        batch_op.drop_column("import_id")
    op.drop_table("imported_chat")
    op.drop_table("imported_actor")
    op.drop_index(op.f("ix_project_import_bundle_sha256"), table_name="project_import")
    op.drop_table("project_import")
