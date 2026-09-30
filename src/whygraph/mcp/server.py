"""The WhyGraph MCP server — assembly point and ``whygraph-mcp`` entry.

Owns the single ``FastMCP("whygraph")`` instance, attaches every feature
module's tools to it at import time (so ``mcp.list_tools()`` works without
running the server), and exposes :func:`main` for the ``whygraph-mcp``
console script.

Adding a feature: create a ``whygraph/mcp/<feature>.py`` with a
``register(mcp)`` function, then import it and call ``register`` below.

Feature modules register through :class:`ThreadOffload`, not the
``FastMCP`` instance itself. FastMCP calls a plain ``def`` tool inline on
the event loop, and every WhyGraph tool does blocking work (git blame,
SQLite, LLM calls). Served over HTTP by the portal, one slow call would
freeze every other request, so each sync body is wrapped as an
``async def`` that runs it on a worker thread (context copied, so the
project-context seam still holds). The bodies themselves are unchanged.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable

import anyio.to_thread
from anyio import CapacityLimiter
from mcp.server.fastmcp import FastMCP

from whygraph.core import configure_logging, get_config

from . import area_history, evidence, prompts, rationale, resources

MCP_THREAD_TOKENS = 8
"""Worker threads MCP bodies may occupy at once (see :data:`_MCP_LIMITER`)."""

# A dedicated pool: slow MCP calls must not exhaust anyio's default
# 40-token limiter, which every sync HTTP handler and chat stream shares.
_MCP_LIMITER = CapacityLimiter(MCP_THREAD_TOKENS)


def offload(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a sync callable as an ``async def`` that runs it on a worker thread.

    Parameters
    ----------
    fn : callable
        A tool, resource or prompt body. An ``async def`` is returned
        unchanged.

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
        return fn

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
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
        return self._wrap(self._server.tool(*args, **kwargs))

    def resource(self, *args: Any, **kwargs: Any) -> Callable[[Callable], Callable]:
        """Like :meth:`FastMCP.resource`, registering an offloaded body."""
        return self._wrap(self._server.resource(*args, **kwargs))

    def prompt(self, *args: Any, **kwargs: Any) -> Callable[[Callable], Callable]:
        """Like :meth:`FastMCP.prompt`, registering an offloaded body."""
        return self._wrap(self._server.prompt(*args, **kwargs))

    @staticmethod
    def _wrap(decorator: Callable[[Callable], Any]) -> Callable[[Callable], Callable]:
        def register(fn: Callable) -> Callable:
            decorator(offload(fn))
            return fn

        return register


mcp = FastMCP("whygraph")

_registrar = ThreadOffload(mcp)
evidence.register(_registrar)
rationale.register(_registrar)
area_history.register(_registrar)
resources.register(_registrar)
prompts.register(_registrar)


def main() -> None:
    """Run the WhyGraph MCP server on stdio. Entry point for ``whygraph-mcp``."""
    cfg = get_config()
    configure_logging(cfg.log_level, file_config=cfg.logging)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
