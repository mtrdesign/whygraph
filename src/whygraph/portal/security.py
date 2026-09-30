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

The guard also resolves the request's :class:`Principal` once (in local
mode, the single user, or ``None`` before first-run setup), stores it in
``scope["state"]["principal"]`` and in a :class:`~contextvars.ContextVar`,
so FastAPI routes, the MCP dispatcher (not a FastAPI route) and the hook
``POST .../scans`` all see the same one. :func:`whygraph.portal.deps.current_user`
only reads it.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

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
    """

    port: int
    hosts: frozenset[str]
    origins: frozenset[str]
    base_url: str
    agent_host: str = "127.0.0.1"


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
    role : str
        ``"owner"`` in M1.
    """

    user_id: int
    uid: str
    display_name: str
    role: str


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
        flag and principal resolver.
    """

    def __init__(self, app: ASGIApp, *, state: "PortalState") -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        rejection = self._check(scope)
        if rejection is not None:
            await rejection(scope, receive, send)
            return

        principal = None
        if not self.state.degraded:
            principal = await self.state.resolve_principal(scope)
        scope.setdefault("state", {})["principal"] = principal
        token = _principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _principal.reset(token)

    def _check(self, scope: Scope) -> JSONResponse | None:
        origins = self.state.origins
        if origins is None:
            return _error(503, "portal is starting")
        headers = Headers(scope=scope)
        path = scope["path"]

        if headers.get("host") not in origins.hosts:
            return _error(421, "unknown Host")

        is_api = _is_under(path, "/api")
        if not (is_api or _is_under(path, "/mcp")):
            return None

        if headers.get("sec-fetch-site") in ("cross-site", "same-site"):
            return _error(403, "cross-site request refused")
        origin = headers.get("origin")
        if origin is not None and origin not in origins.origins:
            return _error(403, "origin not allowed")

        if is_api:
            if headers.get(CLIENT_HEADER) != "1":
                return _error(403, f"missing {CLIENT_HEADER} header")
            if self.state.degraded and not (
                path == "/api/portal/state" and scope["method"] == "GET"
            ):
                return _error(503, "portal database unavailable")
        return None


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


__all__ = [
    "CLIENT_HEADER",
    "DEV_ORIGINS_ENV",
    "LOOPBACK_HOSTS",
    "PortalGuard",
    "PortalOrigins",
    "Principal",
    "build_origins",
    "current_principal",
]
