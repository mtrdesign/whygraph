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

**Invitations** (M2f-1 plan section 4.8): adding resolves the username on
GitHub first and matches accounts and invitations by the **GitHub id**,
never by a stored login (logins are released and reused). Someone with an
account is added at once; anyone else gets an invitation (14 days, one open
per org and GitHub id) that :mod:`whygraph.portal.auth_routes` redeems on
that account's next sign-in. Project grants travel with either.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Integer, Text, and_, literal, or_, update
from sqlalchemy import select as sa_select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, col, delete, select

from . import connections
from .audit import audit
from .authz import PROJECT_ROLES, ROLES, Action, OrgAccess, Role
from .db import get_session
from .deps import ApiError, current_user, org_access, portal_state, require_production
from .github_auth import GitHubNoSuchUser, GitHubRateLimited, GitHubUnavailable
from .models import (
    Invitation,
    InvitationGrant,
    Membership,
    Organization,
    Project,
    ProjectGrant,
    User,
)
from .orgs import add_member
from .security import Principal

logger = logging.getLogger(__name__)

GITHUB_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
"""A GitHub username: alphanumerics and single inner hyphens, 1-39 characters."""

NO_SUCH_GITHUB_USER = "GitHub has no user with that username."
"""The ``404 no_such_github_user`` message (an organization's login counts too)."""

INVITATION_LIFETIME = timedelta(days=14)
"""How long an invitation stays open (M2f-1 plan section 0.2 #9)."""

INVITATION_HISTORY = timedelta(days=30)
"""How far back ``GET /api/org/invitations`` lists redeemed and revoked ones."""

GITHUB_LOOKUP_KEY = "anonymous"
"""The one key of the instance-wide ``github_lookup`` throttle."""

members_router = APIRouter(dependencies=[Depends(require_production)])
"""Every route of this module; included before the ``/api`` 404 catch-all."""


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GrantBody(_Strict):
    """One project grant of ``POST /api/org/members``."""

    project: str = Field(max_length=100)
    role: str = Field(max_length=20)


class MemberAddBody(_Strict):
    """``POST /api/org/members``."""

    github_login: str = Field(max_length=100)
    role: str = Field(max_length=20)
    grants: list[GrantBody] = Field(default_factory=list, max_length=500)


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    """The stored timestamp form (``models._now``'s): ISO-8601 UTC, seconds."""
    return moment.isoformat(timespec="seconds")


def _checked_grants(role: str, grants: list[GrantBody]) -> list[tuple[str, str]]:
    """``(project slug, project role)`` pairs a new member may carry; else ``422``.

    An org admin or owner is an admin of every project, so grants for them
    are ``422 grants_for_org_admin``; a role outside
    :data:`~whygraph.portal.authz.PROJECT_ROLES` is ``422 bad_role`` and a
    project named twice ``422 duplicate_grant``.
    """
    if grants and role in (Role.OWNER.value, Role.ADMIN.value):
        raise ApiError(
            422,
            "org admins and owners are admins of every project: drop the grants",
            code="grants_for_org_admin",
        )
    seen: set[str] = set()
    for grant in grants:
        if grant.role not in PROJECT_ROLES:
            raise ApiError(
                422,
                f"a project role must be one of {', '.join(PROJECT_ROLES)}",
                code="bad_role",
            )
        if grant.project in seen:
            raise ApiError(
                422, f"project {grant.project!r} is named twice", code="duplicate_grant"
            )
        seen.add(grant.project)
    return [(grant.project, grant.role) for grant in grants]


def _granted_projects(
    db: Session, org_id: int, grants: list[tuple[str, str]]
) -> list[tuple[int, str, str]]:
    """``(project id, slug, role)`` per grant, rows locked ``FOR SHARE``; else ``404``."""
    if not grants:
        return []
    slugs = [slug for slug, _ in grants]
    ids = dict(
        db.exec(
            select(Project.slug, Project.id)
            .where(Project.org_id == org_id, col(Project.slug).in_(slugs))
            .with_for_update(read=True)
        ).all()
    )
    for slug in slugs:
        if slug not in ids:
            raise ApiError(
                404, f"no project {slug!r} in this organization", code="no_such_project"
            )
    return [(ids[slug], slug, role) for slug, role in grants]


def _invitation_status(invitation: Invitation, now: str) -> str:
    if invitation.redeemed_at is not None:
        return "redeemed"
    if invitation.revoked_at is not None:
        return "revoked"
    return "expired" if invitation.expires_at <= now else "open"


def _invitation_row(
    invitation: Invitation,
    inviter: User | None,
    grants: list[tuple[str, str]],
    now: str,
) -> dict:
    """One invitation as the API shows it (never an email)."""
    return {
        "uid": invitation.uid,
        "github_login": invitation.github_login,
        "role": invitation.role,
        "status": _invitation_status(invitation, now),
        "invited_by": None
        if inviter is None
        else {
            "uid": inviter.uid,
            "display_name": inviter.display_name,
            "github_login": inviter.github_login,
        },
        "created_at": invitation.created_at,
        "expires_at": invitation.expires_at,
        "redeemed_at": invitation.redeemed_at,
        "revoked_at": invitation.revoked_at,
        "grants": [{"project": slug, "role": role} for slug, role in grants],
    }


def _open_invitation(github_id: int, now: str) -> list:
    """``WHERE`` clauses of an open, unexpired invitation for ``github_id``."""
    return [
        Invitation.github_id == github_id,
        col(Invitation.redeemed_at).is_(None),
        col(Invitation.revoked_at).is_(None),
        col(Invitation.expires_at) > now,
    ]


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
    """Add someone by GitHub username: at once with an account, else invite them.

    The username is resolved on GitHub and matched by the **GitHub id**
    (M2f-1 plan section 0.2 #8): an account with that id becomes a member
    now, with the ``grants``; with no account an invitation is opened for
    the id (14 days), redeemed on that account's first sign-in. An expired
    open invitation is closed first.

    ``422 bad_login`` (not a GitHub username; a leading ``@`` is fine),
    ``422 bad_role``, ``422 grants_for_org_admin`` (grants with the role
    ``admin`` / ``owner``), ``422 duplicate_grant``, ``403 owner_required``
    (only an owner adds an owner), ``429`` past 60 attempts per org per
    hour, ``404 no_such_github_user`` (GitHub has no such user, or it is an
    organization), ``502 github_unavailable``, ``503 github_rate_limited``
    (with ``Retry-After``), ``404 no_such_project`` (a grant's project is
    not in the org), ``409 user_disabled``, ``409 already_member``, ``409
    already_invited``. Every refusal is audited with the tried login.

    Returns
    -------
    dict
        The new member's row, or the invitation's row with ``"pending":
        true`` (``201``).
    """
    state = portal_state(request)

    def refused(reason: str, login: str | None) -> None:
        audit(
            "member_add_refused",
            request,
            uid=principal.uid,
            org_id=access.org_id,
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
        grants = _checked_grants(role, body.grants)
    except ApiError as exc:
        refused(exc.code or "bad_request", login)
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
    github = state.github
    assert github is not None  # production start-up built it
    try:
        found = github.user_by_login(
            login,
            anonymous_budget=lambda: state.github_lookup.hit(GITHUB_LOOKUP_KEY),
        )
    except GitHubNoSuchUser:
        refused("no_such_github_user", login)
        raise ApiError(404, NO_SUCH_GITHUB_USER, code="no_such_github_user") from None
    except GitHubRateLimited as exc:
        refused("github_rate_limited", login)
        raise ApiError(
            503,
            "GitHub's rate limit was reached: try again later",
            code="github_rate_limited",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except GitHubUnavailable:
        refused("github_unavailable", login)
        raise ApiError(
            502,
            "GitHub could not be reached: try again in a moment",
            code="github_unavailable",
        ) from None
    already = ApiError(
        409, "they are already a member of this organization", code="already_member"
    )
    invited = ApiError(
        409,
        "they already have an open invitation to this organization",
        code="already_invited",
    )
    conflict = already
    invitation_uid: str | None = None
    try:
        with get_session() as db:
            # Held FOR SHARE (as the import does), so a concurrent org
            # deletion either finished first or waits for this transaction.
            if (
                db.exec(
                    select(Organization.id)
                    .where(Organization.id == access.org_id)
                    .with_for_update(read=True)
                ).first()
                is None
            ):
                raise ApiError(404, "not found")
            try:
                projects = _granted_projects(db, access.org_id, grants)
            except ApiError:
                refused("no_such_project", login)
                raise
            user = db.exec(select(User).where(User.github_id == found.id)).first()
            now = _now()
            if user is not None:
                assert user.id is not None
                if user.disabled_at is not None:
                    refused("user_disabled", login)
                    raise ApiError(
                        409, "this account is disabled", code="user_disabled"
                    )
                if db.get(Membership, (access.org_id, user.id)) is not None:
                    refused("already_member", login)
                    raise already
                membership = add_member(
                    db, org_id=access.org_id, user_id=user.id, role=role
                )
                for project_id, _, project_role in projects:
                    db.add(
                        ProjectGrant(
                            org_id=access.org_id,
                            project_id=project_id,
                            user_id=user.id,
                            role=project_role,
                            granted_by=principal.user_id,
                            created_at=_iso(now),
                        )
                    )
                db.flush()
                row = _member_row(user, membership.role, membership.created_at)
            else:
                conflict = invited
                db.exec(
                    update(Invitation)
                    .where(
                        col(Invitation.org_id) == access.org_id,
                        col(Invitation.github_id) == found.id,
                        col(Invitation.redeemed_at).is_(None),
                        col(Invitation.revoked_at).is_(None),
                        col(Invitation.expires_at) <= _iso(now),
                    )
                    .values(revoked_at=_iso(now))
                )
                still_open = db.exec(
                    select(Invitation.id).where(
                        Invitation.org_id == access.org_id,
                        *_open_invitation(found.id, _iso(now)),
                    )
                ).first()
                if still_open is not None:
                    refused("already_invited", login)
                    raise invited
                invitation = Invitation(
                    org_id=access.org_id,
                    github_id=found.id,
                    github_login=found.login,
                    role=role,
                    invited_by=principal.user_id,
                    created_at=_iso(now),
                    expires_at=_iso(now + INVITATION_LIFETIME),
                )
                db.add(invitation)
                db.flush()
                assert invitation.id is not None
                for project_id, _, project_role in projects:
                    db.add(
                        InvitationGrant(
                            invitation_id=invitation.id,
                            org_id=access.org_id,
                            project_id=project_id,
                            role=project_role,
                        )
                    )
                db.flush()
                invitation_uid = invitation.uid
                row = _invitation_row(
                    invitation,
                    db.get(User, principal.user_id),
                    [(slug, project_role) for _, slug, project_role in projects],
                    _iso(now),
                ) | {"pending": True}
    except IntegrityError:  # a concurrent add or invitation of the same person won
        refused(conflict.code or "conflict", login)
        raise conflict from None
    if invitation_uid is not None:
        audit(
            "invitation_created",
            request,
            uid=principal.uid,
            org_id=access.org_id,
            org=access.org_slug,
            invitation=invitation_uid,
            role=role,
            github_login=found.login,
            github_id=found.id,
            grants=len(projects),
        )
        return row
    audit(
        "member_added",
        request,
        uid=principal.uid,
        target=row["uid"],
        org_id=access.org_id,
        org=access.org_slug,
        role=role,
        github_login=row["github_login"],
        grants=len(projects),
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
        assert user.id is not None
        if role in (Role.OWNER.value, Role.ADMIN.value):
            # An org admin is an admin of every project; grants no longer apply.
            db.exec(
                delete(ProjectGrant).where(
                    col(ProjectGrant.org_id) == access.org_id,
                    col(ProjectGrant.user_id) == user.id,
                )
            )
        elif previous != role:
            connections.revoke_lost_access(db, access.org_id, user_id=user.id)
        row = _member_row(user, role, membership.created_at)
    if previous != role:
        audit(
            "member_role_changed",
            request,
            uid=principal.uid,
            target=uid,
            org_id=access.org_id,
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
        org_id=access.org_id,
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
    audit(
        "member_left",
        request,
        uid=principal.uid,
        org_id=access.org_id,
        org=access.org_slug,
        role=previous,
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Invitations (M2f-1 plan section 4.8)
# ---------------------------------------------------------------------------


@members_router.get("/api/org/invitations")
def get_invitations(
    access: OrgAccess = Depends(org_access(Action.ORG_MEMBERS)),
) -> list[dict]:
    """The org's open and expired invitations, plus 30 days of closed ones.

    Newest first; each row's ``status`` is ``open``, ``expired``,
    ``redeemed`` or ``revoked``.
    """
    now = _now()
    since = _iso(now - INVITATION_HISTORY)
    with get_session() as db:
        rows = db.exec(
            select(Invitation, User)
            .outerjoin(User, col(User.id) == col(Invitation.invited_by))
            .where(
                Invitation.org_id == access.org_id,
                or_(
                    and_(
                        col(Invitation.redeemed_at).is_(None),
                        col(Invitation.revoked_at).is_(None),
                    ),
                    col(Invitation.redeemed_at) >= since,
                    col(Invitation.revoked_at) >= since,
                ),
            )
            .order_by(col(Invitation.created_at).desc(), col(Invitation.id).desc())
        ).all()
        grants: dict[int, list[tuple[str, str]]] = {}
        ids = [invitation.id for invitation, _ in rows]
        if ids:
            for invitation_id, slug, role in db.exec(
                select(
                    InvitationGrant.invitation_id, Project.slug, InvitationGrant.role
                )
                .join(
                    Project,
                    and_(
                        col(Project.org_id) == col(InvitationGrant.org_id),
                        col(Project.id) == col(InvitationGrant.project_id),
                    ),
                )
                .where(
                    InvitationGrant.org_id == access.org_id,
                    col(InvitationGrant.invitation_id).in_(ids),
                )
                .order_by(col(Project.slug))
            ).all():
                grants.setdefault(invitation_id, []).append((slug, role))
        return [
            _invitation_row(
                invitation, inviter, grants.get(invitation.id or 0, []), _iso(now)
            )
            for invitation, inviter in rows
        ]


@members_router.delete("/api/org/invitations/{uid}", status_code=204)
def delete_invitation(
    uid: str,
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_MEMBERS)),
    principal: Principal = Depends(current_user),
) -> Response:
    """Revoke an invitation (an expired one too).

    ``404 no_such_invitation`` (unknown here), ``403 owner_required`` (an
    owner invitation, as a non-owner), ``409 invitation_closed`` (already
    redeemed or revoked). ``204``.
    """
    with get_session() as db:
        invitation = db.exec(
            select(Invitation)
            .where(Invitation.org_id == access.org_id, Invitation.uid == uid)
            .with_for_update()
        ).first()
        if invitation is None:
            raise ApiError(
                404,
                "no such invitation in this organization",
                code="no_such_invitation",
            )
        if invitation.role == Role.OWNER.value and access.role != Role.OWNER:
            raise _owner_required()
        if invitation.redeemed_at is not None or invitation.revoked_at is not None:
            raise ApiError(
                409, "this invitation is already closed", code="invitation_closed"
            )
        invitation.revoked_at = _iso(_now())
        db.add(invitation)
        login, role = invitation.github_login, invitation.role
    audit(
        "invitation_revoked",
        request,
        uid=principal.uid,
        org_id=access.org_id,
        org=access.org_slug,
        invitation=uid,
        role=role,
        github_login=login,
    )
    return Response(status_code=204)


class Redeemed(NamedTuple):
    """One invitation redeemed at sign-in (for the caller's audit record).

    Attributes
    ----------
    org_id : int
    org_slug : str
    role : str
        The member's org role afterwards (an existing membership is kept).
    invitation_uid : str
    """

    org_id: int
    org_slug: str
    role: str
    invitation_uid: str


def redeem_invitations(db: Session, *, github_id: int, user_id: int) -> list[Redeemed]:
    """Redeem every open, unexpired invitation for a GitHub id (at sign-in).

    The caller (:func:`whygraph.portal.auth_routes._sign_in_github_user`)
    runs this in its sign-in transaction, after the disabled check. The
    invitations' org rows are locked ``FOR SHARE`` first (in id order) and
    then the invitations ``FOR UPDATE`` - the order an org deletion takes
    them in - so a concurrent deletion either finished first (its
    invitations cascaded away) or waits. Each invitation is redeemed in its
    own savepoint: the membership is added (an existing one is kept), the
    grants whose projects still exist are inserted ``ON CONFLICT DO
    NOTHING`` (none for an org admin or owner), and ``redeemed_at`` /
    ``redeemed_by`` are set. A failure rolls back that savepoint only and
    skips that invitation; it never fails the sign-in.

    Parameters
    ----------
    db : Session
        The sign-in transaction's session.
    github_id : int
        The signing-in account's GitHub id.
    user_id : int
        Its ``users.id``.

    Returns
    -------
    list of Redeemed
        What was redeemed, in invitation order.
    """
    now = _iso(_now())
    org_ids = sorted(
        set(db.exec(select(Invitation.org_id).where(*_open_invitation(github_id, now))))
    )
    if not org_ids:
        return []
    orgs = dict(
        db.exec(
            select(Organization.id, Organization.slug)
            .where(col(Organization.id).in_(org_ids))
            .order_by(col(Organization.id))
            .with_for_update(read=True)
        ).all()
    )
    if not orgs:
        return []
    invitations = [
        (invitation.id, invitation.org_id, invitation.uid, invitation.role)
        for invitation in db.exec(
            select(Invitation)
            .where(
                col(Invitation.org_id).in_(list(orgs)),
                *_open_invitation(github_id, now),
            )
            .order_by(col(Invitation.id))
            .with_for_update()
        ).all()
    ]
    redeemed: list[Redeemed] = []
    for invitation_id, org_id, uid, invited_role in invitations:
        assert invitation_id is not None
        try:
            with db.begin_nested():
                role = _redeem_one(
                    db, invitation_id, org_id, invited_role, user_id=user_id, now=now
                )
        except SQLAlchemyError as exc:
            logger.warning(
                "skipped redeeming invitation %s: %s", uid, type(exc).__name__
            )
            continue
        redeemed.append(Redeemed(org_id, orgs[org_id], role, uid))
    return redeemed


def _redeem_one(
    db: Session, invitation_id: int, org_id: int, role: str, *, user_id: int, now: str
) -> str:
    """Redeem one locked invitation inside the caller's savepoint; the org role."""
    membership = db.exec(
        select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == user_id
        )
    ).first()
    if membership is None:
        membership = add_member(db, org_id=org_id, user_id=user_id, role=role)
    if membership.role not in (Role.OWNER.value, Role.ADMIN.value):
        grants = InvitationGrant.__table__
        projects = Project.__table__
        invitations = Invitation.__table__
        db.exec(
            pg_insert(ProjectGrant.__table__)
            .from_select(
                ["org_id", "project_id", "user_id", "role", "granted_by", "created_at"],
                sa_select(
                    grants.c.org_id,
                    grants.c.project_id,
                    literal(user_id, Integer),
                    grants.c.role,
                    invitations.c.invited_by,
                    literal(now, Text),
                )
                .select_from(
                    grants.join(
                        projects,
                        and_(
                            projects.c.org_id == grants.c.org_id,
                            projects.c.id == grants.c.project_id,
                        ),
                    ).join(invitations, invitations.c.id == grants.c.invitation_id)
                )
                .where(grants.c.invitation_id == invitation_id),
            )
            .on_conflict_do_nothing()
        )
    db.exec(
        update(Invitation)
        .where(col(Invitation.id) == invitation_id)
        .values(redeemed_at=now, redeemed_by=user_id)
    )
    return membership.role


__all__ = [
    "GITHUB_LOGIN_RE",
    "INVITATION_LIFETIME",
    "NO_SUCH_GITHUB_USER",
    "GrantBody",
    "MemberAddBody",
    "MemberRoleBody",
    "Redeemed",
    "members_router",
    "normalize_login",
    "redeem_invitations",
]
