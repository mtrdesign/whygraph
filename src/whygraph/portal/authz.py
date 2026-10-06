"""Roles, actions and the single authorization check.

Pure module: a role maps to a set of actions (:data:`ROLE_ACTIONS`) and
:func:`authorize` is the one place a request is allowed or refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Role(StrEnum):
    """A member's role in an organization."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    READER = "reader"
    """Internal: an instance admin reading an org they are not a member of.

    Never stored in ``memberships`` (not in :data:`ROLES`)."""


ROLES: tuple[str, ...] = ("owner", "admin", "member")
"""The roles a membership may store (the CHECK constraint and
:func:`~whygraph.portal.orgs.add_member`). Explicit, so adding an enum member
such as :attr:`Role.READER` never widens what a membership may hold."""

PROJECT_ROLES: tuple[str, ...] = ("admin", "contributor", "viewer")
"""The roles a project grant may store (``project_grants.role``,
``invitation_grants.role``). Explicit, like :data:`ROLES`."""

DEFAULT_PROJECT_ROLES: tuple[str, ...] = ("contributor", "viewer", "none")
"""The values of an organization's default project role
(``organizations.default_project_role``): what a member holds on a project
they have no grant on."""


class Action(StrEnum):
    """Something a request wants to do, checked by :func:`authorize`."""

    ORG_READ = "org.read"
    ORG_ADD_PROJECT = "org.add_project"
    ORG_CONFIGURE = "org.configure"
    """The org settings page: defaults and org-level keys (owner only)."""
    ORG_MEMBERS = "org.members"
    """Add, re-role and remove members (admin and owner; the owner rules of
    :mod:`whygraph.portal.member_routes` apply inside it)."""
    ORG_OWN = "org.own"
    PROJECT_READ = "project.read"
    PROJECT_CHAT = "project.chat"
    PROJECT_SCAN = "project.scan"
    PROJECT_CONFIGURE = "project.configure"
    PROJECT_SETUP = "project.setup"
    USER_SELF = "user.self"
    """Any signed-in user acting on their own account (in no role's set)."""
    INSTANCE_ADMIN = "instance.admin"
    """Instance administration (in no role's set; ``users.is_instance_admin``)."""


_MEMBER = frozenset(
    {Action.ORG_READ, Action.PROJECT_READ, Action.PROJECT_CHAT, Action.PROJECT_SCAN}
)
_ADMIN = _MEMBER | {
    Action.ORG_ADD_PROJECT,
    Action.ORG_MEMBERS,
    Action.PROJECT_CONFIGURE,
    Action.PROJECT_SETUP,
}

ROLE_ACTIONS: dict[Role, frozenset[Action]] = {
    Role.MEMBER: _MEMBER,
    Role.ADMIN: _ADMIN,
    Role.OWNER: _ADMIN | {Action.ORG_CONFIGURE, Action.ORG_OWN},
    Role.READER: frozenset({Action.ORG_READ, Action.PROJECT_READ}),
}
"""The actions each role may perform."""


@dataclass(frozen=True)
class OrgAccess:
    """What the current request may do in which organization.

    Attributes
    ----------
    org_id : int
        Organization primary key.
    org_slug : str
        Organization slug.
    org_name : str
        Organization display name.
    role : Role
        The caller's role in it.
    """

    org_id: int
    org_slug: str
    org_name: str
    role: Role


def allowed(role: Role, action: Action) -> bool:
    """Return whether ``role`` may perform ``action``."""
    return action in ROLE_ACTIONS[role]


def authorize(access: OrgAccess, action: Action, project: Any = None) -> None:
    """Refuse the request unless ``access`` may perform ``action``.

    Parameters
    ----------
    access : OrgAccess
        The caller's access to the org the route names.
    action : Action
        What is being attempted.
    project : object, optional
        Anything with an ``org_id`` (a bound project). If it belongs to
        another org the caller sees a 404 (defence in depth).

    Raises
    ------
    ApiError
        404 ``not found`` on an org mismatch; 403 with ``code="forbidden"``
        when the role lacks the action.
    """
    from .deps import ApiError  # lazy: deps imports this module

    if project is not None and project.org_id != access.org_id:
        raise ApiError(404, "not found")
    if not allowed(access.role, action):
        raise ApiError(
            403,
            f"your role ({access.role}) cannot {action}",
            code="forbidden",
            action=str(action),
        )
