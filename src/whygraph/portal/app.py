"""The portal FastAPI application: :func:`create_portal_app`.

Composition (plan section 4.5.1)::

    PortalGuard (pure ASGI)       Host / Origin / Sec-Fetch-Site / X-WhyGraph-Client,
                                  and the principal, resolved once per request
    /api/portal/*                 management (portal/routes.py)
    /api/auth/*, /api/account*,   production identity, org creation and admin
    /api/orgs, /api/admin/*       (portal/auth_routes.py; 404 in local mode)
    /api/projects/*               management; each route names its action through
                                  org_access / project_access / project_db_access
    /api/projects/{slug}/...      serve.routes.router (project.read) + serve.chat.router
                                  (/chat, project.chat), via project_db_access
    /mcp/{slug}                   per-project MCP dispatcher (portal/mcp_mount.py)
    /api/*, /mcp/* not matched    404 {"error"} - never the SPA's index.html
    everything else               the SPA (serve.app._mount_static)

The lifespan waits for the portal database (an unreachable one raises
:class:`~whygraph.portal.db.PortalDatabaseUnreachable`, and the CLI exits 3
so the restart policy retries), takes the "one portal per database"
advisory lock and migrates the portal DB - a held lock or a failure in
those steps leaves a *degraded* app whose ``GET /api/portal/state`` reports
``{"error"}`` and whose other ``/api`` routes answer ``503`` - writes the
``settings`` row at first start and refuses to start when
``WHYGRAPH_MODE`` contradicts it, seeds local mode's built-in org, builds
the :class:`~whygraph.portal.security.PortalOrigins`, marks runs left
``running`` by a previous process ``interrupted``, follows a port change
into the managed repos (:mod:`whygraph.portal.port_change`), starts the
runner and
this app's own MCP session manager and the lock's liveness check (a lost
lock shuts the portal down), and turns strict project-context mode on. On
exit it sets the shutdown event (open streams end), stops the runner, turns
strict mode off again and releases the lock - on every path, including a
failed start.

Production mode (``WHYGRAPH_MODE=production``, M2c) validates its
environment at every start (``WHYGRAPH_BASE_URL``, no shared folders,
``WHYGRAPH_TRUSTED_PROXIES``) before anything commits, never seeds a built-in
org, installs :class:`~whygraph.portal.deps.SessionIdentity`, logs a one-time
bootstrap secret while no instance admin exists, and serves without shared
folders, port reconcile or MCP session manager (plan section 4.2).

The portal is **one process**: the runner, migration lock and caches are
in-process, so it must never run with several workers.
"""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import anyio
import anyio.to_thread
import uvicorn
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
from .auth_routes import auth_router
from .authz import Action
from .deps import (
    ApiError,
    IdentityResolver,
    PortalState,
    SessionIdentity,
    current_user,
    project_db_access,
)
from .github_auth import GitHubOAuth, load_github_config
from .hosts import (
    BASE_URL_ENV,
    TRUSTED_PROXIES_ENV,
    BaseUrl,
    parse_trusted_proxies,
    self_check,
)
from .mcp_mount import McpDispatcher, build_session_manager
from .member_routes import members_router
from .migrate import MIGRATION_LOCK
from .models import ScanRun, Setting, User
from .orgs import ensure_builtin_org
from .port_change import reconcile_port
from .repos import SHARED_FOLDERS_ENV, parse_shared_folders
from .routes import portal_router, projects_router, public_router
from .runner import ScanRunner
from .security import (
    DEV_ORIGINS_ENV,
    PortalGuard,
    PortalOrigins,
    build_origins,
    build_production_origins,
)

_log = logging.getLogger(__name__)

MODE_ENV = "WHYGRAPH_MODE"
"""Mode requested at first start (``local`` or ``production``); later only compared."""

SUPPORTED_MODES: tuple[str, ...] = ("local", "production")
"""Modes this release can run."""

_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


class PortalStartupError(RuntimeError):
    """The portal refuses to start (e.g. a ``WHYGRAPH_MODE`` mismatch)."""


class PortalServer(uvicorn.Server):
    """uvicorn server that ends the portal's open streams as soon as shutdown begins.

    uvicorn waits up to ``timeout_graceful_shutdown`` for open connections
    **before** it runs the lifespan shutdown, and an events stream (SSE)
    only ends when the lifespan's shutdown event is set - so without this,
    every open stream held a stop for the whole timeout and was then cut
    without its ``event: shutdown`` frame. :meth:`shutdown` sets the event
    first (signal or ``should_exit`` alike), so streams send the frame and
    close, and the graceful wait returns promptly.

    Parameters
    ----------
    config : uvicorn.Config
        The server config (its app is ``portal_app``).
    portal_app : FastAPI
        The app from :func:`create_portal_app`, whose
        ``state.portal.shutdown_event`` is set.
    """

    def __init__(self, config: uvicorn.Config, portal_app: FastAPI) -> None:
        super().__init__(config)
        self._portal_app = portal_app
        state: PortalState | None = getattr(portal_app.state, "portal", None)
        if state is not None:
            state.server = self  # a lost instance lock sets our should_exit

    async def shutdown(self, sockets=None) -> None:  # noqa: ANN001 -- uvicorn's signature
        """Set the portal's shutdown event, then run uvicorn's shutdown."""
        state: PortalState | None = getattr(self._portal_app.state, "portal", None)
        if state is not None and state.shutdown_event is not None:
            state.shutdown_event.set()
        await super().shutdown(sockets=sockets)


def create_portal_app(
    data_dir: Path | None = None,
    *,
    port: int = DEFAULT_PORTAL_PORT,
    runner: ScanRunner | None = None,
    instance_lock: bool = True,
    identity: IdentityResolver | None = None,
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
    instance_lock : bool
        Take the "one portal per database" advisory lock at start (the
        default, and what the CLI always uses). ``False`` is a test hook
        for apps that deliberately share one database; it is a factory
        parameter, not configuration, so no deployment can switch it off.
    identity : IdentityResolver, optional
        Who a request acts as and which organization it addresses; local
        mode's :class:`~whygraph.portal.deps.LocalIdentity` by default (what
        the CLI always uses). Like ``instance_lock``, a test hook: tests
        pass a header-driven resolver to drive several users and orgs.

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
    if instance_lock:
        state.instance_lock = portal_db.InstanceLock()
    if identity is not None:
        state.identity = identity
    state.identity_injected = identity is not None

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
    app.include_router(auth_router)  # production-only; local mode answers 404
    app.include_router(members_router)  # production-only, org-scoped
    app.include_router(portal_router)
    app.include_router(projects_router)
    app.include_router(
        data_router,
        prefix="/api/projects/{slug}",
        dependencies=[Depends(project_db_access(Action.PROJECT_READ))],
    )
    app.include_router(
        chat_router,
        prefix="/api/projects/{slug}/chat",
        dependencies=[Depends(project_db_access(Action.PROJECT_CHAT))],
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
    try:
        try:
            await anyio.to_thread.run_sync(_startup, state)
        except BaseException as exc:
            state.startup_error = exc  # the CLI maps it to an exit code
            raise
        async with _serving(state):
            yield
    finally:
        # Also after a failed start: the lock may already be held.
        if state.instance_lock is not None:
            await anyio.to_thread.run_sync(state.instance_lock.release)


@asynccontextmanager
async def _serving(state: PortalState) -> AsyncIterator[None]:
    """Serve one lifespan; what runs depends on the mode (plan section 4.2).

    Local mode: shared folders, the loopback origins, the port reconcile,
    the MCP session manager and the runner. Production: the base URL's
    origins, no shared folders, no port reconcile, no MCP manager, the
    runner and the DNS self-check. Degraded (no stored mode): the origins
    the environment asks for, so a degraded production portal still shows
    its error instead of ``421``; the MCP manager only for local origins.
    """
    if state.mode == "production":
        assert state.base_url is not None  # _startup validated it
        origins = build_production_origins(state.base_url, state.port)
    elif state.mode == "local":
        origins = build_origins(state.port, os.environ.get(DEV_ORIGINS_ENV))
    else:
        origins = _degraded_origins(state)
    local = origins.base is None
    if local:
        state.shared_folders = parse_shared_folders(
            os.environ.get(SHARED_FOLDERS_ENV), state.data_dir
        )
    state.shutdown_event = anyio.Event()
    if state.mode == "local":
        state.port_change = await anyio.to_thread.run_sync(
            _reconcile_port, state, origins.agent_host
        )
    async with AsyncExitStack() as stack:
        if local:
            manager = build_session_manager(origins)
            await stack.enter_async_context(manager.run())
            state.session_manager = manager
        if state.github is not None:
            stack.callback(state.github.close)
        if not state.degraded:
            await state.runner.start(state)
        watcher = anyio.create_task_group()
        await watcher.__aenter__()
        if not state.degraded and state.instance_lock is not None:
            watcher.start_soon(_watch_instance_lock, state)
        if state.mode == "production":
            watcher.start_soon(_check_base_url, state)
        set_strict(True)
        state.origins = origins
        try:
            yield
        finally:
            state.shutdown_event.set()
            watcher.cancel_scope.cancel()
            await watcher.__aexit__(None, None, None)
            try:
                if not state.degraded:
                    await state.runner.shutdown(grace=5.0)
            finally:
                set_strict(False)
                state.session_manager = None


def _degraded_origins(state: PortalState) -> PortalOrigins:
    """The origins of a degraded portal, from the environment alone."""
    if (os.environ.get(MODE_ENV) or "").strip().lower() == "production":
        try:
            base = BaseUrl.parse(os.environ.get(BASE_URL_ENV, ""))
        except ValueError:
            pass
        else:
            return build_production_origins(base, state.port)
    return build_origins(state.port, os.environ.get(DEV_ORIGINS_ENV))


async def _check_base_url(state: PortalState) -> None:
    """Run the never-fatal DNS self-check; keep and log what it finds."""
    assert state.base_url is not None
    problems = await anyio.to_thread.run_sync(
        self_check, state.base_url, abandon_on_cancel=True
    )
    state.base_check = problems
    for problem in problems:
        _log.warning("base URL check: %s", problem)


async def _watch_instance_lock(state: PortalState) -> None:
    """Shut the portal down when its instance lock is gone (section 4.5).

    A dead or half-dead database session silently drops the advisory lock;
    rather than run unguarded (or reconnect and re-check in place), the
    portal exits and the restart policy brings it back to wait for the
    database and take the lock again.
    """
    lock = state.instance_lock
    assert lock is not None
    while True:
        await anyio.sleep(state.lock_check_interval)
        if await anyio.to_thread.run_sync(lock.is_held):
            continue
        _log.error(
            "portal database connection lost - shutting down so the restart "
            "policy can re-acquire the instance lock"
        )
        if state.server is not None:
            state.server.should_exit = True
        elif state.on_lock_lost is not None:
            state.on_lock_lost()
        return


def _startup(state: PortalState) -> None:
    """Wait, lock, migrate, check / store the mode, seed the org, recover stale runs.

    Raises
    ------
    PortalDatabaseUnreachable
        When the database does not answer within the wait budget - a
        transient problem, so the process exits and is restarted. Every
        persistent problem (the lock held elsewhere, a failed migration or
        import) sets ``state.degraded`` instead, so it shows its reason
        rather than crash-looping.
    PortalStartupError
        On a ``WHYGRAPH_MODE`` mismatch, an unsupported mode, or a
        production portal whose environment does not validate (no or a bad
        ``WHYGRAPH_BASE_URL``, shared folders, bad trusted proxies). Raised
        before anything commits, so a refused first start writes no
        ``settings`` row.
    """
    portal_db.wait_for_database()

    if state.instance_lock is not None:
        try:
            acquired = state.instance_lock.acquire()
        except Exception as exc:  # noqa: BLE001 -- any failure means degraded mode
            _log.exception("could not take the portal instance lock")
            state.degraded = f"could not take the portal instance lock: {exc}"
            return
        if not acquired:
            target = portal_db.database_target()
            _log.error("another WhyGraph portal is already using %s", target)
            state.degraded = (
                f"another WhyGraph portal is already using this database ({target})"
            )
            return

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
            setting = Setting(id=1, mode=mode)
            session.add(setting)
        else:
            mode = setting.mode
            if requested is not None and requested != mode:
                raise PortalStartupError(
                    f"{MODE_ENV}={requested!r} but this portal data directory "
                    f"({state.data_dir}) was set up in {mode!r} mode; the mode "
                    "cannot be changed"
                )
        if mode == "production":
            # On the resolved mode, so a stored production portal started
            # without WHYGRAPH_MODE is validated too.
            state.base_url = _production_base_url()
            try:
                github_config = load_github_config(os.environ)
            except ValueError as exc:
                raise PortalStartupError(str(exc)) from exc
            state.github = GitHubOAuth(github_config)
        state.mode = mode
        if mode == "local":
            # Idempotent: creates the built-in org at first start, and
            # repairs a missing link on a later one.
            org = ensure_builtin_org(session, setting)
            state.builtin_org_id = org.id
            state.builtin_org_slug = org.slug
        else:
            if not state.identity_injected:
                state.identity = SessionIdentity(state)
            has_admin = session.exec(
                select(User.id).where(col(User.is_instance_admin).is_(True)).limit(1)
            ).first()
            state.bootstrap_secret = None
            if has_admin is None:
                _start_bootstrap(state)

        # A run still "running" belongs to a previous process that is gone.
        for run in session.exec(
            select(ScanRun).where(col(ScanRun.status) == "running")
        ).all():
            run.status = "interrupted"
            session.add(run)


def _production_base_url() -> BaseUrl:
    """Validate production's environment and return its base URL.

    Raises
    ------
    PortalStartupError
        ``WHYGRAPH_BASE_URL`` missing or invalid, ``WHYGRAPH_SHARED_FOLDERS``
        set (production has no shared folders), or ``WHYGRAPH_TRUSTED_PROXIES``
        invalid.
    """
    raw = (os.environ.get(BASE_URL_ENV) or "").strip()
    if not raw:
        raise PortalStartupError(
            f"production mode needs {BASE_URL_ENV} (e.g. https://whygraph.example.com)"
        )
    try:
        base = BaseUrl.parse(raw)
    except ValueError as exc:
        raise PortalStartupError(f"invalid {BASE_URL_ENV}: {exc}") from exc
    if (os.environ.get(SHARED_FOLDERS_ENV) or "").strip():
        raise PortalStartupError(
            f"{SHARED_FOLDERS_ENV} must be empty in production mode: production "
            "projects come from GitHub, never from a shared folder"
        )
    try:
        parse_trusted_proxies(os.environ.get(TRUSTED_PROXIES_ENV))
    except ValueError as exc:
        raise PortalStartupError(str(exc)) from exc
    if (os.environ.get(DEV_ORIGINS_ENV) or "").strip():
        _log.warning("%s is ignored in production mode", DEV_ORIGINS_ENV)
    return base


def _start_bootstrap(state: PortalState) -> None:
    """Make this start's bootstrap secret and log it (plan section 4.3).

    Two short ``WARNING`` records, so a log formatter that wraps long lines
    never splits the secret; tools read it with
    ``Bootstrap secret: ([A-Za-z0-9_-]{24})``.
    """
    assert state.base_url is not None
    state.bootstrap_secret = secrets.token_urlsafe(18)
    _log.warning(
        "First-time setup: open %s/setup and enter the bootstrap secret below.",
        state.base_url.origin,
    )
    _log.warning("Bootstrap secret: %s", state.bootstrap_secret)


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


__all__ = ["PortalServer", "PortalStartupError", "create_portal_app"]
