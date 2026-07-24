"""FastAPI application factory for the Explorer panel.

:func:`create_app` wires the ``/api`` router (:mod:`whygraph.serve.routes`) onto a
FastAPI instance, translates the shared :class:`WhyGraphError` into HTTP responses,
and serves the built React bundle from ``static/`` with an SPA fallback.

The bundle is gitignored and produced only at build time (Docker ``COPY --from`` or
the hatch build hook), so a **source checkout** may have no ``static/``. The factory
must not crash in that case: it serves ``/api`` normally and returns a short
"UI not built" message at ``/`` (see :func:`_mount_static`).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from whygraph.core.config import Config
from whygraph.db import ensure_initialized
from whygraph.mcp.errors import WhyGraphError

from .routes import router

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_NOT_BUILT_MESSAGE = (
    "WhyGraph Explorer UI is not built.\n\n"
    "This is a source checkout with no static bundle. Build it with:\n"
    "    make playground\n"
    "    # or: npm --prefix src/playground ci && npm --prefix src/playground run build\n\n"
    "The /api endpoints are available and working."
)


def create_app(config: Config) -> FastAPI:
    """Build the Explorer FastAPI app for the current repository.

    Parameters
    ----------
    config : Config
        The resolved WhyGraph config (currently unused by the routes, which pull
        config lazily per request, but threaded through so the factory owns the
        config binding and future settings have a home).

    Returns
    -------
    FastAPI
        The configured application, ready for ``uvicorn.run``.
    """
    ensure_initialized()
    app = FastAPI(title="WhyGraph Explorer", docs_url=None, redoc_url=None)

    @app.exception_handler(WhyGraphError)
    def _whygraph_error_handler(_: Request, exc: WhyGraphError) -> JSONResponse:
        # A "not found" rejection maps to 404; every other WhyGraphError is a
        # bad-request-shaped failure (invalid target, unscanned DB message, …).
        status = 404 if "not found" in str(exc).lower() else 400
        return JSONResponse(status_code=status, content={"error": str(exc)})

    app.include_router(router, prefix="/api")
    _mount_static(app)
    return app


def _mount_static(app: FastAPI) -> None:
    """Serve the built SPA from ``static/`` with a client-routing fallback.

    When the bundle is absent (source checkout), install a placeholder ``/`` route
    instead so the server still starts and ``/api`` keeps working.
    """
    index = _STATIC_DIR / "index.html"
    if not index.is_file():

        @app.get("/")
        def _ui_missing() -> PlainTextResponse:
            return PlainTextResponse(_NOT_BUILT_MESSAGE)

        return

    # Real bundle: serve any built asset by path, else fall back to index.html so
    # client-side routes resolve. Declared after the /api router, so /api wins.
    @app.get("/{full_path:path}")
    def _spa(full_path: str) -> FileResponse:
        candidate = _STATIC_DIR / full_path
        if (
            full_path
            and candidate.is_file()
            and _STATIC_DIR in candidate.resolve().parents
        ):
            return FileResponse(candidate)
        return FileResponse(index)

    # Keep StaticFiles available for a conventional /static prefix too (harmless
    # if the bundle references absolute /assets paths, which the catch-all serves).
    if (_STATIC_DIR / "assets").is_dir():
        app.mount(
            "/assets", StaticFiles(directory=_STATIC_DIR / "assets"), name="assets"
        )
