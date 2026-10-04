"""Request guard for the portal: Host / Origin / CSRF checks and the principal.

Local mode has no login, so the portal is a textbook CSRF and DNS-rebinding
target: any web page the user visits can send requests to
``127.0.0.1:<port>``, and a rebinding domain can read the responses. The
:class:`PortalGuard` pure-ASGI middleware closes that (plan section 6):

* **Host** must be one of :attr:`PortalOrigins.hosts`, on every request,
  else ``421``. A rebinding domain carries its own name in ``Host``.
* On ``/api`` and ``/mcp``, ``Sec-Fetch-Site: cross-site`` / ``same-site``
  is rejected, and an ``Origin`` that is present must be an **exact**
  configured origin (``null`` included in what is rejected), else ``403``.
* On ``/api``, **every** request - GETs too, since some GETs make LLM calls -
  must carry ``X-WhyGraph-Client: 1``, else ``403``. A cross-origin page
  cannot add a custom header without a CORS preflight, which the portal
  never grants.

In production (:attr:`PortalOrigins.base` set) the same checks follow the
base URL instead of the loopback set: ``Host`` must be the base host or one
org label under it (:func:`whygraph.portal.hosts.classify`, else ``421``),
an ``Origin`` must be exactly the request's own host, and every response
carries the security headers of plan section 0.2 (framing, referrer,
sniffing, HSTS for ``https``, ``Cache-Control: no-store`` on ``/api`` and
on any response that sets a cookie).

On ``/api`` and ``/mcp`` only, the guard also resolves the request's
:class:`Principal` once (in local mode, the single user, or ``None`` before
first-run setup; in production, the session cookie's user), stores it in
``scope["state"]["principal"]`` and in a :class:`~contextvars.ContextVar`,
so FastAPI routes, the MCP dispatcher (not a FastAPI route) and the hook
``POST .../scans`` all see the same one. :func:`whygraph.portal.deps.current_user`
only reads it. Next to it, ``scope["state"]["org_slug"]`` names the
organization the request addresses (in local mode, the built-in org). Both
come from the state's :class:`~whygraph.portal.deps.IdentityResolver`; the
guard never queries the org itself - the membership is checked by
:func:`whygraph.portal.deps.current_org`. ``scope["state"]["host_kind"]``
says which kind of host was addressed (``"local"``, ``"base"`` or
``"org"``). Static assets and SPA pages never reach the resolver, so they
cost no database query.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .hosts import TRUSTED_PROXIES_ENV, BaseUrl, classify
from .sessions import clearing_headers

if TYPE_CHECKING:  # pragma: no cover
    from .deps import PortalState

_log = logging.getLogger(__name__)

CLIENT_HEADER = "x-whygraph-client"
"""The custom header every ``/api`` request must carry (value ``"1"``)."""

DEV_ORIGINS_ENV = "WHYGRAPH_DEV_ORIGINS"
"""Dev-only, comma-separated extra origins (e.g. the Vite dev server)."""

LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "[::1]")
"""Host names a local-mode portal answers to."""


@dataclass(frozen=True)
class PortalOrigins:
    """Where the portal is reachable: the one source for hosts, origins and URLs.

    Read by the guard, the MCP SDK's transport-security settings, the
    agent MCP snippets and the repo markers, so the loopback set is never
    rebuilt (or derived from ``Host`` / ``scope["scheme"]``) anywhere else.

    Attributes
    ----------
    port : int
        The portal port.
    hosts : frozenset[str]
        Accepted ``Host`` header values (``127.0.0.1:<port>``,
        ``localhost:<port>``, ``[::1]:<port>`` plus dev origins' hosts,
        which may be portless).
    origins : frozenset[str]
        Accepted ``Origin`` header values, compared exactly.
    base_url : str
        The canonical URL, ``http://127.0.0.1:<port>``.
    agent_host : str
        The host agents are told to connect to (``127.0.0.1``).
    base : BaseUrl or None
        Production's public base URL; ``None`` in local mode. When set, the
        guard classifies ``Host`` against it and ``hosts`` / ``origins``
        are empty.
    """

    port: int
    hosts: frozenset[str]
    origins: frozenset[str]
    base_url: str
    agent_host: str = "127.0.0.1"
    base: BaseUrl | None = None


def build_origins(port: int, dev_origins: str | None = None) -> PortalOrigins:
    """Build the :class:`PortalOrigins` for a portal on ``port``.

    Parameters
    ----------
    port : int
        The published portal port.
    dev_origins : str, optional
        The raw ``WHYGRAPH_DEV_ORIGINS`` value: comma-separated
        ``http(s)://host[:port]`` origins added to both the allowed
        origins and (by their ``host[:port]``) the allowed hosts. Entries
        that are not a bare origin are ignored with a warning.

    Returns
    -------
    PortalOrigins
        The loopback set, plus any dev origins.
    """
    hosts = {f"{h}:{port}" for h in LOOPBACK_HOSTS}
    origins = {f"http://{h}" for h in hosts}
    for raw in (dev_origins or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        parts = urlsplit(raw)
        if (
            parts.scheme not in ("http", "https")
            or not parts.netloc
            or "@" in parts.netloc
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            _log.warning("ignoring invalid %s entry %r", DEV_ORIGINS_ENV, raw)
            continue
        origins.add(f"{parts.scheme}://{parts.netloc}")
        hosts.add(parts.netloc)
    return PortalOrigins(
        port=port,
        hosts=frozenset(hosts),
        origins=frozenset(origins),
        base_url=f"http://127.0.0.1:{port}",
    )


def build_production_origins(base: BaseUrl, port: int = 0) -> PortalOrigins:
    """Build the :class:`PortalOrigins` of a production portal.

    Parameters
    ----------
    base : BaseUrl
        The validated ``WHYGRAPH_BASE_URL``.
    port : int, optional
        The port the portal process listens on (informational).

    Returns
    -------
    PortalOrigins
        Empty ``hosts`` / ``origins`` (the guard classifies against
        ``base``), ``base_url`` the base URL's origin.
    """
    return PortalOrigins(
        port=port,
        hosts=frozenset(),
        origins=frozenset(),
        base_url=base.origin,
        base=base,
    )


@dataclass(frozen=True)
class Principal:
    """Who a request acts as.

    Attributes
    ----------
    user_id : int
        ``users.id``.
    uid : str
        The stable ``users.uid``.
    display_name : str
        Shown in the UI.
    email : str or None
        The login name (production); ``None`` in local mode.
    session_id : int or None
        The ``sessions.id`` the request signed in with (production).
    is_instance_admin : bool
        Whether the user is an instance admin (production).
    github_login : str or None
        The GitHub username of a GitHub account (production, M2d-1).
    avatar_url : str or None
        The GitHub avatar of a GitHub account (production).
    has_password : bool
        Whether the account signs in with a password (production).

    Notes
    -----
    The role is per organization (``memberships``), not on the principal.
    """

    user_id: int
    uid: str
    display_name: str
    email: str | None = None
    session_id: int | None = None
    is_instance_admin: bool = False
    github_login: str | None = None
    avatar_url: str | None = None
    has_password: bool = False


_principal: ContextVar[Principal | None] = ContextVar(
    "whygraph_principal", default=None
)


def current_principal() -> Principal | None:
    """Return the principal the guard resolved for the running request, if any."""
    return _principal.get()


def _is_under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


class PortalGuard:
    """Pure-ASGI middleware enforcing the checks in the module docstring.

    Pure ASGI (not ``BaseHTTPMiddleware``) so streaming responses and
    context variables behave exactly as without it.

    Parameters
    ----------
    app : ASGIApp
        The wrapped application.
    state : PortalState
        The portal's shared state: its ``origins`` (``None`` until the
        lifespan built them - requests then get ``503``), ``degraded``
        flag and ``identity`` resolver.
    """

    def __init__(self, app: ASGIApp, *, state: "PortalState") -> None:
        self.app = app
        self.state = state
        self._forwarding_warned = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        origins = self.state.origins
        if origins is not None and origins.base is not None:
            send = _production_send(scope, send, origins.base)

        rejection, host_kind = self._check(scope)
        if rejection is not None:
            await rejection(scope, receive, send)
            return

        request_state = scope.setdefault("state", {})
        request_state["host_kind"] = host_kind
        principal = None
        org_slug = None
        path = scope["path"]
        if not self.state.degraded and (
            _is_under(path, "/api") or _is_under(path, "/mcp")
        ):
            principal = await self.state.identity.principal(scope)
            org_slug = await self.state.identity.org_slug(scope)
        request_state["principal"] = principal
        request_state["org_slug"] = org_slug
        token = _principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _principal.reset(token)

    def _check(self, scope: Scope) -> tuple[JSONResponse | None, str | None]:
        """Return ``(rejection, host_kind)``; ``rejection`` is ``None`` to pass."""
        origins = self.state.origins
        if origins is None:
            return _error(503, "portal is starting"), None
        headers = Headers(scope=scope)
        path = scope["path"]

        if origins.base is None:
            if headers.get("host") not in origins.hosts:
                return _error(421, "unknown Host"), None
            host_kind = "local"
            allowed_origins = origins.origins
        else:
            kind = classify(headers.get("host"), origins.base)
            if kind is None:
                return _error(421, "unknown Host"), None
            host_kind = "base" if kind.is_base else "org"
            # Exactly the request's own (lower-cased) host: an org page may
            # never write to another org's host or to the base host.
            own = (
                origins.base.origin
                if kind.slug is None
                else origins.base.org_origin(kind.slug)
            )
            allowed_origins = frozenset({own})
            self._warn_untrusted_forwarding(headers)

        is_api = _is_under(path, "/api")
        if not (is_api or _is_under(path, "/mcp")):
            return None, host_kind

        if headers.get("sec-fetch-site") in ("cross-site", "same-site"):
            return _error(403, "cross-site request refused"), host_kind
        origin = headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            return _error(403, "origin not allowed"), host_kind

        if is_api:
            if headers.get(CLIENT_HEADER) != "1":
                return _error(403, f"missing {CLIENT_HEADER} header"), host_kind
            if self.state.degraded and not (
                path == "/api/portal/state" and scope["method"] == "GET"
            ):
                return _error(503, "portal database unavailable"), host_kind
        return None, host_kind

    def _warn_untrusted_forwarding(self, headers: Headers) -> None:
        """Warn once when a proxy forwards a client address nobody trusts."""
        if self._forwarding_warned or "x-forwarded-for" not in headers:
            return
        if os.environ.get(TRUSTED_PROXIES_ENV, "").strip():
            return
        self._forwarding_warned = True
        _log.warning(
            "a request carries X-Forwarded-For but %s is empty, so the proxy's "
            "address counts as every client's (throttling and the security log); "
            "set %s to the proxy's address",
            TRUSTED_PROXIES_ENV,
            TRUSTED_PROXIES_ENV,
        )


SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("x-frame-options", "DENY"),
    (
        "content-security-policy",
        "frame-ancestors 'none'; base-uri 'none'; object-src 'none'",
    ),
    ("referrer-policy", "same-origin"),
    ("x-content-type-options", "nosniff"),
)
"""Headers every production response carries (plan section 0.2)."""

HSTS = "max-age=31536000; includeSubDomains"
"""``Strict-Transport-Security`` for an ``https`` base URL."""


def _production_send(scope: Scope, send: Send, base: BaseUrl) -> Send:
    """Wrap ``send`` to add the production response headers.

    Also appends the two clearing ``Set-Cookie`` headers when the identity
    resolver set ``scope["state"]["clear_session_cookie"]`` (duplicate
    session cookies), and adds ``Cache-Control: no-store`` on ``/api`` and on
    any response that sets a cookie.
    """
    is_api = _is_under(scope["path"], "/api")

    async def wrapped(message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = MutableHeaders(scope=message)
            for name, value in SECURITY_HEADERS:
                headers[name] = value
            if base.scheme == "https":
                headers["strict-transport-security"] = HSTS
            if scope.get("state", {}).get("clear_session_cookie"):
                for name, value in clearing_headers(base):
                    headers.append(name.decode("latin-1"), value.decode("latin-1"))
            if is_api or "set-cookie" in headers:
                headers["cache-control"] = "no-store"
        await send(message)

    return wrapped


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


__all__ = [
    "CLIENT_HEADER",
    "DEV_ORIGINS_ENV",
    "LOOPBACK_HOSTS",
    "PortalGuard",
    "PortalOrigins",
    "Principal",
    "SECURITY_HEADERS",
    "build_origins",
    "build_production_origins",
    "current_principal",
]
