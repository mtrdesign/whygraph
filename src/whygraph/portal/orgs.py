"""Organization slug rules and the organization helpers.

An organization's slug goes into a DNS label and a URL host, so the rules
are strict and the slug is immutable once a row exists. The ORM listeners
and the CHECK constraint reuse :data:`ORG_SLUG_RE` and
:data:`ORG_SLUG_SQL_CHECK`. The helpers (:func:`create_org`,
:func:`ensure_builtin_org`, :func:`add_member`) take a portal DB session;
they import the models lazily, because :mod:`whygraph.portal.models`
imports this module.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .authz import Role

if TYPE_CHECKING:
    from sqlmodel import Session

    from .models import Membership, Organization, Setting

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


def create_org(session: Session, *, slug: str, name: str) -> Organization:
    """Insert an organization and flush it.

    Parameters
    ----------
    session : Session
        A portal DB session; the caller commits.
    slug : str
        The org slug (see :func:`validate_org_slug`).
    name : str
        Display name.

    Returns
    -------
    Organization
        The flushed row (``id`` set).

    Raises
    ------
    ValueError
        If the slug breaks the rules.
    """
    from .models import Organization

    validate_org_slug(slug)
    org = Organization(slug=slug, name=name)
    session.add(org)
    session.flush()
    return org


def ensure_builtin_org(session: Session, setting: Setting) -> Organization:
    """Return local mode's built-in org, creating and linking it when missing.

    Idempotent: the org ``settings.builtin_org_id`` names is returned as
    is; else an existing org of slug :data:`BUILTIN_ORG_SLUG` is linked;
    else that org is created. A second built-in org is never created.

    Parameters
    ----------
    session : Session
        A portal DB session; the caller commits.
    setting : Setting
        The ``settings`` row, possibly only ``session.add``-ed so far (it
        is flushed before ``builtin_org_id`` is set).

    Returns
    -------
    Organization
        The built-in org (``id`` set).
    """
    from sqlmodel import select

    from .models import Organization

    if setting.builtin_org_id is not None:
        org = session.get(Organization, setting.builtin_org_id)
        if org is not None:
            return org
    org = session.exec(
        select(Organization).where(Organization.slug == BUILTIN_ORG_SLUG)
    ).first()
    if org is None:
        org = create_org(session, slug=BUILTIN_ORG_SLUG, name=BUILTIN_ORG_NAME)
    setting.builtin_org_id = org.id
    session.add(setting)
    session.flush()
    return org


def add_member(
    session: Session, *, org_id: int, user_id: int, role: Role | str
) -> Membership:
    """Make a user a member of an organization and flush it.

    Parameters
    ----------
    session : Session
        A portal DB session; the caller commits.
    org_id : int
        The organization.
    user_id : int
        The user.
    role : Role or str
        ``"owner"``, ``"admin"`` or ``"member"``.

    Returns
    -------
    Membership
        The flushed row.

    Raises
    ------
    ValueError
        On an unknown role.
    """
    from .models import Membership

    membership = Membership(org_id=org_id, user_id=user_id, role=Role(role).value)
    session.add(membership)
    session.flush()
    return membership
