"""Local repositories: shared folders, path checks, discovery and detection.

The portal container sees the host only through **shared folders**,
bind-mounted at the same path they have on the host
(``WHYGRAPH_SHARED_FOLDERS``, ``:``-separated). A local project must live
under one of them; a path outside gets the exact ``whygraph up
--add-folder <dir>`` command to run (plan section 4.5.4).

Everything here resolves symlinks first (``realpath``), so neither ``..``
nor a symlink can escape a shared folder, and nothing may overlap the
portal's data directory, which holds ``secret.key``.
"""

from __future__ import annotations

import logging
import os
import shlex
import threading
import time
from pathlib import Path

from whygraph.agents import detect_entries, is_git_tracked
from whygraph.hooks import managed_hook_names
from whygraph.services.git import GitError, Repository
from whygraph.services.github import GitHubClient

_log = logging.getLogger(__name__)

SHARED_FOLDERS_ENV = "WHYGRAPH_SHARED_FOLDERS"
"""``:``-separated absolute paths of the folders shared into the portal."""

DISCOVERY_DEPTH = 4
DISCOVERY_LIMIT = 500
DISCOVERY_TTL_SEC = 60.0
_SKIP_DIRS = frozenset({"node_modules", ".venv", "vendor", "dist", "build"})


def _real(path: str | Path) -> Path:
    return Path(os.path.realpath(path))


def is_within(path: Path, folder: Path) -> bool:
    """Return whether ``path`` equals ``folder`` or lies inside it (both real paths)."""
    try:
        return os.path.commonpath([path, folder]) == str(folder)
    except ValueError:
        return False


def overlaps(a: Path, b: Path) -> bool:
    """Return whether one of two real paths contains (or equals) the other."""
    return is_within(a, b) or is_within(b, a)


def parse_shared_folders(value: str | None, data_dir: Path) -> tuple[Path, ...]:
    """Validate ``WHYGRAPH_SHARED_FOLDERS`` into a tuple of real paths.

    Invalid entries are logged and skipped rather than failing startup
    (the shim enforces the same rules before the container starts): a
    relative path, ``/``, and any folder equal to, inside or containing
    the data directory are refused; sharing ``$HOME`` only warns.

    Parameters
    ----------
    value : str or None
        The raw env value.
    data_dir : Path
        The portal data directory.

    Returns
    -------
    tuple[Path, ...]
        Deduplicated real paths, in the given order.
    """
    data = _real(data_dir)
    home = _real(Path.home())
    folders: list[Path] = []
    for raw in (value or "").split(":"):
        raw = raw.strip()
        if not raw:
            continue
        if not os.path.isabs(raw):
            _log.error("ignoring shared folder %r: not an absolute path", raw)
            continue
        folder = _real(raw)
        if folder == Path(folder.anchor):
            _log.error("ignoring shared folder %r: sharing / is refused", raw)
            continue
        if overlaps(folder, data):
            _log.error(
                "ignoring shared folder %r: it overlaps the portal data dir %s",
                raw,
                data,
            )
            continue
        if folder == home:
            _log.warning(
                "shared folder %s is your home directory; share a narrower folder",
                folder,
            )
        if folder not in folders:
            folders.append(folder)
    return tuple(folders)


def add_folder_command(folder: Path) -> str:
    """Return the host command that shares ``folder`` into the portal."""
    return f"whygraph up --add-folder {shlex.quote(str(folder))}"


def github_link(root: Path) -> dict | None:
    """Return ``{"slug", "remote_url"}`` when ``root``'s ``origin`` is on GitHub.

    Parameters
    ----------
    root : Path
        A git work tree.

    Returns
    -------
    dict or None
        ``slug`` is ``owner/name``; ``None`` for a non-GitHub or missing
        ``origin`` (or an unreadable repo).
    """
    repo = Repository(root)
    try:
        client = GitHubClient.for_repository(repo)
        url = repo.origin_url
    except GitError:
        return None
    if client is None or url is None:
        return None
    return {"slug": f"{client.owner}/{client.name}", "remote_url": url}


def check_path(path: str, shared: tuple[Path, ...], data_dir: Path) -> dict:
    """Check whether ``path`` can be added as a local project.

    Parameters
    ----------
    path : str
        The candidate path as typed. Must be absolute.
    shared : tuple[Path, ...]
        The shared folders (real paths).
    data_dir : Path
        The portal data directory.

    Returns
    -------
    dict
        ``{"path", "shared", "is_git", "protected", "folder_suggestion",
        "command", "github"}``. ``path`` is the resolved real path;
        ``protected`` means it overlaps the data directory. When not
        shared, ``folder_suggestion`` is the folder to share (the repo's
        parent, or the repo itself when the parent is ``/`` or ``$HOME``)
        and ``command`` the ``whygraph up --add-folder`` line.

    Raises
    ------
    ValueError
        If ``path`` is not absolute.
    """
    if not os.path.isabs(path):
        raise ValueError("path must be absolute")
    real = _real(path)
    is_shared = any(is_within(real, folder) for folder in shared)
    is_git = (real / ".git").exists()
    protected = overlaps(real, _real(data_dir))
    suggestion: Path | None = None
    command: str | None = None
    if not is_shared:
        suggestion = real.parent
        if suggestion == Path(suggestion.anchor) or suggestion == _real(Path.home()):
            suggestion = real
        command = add_folder_command(suggestion)
    return {
        "path": str(real),
        "shared": is_shared,
        "is_git": is_git,
        "protected": protected,
        "folder_suggestion": str(suggestion) if suggestion else None,
        "command": command,
        "github": github_link(real) if is_git and is_shared else None,
    }


def discover_repos(shared: tuple[Path, ...]) -> list[Path]:
    """Find git work trees under the shared folders.

    Walks each folder to :data:`DISCOVERY_DEPTH` levels without following
    symlinks, skipping dot-dirs and :data:`_SKIP_DIRS`, and stops at
    :data:`DISCOVERY_LIMIT` results. A repository is not descended into.

    Parameters
    ----------
    shared : tuple[Path, ...]
        The shared folders.

    Returns
    -------
    list[Path]
        Repository roots, in walk order.
    """
    found: list[Path] = []
    for folder in shared:
        base_depth = len(folder.parts)
        for dirpath, dirnames, _files in os.walk(folder, followlinks=False):
            current = Path(dirpath)
            if (current / ".git").exists():
                found.append(current)
                dirnames[:] = []
                if len(found) >= DISCOVERY_LIMIT:
                    return found
                continue
            if len(current.parts) - base_depth >= DISCOVERY_DEPTH:
                dirnames[:] = []
                continue
            dirnames[:] = sorted(
                d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS
            )
    return found


class DiscoveryCache:
    """A :func:`discover_repos` result cached for :data:`DISCOVERY_TTL_SEC`."""

    def __init__(self, ttl: float = DISCOVERY_TTL_SEC) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._at = 0.0
        self._repos: list[Path] | None = None

    def get(self, shared: tuple[Path, ...]) -> list[Path]:
        """Return the cached repos, walking again once the entry expired."""
        with self._lock:
            if self._repos is not None and time.monotonic() - self._at < self._ttl:
                return list(self._repos)
        repos = discover_repos(shared)
        with self._lock:
            self._repos, self._at = repos, time.monotonic()
        return list(repos)


def detect_existing(root: Path, custom_db_paths: list[dict] | None = None) -> dict:
    """Report the 1.x WhyGraph state a repository already carries.

    Read-only: this is the ``detected`` block of the add response.

    Parameters
    ----------
    root : Path
        Repository root.
    custom_db_paths : list of dict, optional
        From :func:`whygraph.portal.policy.preview_import`.

    Returns
    -------
    dict
        ``{"existing_db", "managed_hooks", "detected_agents",
        "custom_db_paths"}``; each ``detected_agents`` item is
        ``{"agent", "file", "key", "shape", "stale", "tracked"}``.
    """
    agents = [
        {
            "agent": entry.agent,
            "file": entry.file,
            "key": entry.key,
            "shape": entry.transport,
            "stale": entry.stale,
            "tracked": is_git_tracked(root, entry.file),
        }
        for entry in detect_entries(root)
    ]
    return {
        "existing_db": (root / ".whygraph" / "whygraph.db").is_file(),
        "managed_hooks": list(managed_hook_names(root)),
        "detected_agents": agents,
        "custom_db_paths": list(custom_db_paths or []),
    }


def root_status(root: Path) -> str:
    """Return ``"ok"``, ``"missing"`` (not mounted / deleted) or ``"not_git"``."""
    if not root.is_dir():
        return "missing"
    return "ok" if (root / ".git").exists() else "not_git"


__all__ = [
    "DISCOVERY_DEPTH",
    "DISCOVERY_LIMIT",
    "DiscoveryCache",
    "SHARED_FOLDERS_ENV",
    "add_folder_command",
    "check_path",
    "detect_existing",
    "discover_repos",
    "github_link",
    "is_within",
    "overlaps",
    "parse_shared_folders",
    "root_status",
]
