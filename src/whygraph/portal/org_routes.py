"""Deleting a production organization: ``DELETE /api/org`` (M2d-2 plan section 4.8).

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
from dataclasses import dataclass
from pathlib import Path

import anyio.to_thread
from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete
from sqlmodel import col, select

from whygraph.db.engine import dispose_engine

from . import connections
from .audit import audit
from .authz import Action, OrgAccess
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
from .models import Organization, Project, RetiredOrgSlug
from .orgs import lock_org_slug
from .paths import db_paths
from .routes import _remove_clone
from .runner import ProjectBusy, remove_run_files, run_files
from .security import Principal

_log = logging.getLogger(__name__)

org_router = APIRouter(dependencies=[Depends(require_production)])
"""Every route of this module; included before the ``/api`` 404 catch-all."""


class DeleteOrgBody(BaseModel):
    """``DELETE /api/org``: the org's slug, typed to confirm."""

    model_config = ConfigDict(extra="forbid")

    confirm_slug: str = Field(default="", max_length=100)


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
        if not _remove_clone(org_dir, state.data_dir, depth=1) and org_dir.exists():
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
    audit(
        "org_deleted",
        request,
        uid=principal.uid,
        org=access.org_slug,
        projects=len(deleted.project_ids),
    )
    return {"deleted": access.org_slug, "projects": len(deleted.project_ids)}


__all__ = ["DeleteOrgBody", "delete_org", "org_router"]
