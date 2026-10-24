"""The project paths the portal opens, and the checks that guard them.

A portal project's WhyGraph DB lives in the portal's data directory, at
``<data>/projects/<org slug>/<project slug>/whygraph.db``
(:func:`project_db_path`): a repository cannot influence where it is, and
deleting a checkout (or ``git clean -xdf``) never loses it. Its CodeGraph
index stays in the repository at ``<root>/.codegraph/codegraph.db``
(:func:`codegraph_db_path`). :mod:`whygraph.portal.context` forces both,
whatever the config rows say.

The repo's content is not trusted, though: ``.codegraph/``, its DB or
``.whygraph/backups/`` can be a committed symlink to another project's
files or to the data directory. :func:`check_project_paths` refuses that,
and :func:`check_project_db` refuses a symlink planted inside the data
directory, before anything opens, migrates or backs up a project DB - the
initialized gate (:func:`whygraph.portal.deps.require_initialized`, so
every data route and ``/mcp/<slug>``),
:meth:`whygraph.portal.migrate.ProjectMigrations.ensure`, the init
endpoint, the project stats and the scan runner all call them.
"""

from __future__ import annotations

from pathlib import Path

from whygraph.core.safe_paths import UnsafePathError, check_inside

CODEGRAPH_DB = Path(".codegraph") / "codegraph.db"
"""The project CodeGraph DB, relative to the root."""

BACKUPS_DIR = Path(".whygraph") / "backups"
"""Where migration and agent-file backups are written, relative to the root."""

PROJECTS_DIR = Path("projects")
"""Under the data dir: one directory of history per project, ``<org>/<slug>/``."""

TRACKED_STATE_PATHS: tuple[str, ...] = (".whygraph", ".codegraph")
"""Paths a server clone must not track: WhyGraph's own state (M2d-2 plan section 0.2 #20).

Checked at import (``HEAD``) and before every sync's checkout (the fetched
default branch).
"""


def codegraph_db_path(root: Path) -> Path:
    """Return the forced CodeGraph DB path of a root (unchecked)."""
    return root / CODEGRAPH_DB


def project_data_dir(data_dir: Path, org_slug: str, slug: str) -> Path:
    """Return a project's history directory, ``<data>/projects/<org>/<slug>`` (unchecked).

    Parameters
    ----------
    data_dir : Path
        The portal's data directory (absolute).
    org_slug : str
        The project's org slug (a validated DNS label, immutable).
    slug : str
        The project slug (a validated DNS label, immutable).

    Returns
    -------
    Path
        The directory; it need not exist.
    """
    return data_dir / PROJECTS_DIR / org_slug / slug


def project_db_path(data_dir: Path, org_slug: str, slug: str) -> Path:
    """Return a project's WhyGraph DB, ``<data>/projects/<org>/<slug>/whygraph.db`` (unchecked).

    Parameters
    ----------
    data_dir : Path
        The portal's data directory. Must be absolute: the scan child runs
        with the repository as its ``cwd``, so a relative path would land
        inside the repository.
    org_slug : str
        The project's org slug.
    slug : str
        The project slug.

    Returns
    -------
    Path
        The absolute DB path; the file need not exist.
    """
    assert data_dir.is_absolute(), f"the data directory must be absolute: {data_dir}"
    return project_data_dir(data_dir, org_slug, slug) / "whygraph.db"


def check_project_db(data_dir: Path, db_path: Path) -> Path:
    """Refuse a project DB path that leaves the data directory or passes a symlink.

    The slugs in the path are validated, so this is defence in depth
    against a symlink planted inside the data directory
    (``projects/<org>/<slug>`` or ``whygraph.db`` itself).

    Parameters
    ----------
    data_dir : Path
        The portal's data directory, as :func:`whygraph.portal.db.data_dir`
        spells it (the check is lexical first, so the spelling must match
        the one the path was built from).
    db_path : Path
        The project's WhyGraph DB path.

    Returns
    -------
    Path
        The checked path.

    Raises
    ------
    whygraph.core.safe_paths.UnsafePathError
        When it is not under ``data_dir``, passes through a symlink, or
        resolves outside ``data_dir``.
    """
    return check_inside(data_dir, db_path)


def check_project_paths(root: Path) -> None:
    """Refuse a project whose repository paths leave the root through a symlink.

    Checks ``.whygraph/``, ``.whygraph/backups/``, ``.codegraph/`` and
    ``.codegraph/codegraph.db`` with
    :func:`whygraph.core.safe_paths.check_inside`. Missing entries are
    fine. The WhyGraph DB is not in the repository: see
    :func:`check_project_db`.

    Parameters
    ----------
    root : Path
        The project root.

    Raises
    ------
    whygraph.core.safe_paths.UnsafePathError
        When any of them is a symlink or resolves outside ``root``.
    """
    check_inside(root, BACKUPS_DIR)
    check_inside(root, CODEGRAPH_DB)


__all__ = [
    "BACKUPS_DIR",
    "CODEGRAPH_DB",
    "PROJECTS_DIR",
    "TRACKED_STATE_PATHS",
    "UnsafePathError",
    "check_project_db",
    "check_project_paths",
    "codegraph_db_path",
    "project_data_dir",
    "project_db_path",
]
