"""The per-project MCP endpoint, ``/mcp/<slug>``.

One set of WhyGraph tool / resource / prompt registrations
(:data:`whygraph.mcp.server.mcp`) serves every project: the dispatcher
binds the project's context for the duration of the call, and the tool
bodies - which read the global lookups - become project-aware through
the seam.

* **Routing.** A Starlette ``Route("/mcp/{slug}")`` with a pure-ASGI
  endpoint, not ``Mount``: a mount compiles to ``/mcp/{slug}/{path:path}``
  and 307-redirects the bare URL, which agents do not follow on POST.
* **Local mode only.** Outside local mode (and on a degraded portal
  serving production hosts) the dispatcher answers ``404`` before any
  other check, and no session manager is built: production has no MCP
  endpoint yet. The route itself stays registered, because routes are
  added before the stored mode is known.
* **Gate.** The same principal (resolved by the guard), the same org
  membership (:func:`~whygraph.portal.deps.load_org_access`, ``404``
  outside the org), the same :func:`~whygraph.portal.deps.bind_project`
  with ``project.read`` and the same
  :func:`~whygraph.portal.deps.require_initialized` gate as the data
  routes, so an uninitialized project answers ``409`` and no DB file is
  created. Not a FastAPI route, so no ``Depends`` runs here: every check
  is called explicitly. M2's bearer-token auth belongs here too, not in FastMCP's
  ``auth=`` settings, which only ``FastMCP.streamable_http_app()`` wires.
* **Session manager.** Each app builds a **fresh** stateless
  :class:`StreamableHTTPSessionManager` (:func:`build_session_manager`)
  and runs it in its own lifespan; the FastMCP singleton's lazy manager
  can run only once per process.
* **Origin.** The guard is authoritative for ``Host`` / ``Origin``; the
  SDK's own check is given the same :class:`PortalOrigins` as defence in
  depth.
"""

from __future__ import annotations

import anyio.to_thread
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from whygraph.core.context import use_project
from whygraph.core.usage import use_usage_sink
from whygraph.mcp.server import mcp

from .authz import Action
from .deps import (
    ApiError,
    PortalState,
    bind_project,
    load_org_access,
    require_initialized,
)
from .security import PortalOrigins
from .usage import usage_sink_for


def build_session_manager(origins: PortalOrigins) -> StreamableHTTPSessionManager:
    """Build a stateless MCP session manager for one portal app.

    Parameters
    ----------
    origins : PortalOrigins
        Feeds the SDK's transport-security allowlists.

    Returns
    -------
    StreamableHTTPSessionManager
        Not yet running - enter its ``run()`` in the app lifespan.
    """
    return StreamableHTTPSessionManager(
        app=mcp._mcp_server,
        stateless=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=sorted(origins.hosts),
            allowed_origins=sorted(origins.origins),
        ),
    )


def serves_mcp(state: PortalState) -> bool:
    """Whether this portal serves ``/mcp`` (local mode only).

    A degraded portal has no stored mode; it follows the origins it serves,
    which are production's when the environment asks for production.
    """
    if state.mode is not None:
        return state.mode == "local"
    return state.origins is None or state.origins.base is None


def mcp_not_found(scope: Scope) -> JSONResponse:
    """The ``404`` for a path with no MCP endpoint."""
    return JSONResponse({"error": f"no MCP endpoint {scope['path']}"}, status_code=404)


class McpDispatcher:
    """ASGI endpoint of ``/mcp/{slug}``: gate, bind, hand to the session manager.

    Parameters
    ----------
    state : PortalState
        The portal state (its ``session_manager`` must be running).
    """

    def __init__(self, state: PortalState) -> None:
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not serves_mcp(self.state):
            await mcp_not_found(scope)(scope, receive, send)
            return
        try:
            request_state = scope.get("state", {})
            principal = request_state.get("principal")
            if principal is None:
                raise ApiError(409, "setup required")
            org_slug = request_state.get("org_slug")
            access = None
            if org_slug is not None:
                access = await anyio.to_thread.run_sync(
                    load_org_access, principal.user_id, org_slug
                )
            if access is None:
                raise ApiError(404, "not found")
            project = await bind_project(
                self.state, access, scope["path_params"]["slug"], Action.PROJECT_READ
            )
            await require_initialized(self.state, project)
            if self.state.session_manager is None:
                raise ApiError(503, "MCP is not running")
        except ApiError as exc:
            await exc.response()(scope, receive, send)
            return
        sink = usage_sink_for(
            self.state,
            project,
            source="mcp",
            org_slug=access.org_slug,
            principal=principal,
            user_id=access.user_id,
        )
        with use_project(project.ctx), use_usage_sink(sink):
            await self.state.session_manager.handle_request(scope, receive, send)


__all__ = ["McpDispatcher", "build_session_manager", "mcp_not_found", "serves_mcp"]
