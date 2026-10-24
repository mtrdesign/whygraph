"""A project's history directory and the identity sidecar that owns it.

A portal project's WhyGraph DB lives at
``<data>/projects/<org>/<slug>/whygraph.db``
(:func:`whygraph.portal.paths.project_db_path`) and survives the project's
removal, so re-adding the same repository can re-attach it. Slugs are unique
only among *live* rows, though: without more, an unrelated repository that
took a removed project's slug would inherit its commits, cards and chats.

So every directory carries ``project.json``, the **identity sidecar**,
written by the portal when the row is inserted (never by a repository)::

    {"version": 1, "org": "local", "slug": "api", "source": "local",
     "identity": {"origin": "github.com/acme/api"},
     "root": "/Users/x/src/api", "created_at": "2026-10-21T10:00:00Z"}

``identity`` holds exactly one key: ``github_repo_id`` (production, from the
GitHub App, which a repository cannot forge), ``origin`` (local mode, the
``host/owner/name`` of the checkout's ``origin``) or ``path`` (local mode,
no usable ``origin``; a moved checkout then starts with empty history).

The sidecar is an **exclusive reservation**: :func:`write_sidecar` creates it
with ``os.link`` from a temporary file, never ``os.replace``, so a second
writer can never overwrite it, and a committed row's directory always names
that row's identity. Anything unusual - a missing or malformed sidecar, a
symlink anywhere from ``projects/`` down, a directory whose name is not the
sidecar's ``slug`` - counts as **foreign**: never attached, never reused.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from whygraph.core.safe_paths import UnsafePathError, check_inside

from .clones import remove_portal_dir
from .db import data_dir as _portal_data_dir
from .paths import PROJECTS_DIR, project_data_dir
from .projects import is_valid_slug

SIDECAR = "project.json"
"""The identity sidecar's file name, in a project's history directory."""

SIDECAR_VERSION = 1
"""The sidecar format this module writes and reads."""

IDENTITY_KEYS = frozenset({"github_repo_id", "origin", "path"})
"""The keys an identity may hold (exactly one of them)."""

_MAX_SIDECAR_BYTES = 64 * 1024
_DB_NAME = "whygraph.db"


@dataclass(frozen=True)
class Reservation:
    """The outcome of :func:`write_sidecar`: truthy when the slug is this identity's.

    Attributes
    ----------
    slug_dir : Path
        The history directory, ``<data>/projects/<org>/<slug>``.
    ok : bool
        Whether ``project.json`` now names this identity (a fresh reservation
        or a re-attach). ``False`` means "pick another slug".
    created_sidecar : bool
        Whether this call created ``project.json``.
    created_dir : bool
        Whether this call created the directory itself.
    """

    slug_dir: Path
    ok: bool
    created_sidecar: bool = False
    created_dir: bool = False

    def __bool__(self) -> bool:
        return self.ok


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_identity(identity: object) -> bool:
    if not isinstance(identity, dict) or len(identity) != 1:
        return False
    ((key, value),) = identity.items()
    if key not in IDENTITY_KEYS:
        return False
    if key == "github_repo_id":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, str) and bool(value)


def _read_sidecar(path: Path) -> dict | None:
    """The parsed, validated sidecar at ``path`` (already checked), or ``None``."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(_MAX_SIDECAR_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_SIDECAR_BYTES:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("version") != SIDECAR_VERSION:
        return None
    if not _valid_identity(data.get("identity")):
        return None
    for key in ("org", "slug", "source"):
        if not isinstance(data.get(key), str):
            return None
    if not isinstance(data.get("created_at"), str):
        return None
    if not isinstance(data.get("root"), (str, type(None))):
        return None
    return data


def history_owner(slug_dir: Path, *, data_dir: Path | None = None) -> dict | None:
    """Read the identity sidecar of a history directory.

    Parameters
    ----------
    slug_dir : Path
        ``<data>/projects/<org>/<slug>``.
    data_dir : Path, optional
        The portal's data directory, spelled as ``slug_dir`` was built from
        it; :func:`whygraph.portal.db.data_dir` by default.

    Returns
    -------
    dict or None
        The sidecar, or ``None`` when it is missing or malformed, or when
        ``projects/``, ``<org>/``, ``<slug>/`` or the sidecar is a symlink
        (all of which make the directory foreign).
    """
    root = data_dir if data_dir is not None else _portal_data_dir()
    try:
        path = check_inside(root, slug_dir / SIDECAR)
    except UnsafePathError:
        return None
    return _read_sidecar(path)


def _owns(owner: dict | None, org_slug: str, name: str, identity: dict) -> bool:
    """Whether ``owner`` (a sidecar) makes directory ``name`` this identity's."""
    return (
        owner is not None
        and owner["org"] == org_slug
        and owner["slug"] == name
        and owner["identity"] == identity
    )


def _org_entries(data_dir: Path, org_slug: str) -> list[Path]:
    """The entries of ``projects/<org>/``; none when missing or symlinked."""
    org_dir = data_dir / PROJECTS_DIR / org_slug
    try:
        check_inside(data_dir, org_dir)
        return sorted(org_dir.iterdir()) if org_dir.is_dir() else []
    except (UnsafePathError, OSError):
        return []


def find_history(
    data_dir: Path,
    org_slug: str,
    identity: dict,
    root: str | None,
    used: set[str],
) -> str | None:
    """The slug of a kept history directory this identity owns, or ``None``.

    Only directories whose name is a valid slug, equals their sidecar's
    ``slug``, and is not in ``used`` are considered. Among several matches
    (local mode: two clones of one ``origin`` can be two projects) the
    pick is by ``(sidecar root == root) desc, has whygraph.db desc,
    created_at asc, slug asc``.

    Parameters
    ----------
    data_dir : Path
        The portal's data directory (:func:`whygraph.portal.db.data_dir`).
    org_slug : str
        The org whose ``projects/<org>/`` is searched (and nothing else).
    identity : dict
        The repository's identity (one key, see the module docstring).
    root : str or None
        The checkout's root, a tie-break; production passes ``None``.
    used : set of str
        Slugs that cannot be re-attached: the org's live rows, slugs already
        tried, and in production the names under ``repos/<org>/``.

    Returns
    -------
    str or None
        The chosen slug.
    """
    candidates: list[tuple[bool, bool, str, str]] = []
    for entry in _org_entries(data_dir, org_slug):
        name = entry.name
        if not is_valid_slug(name) or name in used:
            continue
        owner = history_owner(entry, data_dir=data_dir)
        if not _owns(owner, org_slug, name, identity):
            continue
        assert owner is not None
        db = entry / _DB_NAME
        has_db = db.is_file() and not db.is_symlink()
        same_root = root is not None and owner.get("root") == root
        candidates.append((not same_root, not has_db, owner["created_at"], name))
    if not candidates:
        return None
    return min(candidates)[3]


def foreign_history(data_dir: Path, org_slug: str, identity: dict) -> set[str]:
    """Names under ``projects/<org>/`` this identity must not take.

    Parameters
    ----------
    data_dir : Path
        The portal's data directory (:func:`whygraph.portal.db.data_dir`).
    org_slug : str
        The org.
    identity : dict
        The repository's identity.

    Returns
    -------
    set of str
        Every directory name whose sidecar is missing, malformed, symlinked,
        names another slug, or names another identity.
    """
    return {
        entry.name
        for entry in _org_entries(data_dir, org_slug)
        if not _owns(
            history_owner(entry, data_dir=data_dir), org_slug, entry.name, identity
        )
    }


def write_sidecar(
    data_dir: Path,
    org_slug: str,
    slug: str,
    source: str,
    identity: dict,
    root: str | None,
) -> Reservation:
    """Reserve ``projects/<org>/<slug>/`` for ``identity`` (exclusive, never overwrites).

    The sidecar is written to a temporary file in the directory, then
    ``os.link``-ed to ``project.json``, which fails when the name exists:
    a concurrent writer can never replace it. When it exists, it is read:
    naming this identity makes the slug a re-attach.

    Parameters
    ----------
    data_dir : Path
        The portal's data directory (:func:`whygraph.portal.db.data_dir`).
    org_slug : str
        The project's org slug.
    slug : str
        The chosen project slug.
    source : str
        ``"local"`` or ``"github"``.
    identity : dict
        The repository's identity (exactly one key of :data:`IDENTITY_KEYS`).
    root : str or None
        Where the project is (informational; a tie-break of
        :func:`find_history`).

    Returns
    -------
    Reservation
        Truthy when ``project.json`` now names this identity; ``False`` means
        another identity owns the directory (or its sidecar is unreadable),
        so the caller picks another slug.

    Raises
    ------
    whygraph.core.safe_paths.UnsafePathError
        When the directory or the sidecar passes through a symlink, or
        leaves the data directory.
    ValueError
        When ``identity`` is not exactly one known key.
    """
    if not _valid_identity(identity):
        raise ValueError(f"not a history identity: {identity!r}")
    slug_dir = project_data_dir(data_dir, org_slug, slug)
    check_inside(data_dir, slug_dir)
    slug_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    created_dir = False
    try:
        slug_dir.mkdir(mode=0o700)
        created_dir = True
    except FileExistsError:
        pass
    final = check_inside(data_dir, slug_dir / SIDECAR)
    body = {
        "version": SIDECAR_VERSION,
        "org": org_slug,
        "slug": slug,
        "source": source,
        "identity": dict(identity),
        "root": root,
        "created_at": _now(),
    }
    tmp = slug_dir / f".{SIDECAR}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, final)
        except FileExistsError:
            owner = history_owner(slug_dir, data_dir=data_dir)
            return Reservation(
                slug_dir, _owns(owner, org_slug, slug, identity), False, created_dir
            )
        return Reservation(slug_dir, True, True, created_dir)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def release_sidecar(reservation: Reservation, *, data_dir: Path) -> bool:
    """Undo a reservation whose row was never inserted.

    Removes only what this reservation created: its ``project.json`` (when
    the directory holds no ``whygraph.db``) and then the directory, if it
    created that too and it is now empty. Never removes anything else. The
    caller checks first that no row holds the slug.

    Parameters
    ----------
    reservation : Reservation
        As returned by :func:`write_sidecar`.
    data_dir : Path
        The portal's data directory, as the reservation was made under.

    Returns
    -------
    bool
        Whether the directory is gone.
    """
    if not reservation.created_sidecar:
        return False
    slug_dir = reservation.slug_dir
    db = slug_dir / _DB_NAME
    try:
        sidecar = check_inside(data_dir, slug_dir / SIDECAR)
        if db.exists() or db.is_symlink():
            return False
        sidecar.unlink()
        if reservation.created_dir:
            slug_dir.rmdir()  # only when empty: never a tree
            return True
    except (UnsafePathError, OSError):
        pass
    return False


def remove_history(slug_dir: Path, *, data_dir: Path | None = None) -> bool:
    """Delete a project's history directory, ``<data>/projects/<org>/<slug>``.

    Only when it exists and passes the rmtree guard
    (:func:`~whygraph.portal.clones.portal_dir_is_safe` at
    ``base="projects", depth=2``: neither ``<org>/`` nor ``<slug>/`` is a
    symlink, and the real path is exactly two levels under ``projects/``). No identity comparison: a committed
    local or GitHub row's directory always names that row (the exclusive
    reservation), and an identity recomputed from a moved or deleted
    checkout would be unreliable exactly when removal matters. Never called
    for a linked (``platform``) row, which owns no history.

    Parameters
    ----------
    slug_dir : Path
        The directory (the parent of the row's ``whygraph_db``).
    data_dir : Path, optional
        The portal's data directory; :func:`whygraph.portal.db.data_dir` by
        default (real paths are compared, so the spelling does not matter).

    Returns
    -------
    bool
        Whether it was deleted.

    Raises
    ------
    OSError
        When the ``rmtree`` itself fails.
    """
    root = data_dir if data_dir is not None else _portal_data_dir()
    return remove_portal_dir(slug_dir, root, base="projects", depth=2)


__all__ = [
    "IDENTITY_KEYS",
    "Reservation",
    "SIDECAR",
    "SIDECAR_VERSION",
    "find_history",
    "foreign_history",
    "history_owner",
    "release_sidecar",
    "remove_history",
    "write_sidecar",
]
