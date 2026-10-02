"""The project paths the portal opens, and the check that guards them.

Rule 4.2.1 #2 forces a project's DB paths to ``<root>/.whygraph/whygraph.db``
and ``<root>/.codegraph/codegraph.db``. The repo's content is not trusted,
though: either DB file, or ``.whygraph/`` / ``.codegraph/`` themselves, can
be a committed symlink to another project's DB or to a file in the data directory.
:func:`check_project_paths` refuses that before anything opens, migrates
or backs up a project DB - the initialized gate
(:func:`whygraph.portal.deps.require_initialized`, so every data route and
``/mcp/<slug>``), :meth:`whygraph.portal.migrate.ProjectMigrations.ensure`,
the init endpoint, the project stats and the scan runner all call it.
"""

from __future__ import annotations

from pathlib import Path

from whygraph.core.safe_paths import UnsafePathError, check_inside

WHYGRAPH_DB = Path(".whygraph") / "whygraph.db"
"""The project WhyGraph DB, relative to the root."""

CODEGRAPH_DB = Path(".codegraph") / "codegraph.db"
"""The project CodeGraph DB, relative to the root."""

BACKUPS_DIR = Path(".whygraph") / "backups"
"""Where migration and agent-file backups are written, relative to the root."""


def db_paths(root: Path) -> tuple[Path, Path]:
    """Return the forced ``(whygraph_db, codegraph_db)`` paths of a root (unchecked)."""
    return root / WHYGRAPH_DB, root / CODEGRAPH_DB


def check_project_paths(root: Path) -> tuple[Path, Path]:
    """Refuse a project whose DB paths leave the root through a symlink.

    Checks ``.whygraph/``, ``.whygraph/whygraph.db``,
    ``.whygraph/backups/``, ``.codegraph/`` and ``.codegraph/codegraph.db``
    with :func:`whygraph.core.safe_paths.check_inside`. Missing entries
    are fine.

    Parameters
    ----------
    root : Path
        The project root.

    Returns
    -------
    tuple of Path
        The checked ``(whygraph_db, codegraph_db)``.

    Raises
    ------
    whygraph.core.safe_paths.UnsafePathError
        When any of them is a symlink or resolves outside ``root``.
    """
    check_inside(root, BACKUPS_DIR)
    return check_inside(root, WHYGRAPH_DB), check_inside(root, CODEGRAPH_DB)


__all__ = [
    "BACKUPS_DIR",
    "CODEGRAPH_DB",
    "UnsafePathError",
    "WHYGRAPH_DB",
    "check_project_paths",
    "db_paths",
]
