"""Production org members: list, add by GitHub username, re-role, remove, leave.

M2d-1 plan section 4.5. Every route is **production-only** - the router's
:func:`~whygraph.portal.deps.require_production` runs before the route's
:func:`~whygraph.portal.deps.org_access`, so local mode answers ``404`` -
and org-scoped: the org is the one ``Host`` names.

Admins manage people (``org.members``); three **owner rules** keep
ownership an owner's to give or take: only an owner may set the role
``owner`` (on add or change), and only an owner may change or remove
someone whose current role is ``owner`` - anyone else gets ``403
owner_required``. An org is never left without an owner (``409
last_owner``): a role change, a removal and leaving each lock the org's
owner rows (``SELECT ... FOR UPDATE``) first and read the target's current
role after that, so two concurrent demotions, or a promotion racing an
admin's change, are serialized.

Membership is read per request and never cached
(:func:`~whygraph.portal.deps.load_org_access`), so every change here
applies to the member's next request.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, func, select

from . import connections
from .audit import audit
from .authz import ROLES, Action, OrgAccess, Role
from .db import get_session
from .deps import ApiError, current_user, org_access, portal_state, require_production
from .models import Membership, User
from .orgs import add_member
from .security import Principal

GITHUB_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
"""A GitHub username: alphanumerics and single inner hyphens, 1-39 characters."""

NO_SUCH_USER = (
    "No one with that GitHub username has signed in to WhyGraph yet. Ask them to "
    "sign in once, then add them."
)
"""The ``404 no_such_user`` message, shown verbatim by the Members page."""

members_router = APIRouter(dependencies=[Depends(require_production)])
"""Every route of this module; included before the ``/api`` 404 catch-all."""


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemberAddBody(_Strict):
    """``POST /api/org/members``."""

    github_login: str = Field(max_length=100)
    role: str = Field(max_length=20)


class MemberRoleBody(_Strict):
    """``PATCH /api/org/members/{uid}``."""

    role: str = Field(max_length=20)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def normalize_login(raw: str) -> str | None:
    """Return the GitHub username in ``raw``, or ``None`` when it is not one.

    Surrounding whitespace and one leading ``@`` are dropped; the rest
    must match :data:`GITHUB_LOGIN_RE` as a whole.

    Parameters
    ----------
    raw : str
        What the user typed.

    Returns
    -------
    str or None
        The username, case kept.
    """
    login = raw.strip()
    if login.startswith("@"):
        login = login[1:]
    return login if GITHUB_LOGIN_RE.fullmatch(login) else None


def _checked_role(value: str) -> str:
    """``value`` when a membership may store it, else ``422 bad_role``."""
    if value not in ROLES:
        raise ApiError(422, f"role must be one of {', '.join(ROLES)}", code="bad_role")
    return value


def _owner_required() -> ApiError:
    return ApiError(
        403,
        "only an owner can make someone an owner, or change or remove an owner",
        code="owner_required",
    )


def _last_owner() -> ApiError:
    return ApiError(
        409,
        "this is the organization's last owner: make someone else an owner first",
        code="last_owner",
    )


def _not_member() -> ApiError:
    return ApiError(404, "no such member in this organization", code="not_member")


def _member_row(user: User, role: str, joined_at: str) -> dict:
    """One member as the API shows it (never an email)."""
    return {
        "uid": user.uid,
        "display_name": user.display_name,
        "github_login": user.github_login,
        "avatar_url": user.avatar_url,
        "role": role,
        "joined_at": joined_at,
        "disabled": user.disabled_at is not None,
    }


def _lock_owners(db: Session, org_id: int) -> list[int]:
    """Lock the org's owner rows (``SELECT ... FOR UPDATE``); their user ids.

    Every role change, removal and leave takes this lock before it reads
    the target's role, so the owner count and the owner rules see one
    consistent state.
    """
    return list(
        db.exec(
            select(Membership.user_id)
            .where(Membership.org_id == org_id, Membership.role == Role.OWNER.value)
            .with_for_update()
        ).all()
    )


def _locked_target(db: Session, org_id: int, uid: str) -> tuple[Membership, User]:
    """The membership of user ``uid`` in the org, locked; ``404 not_member``."""
    row = db.exec(
        select(Membership, User)
        .join(User, col(User.id) == col(Membership.user_id))
        .where(Membership.org_id == org_id, User.uid == uid)
        .with_for_update(of=Membership)
    ).first()
    if row is None:
        raise _not_member()
    return row


def _acts_as_owner(access: OrgAccess, principal: Principal, owners: list[int]) -> bool:
    """Whether the caller is an owner, re-checked against the locked owner rows."""
    return access.role == Role.OWNER and principal.user_id in owners


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@members_router.get("/api/org/members")
def get_members(access: OrgAccess = Depends(org_access(Action.ORG_READ))) -> list[dict]:
    """The org's members, oldest membership first (a ``reader`` sees them too)."""
    with get_session() as db:
        rows = db.exec(
            select(User, Membership.role, Membership.created_at)
            .join(Membership, col(Membership.user_id) == col(User.id))
            .where(Membership.org_id == access.org_id)
            .order_by(col(Membership.created_at), col(User.id))
        ).all()
        return [_member_row(user, role, joined) for user, role, joined in rows]


@members_router.post("/api/org/members", status_code=201)
def post_member(
    body: MemberAddBody,
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_MEMBERS)),
    principal: Principal = Depends(current_user),
) -> dict:
    """Add someone by exact GitHub username; they are a member at once.

    ``422 bad_login`` (not a GitHub username; a leading ``@`` is fine),
    ``422 bad_role``, ``403 owner_required`` (only an owner adds an
    owner), ``429`` past 60 attempts per org per hour, ``404
    no_such_user`` (nobody with that username, case-insensitively, has
    signed in - a password account has none), ``409 user_disabled``,
    ``409 already_member``. Every refusal is audited with the tried login.

    Returns
    -------
    dict
        The new member's row (``201``).
    """
    state = portal_state(request)

    def refused(reason: str, login: str | None) -> None:
        audit(
            "member_add_refused",
            request,
            uid=principal.uid,
            org=access.org_slug,
            reason=reason,
            github_login=login,
        )

    login = normalize_login(body.github_login)
    if login is None:
        refused("bad_login", repr(body.github_login[:64]))
        raise ApiError(422, "that is not a GitHub username", code="bad_login")
    try:
        role = _checked_role(body.role)
    except ApiError:
        refused("bad_role", login)
        raise
    if role == Role.OWNER.value and access.role != Role.OWNER:
        refused("owner_required", login)
        raise _owner_required()
    retry_after = state.member_add_org.hit(access.org_id)
    if retry_after is not None:
        refused("throttled", login)
        raise ApiError(
            429,
            "too many attempts; try again later",
            code="throttled",
            headers={"Retry-After": str(retry_after)},
        )
    already = ApiError(
        409, "they are already a member of this organization", code="already_member"
    )
    try:
        with get_session() as db:
            user = db.exec(
                select(User).where(func.lower(col(User.github_login)) == login.lower())
            ).first()
            if user is None:
                refused("no_such_user", login)
                raise ApiError(404, NO_SUCH_USER, code="no_such_user")
            assert user.id is not None
            if user.disabled_at is not None:
                refused("user_disabled", login)
                raise ApiError(409, "this account is disabled", code="user_disabled")
            if db.get(Membership, (access.org_id, user.id)) is not None:
                refused("already_member", login)
                raise already
            membership = add_member(
                db, org_id=access.org_id, user_id=user.id, role=role
            )
            row = _member_row(user, membership.role, membership.created_at)
    except IntegrityError:  # a concurrent add of the same person won
        refused("already_member", login)
        raise already from None
    audit(
        "member_added",
        request,
        uid=principal.uid,
        target=row["uid"],
        org=access.org_slug,
        role=role,
        github_login=row["github_login"],
    )
    return row


@members_router.patch("/api/org/members/{uid}")
def patch_member(
    uid: str,
    body: MemberRoleBody,
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_MEMBERS)),
    principal: Principal = Depends(current_user),
) -> dict:
    """Change a member's role.

    ``422 bad_role``, ``404 not_member``, ``403 owner_required`` (setting
    ``owner``, or changing an owner, as a non-owner), ``409 last_owner``
    (demoting the final owner). An admin may demote themselves.

    Returns
    -------
    dict
        The member's row.
    """
    role = _checked_role(body.role)
    with get_session() as db:
        owners = _lock_owners(db, access.org_id)
        membership, user = _locked_target(db, access.org_id, uid)
        previous = membership.role
        owner = Role.OWNER.value
        if (role == owner or previous == owner) and not _acts_as_owner(
            access, principal, owners
        ):
            raise _owner_required()
        if previous == owner and role != owner and not set(owners) - {user.id}:
            raise _last_owner()
        membership.role = role
        db.add(membership)
        db.flush()
        row = _member_row(user, role, membership.created_at)
    if previous != role:
        audit(
            "member_role_changed",
            request,
            uid=principal.uid,
            target=uid,
            org=access.org_slug,
            role=role,
            previous=previous,
        )
    return row


@members_router.delete("/api/org/members/{uid}", status_code=204)
def delete_member(
    uid: str,
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_MEMBERS)),
    principal: Principal = Depends(current_user),
) -> Response:
    """Remove a member (their chat sessions stay theirs, invisible to others).

    Their connection tokens for the org's projects are revoked
    (``member_removed``) in the same transaction.

    ``404 not_member``, ``403 owner_required`` (removing an owner as a
    non-owner), ``409 last_owner``. ``204``.
    """
    with get_session() as db:
        owners = _lock_owners(db, access.org_id)
        membership, user = _locked_target(db, access.org_id, uid)
        previous = membership.role
        if previous == Role.OWNER.value:
            if not _acts_as_owner(access, principal, owners):
                raise _owner_required()
            if not set(owners) - {user.id}:
                raise _last_owner()
        db.delete(membership)
        assert user.id is not None
        connections.revoke_for_member(db, access.org_id, user.id, "member_removed")
    audit(
        "member_removed",
        request,
        uid=principal.uid,
        target=uid,
        org=access.org_slug,
        role=previous,
    )
    return Response(status_code=204)


@members_router.delete("/api/org/membership", status_code=204)
def delete_membership(
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_READ)),
    principal: Principal = Depends(current_user),
) -> Response:
    """Leave the org (any member; a ``reader`` is refused before this runs).

    The caller's connection tokens for the org's projects are revoked
    (``member_left``) in the same transaction.

    ``409 last_owner`` for the final owner. ``204``.
    """
    with get_session() as db:
        owners = _lock_owners(db, access.org_id)
        membership = db.exec(
            select(Membership)
            .where(
                Membership.org_id == access.org_id,
                Membership.user_id == principal.user_id,
            )
            .with_for_update()
        ).first()
        if membership is None:  # removed meanwhile
            raise _not_member()
        previous = membership.role
        if previous == Role.OWNER.value and not set(owners) - {principal.user_id}:
            raise _last_owner()
        db.delete(membership)
        connections.revoke_for_member(
            db, access.org_id, principal.user_id, "member_left"
        )
    audit("member_left", request, uid=principal.uid, org=access.org_slug, role=previous)
    return Response(status_code=204)


__all__ = [
    "GITHUB_LOGIN_RE",
    "NO_SUCH_USER",
    "MemberAddBody",
    "MemberRoleBody",
    "members_router",
    "normalize_login",
]
