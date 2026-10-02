"""The WhyGraph portal: a long-running, multi-project host.

This package owns the portal's *own* state - a Postgres database of
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
* :mod:`~whygraph.portal.app` - :func:`~whygraph.portal.app.create_portal_app`,
  the FastAPI app and its lifespan.
* :mod:`~whygraph.portal.security` - the Host / Origin / CSRF guard, the
  principal and :class:`~whygraph.portal.security.PortalOrigins`.
* :mod:`~whygraph.portal.deps` - portal state and the
  ``org_access`` / ``project_access`` / ``project_db_access`` dependencies.
* :mod:`~whygraph.portal.routes` - the management endpoints.
* :mod:`~whygraph.portal.mcp_mount` - the per-project ``/mcp/<slug>`` endpoint.
* :mod:`~whygraph.portal.policy` - the config allowlists and the
  ``whygraph.toml`` import.
* :mod:`~whygraph.portal.repos` - shared folders, path checks, discovery.
* :mod:`~whygraph.portal.migrate` - the per-project migration owner.
* :mod:`~whygraph.portal.runner` - the scan runner seam (filled in by step 8).
"""
