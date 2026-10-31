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
* **Body cap.** A request body above :data:`MAX_BODY_BYTES` is answered
  ``413`` before the SDK reads it (the SDK reads bodies uncapped); the
  CodeGraph endpoint (:mod:`whygraph.portal.codegraph_host`) shares the
  gate and the cap.
"""

from __future__ import annotations

from typing import Any

import anyio.to_thread
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from whygraph.core.context import use_actor, use_project
from whygraph.core.usage import use_usage_sink
from whygraph.mcp.server import mcp

from .authz import Action, OrgAccess
from .deps import (
    ApiError,
    BoundProject,
    PortalState,
    bind_project,
    load_org_access,
    require_initialized,
)
from .security import PortalOrigins
from .usage import usage_sink_for


MAX_BODY_BYTES = 1 << 20
"""The largest request body either ``/mcp`` dispatcher accepts (1 MiB)."""


def build_session_manager(
    origins: PortalOrigins, *, app: Any = None
) -> StreamableHTTPSessionManager:
    """Build a stateless MCP session manager for one portal app.

    Parameters
    ----------
    origins : PortalOrigins
        Feeds the SDK's transport-security allowlists.
    app : mcp.server.lowlevel.Server, optional
        The server to run (default WhyGraph's, ``mcp._mcp_server``; the
        CodeGraph bridge passes its own).

    Returns
    -------
    StreamableHTTPSessionManager
        Not yet running - enter its ``run()`` in the app lifespan.
    """
    return StreamableHTTPSessionManager(
        app=mcp._mcp_server if app is None else app,
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


async def gate_project(
    state: PortalState, scope: Scope
) -> tuple[BoundProject, OrgAccess, Any]:
    """The ``/mcp`` gate: principal, org membership, ``project.read``, Initialize.

    Parameters
    ----------
    state : PortalState
        The portal state.
    scope : Scope
        The ASGI scope (the guard stored the principal and org slug).

    Returns
    -------
    tuple
        The bound project, the caller's org access and the principal.

    Raises
    ------
    ApiError
        ``409 setup_required`` without a principal, ``404`` outside the org
        or for an unknown slug, the Initialize gate's ``409``.
    """
    request_state = scope.get("state", {})
    principal = request_state.get("principal")
    if principal is None:
        raise ApiError(409, "setup required", code="setup_required")
    org_slug = request_state.get("org_slug")
    access = None
    if org_slug is not None:
        access = await anyio.to_thread.run_sync(
            load_org_access, principal.user_id, org_slug
        )
    if access is None:
        raise ApiError(404, "not found")
    project = await bind_project(
        state, access, scope["path_params"]["slug"], Action.PROJECT_READ
    )
    await require_initialized(state, project)
    return project, access, principal


async def capped_receive(scope: Scope, receive: Receive) -> Receive:
    """Read a request body of at most :data:`MAX_BODY_BYTES`, then replay it.

    Parameters
    ----------
    scope : Scope
        The ASGI scope.
    receive : Receive
        The ASGI receive callable.

    Returns
    -------
    Receive
        A receive that hands over the buffered body once, then defers to
        ``receive`` (so a disconnect is still seen). A request without a
        body to read gets ``receive`` back.

    Raises
    ------
    ApiError
        ``413`` when ``Content-Length`` or the body read exceeds the cap.
    """
    for name, value in scope.get("headers") or ():
        if name == b"content-length":
            try:
                declared = int(value)
            except ValueError:
                declared = 0
            if declared > MAX_BODY_BYTES:
                raise ApiError(413, "request body too large")
    if scope.get("method") != "POST":
        return receive
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            first = message
            break
        body = message.get("body", b"")
        size += len(body)
        if size > MAX_BODY_BYTES:
            raise ApiError(413, "request body too large")
        chunks.append(body)
        if not message.get("more_body", False):
            first = {
                "type": "http.request",
                "body": b"".join(chunks),
                "more_body": False,
            }
            break
    pending = [first]

    async def replay() -> dict:
        if pending:
            return pending.pop()
        return await receive()

    return replay


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
            project, access, principal = await gate_project(self.state, scope)
            if self.state.session_manager is None:
                raise ApiError(503, "MCP is not running")
            receive = await capped_receive(scope, receive)
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
        with (
            use_project(project.ctx),
            use_actor(principal.uid),
            use_usage_sink(sink),
        ):
            await self.state.session_manager.handle_request(scope, receive, send)


__all__ = [
    "MAX_BODY_BYTES",
    "McpDispatcher",
    "build_session_manager",
    "capped_receive",
    "gate_project",
    "mcp_not_found",
    "serves_mcp",
]
