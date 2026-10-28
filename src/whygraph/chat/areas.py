"""Configured product areas, as the chat assistant uses them.

An *area* (``[areas.<name>]``, :class:`~whygraph.core.config.AreaConfig`)
names a set of repo paths and issue / PR labels. This module is the one place
that turns an area into something a query can use: escaped ``LIKE``
predicates over ``commit_file_change.path``, a lookup by name that fails with
a message listing the configured names, the reverse lookups (paths and labels
to area names) and the compact block the chat overview shows the model.

Matching is by prefix and exact path only. Renames are **not** followed for
area paths (``resolve_path_aliases`` follows exact paths only): list a
directory's old location in ``paths`` to keep its earlier history.
Nothing here opens a path; entries are only compared with recorded
file-change paths.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, col, select

from whygraph.core import get_config
from whygraph.core.config import AreaConfig, area_errors
from whygraph.db.models import Commit, CommitFileChange
from whygraph.core.utils import like_escape
from whygraph.mcp.errors import WhyGraphError

OVERVIEW_BUDGET_CHARS = 4_000
"""Upper bound on the serialized size of :func:`overview_block`."""

OVERVIEW_PATHS_PER_AREA = 5
"""Paths shown per area in the overview before they are cut."""

SUGGEST_DAYS = 365
"""How far back :func:`suggest_areas` looks at commits."""

SUGGEST_MAX = 12
"""Most areas :func:`suggest_areas` proposes."""

SUGGEST_MAX_DEPTH = 4
"""Deepest directory level the suggestion descends to."""

SUGGEST_DESCEND_SHARE = 0.7
"""A child holding more than this share of the commits is descended into."""

SUGGEST_MIN_SHARE = 0.05
"""Smallest share of commits a proposed directory must hold."""

SUGGEST_DENYLIST: frozenset[str] = frozenset(
    {"node_modules", "dist", "build", "vendor", ".venv", "target", "coverage"}
)
"""Directory names never proposed (generated or vendored trees)."""

PathPredicate = tuple[str, str | None]
"""``(escaped LIKE pattern, exact path or None)`` for one area path entry."""


def configured_areas() -> tuple[AreaConfig, ...]:
    """Return the areas configured for the bound project.

    Returns
    -------
    tuple of AreaConfig
        Sorted by name; empty when none are configured.
    """
    return get_config().areas


def resolve_area(name: str) -> AreaConfig:
    """Return the configured area called ``name``.

    Parameters
    ----------
    name : str
        An area name, compared case-insensitively after trimming.

    Returns
    -------
    AreaConfig
        The matching area.

    Raises
    ------
    WhyGraphError
        No such area. The message lists the configured names and points at
        the overview. No ``code`` is set: this is guidance for the model, not
        a condition the UI words itself.
    """
    wanted = (name or "").strip().lower()
    areas = configured_areas()
    for area in areas:
        if area.name == wanted:
            return area
    if areas:
        known = ", ".join(a.name for a in areas)
        raise WhyGraphError(
            f"unknown area {name!r}. Configured areas: {known} "
            "(see areas in the overview)."
        )
    raise WhyGraphError(
        f"unknown area {name!r}: no areas are configured for this project "
        "(see areas in the overview)."
    )


def path_predicates(area: AreaConfig) -> list[PathPredicate]:
    """Return the escaped match predicates for an area's paths.

    A path ending in ``/`` is a prefix. Any other entry matches that exact
    path or anything under ``<path>/``.

    Parameters
    ----------
    area : AreaConfig
        The area.

    Returns
    -------
    list of tuple
        One ``(pattern, exact)`` per path. ``pattern`` is a ``LIKE`` pattern
        escaped with :func:`~whygraph.core.utils.like_escape` (the query must
        declare ``ESCAPE`` with :data:`~whygraph.core.utils.LIKE_ESCAPE_CHAR`);
        ``exact`` is the path for an exact comparison, or ``None`` for a
        prefix entry.
    """
    predicates: list[PathPredicate] = []
    for raw in area.paths:
        path = raw.strip().removeprefix("./")
        if not path:
            continue
        if path.endswith("/"):
            predicates.append((f"{like_escape(path)}%", None))
        else:
            predicates.append((f"{like_escape(path)}/%", path))
    return predicates


def path_in_area(area: AreaConfig, path: str) -> bool:
    """Return whether ``path`` falls under one of the area's paths.

    The Python twin of :func:`path_predicates`, with the same semantics.

    Parameters
    ----------
    area : AreaConfig
        The area.
    path : str
        A repo-relative posix path.

    Returns
    -------
    bool
        ``True`` on a prefix or exact match.
    """
    for raw in area.paths:
        entry = raw.strip().removeprefix("./")
        if not entry:
            continue
        if entry.endswith("/"):
            if path.startswith(entry):
                return True
        elif path == entry or path.startswith(f"{entry}/"):
            return True
    return False


def areas_for_paths(paths: Iterable[str]) -> list[str]:
    """Return the names of the configured areas any of ``paths`` falls under.

    Parameters
    ----------
    paths : iterable of str
        Repo-relative posix paths.

    Returns
    -------
    list of str
        Area names, sorted.
    """
    paths = list(paths)
    return [
        area.name
        for area in configured_areas()
        if any(path_in_area(area, p) for p in paths)
    ]


def areas_for_labels(labels: Iterable[str]) -> list[str]:
    """Return the names of the configured areas any of ``labels`` belongs to.

    Parameters
    ----------
    labels : iterable of str
        Issue / PR label names; compared case-insensitively.

    Returns
    -------
    list of str
        Area names, sorted.
    """
    have = {str(label).strip().lower() for label in labels}
    return [
        area.name
        for area in configured_areas()
        if have & {label.strip().lower() for label in area.labels}
    ]


def overview_block() -> dict:
    """Return the compact areas block for the chat repository overview.

    Returns
    -------
    dict
        ``{"items": [{"name", "description", "paths"}, ...]}`` with at most
        :data:`OVERVIEW_PATHS_PER_AREA` paths each. When the serialized block
        would exceed :data:`OVERVIEW_BUDGET_CHARS`, the items are names only
        and ``"truncated": True`` is added.
    """
    areas = configured_areas()
    block: dict = {
        "items": [
            {
                "name": a.name,
                "description": a.description,
                "paths": list(a.paths[:OVERVIEW_PATHS_PER_AREA]),
            }
            for a in areas
        ]
    }
    if len(json.dumps(block)) <= OVERVIEW_BUDGET_CHARS:
        return block
    return {"items": [{"name": a.name} for a in areas], "truncated": True}


def _slug(segment: str) -> str:
    """Lowercase ``segment`` into the area-name alphabet (may return ``""``)."""
    return re.sub(r"[^a-z0-9-]+", "-", segment.lower()).strip("-")[:40].strip("-")


def suggest_areas(session: Session) -> list[dict]:
    """Propose areas from where recent commits land in the tree.

    Looks at distinct commits of the last :data:`SUGGEST_DAYS` days (by
    ``authored_at``) and the directories their file changes touch, ignoring
    :data:`SUGGEST_DENYLIST` trees and top-level files. From the repo root it
    descends while a single child directory holds more than 70 % of the
    commits under the current directory (up to :data:`SUGGEST_MAX_DEPTH`
    levels), so ``src/<pkg>/`` and ``packages/*`` layouts surface their real
    components. It then proposes each child holding at least 5 % of those
    commits, busiest first, at most :data:`SUGGEST_MAX`. Nothing is saved.

    Parameters
    ----------
    session : sqlmodel.Session
        An open session on the project's WhyGraph database.

    Returns
    -------
    list of dict
        ``{"name", "paths", "labels": [], "description": ""}`` entries, each
        valid under :func:`~whygraph.core.config.area_errors`; at most
        :data:`SUGGEST_MAX`, so within ``MAX_AREAS``.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SUGGEST_DAYS)).isoformat()
    rows = session.exec(
        select(CommitFileChange.commit_sha, CommitFileChange.path)
        .join(Commit, col(Commit.sha) == col(CommitFileChange.commit_sha))
        .where(col(Commit.authored_at) >= cutoff)
    ).all()
    # Commits per directory prefix, for every directory above a file.
    commits: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for sha, path in rows:
        parts = tuple(p for p in path.split("/") if p)
        if len(parts) < 2 or SUGGEST_DENYLIST.intersection(parts[:-1]):
            continue
        for depth in range(1, len(parts)):
            commits[parts[:depth]].add(sha)

    def children(prefix: tuple[str, ...]) -> list[tuple[tuple[str, ...], int]]:
        found = [
            (key, len(shas))
            for key, shas in commits.items()
            if len(key) == len(prefix) + 1 and key[: len(prefix)] == prefix
        ]
        return sorted(found, key=lambda item: (-item[1], item[0]))

    prefix: tuple[str, ...] = ()
    kids = children(prefix)
    total = len({sha for shas in commits.values() for sha in shas})
    while kids and len(prefix) < SUGGEST_MAX_DEPTH:
        top, top_count = kids[0]
        if top_count <= SUGGEST_DESCEND_SHARE * total:
            break
        sub = children(top)
        if not sub:
            break
        prefix, total, kids = top, top_count, sub

    suggestions: list[dict] = []
    taken: set[str] = set()
    for key, count in kids:
        if len(suggestions) >= SUGGEST_MAX:
            break
        if count < SUGGEST_MIN_SHARE * total:
            continue
        name = _slug(key[-1])
        if name in taken and len(key) > 1:
            name = _slug(f"{key[-2]}-{key[-1]}")
        entry = {
            "name": name,
            "paths": ["/".join(key) + "/"],
            "labels": [],
            "description": "",
        }
        body = {k: v for k, v in entry.items() if k != "name"}
        if name in taken or area_errors(name, body):
            continue
        taken.add(name)
        suggestions.append(entry)
    return suggestions
