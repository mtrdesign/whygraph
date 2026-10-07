"""Roles, actions and the single authorization check.

Pure module: an org role maps to a set of org actions (:data:`ROLE_ACTIONS`),
an effective project role to a set of project actions
(:data:`PROJECT_ROLE_ACTIONS`, the role from :func:`effective_project_role`),
and :func:`authorize` is the one place a request is allowed or refused.
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


class ProjectRole(StrEnum):
    """A user's effective role on one project (M2f-1).

    Shares the value ``"admin"`` with :attr:`Role.ADMIN` (both are
    ``StrEnum``), so the two must never be looked up in each other's table:
    :func:`allowed` and :func:`project_allowed` refuse the other enum.
    """

    ADMIN = "admin"
    CONTRIBUTOR = "contributor"
    VIEWER = "viewer"


class Action(StrEnum):
    """Something a request wants to do, checked by :func:`authorize`.

    Org actions (``org.*``) are checked against the caller's org role
    (:data:`ROLE_ACTIONS`); project actions (:data:`PROJECT_ACTIONS`)
    against the caller's effective role on the project
    (:data:`PROJECT_ROLE_ACTIONS`).
    """

    ORG_READ = "org.read"
    ORG_ADD_PROJECT = "org.add_project"
    ORG_REMOVE_PROJECT = "org.remove_project"
    """Remove a project from the org (admin and owner; M2f-1 plan section
    0.2 #7)."""
    ORG_CONFIGURE = "org.configure"
    """The org settings page: defaults and org-level keys (owner only)."""
    ORG_MEMBERS = "org.members"
    """Add, re-role and remove members (admin and owner; the owner rules of
    :mod:`whygraph.portal.member_routes` apply inside it)."""
    ORG_OWN = "org.own"
    ORG_AUDIT = "org.audit"
    """Read the org's audit log (owner only)."""
    ORG_USAGE = "org.usage"
    """Read the org's LLM usage and cost, every member's included (owner,
    admin and the instance-admin reader)."""
    ORG_BUDGETS = "org.budgets"
    """Set and remove budgets and price overrides (admin and owner; the
    self / owner limit is enforced in the route)."""
    PROJECT_READ = "project.read"
    PROJECT_CHAT = "project.chat"
    PROJECT_SCAN = "project.scan"
    PROJECT_SCAN_FULL = "project.scan_full"
    """Start (or cancel) a scan that spends LLM money (project admin)."""
    PROJECT_CONFIGURE = "project.configure"
    PROJECT_SETUP = "project.setup"
    PROJECT_ACCESS = "project.access"
    """Read and change who may use the project (project admin)."""
    PROJECT_USAGE = "project.usage"
    """Read the project's LLM usage and cost (project admin)."""
    USER_SELF = "user.self"
    """Any signed-in user acting on their own account (in no role's set)."""
    INSTANCE_ADMIN = "instance.admin"
    """Instance administration (in no role's set; ``users.is_instance_admin``)."""


_ORG_MEMBER = frozenset({Action.ORG_READ})
_ORG_READER = _ORG_MEMBER | {Action.ORG_USAGE}
_ORG_ADMIN = _ORG_READER | {
    Action.ORG_ADD_PROJECT,
    Action.ORG_REMOVE_PROJECT,
    Action.ORG_MEMBERS,
    Action.ORG_BUDGETS,
}

ROLE_ACTIONS: dict[Role, frozenset[Action]] = {
    Role.MEMBER: _ORG_MEMBER,
    Role.ADMIN: _ORG_ADMIN,
    Role.OWNER: _ORG_ADMIN | {Action.ORG_CONFIGURE, Action.ORG_OWN, Action.ORG_AUDIT},
    Role.READER: _ORG_READER,
}
"""The **org** actions each org role may perform (never a project action)."""

_VIEWER = frozenset({Action.PROJECT_READ})
_CONTRIBUTOR = _VIEWER | {Action.PROJECT_CHAT, Action.PROJECT_SCAN}

PROJECT_ROLE_ACTIONS: dict[ProjectRole, frozenset[Action]] = {
    ProjectRole.VIEWER: _VIEWER,
    ProjectRole.CONTRIBUTOR: _CONTRIBUTOR,
    ProjectRole.ADMIN: _CONTRIBUTOR
    | {
        Action.PROJECT_SCAN_FULL,
        Action.PROJECT_CONFIGURE,
        Action.PROJECT_SETUP,
        Action.PROJECT_ACCESS,
        Action.PROJECT_USAGE,
    },
}
"""The project actions each effective project role may perform."""

PROJECT_ACTIONS: frozenset[Action] = frozenset(PROJECT_ROLE_ACTIONS[ProjectRole.ADMIN])
"""Every project-scoped action: explicit, never a prefix match on ``project.``."""


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
    user_id : int
        ``users.id`` of the caller (the project grants are looked up by it).
    default_project_role : str
        The org's default project role, one of
        :data:`DEFAULT_PROJECT_ROLES`.
    """

    org_id: int
    org_slug: str
    org_name: str
    role: Role
    user_id: int
    default_project_role: str


def allowed(role: Role, action: Action) -> bool:
    """Return whether the org role ``role`` may perform the org action ``action``.

    Raises
    ------
    TypeError
        When ``role`` is not a :class:`Role` (a :class:`ProjectRole` shares
        the value ``"admin"`` and would otherwise be looked up silently).
    """
    if not isinstance(role, Role):
        raise TypeError(f"not an org role: {role!r}")
    return action in ROLE_ACTIONS[role]


def project_allowed(role: ProjectRole, action: Action) -> bool:
    """Return whether the project role ``role`` may perform ``action``.

    Raises
    ------
    TypeError
        When ``role`` is not a :class:`ProjectRole`.
    """
    if not isinstance(role, ProjectRole):
        raise TypeError(f"not a project role: {role!r}")
    return action in PROJECT_ROLE_ACTIONS[role]


def effective_project_role(
    org_role: Role,
    default: str,
    restricted: bool,
    grant: ProjectRole | None,
) -> ProjectRole | None:
    """The caller's role on one project, or ``None`` for no access.

    The one place the access rules live (M2f-1 plan section 0.2 #2-#4,
    #19): org owners and admins are project admins everywhere, an instance
    admin reading the org is a viewer everywhere, a grant replaces the org
    default (it can lower as well as raise), and without a grant a
    Restricted project or the default ``none`` means no access.

    Parameters
    ----------
    org_role : Role
        The caller's org role.
    default : str
        The org's default project role (:data:`DEFAULT_PROJECT_ROLES`).
    restricted : bool
        ``projects.restricted``.
    grant : ProjectRole or None
        The caller's grant on the project, if any.

    Returns
    -------
    ProjectRole or None
        The effective role; ``None`` means the project does not exist for
        the caller.
    """
    if org_role in (Role.OWNER, Role.ADMIN):
        return ProjectRole.ADMIN
    if org_role is Role.READER:
        return ProjectRole.VIEWER
    if grant is not None:
        return grant
    if restricted or default == "none":
        return None
    return ProjectRole(default)


class _Unset:
    """The type of :data:`_UNSET`."""

    def __repr__(self) -> str:
        return "<unset>"


_UNSET: Any = _Unset()
"""``project_role`` not computed: a programming error for a project check."""


def authorize(
    access: OrgAccess,
    action: Action,
    project: Any = None,
    *,
    project_role: ProjectRole | None = _UNSET,
) -> None:
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
    project_role : ProjectRole or None, optional
        The caller's effective role on ``project``
        (:func:`effective_project_role`); required whenever ``project`` is
        given. ``None`` means no access: ``404`` for every action, org
        actions included.

    Raises
    ------
    ApiError
        404 ``not found`` on an org mismatch or without access to
        ``project``; 403 with ``code="forbidden"`` when the role lacks the
        action.
    ValueError
        A project action without a ``project``, or a ``project`` without a
        computed ``project_role`` - a programming error, never a silent
        refusal.
    """
    from .deps import ApiError  # lazy: deps imports this module

    if project is not None:
        if project.org_id != access.org_id:
            raise ApiError(404, "not found")
        if project_role is _UNSET:
            raise ValueError(f"authorize({action}) on a project needs project_role")
        if project_role is None:
            raise ApiError(404, "not found")
    if action in PROJECT_ACTIONS:
        if project is None:
            raise ValueError(f"{action} is a project action and needs a project")
        if not project_allowed(project_role, action):
            raise ApiError(
                403,
                f"your role on this project ({project_role}) cannot {action}",
                code="forbidden",
                action=str(action),
            )
        return
    if not allowed(access.role, action):
        raise ApiError(
            403,
            f"your role ({access.role}) cannot {action}",
            code="forbidden",
            action=str(action),
        )
