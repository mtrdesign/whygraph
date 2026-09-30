"""The WhyGraph portal: a long-running, multi-project host.

This package owns the portal's *own* state - a SQLite database of
projects, users, per-project config and encrypted secrets, kept apart
from every project's ``whygraph.db`` (see :mod:`whygraph.portal.db`).

Modules
-------
* :mod:`~whygraph.portal.db` - data directory, engine, Alembic bootstrap.
* :mod:`~whygraph.portal.models` - the tables, on their own ``MetaData``.
* :mod:`~whygraph.portal.secrets` - Fernet key file, keyring, secret store.
* :mod:`~whygraph.portal.config_layers` - the ``project_config`` rows and
  the config policy applied on write.
* :mod:`~whygraph.portal.context` - building (and caching) a
  :class:`whygraph.core.context.ProjectContext` from those rows.
* :mod:`~whygraph.portal.projects` - slug rules.
"""
