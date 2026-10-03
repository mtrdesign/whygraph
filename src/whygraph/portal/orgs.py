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

from .authz import ROLES, Role

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
        *"""
        mx ftp ns ns1 ns2 ns3 ns4 dns imap pop pop3 webmail email autodiscover
        autoconfig mta-sts wpad isatap postmaster hostmaster webmaster abuse
        security noc root vpn secure m mobile beta demo internal intranet git
        files media img images download downloads public org orgs setup signin
        signout reset system null undefined example ws cache proxy
        """.split(),
    }
)
"""Slugs that would collide with platform hosts or routes.

Enforced only when an org is created (:func:`validate_org_slug`); lookups
check the format only, so growing this set never makes an existing org
unreachable. ``local`` is deliberately absent: it is the built-in org's slug.
"""

NEVER_ORG_HOSTS = frozenset(
    {
        "www",
        "api",
        "app",
        "admin",
        "auth",
        "mail",
        "smtp",
        "mx",
        "ns1",
        "ns2",
        "autodiscover",
        "autoconfig",
        "mta-sts",
        "status",
        "docs",
        "static",
        "assets",
        "cdn",
    }
)
"""Hosts under the base domain that are never an org, even if one exists.

Never grow this: an org that already holds such a slug would become
unreachable. Grow :data:`RESERVED_ORG_SLUGS` instead, which only blocks new
orgs.
"""

BUILTIN_ORG_SLUG = "local"
BUILTIN_ORG_NAME = "Local"


def is_valid_org_slug(slug: str) -> bool:
    """Return whether ``slug`` has a valid format.

    Format only: reserved names are not checked, because this gates lookups
    (an existing org must stay reachable when the reserved list grows). Use
    :func:`validate_org_slug` before creating an org.

    Parameters
    ----------
    slug : str
        Candidate slug.

    Returns
    -------
    bool
        ``True`` if ``slug`` matches :data:`ORG_SLUG_RE`.
    """
    return ORG_SLUG_RE.fullmatch(slug) is not None


def validate_org_slug(slug: str) -> None:
    """Raise if ``slug`` breaks the rules for a new org.

    Parameters
    ----------
    slug : str
        Candidate slug.

    Raises
    ------
    ValueError
        If the format is invalid (a trailing newline included), the slug
        is reserved, or its 3rd and 4th characters are ``--`` (``xn--``
        punycode lookalikes; RFC 5891 reserves the pattern).
    """
    if ORG_SLUG_RE.fullmatch(slug) is None:
        raise ValueError(
            f"invalid organization slug {slug!r}: use 1-40 lower-case letters, "
            "digits and dashes, starting and ending with a letter or digit"
        )
    if slug in RESERVED_ORG_SLUGS:
        raise ValueError(f"organization slug {slug!r} is reserved")
    if slug[2:4] == "--":
        raise ValueError(
            f"invalid organization slug {slug!r}: a double dash in the third "
            "and fourth positions is reserved"
        )


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
        On a role a membership may not store (an unknown one, or the
        internal ``reader``).
    """
    from .models import Membership

    value = Role(role).value
    if value not in ROLES:
        raise ValueError(f"role {value!r} cannot be stored in a membership")
    membership = Membership(org_id=org_id, user_id=user_id, role=value)
    session.add(membership)
    session.flush()
    return membership
