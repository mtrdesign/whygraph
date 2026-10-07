"""Production project access: who may see a project, and as what (M2f-1).

M2f-1 plan section 4.8, "Project access". Every route is **production-only**
(:func:`~whygraph.portal.deps.require_production` runs first, so local mode
answers ``404``) and needs ``project.access`` - a project admin, or an org
admin / owner, who are project admins everywhere.

* ``GET /api/projects/{slug}/access`` lists every member with the role they
  have on this project and where it comes from (``org_admin``, ``grant`` or
  ``default``), plus the open invitations that carry a grant for it.
* ``PATCH`` flips **Restricted**.
* ``PUT /api/projects/{slug}/access/{user_uid}`` upserts a grant;
  ``DELETE`` removes it.

Org admins and owners cannot be granted (``409 org_admin``): they are admins
of every project by their org role. Whatever takes a person's access away
(a grant removed, a project made Restricted) revokes their connection tokens
for the project in the same transaction
(:func:`~whygraph.portal.connections.revoke_lost_access`).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import col, select

from . import connections
from .audit import audit
from .authz import PROJECT_ROLES, Action, OrgAccess, ProjectRole, Role
from .db import get_session
from .deps import (
    ApiError,
    BoundProject,
    current_org,
    current_user,
    project_access,
    require_production,
)
from .models import (
    Invitation,
    InvitationGrant,
    Membership,
    Project,
    ProjectGrant,
    User,
)
from .security import Principal
from .usage_routes import project_month_spend

access_router = APIRouter(dependencies=[Depends(require_production)])
"""Every route of this module; included before the ``/api`` 404 catch-all."""

_ORG_ADMIN_ROLES = (Role.OWNER.value, Role.ADMIN.value)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RestrictedBody(_Strict):
    """``PATCH /api/projects/{slug}/access``."""

    restricted: bool


class GrantBody(_Strict):
    """``PUT /api/projects/{slug}/access/{user_uid}``."""

    role: str = Field(max_length=20)


def _person(
    user: User, org_role: str, grant: str | None, default: str, restricted: bool
) -> dict:
    """One member as the access list shows it (never an email)."""
    if org_role in _ORG_ADMIN_ROLES:
        project_role, source = ProjectRole.ADMIN.value, "org_admin"
    elif grant is not None:
        project_role, source = grant, "grant"
    elif restricted or default == "none":
        project_role, source = None, "default"
    else:
        project_role, source = default, "default"
    return {
        "uid": user.uid,
        "login": user.github_login,
        "name": user.display_name,
        "avatar": user.avatar_url,
        "org_role": org_role,
        "project_role": project_role,
        "source": source,
    }


def _target(db, org_id: int, uid: str) -> tuple[User, str]:
    """The member ``uid`` of the org and their org role; ``404 not_member``."""
    row = db.exec(
        select(User, Membership.role)
        .join(Membership, col(Membership.user_id) == col(User.id))
        .where(Membership.org_id == org_id, User.uid == uid)
    ).first()
    if row is None:
        raise ApiError(404, "no such member in this organization", code="not_member")
    return row


@access_router.get("/api/projects/{slug}/access")
def get_access(
    project: BoundProject = Depends(project_access(Action.PROJECT_ACCESS)),
    access: OrgAccess = Depends(current_org),
) -> dict:
    """The project's access list: Restricted flag, org default, people, invitations.

    Each person carries ``month_spend_usd``: their spend on this project
    this month.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_session() as db:
        restricted = bool(db.get(Project, project.id).restricted)  # type: ignore[union-attr]
        rows = db.exec(
            select(User, Membership.role, ProjectGrant.role)
            .join(Membership, col(Membership.user_id) == col(User.id))
            .outerjoin(
                ProjectGrant,
                (ProjectGrant.project_id == project.id)
                & (col(ProjectGrant.user_id) == col(User.id)),
            )
            .where(Membership.org_id == project.org_id)
            .order_by(col(Membership.created_at), col(User.id))
        ).all()
        default = access.default_project_role
        invitations = db.exec(
            select(Invitation, InvitationGrant.role)
            .join(
                InvitationGrant,
                col(InvitationGrant.invitation_id) == col(Invitation.id),
            )
            .where(
                InvitationGrant.project_id == project.id,
                col(Invitation.redeemed_at).is_(None),
                col(Invitation.revoked_at).is_(None),
                col(Invitation.expires_at) > now,
            )
            .order_by(col(Invitation.created_at), col(Invitation.id))
        ).all()
        # Each person's spend on this project this month (M2f-2 plan
        # section 4.12; a project admin holds project.usage).
        spend = project_month_spend(project.org_id, project.id)
        return {
            "restricted": restricted,
            "org_default": default,
            "people": [
                {
                    **_person(user, org_role, grant, default, restricted),
                    "month_spend_usd": spend.get(user.id, 0.0),
                }
                for user, org_role, grant in rows
            ],
            "invitations": [
                {
                    "uid": inv.uid,
                    "github_login": inv.github_login,
                    "role": role,
                    "created_at": inv.created_at,
                    "expires_at": inv.expires_at,
                }
                for inv, role in invitations
            ],
        }


@access_router.patch("/api/projects/{slug}/access")
def patch_access(
    body: RestrictedBody,
    request: Request,
    project: BoundProject = Depends(project_access(Action.PROJECT_ACCESS)),
    access: OrgAccess = Depends(current_org),
    principal: Principal = Depends(current_user),
) -> dict:
    """Make the project Restricted or not.

    Making it Restricted revokes the connection tokens of everyone who has
    no grant on it (they lose access) in the same transaction.

    Returns
    -------
    dict
        ``{"restricted": bool}``.
    """
    with get_session() as db:
        row = db.exec(
            select(Project).where(Project.id == project.id).with_for_update()
        ).one()
        previous = bool(row.restricted)
        row.restricted = body.restricted
        db.add(row)
        db.flush()
        if body.restricted and not previous:
            connections.revoke_lost_access(db, project.org_id, project_id=project.id)
    if previous != body.restricted:
        audit(
            "project_restricted_changed",
            request,
            uid=principal.uid,
            org=access.org_slug,
            project=project.slug,
            restricted=body.restricted,
        )
    return {"restricted": body.restricted}


@access_router.put("/api/projects/{slug}/access/{user_uid}")
def put_grant(
    user_uid: str,
    body: GrantBody,
    request: Request,
    project: BoundProject = Depends(project_access(Action.PROJECT_ACCESS)),
    access: OrgAccess = Depends(current_org),
    principal: Principal = Depends(current_user),
) -> dict:
    """Give a member a role on this project (an upsert).

    ``422 bad_role``, ``404 not_member``, ``409 org_admin`` (org admins and
    owners are admins of every project already).

    Returns
    -------
    dict
        The person's row, as in the access list.
    """
    if body.role not in PROJECT_ROLES:
        raise ApiError(
            422, f"role must be one of {', '.join(PROJECT_ROLES)}", code="bad_role"
        )
    with get_session() as db:
        user, org_role = _target(db, project.org_id, user_uid)
        if org_role in _ORG_ADMIN_ROLES:
            raise ApiError(
                409,
                "organization admins and owners are admins of every project",
                code="org_admin",
            )
        assert user.id is not None
        grant = db.get(ProjectGrant, (project.id, user.id))
        previous = None if grant is None else grant.role
        if grant is None:
            grant = ProjectGrant(
                org_id=project.org_id,
                project_id=project.id,
                user_id=user.id,
                role=body.role,
                granted_by=principal.user_id,
            )
        else:
            grant.role = body.role
        db.add(grant)
        db.flush()
        restricted = bool(db.get(Project, project.id).restricted)  # type: ignore[union-attr]
        row = _person(
            user, org_role, body.role, access.default_project_role, restricted
        )
    if previous != body.role:
        audit(
            "project_grant_added" if previous is None else "project_grant_changed",
            request,
            uid=principal.uid,
            target=user_uid,
            org=access.org_slug,
            project=project.slug,
            role=body.role,
            previous=previous,
        )
    return row


@access_router.delete("/api/projects/{slug}/access/{user_uid}", status_code=204)
def delete_grant(
    user_uid: str,
    request: Request,
    project: BoundProject = Depends(project_access(Action.PROJECT_ACCESS)),
    access: OrgAccess = Depends(current_org),
    principal: Principal = Depends(current_user),
) -> Response:
    """Remove a member's grant; they fall back to the org default.

    ``404 not_member`` (not a member, or no grant to remove). If the
    fallback is no access (Restricted, or default ``none``) their
    connection tokens for the project are revoked in the same transaction.
    """
    with get_session() as db:
        user, _ = _target(db, project.org_id, user_uid)
        assert user.id is not None
        grant = db.get(ProjectGrant, (project.id, user.id))
        if grant is None:
            raise ApiError(404, "that member has no grant here", code="no_grant")
        previous = grant.role
        db.delete(grant)
        db.flush()
        connections.revoke_lost_access(
            db, project.org_id, project_id=project.id, user_id=user.id
        )
    audit(
        "project_grant_removed",
        request,
        uid=principal.uid,
        target=user_uid,
        org=access.org_slug,
        project=project.slug,
        previous=previous,
    )
    return Response(status_code=204)


__all__ = ["GrantBody", "RestrictedBody", "access_router"]
