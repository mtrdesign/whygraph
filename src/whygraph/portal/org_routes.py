"""A production organization's settings, ownership, audit log and deletion.

M2f-1 plan sections 4.8 and 4.9 add, beside the deletion below:

- ``PATCH /api/org`` (owner, ``org.configure``): rename the org and set its
  default project role; a default of ``none`` revokes, in the same
  transaction, every connection token whose user lost access by it;
- ``POST /api/org/transfer`` (owner, ``org.own``): make another member the
  owner and the caller an admin, in one transaction under the owner lock;
- ``GET /api/org/audit`` and ``GET /api/org/audit.csv`` (owner,
  ``org.audit``): the org's persisted security events
  (:mod:`~whygraph.portal.audit_store`), keyset-paged or as escaped CSV.

Deleting a production organization: ``DELETE /api/org`` (M2d-2 plan section 4.8).

Production-only - the router's :func:`~whygraph.portal.deps.require_production`
runs before the route's :func:`~whygraph.portal.deps.org_access`, so local
mode answers ``404`` and its built-in org can never be deleted - and
org-scoped: the org is the one ``Host`` names. Owner only
(:attr:`~whygraph.portal.authz.Action.ORG_OWN`); an instance admin who is not
an owner is refused like anyone else.

The deletion is immediate and confirmed by typing the org's slug:

1. :meth:`~whygraph.portal.runner.ScanRunner.reserve_projects` refuses
   every new run request for the org's projects and cancels its runs
   (``409 busy`` when a sync is still fetching, everything released);
2. one transaction takes the slug's advisory lock
   (:func:`~whygraph.portal.orgs.lock_org_slug`), locks the org row ``FOR
   UPDATE`` (an import holds it ``FOR SHARE`` while it inserts), revokes
   the org's connection tokens (``org_deleted``), deletes the org's
   projects (cascading to their agents, config, secrets and runs), retires
   the slug and deletes the org (cascading to its memberships, org config
   and org secrets);
3. after the commit: project engines disposed and forgotten, contexts
   invalidated, run files deleted, ``<data>/repos/<org>/`` removed
   (including an in-flight import's ``.clone-*``);
4. audit ``org_deleted``.

Members keep their accounts and sessions; the org's host answers ``404``
from then on, and the slug can never be created again.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import anyio.to_thread
from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete
from sqlmodel import col, select

from whygraph.db.engine import dispose_engine

from . import audit_store, connections
from .audit import audit
from .authz import DEFAULT_PROJECT_ROLES, Action, OrgAccess, Role
from .clones import remove_clone
from .context import resolve_root
from .db import get_session
from .deps import (
    ApiError,
    PortalState,
    current_user,
    org_access,
    portal_state,
    require_production,
)
from .member_routes import (
    _lock_owners,
    _locked_target,
    _member_row,
    _owner_required,
)
from .models import (
    Membership,
    Organization,
    Project,
    ProjectGrant,
    RetiredOrgSlug,
    User,
)
from .orgs import lock_org_slug
from .paths import db_paths
from .runner import ProjectBusy, remove_run_files, run_files
from .security import Principal

_log = logging.getLogger(__name__)

org_router = APIRouter(dependencies=[Depends(require_production)])
"""Every route of this module; included before the ``/api`` 404 catch-all."""


class DeleteOrgBody(BaseModel):
    """``DELETE /api/org``: the org's slug, typed to confirm."""

    model_config = ConfigDict(extra="forbid")

    confirm_slug: str = Field(default="", max_length=100)


ORG_NAME_MAX = 200
"""Longest org name, as ``POST /api/orgs`` allows."""


class OrgPatchBody(BaseModel):
    """``PATCH /api/org``: either field, or both (at least one)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=1000)
    default_project_role: str | None = Field(default=None, max_length=20)


class TransferBody(BaseModel):
    """``POST /api/org/transfer``: the new owner, and the org's slug typed to confirm."""

    model_config = ConfigDict(extra="forbid")

    user_uid: str = Field(max_length=100)
    confirm_slug: str = Field(default="", max_length=100)


def _confirm_slug(access: OrgAccess, typed: str, doing: str) -> None:
    """``409 confirm_slug`` unless ``typed`` is the org's slug."""
    if typed != access.org_slug:
        raise ApiError(
            409,
            f"type the organization's slug ({access.org_slug}) to {doing}",
            code="confirm_slug",
        )


def _checked_name(raw: str) -> str:
    """The stripped org name; ``422 bad_name`` when blank, too long or with control characters."""
    name = raw.strip()
    if not name:
        raise ApiError(422, "name must not be blank", code="bad_name")
    if len(name) > ORG_NAME_MAX:
        raise ApiError(
            422, f"name must be at most {ORG_NAME_MAX} characters", code="bad_name"
        )
    if any(unicodedata.category(c) == "Cc" for c in name):
        raise ApiError(422, "name must not contain control characters", code="bad_name")
    return name


def _org_row(org: Organization) -> dict:
    return {
        "slug": org.slug,
        "name": org.name,
        "default_project_role": org.default_project_role,
    }


@dataclass(frozen=True)
class _Deleted:
    """What the transaction deleted, for the clean-up after the commit."""

    project_ids: list[int]
    project_dbs: list[Path]
    runs: list[tuple[int, str | None, str | None]]


def _delete_rows(org_id: int, slug: str) -> _Deleted:
    """Step 2: delete the org's projects and the org, retire the slug (one transaction).

    Raises
    ------
    ApiError
        ``404`` when the org is already gone (a concurrent deletion).
    """
    with get_session() as session:
        lock_org_slug(session, slug)
        org = session.exec(
            select(Organization.id).where(Organization.id == org_id).with_for_update()
        ).first()
        if org is None:
            raise ApiError(404, "not found")
        # By org, under the lock: an import that committed first is
        # included, and one that commits later finds no org.
        projects = session.exec(select(Project).where(Project.org_id == org_id)).all()
        ids = [p.id for p in projects if p.id is not None]
        dbs = [db_paths(resolve_root(p))[0] for p in projects]
        runs = run_files(ids)
        # Before the deletes, which set the tokens' org_id / project_id to NULL.
        connections.revoke_for_org(session, org_id, "org_deleted")
        session.exec(delete(Project).where(col(Project.org_id) == org_id))  # type: ignore[call-overload]
        session.add(RetiredOrgSlug(slug=slug))
        session.flush()
        session.exec(delete(Organization).where(col(Organization.id) == org_id))  # type: ignore[call-overload]
    return _Deleted(project_ids=ids, project_dbs=dbs, runs=runs)


def _clean_up(state: PortalState, slug: str, deleted: _Deleted) -> None:
    """Step 3, after the commit: engines, contexts, run files, ``repos/<org>/``."""
    for project_id, db_path in zip(deleted.project_ids, deleted.project_dbs):
        dispose_engine(db_path)
        state.migrations.forget(db_path)
        state.contexts.invalidate(project_id)
    remove_run_files(state.data_dir, deleted.runs)
    org_dir = state.data_dir / "repos" / slug
    try:
        # Exactly one level under <data>/repos, no symlink (the clone guard).
        if not remove_clone(org_dir, state.data_dir, depth=1) and org_dir.exists():
            _log.warning(
                "org deletion: refused to delete %s: not a portal folder", org_dir
            )
    except OSError:
        _log.exception("org deletion: could not delete %s", org_dir)


@org_router.delete("/api/org")
async def delete_org(
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_OWN)),
    principal: Principal = Depends(current_user),
    body: DeleteOrgBody | None = Body(default=None),
) -> dict:
    """Delete the org, its projects, clones, run files, members, config and keys.

    ``409 confirm_slug`` unless ``confirm_slug`` equals the org's slug;
    ``409 busy`` while a run cannot be stopped yet (a sync still fetching)
    or the org is already being deleted - nothing is deleted then.

    Returns
    -------
    dict
        ``{"deleted": <slug>, "projects": <count>}``.
    """
    state = portal_state(request)
    body = body or DeleteOrgBody()
    if body.confirm_slug != access.org_slug:
        raise ApiError(
            409,
            f"type the organization's slug ({access.org_slug}) to delete it",
            code="confirm_slug",
        )
    try:
        async with state.runner.reserve_projects(access.org_id):
            deleted = await anyio.to_thread.run_sync(
                _delete_rows, access.org_id, access.org_slug
            )
            await anyio.to_thread.run_sync(_clean_up, state, access.org_slug, deleted)
    except ProjectBusy as exc:
        raise ApiError(409, str(exc), code="busy") from exc
    state.budgets.set_org(access.org_id, None)  # its budgets went with the org
    audit(
        "org_deleted",
        request,
        uid=principal.uid,
        org_id=access.org_id,
        org=access.org_slug,
        projects=len(deleted.project_ids),
    )
    return {"deleted": access.org_slug, "projects": len(deleted.project_ids)}


# ---------------------------------------------------------------------------
# Settings and ownership (M2f-1 plan section 4.8)
# ---------------------------------------------------------------------------


@org_router.patch("/api/org")
def patch_org(
    body: OrgPatchBody,
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_CONFIGURE)),
    principal: Principal = Depends(current_user),
) -> dict:
    """Rename the org and / or set its default project role.

    ``422`` with neither field; ``422 bad_name`` for a blank name, one
    longer than 200 characters or one with control characters (names need
    not be unique); ``422 bad_role`` for a default outside
    ``contributor`` / ``viewer`` / ``none``. Setting the default to
    ``none`` revokes, in the same transaction, the connection tokens of
    every member who lost access to their project by it
    (``project_access_removed``); ``viewer`` keeps them (viewers may
    connect agents).

    Returns
    -------
    dict
        ``{"slug", "name", "default_project_role"}``.
    """
    if body.name is None and body.default_project_role is None:
        raise ApiError(422, "nothing to change: set name or default_project_role")
    name = None if body.name is None else _checked_name(body.name)
    role = body.default_project_role
    if role is not None and role not in DEFAULT_PROJECT_ROLES:
        raise ApiError(
            422,
            f"default_project_role must be one of {', '.join(DEFAULT_PROJECT_ROLES)}",
            code="bad_role",
        )
    with get_session() as db:
        org = db.exec(
            select(Organization)
            .where(Organization.id == access.org_id)
            .with_for_update()
        ).first()
        if org is None:  # deleted meanwhile
            raise ApiError(404, "not found")
        previous_name, previous_role = org.name, org.default_project_role
        if name is not None:
            org.name = name
        if role is not None:
            org.default_project_role = role
        db.add(org)
        db.flush()
        revoked = 0
        if role == "none" and previous_role != "none":
            revoked = connections.revoke_lost_access(db, access.org_id)
        row = _org_row(org)
    if name is not None and name != previous_name:
        audit(
            "org_renamed",
            request,
            uid=principal.uid,
            org_id=access.org_id,
            org=access.org_slug,
            name=name,
            previous=previous_name,
        )
    if role is not None and role != previous_role:
        audit(
            "org_default_role_changed",
            request,
            uid=principal.uid,
            org_id=access.org_id,
            org=access.org_slug,
            role=role,
            previous=previous_role,
            tokens_revoked=revoked,
        )
    return row


@org_router.post("/api/org/transfer")
def post_transfer(
    body: TransferBody,
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_OWN)),
    principal: Principal = Depends(current_user),
) -> dict:
    """Make another member the owner and the caller an admin (one transaction).

    ``409 confirm_slug`` unless ``confirm_slug`` is the org's slug; ``409
    self_transfer`` for the caller themselves; ``404 not_member``; ``409
    already_owner``; ``409 user_disabled``; ``403 owner_required`` when the
    caller stopped being an owner meanwhile. The owner rows are locked
    first (as every role change does), and the new owner's project grants
    are deleted (an org owner is admin on every project, and an old grant
    must not come back on a later demotion).

    Returns
    -------
    dict
        ``{"owner": <member row>, "previous_owner": <member row>}``.
    """
    _confirm_slug(access, body.confirm_slug, "transfer its ownership")
    if body.user_uid == principal.uid:
        raise ApiError(409, "you already own this organization", code="self_transfer")
    owner = Role.OWNER.value
    with get_session() as db:
        owners = _lock_owners(db, access.org_id)
        if principal.user_id not in owners:
            raise _owner_required()
        membership, user = _locked_target(db, access.org_id, body.user_uid)
        if membership.role == owner:
            raise ApiError(409, "they are already an owner", code="already_owner")
        if user.disabled_at is not None:
            raise ApiError(409, "this account is disabled", code="user_disabled")
        mine = db.get(Membership, (access.org_id, principal.user_id))
        me = db.get(User, principal.user_id)
        if mine is None or me is None:  # removed meanwhile
            raise _owner_required()
        previous = membership.role
        membership.role = owner
        mine.role = Role.ADMIN.value
        db.add(membership)
        db.add(mine)
        db.exec(  # type: ignore[call-overload]
            delete(ProjectGrant).where(
                col(ProjectGrant.org_id) == access.org_id,
                col(ProjectGrant.user_id) == user.id,
            )
        )
        db.flush()
        result = {
            "owner": _member_row(user, owner, membership.created_at),
            "previous_owner": _member_row(me, mine.role, mine.created_at),
        }
    audit(
        "org_ownership_transferred",
        request,
        uid=principal.uid,
        target=body.user_uid,
        org_id=access.org_id,
        org=access.org_slug,
        previous=previous,
    )
    return result


# ---------------------------------------------------------------------------
# The audit log (M2f-1 plan section 4.9)
# ---------------------------------------------------------------------------


def org_audit_filter(
    access: OrgAccess,
    event: str | None,
    actor: str | None,
    since: str | None,
    until: str | None,
) -> audit_store.AuditFilter:
    """The org's audit filter; ``422 bad_date`` for a malformed ``from`` / ``to``."""
    try:
        return audit_store.make_filter(
            org_id=access.org_id, event=event, actor=actor, since=since, until=until
        )
    except ValueError as exc:
        raise ApiError(
            422, "from and to must be ISO-8601 dates or date-times", code="bad_date"
        ) from exc


@org_router.get("/api/org/audit")
def get_org_audit(
    access: OrgAccess = Depends(org_access(Action.ORG_AUDIT)),
    event: str | None = Query(default=None, max_length=100),
    actor: str | None = Query(default=None, max_length=200),
    since: str | None = Query(default=None, alias="from", max_length=40),
    until: str | None = Query(default=None, alias="to", max_length=40),
    before: int | None = Query(default=None, ge=1),
    limit: int = Query(
        default=audit_store.PAGE_SIZE, ge=1, le=audit_store.MAX_PAGE_SIZE
    ),
) -> dict:
    """The org's security events, newest first, keyset-paged on ``id``.

    Filters: ``event`` (exact name), ``actor`` (a user uid or a label such
    as ``@ben``), ``from`` / ``to`` (ISO-8601; a date-only ``to`` includes
    that day). ``before`` is the previous page's ``next``.

    Returns
    -------
    dict
        ``{"events": [...], "next": <id or null>}``.
    """
    f = org_audit_filter(access, event, actor, since, until)
    events, next_before = audit_store.query_events(f, before=before, limit=limit)
    return {"events": events, "next": next_before}


@org_router.get("/api/org/audit.csv")
def get_org_audit_csv(
    access: OrgAccess = Depends(org_access(Action.ORG_AUDIT)),
    event: str | None = Query(default=None, max_length=100),
    actor: str | None = Query(default=None, max_length=200),
    since: str | None = Query(default=None, alias="from", max_length=40),
    until: str | None = Query(default=None, alias="to", max_length=40),
) -> StreamingResponse:
    """The same events as CSV (same filters), streamed, at most 50 000 rows.

    Every cell that a spreadsheet would read as a formula is prefixed with
    ``'``; ``fields`` is one JSON cell.
    """
    f = org_audit_filter(access, event, actor, since, until)
    return StreamingResponse(
        audit_store.iter_csv(f),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="whygraph-audit-{access.org_slug}.csv"'
            )
        },
    )


__all__ = [
    "DeleteOrgBody",
    "OrgPatchBody",
    "TransferBody",
    "delete_org",
    "get_org_audit",
    "get_org_audit_csv",
    "org_router",
    "patch_org",
    "post_transfer",
]
