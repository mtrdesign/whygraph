"""Serving the built React bundle (the SPA) from ``static/``.

The 1.x single-project ``create_app`` factory was replaced by the portal's
:func:`whygraph.portal.app.create_portal_app`, which mounts the data and chat
routers per project and calls :func:`_mount_static` last for the SPA.

The bundle is gitignored and produced only at build time (Docker ``COPY --from`` or
the hatch build hook), so a **source checkout** may have no ``static/``. The app
must not crash in that case: it serves ``/api`` normally and returns a short
"UI not built" message at ``/`` (see :func:`_mount_static`).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_NOT_BUILT_MESSAGE = (
    "WhyGraph Explorer UI is not built.\n\n"
    "This is a source checkout with no static bundle. Build it with:\n"
    "    make playground\n"
    "    # or: npm --prefix src/playground ci && npm --prefix src/playground run build\n\n"
    "The /api endpoints are available and working."
)


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
