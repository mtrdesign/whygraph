"""Alembic environment for the portal database.

A separate chain from the per-project one in ``whygraph/db/migrations``:
its ``target_metadata`` is :attr:`whygraph.portal.models.PortalBase.metadata`
(never ``SQLModel.metadata``) and its URL comes from
:func:`whygraph.portal.db.get_engine`, i.e. ``<data dir>/portal.db``.
The file holds only portal tables, so no ``include_object`` filter is
needed.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from whygraph.portal.db import get_engine
from whygraph.portal.models import PortalBase

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = PortalBase.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no DBAPI)."""
    url = get_engine().url.render_as_string(hide_password=False)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against the portal engine."""
    connectable = get_engine()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
