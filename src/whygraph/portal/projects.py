"""Project registry helpers: slug rules.

A project's slug is its identity across the portal: it builds the clone
path (``repos/<slug>``), is interpolated by the host hook helper and the
agent MCP URL (``/mcp/<slug>``), and scopes every query key. That is why
the rules are strict and the slug is **immutable** once a row exists
(:mod:`whygraph.portal.models` enforces it on flush); ``PATCH`` renames
only ``name``.
"""

from __future__ import annotations

import re

from sqlmodel import Session

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
"""Valid slug: lower-case alphanumerics and dashes, 1-63 chars, no leading dash."""

_MAX_LEN = 63
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def is_valid_slug(slug: str) -> bool:
    """Return whether ``slug`` matches :data:`SLUG_RE` exactly.

    Parameters
    ----------
    slug : str
        Candidate slug.

    Returns
    -------
    bool
        ``True`` for a valid slug. A trailing newline is *not* valid
        (``$`` alone would allow it), so a slug can never smuggle a line
        break into a hook file or URL.
    """
    return SLUG_RE.fullmatch(slug) is not None


def validate_slug(slug: str) -> str:
    """Return ``slug`` unchanged, or raise if it breaks the slug rules.

    Parameters
    ----------
    slug : str
        Candidate slug.

    Returns
    -------
    str
        The same slug.

    Raises
    ------
    ValueError
        If ``slug`` does not match :data:`SLUG_RE`.
    """
    if not is_valid_slug(slug):
        raise ValueError(f"invalid project slug {slug!r}: must match {SLUG_RE.pattern}")
    return slug


def slugify(name: str) -> str:
    """Derive a slug from a repository or project name.

    Lower-cases, turns every run of other characters into ``-``, strips
    leading and trailing dashes and truncates to 63 characters.

    Parameters
    ----------
    name : str
        A repo name such as ``"My Repo_v2"``.

    Returns
    -------
    str
        A valid slug; ``"project"`` when nothing usable remains.

    Examples
    --------
    >>> slugify("My Repo_v2")
    'my-repo-v2'
    """
    slug = _NON_SLUG.sub("-", name.lower()).strip("-")[:_MAX_LEN].strip("-")
    return slug or "project"


def unique_slug(session: Session, name: str) -> str:
    """Return a slug for ``name`` that no project row uses yet.

    On collision a numeric suffix is appended (``foo``, ``foo-2``,
    ``foo-3``), shortening the base so the result stays within 63 chars.

    Parameters
    ----------
    session : Session
        A portal DB session.
    name : str
        The name to derive the slug from (see :func:`slugify`).

    Returns
    -------
    str
        A valid, currently unused slug. Not reserved: two racing callers
        can pick the same one, and the ``UNIQUE (slug)`` constraint then
        rejects the second insert.
    """
    from sqlmodel import select

    from .models import Project

    base = slugify(name)
    taken = set(session.exec(select(Project.slug)).all())
    candidate = base
    n = 1
    while candidate in taken:
        n += 1
        suffix = f"-{n}"
        candidate = base[: _MAX_LEN - len(suffix)].rstrip("-") + suffix
    return candidate


__all__ = ["SLUG_RE", "is_valid_slug", "slugify", "unique_slug", "validate_slug"]
