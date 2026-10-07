"""remove claude-cli

Revision ID: bd0a25e6df74
Revises: d8a31f6c07e5
Create Date: 2026-10-07 12:00:00

The ``claude-cli`` provider is removed: it ran ``claude --print`` on a Claude
subscription token that the portal stored, and Anthropic does not allow
third-party tools to store subscription credentials or route requests
through them.

* Every ``claude_oauth_token`` secret and every ``llm_api_key`` filed under
  the ``claude-cli`` provider is deleted.
* ``ck_secrets_kind`` loses ``'claude_oauth_token'``: dropped and re-created
  from literal strings.
* Every config layer (``project_config.config``) loses its references to the
  provider - the ``[llm.claude_cli]`` table, an ``[llm].model`` naming it,
  and a task's ``provider`` (with that task's ``model``) or a ``model``
  prefixed with it - so the task falls back to the org default or the
  built-in provider. Config loading drops the same references
  (``whygraph.core.config.normalize_v2``); this keeps the stored rows, and
  so the settings forms, consistent with what runs.

``downgrade()`` restores the wider check; the deleted secrets and settings
are not restored.
"""

import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "bd0a25e6df74"
down_revision: Union[str, Sequence[str], None] = "d8a31f6c07e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_SECRET_KINDS = "kind IN ('llm_api_key', 'github_token', 'claude_oauth_token')"
_NEW_SECRET_KINDS = "kind IN ('llm_api_key', 'github_token')"
_REMOVED = ("claude-cli", "claude_cli")
_TASKS = ("analyze", "rationale", "chat")

_log = logging.getLogger("alembic.runtime.migration")


def _names_removed(value: object) -> bool:
    """Whether ``value`` is a removed provider tag or ``"<removed>/<model>"``."""
    if not isinstance(value, str):
        return False
    return value in _REMOVED or value.partition("/")[0] in _REMOVED


def _strip(config: dict) -> bool:
    """Drop the removed provider's references from one layer in place; changed?"""
    changed = False
    llm = config.get("llm")
    if isinstance(llm, dict):
        for tag in _REMOVED:
            if tag in llm:
                del llm[tag]
                changed = True
        if _names_removed(llm.get("model")):
            del llm["model"]
            changed = True
    for task in _TASKS:
        table = config.get(task)
        if not isinstance(table, dict):
            continue
        if _names_removed(table.get("provider")):
            del table["provider"]
            table.pop("model", None)
            changed = True
        elif table.get("provider") is None and _names_removed(table.get("model")):
            del table["model"]
            changed = True
    return changed


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    deleted = bind.execute(
        sa.text(
            "DELETE FROM secrets WHERE kind = 'claude_oauth_token' "
            "OR (kind = 'llm_api_key' AND provider IN ('claude-cli', 'claude_cli'))"
        )
    ).rowcount
    if deleted:
        _log.warning("removed %d claude-cli secret(s)", deleted)
    op.drop_constraint("ck_secrets_kind", "secrets", type_="check")
    op.create_check_constraint("ck_secrets_kind", "secrets", _NEW_SECRET_KINDS)

    layers = sa.table(
        "project_config",
        sa.column("id", sa.Integer()),
        sa.column("config", sa.JSON()),
    )
    rows = bind.execute(sa.select(layers.c.id, layers.c.config)).all()
    for row_id, config in rows:
        if isinstance(config, dict) and _strip(config):
            bind.execute(
                layers.update().where(layers.c.id == row_id).values(config=config)
            )
            _log.warning("removed claude-cli settings from config layer %d", row_id)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("ck_secrets_kind", "secrets", type_="check")
    op.create_check_constraint("ck_secrets_kind", "secrets", _OLD_SECRET_KINDS)
