"""Alembic environment for WhyGraph's SQLModel-managed tables.

Reuses the project's :func:`whygraph.db.get_engine` so the DB URL is
sourced from :func:`whygraph.core.get_config` (one place to configure
the path), and applies an ``include_object`` filter so autogenerate
only considers tables registered on :data:`whygraph.db.base.metadata`.
Hand-rolled tables owned by :mod:`whygraph.scan.db` are intentionally
invisible to Alembic.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from whygraph.db import get_engine
from whygraph.db import models as _models  # noqa: F401  -- side-effect: register models on metadata
from whygraph.db.base import metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001
    """Skip every table not registered on :data:`target_metadata`.

    Without this filter, autogenerate sees the hand-rolled tables owned
    by :mod:`whygraph.scan.db` as "extra" and would emit ``drop_table``
    operations for each of them — catastrophic. Non-table objects are
    left alone (Alembic handles indexes/constraints relative to their
    tables anyway).
    """
    if type_ == "table" and name not in target_metadata.tables:
        return False
    return True


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no DBAPI)."""
    url = get_engine().url.render_as_string(hide_password=False)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against the WhyGraph engine."""
    connectable = get_engine()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
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
