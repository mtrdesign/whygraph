"""Tests for `whygraph.render.server` — the live HTTP handler.

We bind the `HTTPServer` to port 0 (kernel-assigned), run it in a
background thread, and hit it with `urllib.request`. This proves the
real wire path; mocking the handler-internal stuff would test less.
"""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from whygraph.render import server as server_module
from whygraph.scan import authors as authors_module
from whygraph.scan.db import Database
from whygraph.scan.git import Commit


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def server_paths(tmp_path: Path, fake_codegraph_db) -> tuple[Path, Path, Path]:
    """Build a minimal repo with a git history + WhyGraph DB + CodeGraph DB.
    Return (repo_root, codegraph_db, whygraph_db)."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "alice@example.com")
    _git(tmp_path, "config", "user.name", "Alice")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "a.py").write_text("L1\nL2\nL3\nL4\nL5\n")
    (pkg / "b.py").write_text("L1\nL2\nL3\nL4\nL5\n")
    (pkg / "c.py").write_text("L1\nL2\nL3\nL4\nL5\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    sha = _git_out(tmp_path, "rev-parse", "HEAD")

    wg_db = tmp_path / ".whygraph" / "whygraph.db"
    with Database(wg_db) as db:
        db.upsert_commit(
            Commit(
                sha=sha, parent_shas=[],
                author_name="Alice", author_email="alice@example.com",
                authored_at="2026-04-01T00:00:00+00:00",
                committed_at="2026-04-01T00:00:00+00:00",
                subject="init", body="",
                files_changed=3, insertions=15, deletions=0,
            )
        )
        authors_module.build_authors(db)
    return tmp_path, fake_codegraph_db, wg_db


@pytest.fixture
def live_server(server_paths):
    """Spin up the HTTP server on port 0 in a background thread.
    Yields ``(base_url)``; tears down on exit."""
    repo_root, cg_db, wg_db = server_paths
    handler = server_module._make_handler(
        repo_root=repo_root,
        codegraph_db=cg_db,
        whygraph_db=wg_db,
    )
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _get_json(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_text(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def test_healthz(live_server) -> None:
    status, body = _get_json(f"{live_server}/api/healthz")
    assert status == 200
    assert body == {"ok": True}


def test_index_serves_html_with_serve_runtime(live_server) -> None:
    status, body = _get_text(f"{live_server}/")
    assert status == 200
    assert 'id="whygraph-data"' in body
    assert '"runtime": "serve"' in body


def test_rationale_endpoint_proxies_to_mcp_tool(live_server) -> None:
    """The handler imports `whygraph_rationale_brief` lazily, so we can
    patch it via `whygraph.mcp_server`."""
    canned = {
        "target": {"qualified_name": "pkg.a"},
        "purpose": "p",
        "why": "w",
        "constraints": [],
        "tradeoffs": [],
        "risks": [],
        "confidence": 0.5,
        "cached": False,
    }
    with patch(
        "whygraph.mcp_server.whygraph_rationale_brief",
        return_value=canned,
    ) as mocked:
        status, body = _get_json(
            f"{live_server}/api/rationale?qualified_name=pkg.a"
        )
    assert status == 200
    assert body["purpose"] == "p"
    assert mocked.call_count == 1
    kwargs = mocked.call_args.kwargs
    assert kwargs["qualified_name"] == "pkg.a"
    assert kwargs["force_refresh"] is False


def test_rationale_endpoint_passes_force_refresh(live_server) -> None:
    with patch(
        "whygraph.mcp_server.whygraph_rationale_brief",
        return_value={
            "target": {}, "purpose": "p", "why": "w",
            "constraints": [], "tradeoffs": [], "risks": [],
            "confidence": 0.0, "cached": False,
        },
    ) as mocked:
        _get_json(f"{live_server}/api/rationale?qualified_name=pkg.a&force_refresh=1")
    kwargs = mocked.call_args.kwargs
    assert kwargs["force_refresh"] is True


def test_rationale_endpoint_400_on_missing_param(live_server) -> None:
    status, body = _get_json(f"{live_server}/api/rationale")
    assert status == 400
    assert "qualified_name" in body["error"]


def test_rationale_endpoint_500_on_tool_error(live_server) -> None:
    with patch(
        "whygraph.mcp_server.whygraph_rationale_brief",
        side_effect=RuntimeError("boom"),
    ):
        status, body = _get_json(
            f"{live_server}/api/rationale?qualified_name=pkg.a"
        )
    assert status == 500
    assert "boom" in body["error"]


def test_unknown_route_returns_404(live_server) -> None:
    status, body = _get_json(f"{live_server}/api/nope")
    assert status == 404
    assert "unknown route" in body["error"]
