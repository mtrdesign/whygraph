"""Organization slug rules.

An organization's slug goes into a DNS label and a URL host, so the rules
are strict and the slug is immutable once a row exists. This module is
pure (no database): the ORM listeners and the CHECK constraint reuse
:data:`ORG_SLUG_RE` and :data:`ORG_SLUG_SQL_CHECK`.
"""

from __future__ import annotations

import re

ORG_SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")
"""Valid org slug: lower-case alphanumerics and inner dashes, 1-40 chars."""

ORG_SLUG_SQL_CHECK = "slug ~ '^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$'"
""":data:`ORG_SLUG_RE` as a Postgres predicate for a CHECK constraint."""

RESERVED_ORG_SLUGS = frozenset(
    {
        "www",
        "api",
        "app",
        "admin",
        "auth",
        "login",
        "logout",
        "signup",
        "register",
        "mail",
        "smtp",
        "status",
        "docs",
        "help",
        "support",
        "static",
        "assets",
        "cdn",
        "blog",
        "billing",
        "dashboard",
        "settings",
        "account",
        "accounts",
        "oauth",
        "sso",
        "id",
        "mcp",
        "portal",
        "whygraph",
        "localhost",
        "test",
        "dev",
        "staging",
        "prod",
    }
)
"""Slugs that would collide with platform hosts or routes."""

BUILTIN_ORG_SLUG = "local"
BUILTIN_ORG_NAME = "Local"


def is_valid_org_slug(slug: str) -> bool:
    """Return whether ``slug`` has a valid format and is not reserved.

    Parameters
    ----------
    slug : str
        Candidate slug.

    Returns
    -------
    bool
        ``True`` if :func:`validate_org_slug` would accept it.
    """
    return ORG_SLUG_RE.fullmatch(slug) is not None and slug not in RESERVED_ORG_SLUGS


def validate_org_slug(slug: str) -> None:
    """Raise if ``slug`` breaks the org slug rules.

    Parameters
    ----------
    slug : str
        Candidate slug.

    Raises
    ------
    ValueError
        If the format is invalid (a trailing newline included) or the
        slug is reserved.
    """
    if ORG_SLUG_RE.fullmatch(slug) is None:
        raise ValueError(
            f"invalid organization slug {slug!r}: use 1-40 lower-case letters, "
            "digits and dashes, starting and ending with a letter or digit"
        )
    if slug in RESERVED_ORG_SLUGS:
        raise ValueError(f"organization slug {slug!r} is reserved")
