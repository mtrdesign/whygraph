"""Portal state and the request dependencies that bind a project.

Every ``/api`` route names the one action it performs through a
dependency factory (plan section 4.5); the route-inventory test checks
that each route declares exactly one. Each factory's inner function
carries the action as ``whygraph_action``:

* :func:`project_access` - resolve the slug **within the request's org**
  (``404`` if unknown there), :func:`~whygraph.portal.authz.authorize`
  the action (``403``) - before any context is built, so a refused
  request never decrypts a secret - then build the
  :class:`~whygraph.core.context.ProjectContext` and bind it for the
  request. **No** initialized gate: ``POST init``, ``GET/PUT config`` and
  the project details use it, so an uninitialized project can be set up.
* :func:`project_db_access` - the same, plus the initialized gate
  (``409 {"error": "not initialized"}`` until ``projects.initialized_at``
  is set **and** the DB file exists, so nothing creates an empty
  ``.whygraph/whygraph.db`` in the user's repo) plus the memoized
  migration (:mod:`whygraph.portal.migrate`). The data routers and the
  scan endpoints use it; the MCP dispatcher calls the same steps. A DB
  path that is a symlink (or leaves the root) is a
  ``409 {"code": "unsafe_path"}`` (:func:`checked_db_paths`).
* :func:`org_access` - portal-level routes: the caller's
  :class:`~whygraph.portal.authz.OrgAccess`, authorized for the action.
* :func:`user_access` (``user.self``) and :func:`instance_access`
  (``instance.admin``) - production's account, org-creation and admin
  routes, which name no org: mode, then host (both ``404``), then
  :func:`current_user`, then the action.
* :func:`v1_project_access` - production's bearer-only ``/api/v1``
  project routes (M2e): :func:`v1_user`, then :func:`bind_v1_project` (the
  token's one project, by id).

A route takes exactly **one** project dependency: two different closures
are two callables to FastAPI, which would bind the project twice.

Who the request acts as, and in which organization, comes from the
guard (:class:`~whygraph.portal.security.PortalGuard`) through the
state's :class:`IdentityResolver`; :func:`current_org` turns the org slug
it stored into the caller's :class:`~whygraph.portal.authz.OrgAccess`.

Why ``async def``: a sync dependency runs in the threadpool, and a
``ContextVar`` set there never reaches the endpoint. Set in the request
task instead, the binding is copied into every sync endpoint and into the
chat ``StreamingResponse`` generator by Starlette's threadpool helpers.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Literal,
    Protocol,
)

import anyio
import anyio.to_thread
from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from sqlmodel import select
from starlette.datastructures import Headers
from starlette.types import Scope

from whygraph.core.config import ConfigError
from whygraph.core.context import ProjectContext, use_project
from whygraph.core.safe_paths import UnsafePathError

from .audit import audit
from .authz import Action, OrgAccess, Role, authorize
from .context import ContextCache, ProjectNotFound, resolve_root
from . import connections, sessions
from .db import InstanceLock, get_session
from .github_app import UserTokens
from .hosts import BaseUrl, classify
from .migrate import ProjectMigrations
from .models import Membership, Organization, Project, User
from .orgs import is_valid_org_slug
from .paths import check_project_paths
from .projects import is_valid_slug
from .repos import DiscoveryCache
from .runner import ScanRunner
from .security import PortalOrigins, Principal, _is_under
from .throttle import Throttle, ip_key
from .webhook import DeliveryIds

if TYPE_CHECKING:
    from .github_app import GitHubApp
    from .github_auth import GitHubOAuth


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
    headers : dict, optional
        Response headers (e.g. ``Retry-After`` on a ``429``).
    **extra
        More JSON fields for the body.
    """

    def __init__(
        self,
        status: int,
        error: str,
        *,
        code: str | None = None,
        headers: dict[str, str] | None = None,
        **extra,
    ):
        super().__init__(error)
        self.status = status
        self.error = error
        self.code = code
        self.headers = headers
        self.extra = extra

    def response(self) -> JSONResponse:
        """Render the error as a :class:`JSONResponse`."""
        body: dict[str, Any] = {"error": self.error}
        if self.code is not None:
            body["code"] = self.code
        body.update(self.extra)
        return JSONResponse(body, status_code=self.status, headers=self.headers)


_UNSET = object()


LOCK_CHECK_INTERVAL_SEC = 15.0
"""How often a running portal checks that it still holds the instance lock."""


class IdentityResolver(Protocol):
    """Names who a request acts as and which organization it addresses.

    The guard calls both methods once per request (never when the portal
    is degraded) and stores the answers in ``scope["state"]``. Both must
    be cheap: the org slug is only *named* here, and the membership is
    checked later by :func:`current_org` (:func:`load_org_access`).
    """

    async def principal(self, scope: Scope) -> Principal | None:
        """Return the request's principal, or ``None`` (no user yet)."""
        ...

    async def org_slug(self, scope: Scope) -> str | None:
        """Return the slug of the organization the request addresses, if any."""
        ...


class LocalIdentity:
    """Local mode's identity: the single user, in the built-in organization.

    Parameters
    ----------
    state : PortalState
        The portal state. :meth:`principal` delegates to its cached
        :meth:`PortalState.resolve_principal` (so
        :meth:`PortalState.set_principal` keeps working), and
        :meth:`org_slug` answers its ``builtin_org_slug`` from memory.
    """

    def __init__(self, state: PortalState) -> None:
        self.state = state

    async def principal(self, scope: Scope) -> Principal | None:
        """Return the local user (``None`` before first-run setup)."""
        return await self.state.resolve_principal(scope)

    async def org_slug(self, scope: Scope) -> str | None:
        """Return the built-in organization's slug (``None`` in production)."""
        return self.state.builtin_org_slug


class SessionIdentity:
    """Production identity: the session cookie names the user, the Host names the org.

    The guard calls it only on ``/api`` and ``/mcp``, so static assets and
    SPA pages never query the database. The membership is still checked
    per request by :func:`current_org`.

    Parameters
    ----------
    state : PortalState
        The portal state; its ``base_url`` must be set (the lifespan does
        so before it installs this resolver).
    """

    def __init__(self, state: PortalState) -> None:
        self.state = state

    async def principal(self, scope: Scope) -> Principal | None:
        """Return the session cookie's user, or ``None`` (signed out).

        More than one ``whygraph_session`` cookie (another host under the
        same parent domain tossed one in) counts as signed out, and sets
        ``scope["state"]["clear_session_cookie"]`` so the guard clears both
        cookie forms on the response. A live session last touched over
        :data:`~whygraph.portal.sessions.TOUCH_EVERY` ago is touched.
        """
        tokens = sessions.session_cookies(Headers(scope=scope).getlist("cookie"))
        if not tokens:
            return None
        if len(tokens) > 1:
            scope.setdefault("state", {})["clear_session_cookie"] = True
            return None
        row = await anyio.to_thread.run_sync(_lookup_session, tokens[0])
        if row is None:
            return None
        return Principal(
            user_id=row.user_id,
            uid=row.uid,
            display_name=row.display_name,
            email=row.email,
            session_id=row.session_id,
            is_instance_admin=row.is_instance_admin,
            github_login=row.github_login,
            avatar_url=row.avatar_url,
            has_password=row.has_password,
        )

    async def org_slug(self, scope: Scope) -> str | None:
        """Return the org label of ``Host`` (``None`` on the base host)."""
        base = self.state.base_url
        if base is None:
            return None
        kind = classify(Headers(scope=scope).get("host"), base)
        return None if kind is None else kind.slug


def _lookup_session(token: str) -> sessions.SessionRow | None:
    row = sessions.lookup(token)
    if row is not None and sessions.is_stale(row):
        sessions.touch(row.session_id)
    return row


V1_PREFIX = "/api/v1"
"""The bearer-only API a connected portal calls (M2e plan section 4.3)."""

V1_META = "/api/v1/meta"
"""The one public path under :data:`V1_PREFIX` (exactly this path)."""


def is_bearer_path(path: str) -> bool:
    """Whether ``path`` (``scope["path"]``) authenticates with a bearer token only.

    Everything under :data:`V1_PREFIX` except exactly :data:`V1_META`.
    Decided on ``scope["path"]`` - the value the router matches - so a
    percent-encoded or doubled slash cannot route to one place and
    authenticate as another.
    """
    return _is_under(path, V1_PREFIX) and path != V1_META


def bearer_token(scope: Scope) -> str | None:
    """The token of the request's single ``Authorization: Bearer`` header, if any."""
    values = Headers(scope=scope).getlist("authorization")
    if len(values) != 1:
        return None
    scheme, _, token = values[0].strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


class TokenIdentity(SessionIdentity):
    """Production identity with connection tokens: bearer on ``/api/v1``, else sessions.

    On a :func:`is_bearer_path` path the ``Authorization: Bearer wgc_...``
    header alone names the user - cookies are never read there - and the
    principal carries ``token_id`` / ``token_project_id``, no session and
    never the instance-admin flag. A refused token resolves to ``None`` and
    stores its :class:`~whygraph.portal.connections.Refusal` in
    ``scope["state"]["token_refusal"]`` for :func:`v1_user`. Every other
    path is :class:`SessionIdentity`'s, which ignores ``Authorization``.

    Failed lookups count against :attr:`PortalState.v1_auth_ip` per client
    address; past its limit a failure answers ``429``. A valid token is
    never refused by it (the throttle is consulted only after the lookup
    failed).

    Parameters
    ----------
    state : PortalState
        The portal state, as for :class:`SessionIdentity`.
    """

    async def principal(self, scope: Scope) -> Principal | None:
        """Return the request's principal (see the class docstring)."""
        if not is_bearer_path(scope["path"]):
            return await super().principal(scope)
        raw = bearer_token(scope)
        if raw is None:
            refusal = connections.INVALID
        else:
            found = await anyio.to_thread.run_sync(_lookup_token, raw)
            if isinstance(found, connections.TokenPrincipal):
                return Principal(
                    user_id=found.user_id,
                    uid=found.uid,
                    display_name=found.display_name,
                    email=found.email,
                    session_id=None,
                    is_instance_admin=False,
                    github_login=found.github_login,
                    avatar_url=found.avatar_url,
                    has_password=found.has_password,
                    token_id=found.token_id,
                    token_project_id=found.project_id,
                )
            refusal = found
            key = ip_key(scope)
            retry = self.state.v1_auth_ip.check(key)
            if retry is None:
                self.state.v1_auth_ip.record(key)
            else:
                refusal = connections.Refusal("throttled", retry_after=retry)
        scope.setdefault("state", {})["token_refusal"] = refusal
        return None


def _lookup_token(raw: str) -> connections.TokenPrincipal | connections.Refusal:
    with get_session() as db:
        found = connections.lookup(db, raw)
    if isinstance(found, connections.TokenPrincipal) and connections.is_stale(found):
        connections.touch(found.token_id)
    return found


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
    setup_lock : threading.Lock
        Serializes first-run setup.
    port_change : dict or None
        What the start-up port reconcile did
        (:func:`whygraph.portal.port_change.reconcile_port`), or ``None``.
    instance_lock : InstanceLock or None
        The "one portal per database" advisory lock; ``None`` only for
        test apps that deliberately share a database.
    lock_check_interval : float
        Seconds between liveness checks of ``instance_lock``.
    server : object or None
        The :class:`~whygraph.portal.app.PortalServer` serving this app
        (``None`` under a ``TestClient``); a lost lock sets its
        ``should_exit``.
    on_lock_lost : callable or None
        Called instead when there is no ``server`` (tests).
    startup_error : BaseException or None
        The exception that stopped the lifespan's start, for the CLI's
        exit code.
    builtin_org_id, builtin_org_slug : int, str or None
        Local mode's built-in organization, set by the lifespan
        (:func:`whygraph.portal.orgs.ensure_builtin_org`); ``None`` in
        production.
    identity : IdentityResolver
        What the guard asks for the principal and the org slug;
        :class:`LocalIdentity` by default, :class:`TokenIdentity` (a
        :class:`SessionIdentity` that also reads ``/api/v1`` bearer tokens)
        in production (tests inject their own through
        :func:`whygraph.portal.app.create_portal_app`).
    identity_injected : bool
        Whether ``identity`` was injected (then production keeps it).
    base_url : BaseUrl or None
        Production's validated ``WHYGRAPH_BASE_URL``; ``None`` in local mode.
    bootstrap_secret : str or None
        Production's one-time bootstrap secret, kept only in memory while
        no instance admin exists.
    base_check : list of str or None
        What the base-URL DNS self-check found (production; ``None`` until
        it ran, ``[]`` when healthy).
    github : GitHubOAuth or None
        Production's GitHub sign-in client (M2d-1); ``None`` in local mode.
    github_app : GitHubApp or None
        Production's GitHub App client (M2d-2), when the
        ``WHYGRAPH_GITHUB_APP_*`` variables are set; ``None`` otherwise.
    login_pair, login_email, login_ip : Throttle
        Sign-in **failures** (also wrong current passwords): per
        ``(email, ip_key)`` 5 / 15 min, per email 100 / hour, per
        ``ip_key`` 20 / 15 min (plan section 0.2).
    bootstrap_ip, reset_ip, github_ip : Throttle
        Every attempt, per ``ip_key``: bootstrap and reset 10 / 15 min,
        GitHub sign-in (start and callback, shared) 60 / 15 min.
    member_add_org : Throttle
        Every ``POST /api/org/members`` attempt, per org id: 60 / hour, so
        the route cannot probe which usernames have accounts at scale.
    import_org : Throttle
        GitHub imports that passed the access checks, per org id: 30 / hour
        (M2d-2 plan section 4.12).
    user_tokens : UserTokens
        The import page's GitHub App user tokens, per WhyGraph session, in
        memory only (M2d-2 plan section 4.3).
    webhook_deliveries : DeliveryIds
        The ``X-GitHub-Delivery`` ids of the GitHub App's webhook seen lately,
        so a replayed delivery does nothing (M2d-2 plan section 4.7).
    v1_auth_ip : Throttle
        Failed ``/api/v1`` bearer lookups, per ``ip_key``: 30 / 10 min
        (M2e plan section 4.3); a valid token is never refused by it.
    connect_codes : PendingCodes
        The authorization codes of ``POST /api/connect/authorize`` in
        flight, single use, 60 s (M2e plan section 4.4).
    connect_user : Throttle
        Every ``POST /api/connect/authorize`` attempt, per user id: 30 / hour.
    connect_ip : Throttle
        Failed ``POST /api/connect/token`` exchanges, per ``ip_key``:
        60 / 10 min (a successful exchange is never counted).
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
        self.port_change: dict | None = None
        self.instance_lock: InstanceLock | None = None
        self.lock_check_interval = LOCK_CHECK_INTERVAL_SEC
        self.server: Any = None
        self.on_lock_lost: Callable[[], None] | None = None
        self.startup_error: BaseException | None = None
        self.builtin_org_id: int | None = None
        self.builtin_org_slug: str | None = None
        self.identity: IdentityResolver = LocalIdentity(self)
        self.identity_injected = False
        self.base_url: BaseUrl | None = None
        self.bootstrap_secret: str | None = None
        self.base_check: list[str] | None = None
        self.github: GitHubOAuth | None = None
        self.github_app: GitHubApp | None = None
        self.login_pair = Throttle(5, 15 * 60)
        self.login_email = Throttle(100, 60 * 60)
        self.login_ip = Throttle(20, 15 * 60)
        self.bootstrap_ip = Throttle(10, 15 * 60)
        self.reset_ip = Throttle(10, 15 * 60)
        self.github_ip = Throttle(60, 15 * 60)
        self.member_add_org = Throttle(60, 60 * 60)
        self.import_org = Throttle(30, 60 * 60)
        self.user_tokens = UserTokens()
        self.webhook_deliveries = DeliveryIds()
        self.v1_auth_ip = Throttle(30, 10 * 60)
        self.connect_codes = connections.PendingCodes()
        self.connect_user = Throttle(30, 60 * 60)
        self.connect_ip = Throttle(60, 10 * 60)
        self._principal: Any = _UNSET
        self._principal_lock = threading.Lock()
        self._principal_generation = 0

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
            A load that finishes after a :meth:`set_principal` is
            discarded (a generation counter), so a slow first read can
            never reset the principal setup just created.
        """
        with self._principal_lock:
            cached, generation = self._principal, self._principal_generation
        if cached is not _UNSET:
            return cached
        loaded = await anyio.to_thread.run_sync(_load_local_principal)
        with self._principal_lock:
            if self._principal_generation == generation:
                self._principal = loaded
                return loaded
            return self._principal

    def set_principal(self, principal: Principal | None) -> None:
        """Replace the cached local-mode principal (wins over any load in flight)."""
        with self._principal_lock:
            self._principal = principal
            self._principal_generation += 1


def _load_local_principal() -> Principal | None:
    with get_session() as session:
        user = session.exec(select(User).order_by(User.id)).first()
        if user is None:
            return None
        return principal_of(user)


def principal_of(user: User) -> Principal:
    """Build the :class:`Principal` of a ``users`` row."""
    assert user.id is not None
    return Principal(user_id=user.id, uid=user.uid, display_name=user.display_name)


def portal_state(request: Request) -> PortalState:
    """Return the app's :class:`PortalState`."""
    return request.app.state.portal


async def current_user(request: Request) -> Principal:
    """The principal the guard resolved; ``409 setup required`` before setup.

    Every ``/api`` route except ``GET /api/portal/state`` and
    ``POST /api/portal/setup`` depends on this.

    A connection-token principal (``token_id`` set) never passes: a token
    authenticates ``/api/v1`` only (:func:`v1_user`), never a session route.

    Raises
    ------
    ApiError
        ``409`` when no user exists yet (local mode);
        ``401 {"code": "login_required"}`` without a session (production),
        or for a token principal.
    """
    principal = request.scope.get("state", {}).get("principal")
    if principal is not None and principal.token_id is not None:
        raise ApiError(401, "sign-in required", code="login_required")
    if principal is None:
        if portal_state(request).mode == "production":
            raise ApiError(401, "sign-in required", code="login_required")
        raise ApiError(409, "setup required")
    return principal


_REFUSAL_MESSAGES = {
    "invalid_token": "a valid connection token is required",
    "token_revoked": "this connection token was revoked",
}


def v1_refusal(refusal: connections.Refusal) -> ApiError:
    """The ``401`` (or ``429``) a refused bearer token gets.

    Parameters
    ----------
    refusal : Refusal
        What :class:`TokenIdentity` stored.

    Returns
    -------
    ApiError
        ``401 {"code": "invalid_token"}`` or ``401 {"code":
        "token_revoked", "reason": ...}`` with ``WWW-Authenticate: Bearer
        error="invalid_token"`` (RFC 6750); ``429 {"code": "throttled"}``
        with ``Retry-After``.
    """
    if refusal.code == "throttled":
        return ApiError(
            429,
            "too many attempts; try again later",
            code="throttled",
            headers={"Retry-After": str(refusal.retry_after or 1)},
        )
    extra = {} if refusal.reason is None else {"reason": refusal.reason}
    return ApiError(
        401,
        _REFUSAL_MESSAGES.get(refusal.code, _REFUSAL_MESSAGES["invalid_token"]),
        code=refusal.code,
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        **extra,
    )


async def v1_user(request: Request) -> Principal:
    """The connection-token principal of an ``/api/v1`` request.

    Never answers ``login_required``: a ``/api/v1`` client is a portal, not
    a browser.

    Raises
    ------
    ApiError
        As :func:`v1_refusal`, with the refusal :class:`TokenIdentity`
        stored (``invalid_token`` when there is none, or when the
        principal did not come from a token).
    """
    request_state = request.scope.get("state", {})
    principal = request_state.get("principal")
    if principal is not None and principal.token_id is not None:
        return principal
    raise v1_refusal(request_state.get("token_refusal") or connections.INVALID)


def load_org_access(
    user_id: int, org_slug: str, *, instance_admin: bool = False
) -> OrgAccess | None:
    """Return ``user_id``'s access to the organization ``org_slug``.

    One join of ``organizations`` and ``memberships``, run on every request
    and never cached, so a membership change (or an admin's demotion)
    applies to the next request.

    Parameters
    ----------
    user_id : int
        ``users.id`` of the principal.
    org_slug : str
        The organization slug the request addresses.
    instance_admin : bool, optional
        Whether the principal is an instance admin: without a membership
        they get :attr:`~whygraph.portal.authz.Role.READER`. Passed by
        :func:`current_org` and ``GET /api/portal/state``, never by the MCP
        dispatcher.

    Returns
    -------
    OrgAccess or None
        ``None`` when the org does not exist (or the slug is malformed) or
        the user is neither a member of it nor an instance admin - callers
        answer both the same way. A real membership wins over ``READER``.
    """
    if not is_valid_org_slug(org_slug):
        return None
    with get_session() as session:
        row = session.exec(
            select(
                Organization.id, Organization.slug, Organization.name, Membership.role
            )
            .join(Membership, Membership.org_id == Organization.id)
            .where(Organization.slug == org_slug, Membership.user_id == user_id)
        ).first()
        if row is None and instance_admin:
            org = session.exec(
                select(Organization.id, Organization.slug, Organization.name).where(
                    Organization.slug == org_slug
                )
            ).first()
            if org is not None:
                row = (*org, Role.READER.value)
    if row is None:
        return None
    org_id, slug, name, role = row
    return OrgAccess(org_id=org_id, org_slug=slug, org_name=name, role=Role(role))


async def current_org(
    request: Request, principal: Principal = Depends(current_user)
) -> OrgAccess:
    """The caller's access to the organization the guard named.

    FastAPI resolves it once per request, after :func:`current_user` (so
    ``409 setup required`` comes first). An instance admin without a
    membership reads the org as ``reader``: only ``GET`` / ``HEAD``, and
    each such request writes one security event record.

    Returns
    -------
    OrgAccess
        The organization and the caller's role in it.

    Raises
    ------
    ApiError
        ``404 {"error": "not found"}`` when the request names no org, the
        org does not exist or the caller is not a member - never saying
        which; ``403 {"code": "forbidden"}`` for a ``reader`` request with
        another method.
    """
    org_slug = request.scope.get("state", {}).get("org_slug")
    if org_slug is None:
        raise ApiError(404, "not found")
    access = await anyio.to_thread.run_sync(
        partial(
            load_org_access,
            principal.user_id,
            org_slug,
            instance_admin=principal.is_instance_admin,
        )
    )
    if access is None:
        raise ApiError(404, "not found")
    if access.role == Role.READER:
        audit(
            "reader_request",
            request,
            uid=principal.uid,
            org=access.org_slug,
            method=request.method,
            path=request.url.path,
        )
        if request.method not in ("GET", "HEAD"):
            raise ApiError(
                403,
                "instance admins can only read an organization they are not a "
                "member of",
                code="forbidden",
            )
    return access


@dataclass(frozen=True)
class BoundProject:
    """A project row snapshot plus its built context.

    Attributes
    ----------
    id, org_id, slug, name, source, remote_url, initialized_at, last_scan_at, created_at
        Copied from the ``projects`` row.
    stored_root : str
        ``projects.root`` as stored (relative for a GitHub clone).
    root : Path
        The absolute repository root.
    ctx : ProjectContext
        The context bound for the request.
    """

    id: int
    org_id: int
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


def _lookup(org_id: int, slug: str) -> Project | None:
    if not is_valid_slug(slug):
        return None
    with get_session() as session:
        project = session.exec(
            select(Project).where(Project.org_id == org_id, Project.slug == slug)
        ).first()
        if project is not None:
            session.expunge(project)
        return project


async def bind_project(
    state: PortalState,
    access: OrgAccess,
    slug: str,
    action: Action,
    *,
    project_id: int | None = None,
) -> BoundProject:
    """Resolve ``slug`` in the caller's org, authorize, build its context.

    The order is load-bearing: the lookup is scoped to ``access.org_id``
    (another org's project is simply not found), and
    :func:`~whygraph.portal.authz.authorize` runs **before** the context
    is built, so a refused request never reaches
    :meth:`ContextCache.get` (which decrypts the project's secrets).
    No binding and no initialized gate.

    Parameters
    ----------
    state : PortalState
        The portal state.
    access : OrgAccess
        The caller's access to the request's organization.
    slug : str
        The URL slug.
    action : Action
        What the request does with the project.
    project_id : int, optional
        Require this ``projects.id`` (:func:`bind_v1_project`): another
        project under the slug is ``404`` like an unknown one.

    Returns
    -------
    BoundProject
        The snapshot and context.

    Raises
    ------
    ApiError
        ``404`` for a slug unknown (or malformed) in the org, or naming
        another project than ``project_id``; ``403`` (``code="forbidden"``)
        when the role lacks ``action``; ``400`` when the stored config no
        longer validates.
    """
    project = await anyio.to_thread.run_sync(_lookup, access.org_id, slug)
    if project is None or project.id is None:
        raise ApiError(404, f"project {slug!r} not found")
    if project_id is not None and project.id != project_id:
        raise ApiError(404, f"project {slug!r} not found")
    authorize(access, action, project)
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
        org_id=project.org_id,
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


def project_access(
    action: Action,
) -> Callable[..., AsyncIterator[BoundProject]]:
    """Build the dependency that binds ``{slug}`` for a route doing ``action``.

    Parameters
    ----------
    action : Action
        The route's action (also stored as ``whygraph_action`` on the
        returned function, for the route-inventory test).

    Returns
    -------
    callable
        An ``async`` generator dependency yielding the
        :class:`BoundProject`, its context bound until the request ends.
        ``404`` / ``403`` / ``400`` as :func:`bind_project`.
    """

    async def dependency(
        slug: str, request: Request, access: OrgAccess = Depends(current_org)
    ) -> AsyncIterator[BoundProject]:
        project = await bind_project(portal_state(request), access, slug, action)
        with use_project(project.ctx):
            yield project

    dependency.whygraph_action = action  # type: ignore[attr-defined]
    return dependency


def project_db_access(
    action: Action,
) -> Callable[..., AsyncIterator[BoundProject]]:
    """:func:`project_access` plus the initialized gate and the migration.

    Its own closure rather than a ``Depends`` on :func:`project_access`,
    so a route binds its project exactly once.

    Parameters
    ----------
    action : Action
        The route's action (also stored as ``whygraph_action``).

    Returns
    -------
    callable
        An ``async`` generator dependency yielding the (bound,
        initialized, migrated) :class:`BoundProject`; ``409`` as
        :func:`require_initialized`.
    """

    async def dependency(
        slug: str, request: Request, access: OrgAccess = Depends(current_org)
    ) -> AsyncIterator[BoundProject]:
        state = portal_state(request)
        project = await bind_project(state, access, slug, action)
        with use_project(project.ctx):
            await require_initialized(state, project)
            yield project

    dependency.whygraph_action = action  # type: ignore[attr-defined]
    return dependency


async def bind_v1_project(
    state: PortalState,
    principal: Principal,
    org_slug: str | None,
    slug: str,
    action: Action,
) -> BoundProject:
    """Bind the one project a connection token reaches (``/api/v1``).

    :func:`bind_project` with two differences: the org access is loaded
    with ``instance_admin=False`` (the ``reader`` fallback never applies
    to a token), and the project must be the token's by **id** - a project
    deleted and re-created under the same slug is unreachable.

    Parameters
    ----------
    state : PortalState
        The portal state.
    principal : Principal
        The :func:`v1_user` principal (``token_project_id`` set).
    org_slug : str or None
        The org the request's ``Host`` names.
    slug : str
        The URL slug.
    action : Action
        What the request does with the project.

    Returns
    -------
    BoundProject
        The snapshot and context.

    Raises
    ------
    ApiError
        ``404`` outside the token's org or project (never saying which);
        ``403`` / ``400`` as :func:`bind_project`.
    """
    if org_slug is None or principal.token_project_id is None:
        raise ApiError(404, "not found")
    access = await anyio.to_thread.run_sync(
        partial(load_org_access, principal.user_id, org_slug, instance_admin=False)
    )
    if access is None:
        raise ApiError(404, "not found")
    return await bind_project(
        state, access, slug, action, project_id=principal.token_project_id
    )


def v1_project_access(
    action: Action,
) -> Callable[..., AsyncIterator[BoundProject]]:
    """Build the dependency that binds ``{slug}`` for an ``/api/v1`` route doing ``action``.

    Parameters
    ----------
    action : Action
        The route's action (also stored as ``whygraph_action``).

    Returns
    -------
    callable
        An ``async`` generator dependency yielding the
        :class:`BoundProject`, its context bound until the request ends:
        ``401`` / ``429`` as :func:`v1_user`, then ``404`` / ``403`` /
        ``400`` as :func:`bind_v1_project`. No initialized gate.
    """

    async def dependency(
        slug: str, request: Request, principal: Principal = Depends(v1_user)
    ) -> AsyncIterator[BoundProject]:
        org_slug = request.scope.get("state", {}).get("org_slug")
        project = await bind_v1_project(
            portal_state(request), principal, org_slug, slug, action
        )
        with use_project(project.ctx):
            yield project

    dependency.whygraph_action = action  # type: ignore[attr-defined]
    return dependency


def org_access(action: Action) -> Callable[..., Awaitable[OrgAccess]]:
    """Build the dependency of a portal-level route doing ``action``.

    Parameters
    ----------
    action : Action
        The route's action (also stored as ``whygraph_action``).

    Returns
    -------
    callable
        An ``async`` dependency returning the caller's
        :class:`~whygraph.portal.authz.OrgAccess` once
        :func:`~whygraph.portal.authz.authorize` passed: ``404`` outside
        the org (:func:`current_org`), ``403`` for a role without
        ``action``.
    """

    async def dependency(access: OrgAccess = Depends(current_org)) -> OrgAccess:
        authorize(access, action)
        return access

    dependency.whygraph_action = action  # type: ignore[attr-defined]
    return dependency


def require_production(request: Request) -> None:
    """Refuse (``404``) unless the portal runs in production mode.

    Raises
    ------
    ApiError
        ``404 {"error": "not found"}`` in local (or degraded) mode.
    """
    if portal_state(request).mode != "production":
        raise ApiError(404, "not found")


def require_base_host(request: Request) -> None:
    """Refuse (``404``) unless the request addresses the base host.

    Raises
    ------
    ApiError
        ``404 {"error": "not found"}`` on an org host.
    """
    if request.scope.get("state", {}).get("host_kind") != "base":
        raise ApiError(404, "not found")


def require_mode_and_host(
    host: Literal["any", "base"],
) -> Callable[..., Awaitable[None]]:
    """Build the gate of a production-only route.

    Declared **before** :func:`current_user` in a dependency's parameters
    (FastAPI solves them in order), so a wrong mode or host is a ``404``
    rather than a ``401`` / ``403``.

    Parameters
    ----------
    host : {"any", "base"}
        Which hosts serve the route.

    Returns
    -------
    callable
        An ``async`` dependency: :func:`require_production`, then for
        ``"base"`` :func:`require_base_host`.
    """

    async def gate(request: Request) -> None:
        require_production(request)
        if host == "base":
            require_base_host(request)

    return gate


def user_access(
    host: Literal["any", "base"] = "any",
) -> Callable[..., Awaitable[Principal]]:
    """Build the dependency of a route acting on the caller's own account.

    Parameters
    ----------
    host : {"any", "base"}, optional
        Which hosts serve the route (``"base"`` for org creation).

    Returns
    -------
    callable
        An ``async`` dependency returning the :class:`Principal`
        (``whygraph_action`` = ``user.self``): ``404`` outside production
        or on a wrong host, then ``401`` without a session.
    """

    async def dependency(
        _gate: None = Depends(require_mode_and_host(host)),
        principal: Principal = Depends(current_user),
    ) -> Principal:
        return principal

    dependency.whygraph_action = Action.USER_SELF  # type: ignore[attr-defined]
    return dependency


def instance_access() -> Callable[..., Awaitable[Principal]]:
    """Build the dependency of an instance-admin route (base host only).

    Returns
    -------
    callable
        An ``async`` dependency returning the :class:`Principal`
        (``whygraph_action`` = ``instance.admin``): ``404`` outside
        production or on an org host, ``401`` without a session, ``403
        {"code": "forbidden"}`` unless this request's session belongs to an
        instance admin.
    """

    async def dependency(
        _gate: None = Depends(require_mode_and_host("base")),
        principal: Principal = Depends(current_user),
    ) -> Principal:
        if not principal.is_instance_admin:
            raise ApiError(
                403,
                f"only instance admins can {Action.INSTANCE_ADMIN}",
                code="forbidden",
                action=str(Action.INSTANCE_ADMIN),
            )
        return principal

    dependency.whygraph_action = Action.INSTANCE_ADMIN  # type: ignore[attr-defined]
    return dependency


__all__ = [
    "ApiError",
    "BoundProject",
    "IdentityResolver",
    "LocalIdentity",
    "PortalState",
    "SessionIdentity",
    "TokenIdentity",
    "V1_META",
    "V1_PREFIX",
    "bearer_token",
    "bind_project",
    "bind_v1_project",
    "bound_from",
    "checked_db_paths",
    "current_org",
    "current_user",
    "instance_access",
    "is_bearer_path",
    "load_org_access",
    "org_access",
    "portal_state",
    "principal_of",
    "project_access",
    "project_db_access",
    "require_base_host",
    "require_initialized",
    "require_mode_and_host",
    "require_production",
    "unsafe_path_error",
    "user_access",
    "v1_project_access",
    "v1_refusal",
    "v1_user",
]
