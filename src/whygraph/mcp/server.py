"""The WhyGraph MCP server — assembly point.

Owns the single ``FastMCP("whygraph")`` instance and attaches every feature
module's tools to it at import time (so ``mcp.list_tools()`` works without
running the server). The portal serves it over HTTP at ``/mcp/<slug>``
(:mod:`whygraph.portal.mcp_mount`); the 1.x stdio entry point was removed
in 2.0.0.

Adding a feature: create a ``whygraph/mcp/<feature>.py`` with a
``register(mcp)`` function, then import it and call ``register`` below.

Feature modules register through :class:`ThreadOffload`, not the
``FastMCP`` instance itself. FastMCP calls a plain ``def`` tool inline on
the event loop, and every WhyGraph tool does blocking work (git blame,
SQLite, LLM calls). Served over HTTP by the portal, one slow call would
freeze every other request, so each sync body is wrapped as an
``async def`` that runs it on a worker thread (context copied, so the
project-context seam still holds). The bodies themselves are unchanged.
The wrapper also counts each call as agent activity under the registered
name (:func:`offload`).
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable

import anyio.to_thread
from anyio import CapacityLimiter
from mcp.server.fastmcp import FastMCP

from whygraph.core.usage import count_agent_call

from . import area_history, evidence, prompts, rationale, resources

MCP_THREAD_TOKENS = 8
"""Worker threads MCP bodies may occupy at once (see :data:`_MCP_LIMITER`)."""

# A dedicated pool: slow MCP calls must not exhaust anyio's default
# 40-token limiter, which every sync HTTP handler and chat stream shares.
_MCP_LIMITER = CapacityLimiter(MCP_THREAD_TOKENS)


def offload(fn: Callable[..., Any], *, kind: str) -> Callable[..., Any]:
    """Wrap a sync callable as an ``async def`` that runs it on a worker thread.

    Every call through the wrapper is first counted as agent activity
    (:func:`~whygraph.core.usage.count_agent_call`): the portal's MCP
    dispatcher binds a usage sink around the session manager, so the count
    lands on the calling project and member (M2f-3).

    Parameters
    ----------
    fn : callable
        A tool, resource or prompt body. An ``async def`` is not offloaded,
        only counted.
    kind : str
        What the call is counted as: the **registered** name of the tool,
        resource or prompt (never a URI or an argument).

    Returns
    -------
    callable
        A coroutine function with ``fn``'s name, docstring and signature
        (``functools.wraps`` sets ``__wrapped__``, so the schema FastMCP
        builds with ``inspect.signature`` is the original one). It runs
        ``fn`` through :func:`anyio.to_thread.run_sync`, which copies the
        caller's :mod:`contextvars` context, under a dedicated
        :class:`anyio.CapacityLimiter` of :data:`MCP_THREAD_TOKENS`.
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def counted(*args: Any, **kwargs: Any) -> Any:
            count_agent_call(kind)
            return await fn(*args, **kwargs)

        return counted

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        count_agent_call(kind)
        # run_sync takes positional args only, hence the partial.
        return await anyio.to_thread.run_sync(
            functools.partial(fn, *args, **kwargs), limiter=_MCP_LIMITER
        )

    return wrapper


class ThreadOffload:
    """A registration adapter over a :class:`FastMCP` server.

    Exposes the ``tool`` / ``resource`` / ``prompt`` decorator factories
    the feature modules' ``register()`` functions use, with the same
    arguments, and wraps each registered body with :func:`offload`.

    Parameters
    ----------
    server : FastMCP
        The server the registrations land on.
    """

    def __init__(self, server: FastMCP) -> None:
        self._server = server

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable], Callable]:
        """Like :meth:`FastMCP.tool`, registering an offloaded body."""
        return self._wrap(self._server.tool(*args, **kwargs), kwargs.get("name"))

    def resource(self, *args: Any, **kwargs: Any) -> Callable[[Callable], Callable]:
        """Like :meth:`FastMCP.resource`, registering an offloaded body."""
        return self._wrap(self._server.resource(*args, **kwargs), kwargs.get("name"))

    def prompt(self, *args: Any, **kwargs: Any) -> Callable[[Callable], Callable]:
        """Like :meth:`FastMCP.prompt`, registering an offloaded body."""
        return self._wrap(self._server.prompt(*args, **kwargs), kwargs.get("name"))

    @staticmethod
    def _wrap(
        decorator: Callable[[Callable], Any], name: str | None
    ) -> Callable[[Callable], Callable]:
        # The counted kind is the registered name: resources and prompts
        # register private functions under a public ``name=``.
        def register(fn: Callable) -> Callable:
            decorator(offload(fn, kind=name or fn.__name__))
            return fn

        return register


mcp = FastMCP("whygraph")

_registrar = ThreadOffload(mcp)
evidence.register(_registrar)
rationale.register(_registrar)
area_history.register(_registrar)
resources.register(_registrar)
prompts.register(_registrar)
