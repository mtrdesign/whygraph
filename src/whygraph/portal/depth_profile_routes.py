"""The depth estimate over REST: ``GET /api/projects/{slug}/rationale/depth-profile``.

Plan 62 section 14.5a (R33). One read-only router, :data:`depth_profile_router`,
mounted by :mod:`whygraph.portal.app` with
``project_db_access(PROJECT_READ)`` and ``linked_guard``: a linked
project's cards are the platform's, so it answers ``403
managed_on_platform`` like every other mount a platform owns. The
computation is :mod:`whygraph.portal.depth_profile`; this module is the
request side: it starts the sampling **off the request** on the first ask
(a thread under a copy of the project's context, single-flight per
project), answers ``{"status": "computing"}`` until it is done, and prices
the cached sample on every read. No model call is made at any point.
"""

from __future__ import annotations

import contextvars
import threading

from fastapi import APIRouter, Request

from whygraph.core import get_config
from whygraph.mcp.targets import repo_root
from whygraph.serve.errors import ServeError
from whygraph.services.git import GitError, Repository

from .authz import Action, project_allowed
from .depth_profile import render_profile, run_sample
from .deps import ApiError, bound_project, portal_state
from .v1_routes import GIT_BUDGET

depth_profile_router = APIRouter()
"""The estimate; mounted under ``project_db_access(PROJECT_READ)`` and ``linked_guard``."""


def _head(root) -> str | None:
    """The checkout's ``HEAD`` (``None`` when git cannot say)."""
    try:
        return Repository(root).head_commit()
    except GitError:
        return None


@depth_profile_router.get("/depth-profile")
def get_depth_profile(request: Request) -> dict:
    """What a card at each depth level costs in this project (an estimate, no model call).

    Returns
    -------
    dict
        ``{"status": "computing"}`` while the sample is being measured
        (ask again); ``{"status": "no_index"}`` without a CodeGraph index;
        ``{"status": "no_history"}`` when nothing sampled has scanned history;
        otherwise :func:`~whygraph.portal.depth_profile.render_profile`'s
        body (``status`` ``"ready"``). A caller without ``project.usage``
        gets ``cost_hidden: true`` and no tokens or prices.

    Raises
    ------
    ServeError
        ``500 depth_profile_failed`` once after a failed computation (the
        next ask computes again); ``503 busy`` in production when the org's
        heavy slots are taken.
    """
    state = portal_state(request)
    project = bound_project(request)
    config = get_config()
    root = repo_root()
    head = _head(root)
    entry, start = state.depth_profiles.acquire(project.id, head)
    if start:
        _start(request, project, entry, root, config)
    if entry.status == "computing":
        return {"status": "computing"}
    if entry.status == "error":
        raise ServeError(
            500,
            f"the depth estimate could not be computed: {entry.error}",
            code="depth_profile_failed",
        )
    raw = entry.raw or {}
    if raw.get("status") in ("no_index", "no_history"):
        return {"status": raw["status"]}
    return render_profile(
        raw,
        config,
        project_id=project.id,
        overrides=state.prices.for_org(project.org_id),
        show_cost=project_allowed(project.role, Action.PROJECT_USAGE),
        head=entry.head,
    )


def _start(request: Request, project, entry, root, config) -> None:
    """Run the sampling in a thread under a copy of this request's context.

    In production it holds one of the org's heavy slots (``v1_in_flight``)
    for as long as it runs and answers ``503 busy`` when none is free.
    """
    state = portal_state(request)
    slot = state.mode == "production"
    if slot and not state.v1_in_flight.try_acquire(project.org_id):
        state.depth_profiles.discard(project.id)
        raise ApiError(
            503,
            "too many requests in flight for this organization; retry shortly",
            code="busy",
            headers={"Retry-After": "1"},
        )

    def work() -> None:
        try:
            run_sample(
                state.depth_profiles,
                project.id,
                entry,
                root,
                config,
                git_budget=GIT_BUDGET,
            )
        finally:
            if slot:
                state.v1_in_flight.release(project.org_id)

    context = contextvars.copy_context()
    threading.Thread(
        target=context.run,
        args=(work,),
        name=f"depth-profile-{project.slug}",
        daemon=True,
    ).start()
