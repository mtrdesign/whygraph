"""The per-project MCP endpoint, ``/mcp/<slug>``.

One set of WhyGraph tool / resource / prompt registrations
(:data:`whygraph.mcp.server.mcp`) serves every project: the dispatcher
binds the project's context for the duration of the call, and the tool
bodies - which read the global lookups - become project-aware through
the seam.

* **Routing.** A Starlette ``Route("/mcp/{slug}")`` with a pure-ASGI
  endpoint, not ``Mount``: a mount compiles to ``/mcp/{slug}/{path:path}``
  and 307-redirects the bare URL, which agents do not follow on POST.
* **Gate.** The same principal (resolved by the guard) and the same
  :func:`~whygraph.portal.deps.require_initialized` gate as the data
  routes, so an uninitialized project answers ``409`` and no DB file is
  created. M2's bearer-token auth belongs here too, not in FastMCP's
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

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import Receive, Scope, Send

from whygraph.core.context import use_project
from whygraph.mcp.server import mcp

from .deps import ApiError, PortalState, bind_project, require_initialized
from .security import PortalOrigins


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
        try:
            if scope.get("state", {}).get("principal") is None:
                raise ApiError(409, "setup required")
            project = await bind_project(self.state, scope["path_params"]["slug"])
            await require_initialized(self.state, project)
            if self.state.session_manager is None:
                raise ApiError(503, "MCP is not running")
        except ApiError as exc:
            await exc.response()(scope, receive, send)
            return
        with use_project(project.ctx):
            await self.state.session_manager.handle_request(scope, receive, send)


__all__ = ["McpDispatcher", "build_session_manager"]
