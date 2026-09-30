"""Portal state and the request dependencies that bind a project.

Two project dependencies exist (plan section 4.5.1), both ``async def``
generators that depend on :func:`current_user`:

* :func:`project_context` - resolve the slug (``404`` if unknown), build
  the :class:`~whygraph.core.context.ProjectContext` and bind it for the
  request. **No** initialized gate: ``POST init``, ``GET/PUT config`` and
  the project details use it, so an uninitialized project can be set up.
* :func:`project_db` - :func:`project_context` plus the initialized gate
  (``409 {"error": "not initialized"}`` until ``projects.initialized_at``
  is set **and** the DB file exists, so nothing creates an empty
  ``.whygraph/whygraph.db`` in the user's repo) plus the memoized
  migration (:mod:`whygraph.portal.migrate`). The data routers, the
  scan endpoints and the MCP dispatcher use it. A DB path that is a
  symlink (or leaves the root) is a ``409 {"code": "unsafe_path"}``
  (:func:`checked_db_paths`).

Why ``async def``: a sync dependency runs in the threadpool, and a
``ContextVar`` set there never reaches the endpoint. Set in the request
task instead, the binding is copied into every sync endpoint and into the
chat ``StreamingResponse`` generator by Starlette's threadpool helpers.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import anyio
import anyio.to_thread
from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from sqlmodel import select
from starlette.types import Scope

from whygraph.core.config import ConfigError
from whygraph.core.context import ProjectContext, use_project
from whygraph.core.safe_paths import UnsafePathError

from .context import ContextCache, ProjectNotFound, resolve_root
from .db import get_session
from .migrate import ProjectMigrations
from .models import Project, User
from .paths import check_project_paths
from .projects import is_valid_slug
from .repos import DiscoveryCache
from .runner import ScanRunner
from .security import PortalOrigins, Principal


class ApiError(Exception):
    """An error the portal API returns as ``{"error": ..., "code"?: ...}``.

    Parameters
    ----------
    status : int
        HTTP status.
    error : str
        Human-readable message.
    code : str, optional
        Machine-readable code (e.g. ``bad_token``, ``not_shared``).
    **extra
        More JSON fields for the body.
    """

    def __init__(self, status: int, error: str, *, code: str | None = None, **extra):
        super().__init__(error)
        self.status = status
        self.error = error
        self.code = code
        self.extra = extra

    def response(self) -> JSONResponse:
        """Render the error as a :class:`JSONResponse`."""
        body: dict[str, Any] = {"error": self.error}
        if self.code is not None:
            body["code"] = self.code
        body.update(self.extra)
        return JSONResponse(body, status_code=self.status)


_UNSET = object()


class PortalState:
    """Everything one portal app shares between requests.

    Built by :func:`whygraph.portal.app.create_portal_app`; the lifespan
    fills in ``origins``, ``mode``, ``shared_folders`` and the MCP
    ``session_manager``.

    Attributes
    ----------
    port : int
        The portal port.
    data_dir : Path
        The portal data directory.
    origins : PortalOrigins or None
        ``None`` until the lifespan ran (the guard then answers ``503``).
    mode : str or None
        The stored ``settings.mode``.
    degraded : str or None
        Why the portal DB is unusable, when it is (degraded mode).
    shared_folders : tuple[Path, ...]
        Validated shared folders.
    contexts : ContextCache
        Built project contexts; invalidate on every config / secret write.
    migrations : ProjectMigrations
        The per-project migration owner.
    runner : ScanRunner
        The scan runner (step 8).
    discovery : DiscoveryCache
        Cached repository discovery.
    shutdown_event : anyio.Event or None
        Set when the lifespan exits, so open streams end.
    session_manager : object or None
        This app's MCP ``StreamableHTTPSessionManager``.
    """

    def __init__(self, *, port: int, data_dir: Path, runner: ScanRunner) -> None:
        self.port = port
        self.data_dir = data_dir
        self.origins: PortalOrigins | None = None
        self.mode: str | None = None
        self.degraded: str | None = None
        self.shared_folders: tuple[Path, ...] = ()
        self.contexts = ContextCache()
        self.migrations = ProjectMigrations()
        self.runner = runner
        self.discovery = DiscoveryCache()
        self.shutdown_event: anyio.Event | None = None
        self.session_manager: Any = None
        self.setup_lock = threading.Lock()
        self._principal: Any = _UNSET

    async def resolve_principal(self, scope: Scope) -> Principal | None:
        """Return who the request acts as: in local mode, the single user.

        Parameters
        ----------
        scope : Scope
            The ASGI scope (unused in local mode; M2 reads cookies and
            bearer tokens from it).

        Returns
        -------
        Principal or None
            ``None`` before first-run setup. Cached after the first read;
            :meth:`set_principal` updates it when setup creates the user.
        """
        if self._principal is _UNSET:
            self._principal = await anyio.to_thread.run_sync(_load_local_principal)
        return self._principal

    def set_principal(self, principal: Principal | None) -> None:
        """Replace the cached local-mode principal."""
        self._principal = principal


def _load_local_principal() -> Principal | None:
    with get_session() as session:
        user = session.exec(select(User).order_by(User.id)).first()
        if user is None:
            return None
        return principal_of(user)


def principal_of(user: User) -> Principal:
    """Build the :class:`Principal` of a ``users`` row."""
    assert user.id is not None
    return Principal(
        user_id=user.id, uid=user.uid, display_name=user.display_name, role=user.role
    )


def portal_state(request: Request) -> PortalState:
    """Return the app's :class:`PortalState`."""
    return request.app.state.portal


async def current_user(request: Request) -> Principal:
    """The principal the guard resolved; ``409 setup required`` before setup.

    Every ``/api`` route except ``GET /api/portal/state`` and
    ``POST /api/portal/setup`` depends on this.

    Raises
    ------
    ApiError
        ``409`` when no user exists yet.
    """
    principal = request.scope.get("state", {}).get("principal")
    if principal is None:
        raise ApiError(409, "setup required")
    return principal


@dataclass(frozen=True)
class BoundProject:
    """A project row snapshot plus its built context.

    Attributes
    ----------
    id, slug, name, source, remote_url, initialized_at, last_scan_at, created_at
        Copied from the ``projects`` row.
    stored_root : str
        ``projects.root`` as stored (relative for a GitHub clone).
    root : Path
        The absolute repository root.
    ctx : ProjectContext
        The context bound for the request.
    """

    id: int
    slug: str
    name: str
    source: str
    stored_root: str
    root: Path
    remote_url: str | None
    initialized_at: str | None
    last_scan_at: str | None
    created_at: str
    ctx: ProjectContext

    @property
    def db_path(self) -> Path:
        """The project's WhyGraph DB (forced to the root default)."""
        return Path(self.ctx.config.whygraph_db or self.root / ".whygraph/whygraph.db")


def _lookup(slug: str) -> Project | None:
    if not is_valid_slug(slug):
        return None
    with get_session() as session:
        project = session.exec(select(Project).where(Project.slug == slug)).first()
        if project is not None:
            session.expunge(project)
        return project


async def bind_project(state: PortalState, slug: str) -> BoundProject:
    """Resolve ``slug`` and build its context (no binding, no gate).

    Parameters
    ----------
    state : PortalState
        The portal state.
    slug : str
        The URL slug.

    Returns
    -------
    BoundProject
        The snapshot and context.

    Raises
    ------
    ApiError
        ``404`` for an unknown (or malformed) slug; ``400`` when the stored
        config no longer validates.
    """
    project = await anyio.to_thread.run_sync(_lookup, slug)
    if project is None or project.id is None:
        raise ApiError(404, f"project {slug!r} not found")
    try:
        ctx = await state.contexts.aget(project.id)
    except ProjectNotFound as exc:
        raise ApiError(404, f"project {slug!r} not found") from exc
    except ConfigError as exc:
        raise ApiError(400, f"invalid project config: {exc}") from exc
    return bound_from(project, ctx)


def bound_from(project: Project, ctx: ProjectContext) -> BoundProject:
    """Snapshot a loaded ``projects`` row together with its context."""
    assert project.id is not None
    return BoundProject(
        id=project.id,
        slug=project.slug,
        name=project.name,
        source=project.source,
        stored_root=project.root,
        root=resolve_root(project),
        remote_url=project.remote_url,
        initialized_at=project.initialized_at,
        last_scan_at=project.last_scan_at,
        created_at=project.created_at,
        ctx=ctx,
    )


def unsafe_path_error(exc: UnsafePathError) -> ApiError:
    """The ``409 {"code": "unsafe_path"}`` for a refused repository path."""
    return ApiError(
        409,
        f"refusing to use {exc}: WhyGraph never follows a symbolic link out of "
        "the repository",
        code="unsafe_path",
        path=str(exc.path),
    )


def checked_db_paths(project: BoundProject) -> None:
    """Refuse (``409 unsafe_path``) a project whose DB paths are symlinked.

    Raises
    ------
    ApiError
        ``409 {"code": "unsafe_path"}`` - see
        :func:`whygraph.portal.paths.check_project_paths`.
    """
    try:
        check_project_paths(project.root)
    except UnsafePathError as exc:
        raise unsafe_path_error(exc) from exc


async def require_initialized(state: PortalState, project: BoundProject) -> None:
    """Gate on Initialize, then migrate the project DB once (``409`` otherwise).

    Parameters
    ----------
    state : PortalState
        The portal state (owns the migration memo).
    project : BoundProject
        The resolved project.

    Raises
    ------
    ApiError
        ``409 {"error": "not initialized"}`` when ``initialized_at`` is
        unset or the DB file is missing - the DB is then never created;
        ``409 {"code": "unsafe_path"}`` when a DB path is a symlink
        (checked first, before anything follows it).
    """
    checked_db_paths(project)
    if project.initialized_at is None or not project.db_path.is_file():
        raise ApiError(409, "not initialized")
    try:
        await anyio.to_thread.run_sync(state.migrations.ensure, project.ctx)
    except UnsafePathError as exc:
        raise unsafe_path_error(exc) from exc


async def project_context(
    slug: str,
    request: Request,
    principal: Principal = Depends(current_user),
) -> AsyncIterator[BoundProject]:
    """Resolve the slug and bind its project context for the request.

    Yields
    ------
    BoundProject
        The project; its context is bound until the request ends.
    """
    project = await bind_project(portal_state(request), slug)
    with use_project(project.ctx):
        yield project


async def project_db(
    request: Request, project: BoundProject = Depends(project_context)
) -> BoundProject:
    """:func:`project_context` plus the initialized gate and the migration.

    Returns
    -------
    BoundProject
        The (bound, initialized, migrated) project.
    """
    await require_initialized(portal_state(request), project)
    return project


__all__ = [
    "ApiError",
    "BoundProject",
    "PortalState",
    "bind_project",
    "bound_from",
    "checked_db_paths",
    "current_user",
    "portal_state",
    "principal_of",
    "project_context",
    "project_db",
    "require_initialized",
    "unsafe_path_error",
]
