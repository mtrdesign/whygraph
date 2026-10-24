"""Production clones under ``<data>/repos``: the folder guard, removal, Initialize, the sweep.

A production GitHub project's checkout lives at ``repos/<org slug>/<slug>``
under the portal's data directory; the runner clones it into a dot-named
``repos/<org slug>/.clone-*`` temp folder first and renames it into place
(M2f-3 plan section 4.8). Every ``rmtree`` of such a folder goes through
:func:`clone_dir_is_safe`, and of a project's history directory under
``<data>/projects`` through the same guard (:func:`portal_dir_is_safe`).
Used by the routes (removal, org deletion, production Initialize), the
history module (:func:`~whygraph.portal.history.remove_history`) and the
runner (the import's clone): this module
imports neither ``deps`` nor ``routes`` (``deps`` imports the runner), so it
raises :class:`~whygraph.core.safe_paths.UnsafePathError` and each caller
maps it to its own error.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from whygraph.db.engine import dispose_engine

from .paths import check_project_paths

if TYPE_CHECKING:  # pragma: no cover
    from whygraph.core.context import ProjectContext

    from .migrate import ProjectMigrations

_log = logging.getLogger(__name__)

CLONE_PREFIX = ".clone-"
"""The name prefix of an import's temp folder (a dot-name is never a slug)."""


PORTAL_DIR_BASES = frozenset({"repos", "projects"})
"""The data-dir folders whose subtrees the portal deletes (``base`` below)."""


def portal_dir_is_safe(root: Path, data_dir: Path, *, base: str, depth: int) -> bool:
    """Whether ``root`` sits exactly ``depth`` levels under ``<data dir>/<base>`` (rmtree guard).

    Parameters
    ----------
    root : Path
        The folder to check; it need not exist yet.
    data_dir : Path
        The portal's data directory (any spelling: real paths are compared).
    base : {"repos", "projects"}
        ``repos`` for clones and an org's clone folder, ``projects`` for a
        project's history directory and an org's history folder.
    depth : int
        How many levels below ``<data dir>/<base>`` ``root`` must be.

    Returns
    -------
    bool
        ``True`` when neither ``root`` nor any directory between it and
        ``<base>`` is a symlink and its real path's parent chain reaches
        ``realpath(<data dir>/<base>)``.

    Raises
    ------
    ValueError
        When ``base`` is not one of :data:`PORTAL_DIR_BASES` or ``depth`` is
        below 1.
    """
    if base not in PORTAL_DIR_BASES or depth < 1:
        raise ValueError(f"not a portal folder guard: base={base!r}, depth={depth}")
    top = Path(os.path.realpath(data_dir / base))
    real = Path(os.path.realpath(root))
    path = Path(root)
    for _ in range(depth):
        if path.is_symlink():
            return False
        path, real = path.parent, real.parent
    return real == top and Path(os.path.realpath(path)) == top


def remove_portal_dir(path: Path, data_dir: Path, *, base: str, depth: int) -> bool:
    """Delete ``path`` when it exists and passes :func:`portal_dir_is_safe`.

    Parameters
    ----------
    path : Path
        The folder to delete.
    data_dir : Path
        The portal's data directory.
    base : {"repos", "projects"}
        As for :func:`portal_dir_is_safe`.
    depth : int
        As for :func:`portal_dir_is_safe`.

    Returns
    -------
    bool
        Whether it was deleted.
    """
    if path.exists() and portal_dir_is_safe(path, data_dir, base=base, depth=depth):
        shutil.rmtree(path)
        return True
    return False


def clone_dir_is_safe(root: Path, data_dir: Path, *, depth: int) -> bool:
    """Whether ``root`` sits exactly ``depth`` levels under ``<data dir>/repos`` (rmtree guard).

    :func:`portal_dir_is_safe` with ``base="repos"``.

    Parameters
    ----------
    root : Path
        The folder to check; it need not exist yet.
    data_dir : Path
        The portal's data directory.
    depth : int
        2 for production's ``repos/<org slug>/<slug>`` (and its
        ``repos/<org slug>/.clone-*`` temp dirs), 1 for a local GitHub clone
        of an earlier build (``repos/<slug>``) or an org's folder.

    Returns
    -------
    bool
        As for :func:`portal_dir_is_safe`.
    """
    return portal_dir_is_safe(root, data_dir, base="repos", depth=depth)


def remove_clone(path: Path, data_dir: Path, *, depth: int) -> bool:
    """Delete ``path`` when it exists and passes :func:`clone_dir_is_safe`.

    Parameters
    ----------
    path : Path
        The clone (or org) folder.
    data_dir : Path
        The portal's data directory.
    depth : int
        As for :func:`clone_dir_is_safe`.

    Returns
    -------
    bool
        Whether it was deleted.
    """
    return remove_portal_dir(path, data_dir, base="repos", depth=depth)


def discard_clone(
    path: Path, db_path: Path, data_dir: Path, migrations: ProjectMigrations
) -> None:
    """Remove a production clone (``depth`` 2) and forget its project DB.

    The project's history directory in the data dir is kept, so a retried
    import finds it again.

    Parameters
    ----------
    path : Path
        ``repos/<org slug>/<slug>`` under ``data_dir``.
    db_path : Path
        The project's WhyGraph DB (its cached engine is disposed).
    data_dir : Path
        The portal's data directory.
    migrations : ProjectMigrations
        The portal's migration memo (the DB's entry is dropped).
    """
    dispose_engine(db_path)
    migrations.forget(db_path)
    remove_clone(path, data_dir, depth=2)


def production_initialize(migrations: ProjectMigrations, ctx: ProjectContext) -> None:
    """A production project's Initialize: checked DB paths, then the migration.

    No agent files, hooks or markers (M2d-2 plan section 0.2 #12); the
    caller sets ``initialized_at``. Idempotent.

    Parameters
    ----------
    migrations : ProjectMigrations
        The portal's migration owner.
    ctx : ProjectContext
        The project's context (its root holds the clone).

    Raises
    ------
    UnsafePathError
        When a DB path is a symlink.
    """
    check_project_paths(ctx.root)
    migrations.ensure(ctx)


def temp_clone_dir(root: Path) -> Path:
    """A fresh temp folder name beside ``root`` for its clone (``.clone-<slug>-<hex>``).

    Parameters
    ----------
    root : Path
        The project's ``repos/<org slug>/<slug>``.

    Returns
    -------
    Path
        ``repos/<org slug>/.clone-<slug>-<12 hex>`` (not created).
    """
    return root.parent / f"{CLONE_PREFIX}{root.name}-{secrets.token_hex(6)}"


def sweep_clone_dirs(data_dir: Path) -> int:
    """Remove every ``repos/*/.clone-*`` folder left by an earlier process.

    Only the runner clones, and the portal is one process holding
    ``portal.lock``, so at start none can be live. Each removal passes
    :func:`clone_dir_is_safe`; a failure is logged, never raised.

    Parameters
    ----------
    data_dir : Path
        The portal's data directory.

    Returns
    -------
    int
        How many folders were removed.
    """
    repos = data_dir / "repos"
    if not repos.is_dir() or repos.is_symlink():
        return 0
    removed = 0
    for org_dir in repos.iterdir():
        if org_dir.is_symlink() or not org_dir.is_dir():
            continue
        for child in org_dir.iterdir():
            if not child.name.startswith(CLONE_PREFIX):
                continue
            try:
                if remove_clone(child, data_dir, depth=2):
                    removed += 1
            except OSError:
                _log.exception("could not remove the leftover clone %s", child)
    return removed
