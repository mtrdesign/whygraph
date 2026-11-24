"""Committed plans over REST: ``/api/projects/{slug}/plans`` (plan 61 section 4.15.1).

One read-only router, :data:`plans_read_router`, mounted by
:mod:`whygraph.portal.app` beside the notes routers with
``project_db_access(PROJECT_READ)`` and ``linked_guard("Plans")``. There are
no write routes and no audit events: reads are not audited. The domain is
:mod:`whygraph.mcp.plans`; this is the only module that asks it for a whole
plan body (``full=True``), because a person reading the page costs no tokens.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from whygraph.core import get_config
from whygraph.mcp import plans
from whygraph.serve.errors import ServeError, WhyGraphError

plans_read_router = APIRouter()
"""Plan reads; mounted under ``project_db_access(PROJECT_READ)``."""

_STATUS = {"plan_not_found": 404}
"""The REST status of each domain error code."""


@plans_read_router.get("")
def list_plans(
    q: str | None = Query(default=None, max_length=200),
    status: Literal["shipped", "in_flight", "not_implemented"] | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Search the indexed plans.

    Returns
    -------
    dict
        ``{"plans": [...], "total": int, "dir": str}``. Each plan has
        ``stem``, ``title``, ``status``, ``uri``, ``path``, ``present``,
        ``objective`` and ``updated_at``; ``total`` is the filtered count and
        ``dir`` the configured plans folder (``""`` when indexing is off).
    """
    page, total = plans.plans_search(q, status, limit=limit, offset=offset)
    return {"plans": page, "total": total, "dir": get_config().plans.dir}


@plans_read_router.get("/{stem}")
def get_plan(stem: str) -> dict:
    """One plan with its whole body, digest, links and outline.

    Raises
    ------
    ServeError
        ``404 plan_not_found`` for an unknown stem.
    """
    try:
        return plans.plan_record(stem, full=True, with_digest=True)
    except ServeError:
        raise
    except WhyGraphError as exc:
        status = _STATUS.get(exc.code or "")
        if status is None:
            raise
        raise ServeError(status, str(exc), code=exc.code) from exc
