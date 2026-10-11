"""HTTP translation of :class:`~whygraph.mcp.errors.WhyGraphError`.

The Explorer routes (:mod:`whygraph.serve.routes`) raise the same
``WhyGraphError`` the MCP tools do. FastAPI only knows how to turn it
into a response through an **app-level** exception handler, so every app
that mounts those routers - the portal
(:func:`whygraph.portal.app.create_portal_app`) - registers
:func:`whygraph_error_handler`. Without it, "not found" would surface as
a 500.

:class:`ServeError` is the Explorer and Chat routes' own refusal: a
``WhyGraphError`` that carries its HTTP status and a code, so the same
handler renders it and no app needs a second exception handler.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from whygraph.core.usage import BUDGET_EXCEEDED
from whygraph.mcp.errors import WhyGraphError
from whygraph.mcp.rationale import GenerationNotPermitted


class ServeError(WhyGraphError):
    """A refusal of an Explorer or Chat route, with its status and code.

    Rendered by :func:`whygraph_error_handler` as ``{"error", "code"}``
    with ``status`` - a top-level body like the portal's ``ApiError``,
    never FastAPI's nested ``detail``.

    Parameters
    ----------
    status : int
        The HTTP status.
    message : str
        The server's own sentence (the playground shows it under "Show
        details").
    code : str
        The machine-readable code the playground words (``bad_title``,
        ``not_indexed``, ...).

    Attributes
    ----------
    status : int
        As passed.
    code : str
        As passed.
    """

    def __init__(self, status: int, message: str, *, code: str) -> None:
        super().__init__(message, code=code)
        self.status = status


def whygraph_error_handler(_: Request, exc: WhyGraphError) -> JSONResponse:
    """Render a :class:`WhyGraphError` as ``{"error": ..., "code"?: ...}``.

    A :class:`~whygraph.mcp.rationale.GenerationNotPermitted` for an
    exhausted hard-stopped budget is the one exception: ``403
    {"error", "code": "budget_exceeded", "scope"}``, the refusal every
    spending surface shares (M2f-2 plan section 4.7). A viewer's refusal
    keeps its ``400``.

    Parameters
    ----------
    _ : Request
        The failing request (unused).
    exc : WhyGraphError
        The error a route raised.

    Returns
    -------
    JSONResponse
        A :class:`ServeError`'s own ``status``; otherwise ``404`` when the
        message says "not found", else ``400`` - every other
        ``WhyGraphError`` is a bad-request-shaped failure (invalid target,
        unscanned DB message, ...); ``403`` for a budget refusal. The
        error's ``code``, when it has one, rides beside the message.
    """
    if isinstance(exc, GenerationNotPermitted) and exc.reason == BUDGET_EXCEEDED:
        return JSONResponse(
            status_code=403,
            content={"error": str(exc), "code": BUDGET_EXCEEDED, "scope": exc.scope},
        )
    if isinstance(exc, ServeError):
        status = exc.status
    else:
        status = 404 if "not found" in str(exc).lower() else 400
    content: dict[str, str] = {"error": str(exc)}
    if exc.code is not None:
        content["code"] = exc.code
    return JSONResponse(status_code=status, content=content)


__all__ = ["ServeError", "whygraph_error_handler"]
