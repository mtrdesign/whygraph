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


ROLES = tuple(r.value for r in Role)
"""Role values, for a CHECK constraint."""


class Action(StrEnum):
    """Something a request wants to do, checked by :func:`authorize`."""

    ORG_READ = "org.read"
    ORG_ADD_PROJECT = "org.add_project"
    ORG_CONFIGURE = "org.configure"
    ORG_OWN = "org.own"
    PROJECT_READ = "project.read"
    PROJECT_CHAT = "project.chat"
    PROJECT_SCAN = "project.scan"
    PROJECT_CONFIGURE = "project.configure"
    PROJECT_SETUP = "project.setup"


_MEMBER = frozenset(
    {Action.ORG_READ, Action.PROJECT_READ, Action.PROJECT_CHAT, Action.PROJECT_SCAN}
)
_ADMIN = _MEMBER | {
    Action.ORG_ADD_PROJECT,
    Action.ORG_CONFIGURE,
    Action.PROJECT_CONFIGURE,
    Action.PROJECT_SETUP,
}

ROLE_ACTIONS: dict[Role, frozenset[Action]] = {
    Role.MEMBER: _MEMBER,
    Role.ADMIN: _ADMIN,
    Role.OWNER: _ADMIN | {Action.ORG_OWN},
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
