"""Tiny stdlib HTTP server for `whygraph serve`.

Three handlers, localhost-only by default:

- ``GET /`` — serves the assembled HTML (with `meta.runtime: "serve"`).
- ``GET /api/rationale?qualified_name=<qn>[&force_refresh=true]`` —
  calls ``whygraph_rationale_brief`` and returns the JSON.
- ``GET /api/healthz`` — sanity check.

Single-threaded; rationale generation runs sequentially. That's fine
for a local dev viewer; switch to ``ThreadingHTTPServer`` if you ever
want to support concurrent generations.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from whygraph.render import data as data_module
from whygraph.render import template as template_module


def _make_handler(
    *,
    repo_root: Path,
    codegraph_db: Path,
    whygraph_db: Path,
) -> type[BaseHTTPRequestHandler]:
    """Build a request handler closing over the project paths.

    Returning a class (not an instance) is the http.server contract. The
    closure captures paths so request-time code can re-open the DBs.
    """

    class Handler(BaseHTTPRequestHandler):
        # Quieter logs — default BaseHTTPRequestHandler.log_message writes
        # every request to stderr with timestamp formatting. Surface only
        # 4xx/5xx so the console stays readable.
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
            try:
                code = int(args[1]) if len(args) > 1 else 0
            except (ValueError, TypeError):
                code = 0
            if code >= 400:
                super().log_message(fmt, *args)

        def do_GET(self) -> None:  # noqa: N802 — http.server contract
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self._serve_index()
            elif parsed.path == "/api/healthz":
                self._send_json({"ok": True})
            elif parsed.path == "/api/rationale":
                self._serve_rationale(parse_qs(parsed.query))
            else:
                self._send_error(HTTPStatus.NOT_FOUND, f"unknown route {parsed.path}")

        # ---- handlers ----

        def _serve_index(self) -> None:
            try:
                # Live mode populates everything: rationale is on-demand,
                # so artificially limiting per-node detail would just add
                # friction without saving anything meaningful.
                payload = data_module.assemble(
                    repo_root=repo_root,
                    codegraph_db=codegraph_db,
                    whygraph_db=whygraph_db,
                    runtime="serve",
                    depth=4,
                )
                html = template_module.render(payload)
            except Exception as exc:  # noqa: BLE001
                self._send_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"failed to assemble viewer: {exc}",
                )
                return
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _serve_rationale(self, query: dict[str, list[str]]) -> None:
            qn_list = query.get("qualified_name") or []
            qn = (qn_list[0] if qn_list else "").strip()
            if not qn:
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    "missing or empty 'qualified_name' query parameter",
                )
                return
            force_refresh = bool(query.get("force_refresh"))
            try:
                # Imported lazily so test suites can mock it cleanly.
                from whygraph.mcp_server import whygraph_rationale_brief

                result = whygraph_rationale_brief(
                    qualified_name=qn,
                    force_refresh=force_refresh,
                )
            except Exception as exc:  # noqa: BLE001
                self._send_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"rationale generation failed: {exc}",
                )
                return
            self._send_json(result)

        # ---- helpers ----

        def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, status: int, message: str) -> None:
            body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(
    *,
    host: str,
    port: int,
    repo_root: Path,
    codegraph_db: Path,
    whygraph_db: Path,
    open_browser: bool = False,
) -> None:
    """Run the HTTP server until interrupted (Ctrl-C)."""
    handler = _make_handler(
        repo_root=repo_root,
        codegraph_db=codegraph_db,
        whygraph_db=whygraph_db,
    )
    httpd = HTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"Serving WhyGraph viewer at {url}")
    print("Press Ctrl-C to stop.")
    if open_browser:
        # Defer a beat so the server is accepting before we open the browser.
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
