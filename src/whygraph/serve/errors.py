"""HTTP translation of :class:`~whygraph.mcp.errors.WhyGraphError`.

The Explorer routes (:mod:`whygraph.serve.routes`) raise the same
``WhyGraphError`` the MCP tools do. FastAPI only knows how to turn it
into a response through an **app-level** exception handler, so every app
that mounts those routers - ``whygraph serve`` (:mod:`whygraph.serve.app`)
and the portal (:mod:`whygraph.portal.app`) - registers
:func:`whygraph_error_handler`. Without it, "not found" would surface as
a 500.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from whygraph.mcp.errors import WhyGraphError


def whygraph_error_handler(_: Request, exc: WhyGraphError) -> JSONResponse:
    """Map a :class:`WhyGraphError` to ``404`` or ``400`` with ``{"error": ...}``.

    Parameters
    ----------
    _ : Request
        The failing request (unused).
    exc : WhyGraphError
        The error a route raised.

    Returns
    -------
    JSONResponse
        ``404`` when the message says "not found", else ``400`` - every
        other ``WhyGraphError`` is a bad-request-shaped failure (invalid
        target, unscanned DB message, ...).
    """
    status = 404 if "not found" in str(exc).lower() else 400
    return JSONResponse(status_code=status, content={"error": str(exc)})


__all__ = ["whygraph_error_handler"]
