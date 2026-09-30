"""Refusing paths that leave a repository through a symbolic link.

A repository's content is not trusted: a GitHub clone (or any repo under a
shared folder) can commit ``.whygraph/whygraph.db``, ``.whygraph/``,
``.gitignore`` or ``whygraph.toml`` as a symlink to another project's
files or to the portal's own data. :func:`check_inside` is the one check
every portal read or write of a repo path goes through: no component
below ``root`` may be a symlink, and the resolved path must stay under
the resolved root.
"""

from __future__ import annotations

import os
from pathlib import Path


class UnsafePathError(ValueError):
    """A repo path is (or passes through) a symlink, or resolves outside the root.

    Parameters
    ----------
    path : Path
        The offending path (the first symlinked component, when one is).
    reason : str
        Why it was refused.

    Attributes
    ----------
    path : Path
        As given.
    reason : str
        As given.
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path} {reason}")
        self.path = path
        self.reason = reason


def check_inside(root: Path, path: Path | str) -> Path:
    """Return ``path`` under ``root`` if nothing on the way is a symlink.

    Every existing component from ``root`` (exclusive) down to ``path``
    (inclusive) is checked with :meth:`Path.is_symlink`; components that
    do not exist yet are fine (they will be created as real entries).
    Then ``realpath(path)`` must lie under ``realpath(root)``. ``root``
    itself may be a symlink (a mount point, macOS ``/var``).

    Parameters
    ----------
    root : Path
        The repository root.
    path : Path or str
        A path relative to ``root``, or an absolute path under it.

    Returns
    -------
    Path
        ``root / <relative path>``.

    Raises
    ------
    UnsafePathError
        When ``path`` is not lexically under ``root``, contains ``..``,
        passes through a symlink, or resolves outside ``root``.
    """
    root = Path(root)
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            rel = candidate.relative_to(root)
        except ValueError:
            raise UnsafePathError(candidate, f"is not under {root}") from None
    else:
        rel = candidate
    if ".." in rel.parts:
        raise UnsafePathError(root / rel, "contains '..'")
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise UnsafePathError(current, "is a symbolic link")
    full = root / rel
    real_root = os.path.realpath(root)
    real = os.path.realpath(full)
    if os.path.commonpath([real_root, real]) != real_root:
        raise UnsafePathError(full, f"resolves outside {root}")
    return full


__all__ = ["UnsafePathError", "check_inside"]
