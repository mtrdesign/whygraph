"""Onboarding data and the welcome flag (M2f-3 plan section 4.12).

- ``GET /api/onboarding`` (:data:`onboarding_router`, both modes,
  ``org.add_project``): the first-run checklist's items, ``{items: [{id,
  done, can_act}]}``. An item that does not apply is omitted:

  =========  ==========  ====================================================
  id         mode        ``done`` when
  =========  ==========  ====================================================
  llm_key    both        an ``llm_api_key`` secret exists in the org (any
                         layer) or a provider's key comes from the
                         environment; omitted when the org has projects and
                         every one is linked (``platform``)
  project    both        the org has a project
  agent      both        an ``agent_call_days`` row of the org or an
                         unflushed count; in production also an unrevoked
                         connection token of the caller in the org
  github     production  the org has a GitHub project, or the session's user
                         token lists an installation of the GitHub App
                         (memoised 60 s per session)
  invite     production  another membership or an open invitation exists
  =========  ==========  ====================================================

- ``DELETE /api/org/welcome`` (:data:`welcome_router`, production,
  ``org.read``): clears the caller's own ``welcome_pending`` flag (``204``,
  idempotent).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, update
from sqlmodel import col, select

from .authz import Action, OrgAccess, allowed
from .context import _key_env_var
from .db import get_session
from .deps import (
    PortalState,
    current_user,
    org_access,
    portal_state,
    require_production,
)
from .models import (
    AgentCallDay,
    ConnectionToken,
    Invitation,
    Membership,
    Project,
    Secret,
)
from .secrets import LLM_API_KEY, LLM_KEY_PROVIDERS
from .security import Principal

onboarding_router = APIRouter()
"""``GET /api/onboarding`` (both modes)."""

welcome_router = APIRouter(dependencies=[Depends(require_production)])
"""``DELETE /api/org/welcome`` (production only)."""

LISTING_TTL_SEC = 60.0
"""How long one session's "can list an installation" answer is reused."""

_LISTING_MAX = 2_000
"""Most sessions remembered by the installation-listing memo."""


def _can_list_installation(state: PortalState, principal: Principal) -> bool:
    """Whether the session's GitHub App user token lists an installation.

    A session without a live user token, an unconfigured app and any
    GitHub failure all answer ``False``. The answer is kept 60 s per
    session (:data:`LISTING_TTL_SEC`), so a polled checklist is one GitHub
    call a minute.
    """
    session_id = principal.session_id
    app = state.github_app
    if session_id is None or app is None:
        return False
    entry = state.user_tokens.get(session_id, principal.user_id)
    if entry is None:
        return False
    now = time.monotonic()
    memo = state.github_listing
    cached = memo.get(session_id)
    if cached is not None and now - cached[0] < LISTING_TTL_SEC:
        return cached[1]
    try:
        found = bool(app.installations(entry.token))
    except Exception:  # noqa: BLE001 - a flaky listing must not fail the checklist
        return False
    if len(memo) >= _LISTING_MAX:
        memo.clear()
    memo[session_id] = (now, found)
    return found


def _env_key() -> bool:
    return any(os.environ.get(_key_env_var(tag)) for tag in LLM_KEY_PROVIDERS)


@onboarding_router.get("/api/onboarding")
def get_onboarding(
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_ADD_PROJECT)),
    principal: Principal = Depends(current_user),
) -> dict:
    """The first-run checklist's items for this org (see the module docstring)."""
    state = portal_state(request)
    production = state.mode == "production"
    org_id = access.org_id
    with get_session() as db:
        sources = list(db.exec(select(Project.source).where(Project.org_id == org_id)))
        has_key = (
            db.exec(
                select(Secret.id).where(
                    Secret.org_id == org_id, Secret.kind == LLM_API_KEY
                )
            ).first()
            is not None
        )
        agent_row = (
            db.exec(
                select(AgentCallDay.id).where(AgentCallDay.org_id == org_id).limit(1)
            ).first()
            is not None
        )
        connected = production and (
            db.exec(
                select(ConnectionToken.id).where(
                    ConnectionToken.org_id == org_id,
                    ConnectionToken.user_id == principal.user_id,
                    col(ConnectionToken.revoked_at).is_(None),
                )
            ).first()
            is not None
        )
        invited = False
        if production:
            others = db.exec(
                select(func.count())
                .select_from(Membership)
                .where(Membership.org_id == org_id)
            ).one()
            invited = others > 1 or (
                db.exec(
                    select(Invitation.id).where(
                        Invitation.org_id == org_id,
                        col(Invitation.redeemed_at).is_(None),
                        col(Invitation.revoked_at).is_(None),
                        col(Invitation.expires_at)
                        > datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    )
                ).first()
                is not None
            )
    agent = agent_row or connected or state.agent_calls.has_any(org_id)
    items: list[dict] = []
    if production:
        github = "github" in sources or _can_list_installation(state, principal)
        items.append(
            {
                "id": "github",
                "done": github,
                "can_act": principal.github_login is not None,
            }
        )
    key_item = None
    if not (sources and all(s == "platform" for s in sources)):
        key_item = {
            "id": "llm_key",
            "done": has_key or _env_key(),
            "can_act": allowed(access.role, Action.ORG_CONFIGURE),
        }
    if key_item is not None and not production:
        items.append(key_item)
    items.append({"id": "project", "done": bool(sources), "can_act": True})
    if key_item is not None and production:
        items.append(key_item)
    if production:
        items.append(
            {
                "id": "invite",
                "done": invited,
                "can_act": allowed(access.role, Action.ORG_MEMBERS),
            }
        )
    items.append({"id": "agent", "done": agent, "can_act": True})
    return {"items": items}


@welcome_router.delete("/api/org/welcome", status_code=204)
def delete_welcome(
    access: OrgAccess = Depends(org_access(Action.ORG_READ)),
) -> Response:
    """Clear the caller's own welcome flag in this org (idempotent, ``204``)."""
    with get_session() as db:
        db.exec(
            update(Membership)
            .where(
                col(Membership.org_id) == access.org_id,
                col(Membership.user_id) == access.user_id,
            )
            .values(welcome_pending=False)
        )
    return Response(status_code=204)


__all__ = ["onboarding_router", "welcome_router"]
