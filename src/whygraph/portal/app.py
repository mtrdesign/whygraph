"""The portal FastAPI application: :func:`create_portal_app`.

Composition (plan section 4.5.1)::

    PortalGuard (pure ASGI)       Host / Origin / Sec-Fetch-Site / X-WhyGraph-Client,
                                  and the principal, resolved once per request
    /api/portal/*                 management (portal/routes.py)
    /api/projects/*               management; project_context or project_db
    /api/projects/{slug}/...      serve.routes.router + serve.chat.router (/chat),
                                  dependencies=[Depends(project_db)]
    /mcp/{slug}                   per-project MCP dispatcher (portal/mcp_mount.py)
    /api/*, /mcp/* not matched    404 {"error"} - never the SPA's index.html
    everything else               the SPA (serve.app._mount_static)

The lifespan migrates the portal DB (a failure leaves a *degraded* app
whose ``GET /api/portal/state`` reports ``{"error"}`` and whose other
``/api`` routes answer ``503``), writes the ``settings`` row at first
start and refuses to start when ``WHYGRAPH_MODE`` contradicts it, builds
the :class:`~whygraph.portal.security.PortalOrigins`, marks runs left
``running`` by a previous process ``interrupted``, follows a port change
into the managed repos (:mod:`whygraph.portal.port_change`), starts the
runner and
this app's own MCP session manager, and turns strict project-context mode
on. On exit it sets the shutdown event (open streams end), stops the
runner and turns strict mode off again.

The portal is **one process**: the runner, migration lock and caches are
in-process, so it must never run with several workers.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import anyio
import anyio.to_thread
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlmodel import col, select

from whygraph.agents import DEFAULT_PORTAL_PORT
from whygraph.core.context import set_strict
from whygraph.mcp.errors import WhyGraphError
from whygraph.serve.app import _mount_static
from whygraph.serve.chat import router as chat_router
from whygraph.serve.errors import whygraph_error_handler
from whygraph.serve.routes import router as data_router

from . import db as portal_db
from .deps import ApiError, PortalState, current_user, project_db
from .mcp_mount import McpDispatcher, build_session_manager
from .migrate import MIGRATION_LOCK
from .models import ScanRun, Setting
from .port_change import reconcile_port
from .repos import SHARED_FOLDERS_ENV, parse_shared_folders
from .routes import portal_router, projects_router, public_router
from .runner import ScanRunner
from .security import DEV_ORIGINS_ENV, PortalGuard, build_origins

_log = logging.getLogger(__name__)

MODE_ENV = "WHYGRAPH_MODE"
"""Mode requested at first start (``local``); later only compared."""

SUPPORTED_MODES: tuple[str, ...] = ("local",)
"""Modes this release can run (production mode is M2)."""

_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


class PortalStartupError(RuntimeError):
    """The portal refuses to start (e.g. a ``WHYGRAPH_MODE`` mismatch)."""


def create_portal_app(
    data_dir: Path | None = None,
    *,
    port: int = DEFAULT_PORTAL_PORT,
    runner: ScanRunner | None = None,
) -> FastAPI:
    """Build the portal application.

    Parameters
    ----------
    data_dir : Path, optional
        The portal data directory. When given it is exported as
        ``WHYGRAPH_DATA`` for the process (the portal DB, secrets and
        clones all live there); otherwise ``$WHYGRAPH_DATA`` or the
        default applies.
    port : int
        The port the portal is published on; it builds the allowed
        ``Host`` / ``Origin`` values and the agent MCP URLs.
    runner : ScanRunner, optional
        The scan runner; a fresh :class:`ScanRunner` by default.

    Returns
    -------
    FastAPI
        The app, ready for ``uvicorn.run`` (single process, no workers).
    """
    if data_dir is not None:
        os.environ[portal_db.DATA_ENV_VAR] = str(Path(data_dir).expanduser())
    state = PortalState(
        port=port, data_dir=portal_db.data_dir(), runner=runner or ScanRunner()
    )

    app = FastAPI(
        title="WhyGraph Portal",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    app.state.portal = state
    app.add_middleware(PortalGuard, state=state)

    app.add_exception_handler(WhyGraphError, whygraph_error_handler)
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)

    app.include_router(public_router)
    app.include_router(portal_router)
    app.include_router(projects_router)
    app.include_router(
        data_router, prefix="/api/projects/{slug}", dependencies=[Depends(project_db)]
    )
    app.include_router(
        chat_router,
        prefix="/api/projects/{slug}/chat",
        dependencies=[Depends(project_db)],
    )
    app.add_route("/mcp/{slug}", McpDispatcher(state), include_in_schema=False)

    for path in ("/api", "/api/{rest:path}"):
        app.add_api_route(
            path,
            _api_not_found,
            methods=_ALL_METHODS,
            include_in_schema=False,
            dependencies=[Depends(current_user)],
        )
    for path in ("/mcp", "/mcp/{rest:path}"):
        app.add_route(
            path, _mcp_not_found, methods=_ALL_METHODS, include_in_schema=False
        )

    _mount_static(app)
    return app


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    state: PortalState = app.state.portal
    await anyio.to_thread.run_sync(_startup, state)
    state.shared_folders = parse_shared_folders(
        os.environ.get(SHARED_FOLDERS_ENV), state.data_dir
    )
    origins = build_origins(state.port, os.environ.get(DEV_ORIGINS_ENV))
    state.shutdown_event = anyio.Event()
    if not state.degraded:
        state.port_change = await anyio.to_thread.run_sync(
            _reconcile_port, state, origins.agent_host
        )
    manager = build_session_manager(origins)
    async with manager.run():
        state.session_manager = manager
        if not state.degraded:
            await state.runner.start(state)
        set_strict(True)
        state.origins = origins
        try:
            yield
        finally:
            state.shutdown_event.set()
            try:
                if not state.degraded:
                    await state.runner.shutdown(grace=5.0)
            finally:
                set_strict(False)
                state.session_manager = None


def _startup(state: PortalState) -> None:
    """Migrate the portal DB, check / store the mode, recover stale runs."""
    try:
        with MIGRATION_LOCK:
            portal_db.ensure_initialized()
    except Exception as exc:  # noqa: BLE001 -- any failure means degraded mode
        _log.exception("portal database migration failed")
        state.degraded = f"portal database migration failed: {exc}"
        return

    requested = (os.environ.get(MODE_ENV) or "").strip().lower() or None
    with portal_db.get_session() as session:
        setting = session.get(Setting, 1)
        if setting is None:
            mode = requested or "local"
            if mode not in SUPPORTED_MODES:
                raise PortalStartupError(
                    f"{MODE_ENV}={mode!r} is not supported by this version; "
                    f"supported: {', '.join(SUPPORTED_MODES)}"
                )
            session.add(Setting(id=1, mode=mode))
        else:
            mode = setting.mode
            if requested is not None and requested != mode:
                raise PortalStartupError(
                    f"{MODE_ENV}={requested!r} but this portal data directory "
                    f"({state.data_dir}) was set up in {mode!r} mode; the mode "
                    "cannot be changed"
                )
        state.mode = mode

        # A run still "running" belongs to a previous process that is gone.
        for run in session.exec(
            select(ScanRun).where(col(ScanRun.status) == "running")
        ).all():
            run.status = "interrupted"
            session.add(run)


def _reconcile_port(state: PortalState, agent_host: str) -> dict | None:
    """Follow a port change into the managed repos; never fatal (section 4.13)."""
    try:
        return reconcile_port(state.data_dir, state.port, agent_host)
    except Exception:  # noqa: BLE001 -- a repo problem must not stop the portal
        _log.exception("could not follow the portal port into the managed repos")
        return None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return exc.response()


def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Pydantic's default body echoes the offending "input" - which could be
    # a token. Keep only where and what.
    detail = [
        {
            "loc": list(err.get("loc", ())),
            "msg": err.get("msg"),
            "type": err.get("type"),
        }
        for err in exc.errors()
    ]
    return JSONResponse({"error": "invalid request", "detail": detail}, status_code=422)


async def _api_not_found(request: Request) -> JSONResponse:
    raise ApiError(404, f"no API route {request.url.path}")


async def _mcp_not_found(request: Request) -> JSONResponse:
    return JSONResponse(
        {"error": f"no MCP endpoint {request.url.path}"}, status_code=404
    )


__all__ = ["PortalStartupError", "create_portal_app"]
