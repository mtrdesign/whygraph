"""Tests for the per-project MCP endpoint, ``/mcp/<slug>`` (plan section 4.5.3).

Covers the thread-offload adapter (schemas unchanged, sync bodies off the
event loop behind their own limiter), the dispatcher (gate, context,
no redirect on the bare URL), the per-app session manager (two apps in
one process) and the SDK's transport-security settings fed from
``PortalOrigins``. The end-to-end cases run a real uvicorn server in a
thread and talk to it with the MCP SDK client.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import httpx
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP

from test_portal_app import (  # noqa: F401 -- `env` is a fixture
    CLIENT_HEADER,
    env,
    make_repo,
    manual_ctx,
    portal_client,
    seed_codegraph,
)
from whygraph.core.context import use_project
from whygraph.db import get_session
from whygraph.db.models import Commit
from whygraph.mcp import area_history, evidence, prompts, rationale, resources
from whygraph.mcp import server as mcp_server
from whygraph.mcp.evidence import whygraph_evidence_for
from whygraph.services.git import Repository

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _rpc(method: str, params: dict | None = None, id: int = 1) -> dict:
    body: dict = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _sse_json(text: str) -> dict:
    """The JSON-RPC message of a one-event SSE response body."""
    for line in text.splitlines():
        if line.startswith("data: "):
            return json.loads(line.removeprefix("data: "))
    return json.loads(text)


def _setup_project(client, root: Path) -> None:
    """Setup + add + initialize ``root`` through any httpx-style client."""
    assert (
        client.post("/api/portal/setup", json={"display_name": "Tess"}).status_code
        == 201
    )
    added = client.post("/api/projects", json={"source": "local", "path": str(root)})
    assert added.status_code == 201, added.text
    slug = added.json()["project"]["slug"]
    init = client.post(f"/api/projects/{slug}/init", json={"agents": []})
    assert init.status_code == 200 and init.json()["initialized"], init.text


def _seed_commits(root: Path) -> None:
    newest, oldest = list(Repository(root).commits)
    with use_project(manual_ctx(root)), get_session() as session:
        for commit, when in ((oldest, "2026-01-01"), (newest, "2026-02-01")):
            session.add(
                Commit(
                    sha=commit.sha,
                    parent_shas="",
                    author_name="Test User",
                    author_email="tester@example.com",
                    authored_at=f"{when}T00:00:00+00:00",
                    committed_at=f"{when}T00:00:00+00:00",
                    subject=f"commit of {when}",
                    body="",
                    files_changed=1,
                    insertions=1,
                    deletions=0,
                    scanned_at="2026-05-01T00:00:00+00:00",
                    llm_description="Mechanical diff summary.",
                )
            )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def live_portal(port: int) -> Iterator[None]:
    """Run a portal app under a real uvicorn server in a thread."""
    from whygraph.portal.app import create_portal_app

    app = create_portal_app(port=port)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("portal did not start")
        time.sleep(0.02)
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(15)


def _http(port: int, **kw) -> httpx.Client:
    return httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        headers=CLIENT_HEADER,
        trust_env=False,
        timeout=30,
        **kw,
    )


# ---------------------------------------------------------------------------
# The thread-offload adapter
# ---------------------------------------------------------------------------


def _schema_view(tools) -> list[tuple]:
    return sorted(
        (
            t.name,
            t.description,
            json.dumps(t.inputSchema, sort_keys=True),
            json.dumps(t.outputSchema, sort_keys=True),
        )
        for t in tools
    )


def test_list_tools_schemas_are_unchanged_by_the_adapter() -> None:
    baseline = FastMCP("baseline")
    for module in (evidence, rationale, area_history, resources, prompts):
        module.register(baseline)

    wrapped = asyncio.run(mcp_server.mcp.list_tools())
    raw = asyncio.run(baseline.list_tools())
    assert _schema_view(wrapped) == _schema_view(raw)

    def _prompt_view(server: FastMCP) -> list:
        return sorted(
            (p.name, json.dumps([a.model_dump() for a in p.arguments or []]))
            for p in asyncio.run(server.list_prompts())
        )

    assert _prompt_view(mcp_server.mcp) == _prompt_view(baseline)
    templates = lambda s: sorted(  # noqa: E731
        t.uriTemplate for t in asyncio.run(s.list_resource_templates())
    )
    assert templates(mcp_server.mcp) == templates(baseline)


def test_registered_tools_are_async_and_offloaded() -> None:
    tools = mcp_server.mcp._tool_manager.list_tools()
    assert tools and all(t.is_async for t in tools)


def test_offload_runs_the_body_in_a_worker_thread_with_the_context() -> None:
    from whygraph.core.context import ProjectContext, current_project

    def body(x: int, *, y: int = 1) -> tuple:
        ctx = current_project()
        return x + y, threading.current_thread() is threading.main_thread(), ctx

    wrapped = mcp_server.offload(body)
    assert wrapped.__wrapped__ is body

    ctx = ProjectContext(slug="t", root=Path("/r"), config=None)  # type: ignore[arg-type]

    async def _run():
        with use_project(ctx):
            return await wrapped(2, y=3)

    total, on_main, seen = asyncio.run(_run())
    assert total == 5 and on_main is False and seen is ctx


# ---------------------------------------------------------------------------
# Dispatcher behaviour (TestClient)
# ---------------------------------------------------------------------------


def test_two_portal_apps_in_one_process_both_serve_mcp(
    env: SimpleNamespace,  # noqa: F811
) -> None:
    root = make_repo(env.shared, "demo")
    seed_codegraph(root)
    with portal_client(8765) as first:
        _setup_project(first, root)
        with portal_client(8766) as second:
            for client in (first, second):
                response = client.post(
                    "/mcp/demo", json=_rpc("tools/list"), headers=MCP_HEADERS
                )
                assert response.status_code == 200, response.text
                names = {t["name"] for t in _sse_json(response.text)["result"]["tools"]}
                assert "whygraph_evidence_for" in names
    # A third app after both managers shut down starts cleanly too.
    with portal_client(8767) as third:
        response = third.post("/mcp/demo", json=_rpc("tools/list"), headers=MCP_HEADERS)
        assert response.status_code == 200


def test_mcp_origin_checks_and_dev_origins(
    env: SimpleNamespace,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHYGRAPH_DEV_ORIGINS", "http://localhost:5173")
    root = make_repo(env.shared, "demo")
    with portal_client() as client:
        _setup_project(client, root)
        for origin, status in (
            ("http://localhost:3000", 403),
            ("null", 403),
            ("http://localhost:5173", 200),  # passes the guard AND the SDK check
            ("http://127.0.0.1:8765", 200),
        ):
            response = client.post(
                "/mcp/demo",
                json=_rpc("tools/list"),
                headers={**MCP_HEADERS, "Origin": origin},
            )
            assert response.status_code == status, (origin, response.text)
        # The SDK's own allowlist is fed from the same PortalOrigins.
        settings = client.app.state.portal.session_manager.security_settings
        assert "http://localhost:5173" in settings.allowed_origins
        assert "http://localhost:3000" not in settings.allowed_origins
        assert "127.0.0.1:8765" in settings.allowed_hosts


def test_mcp_unknown_slug_and_setup_gate(env: SimpleNamespace) -> None:  # noqa: F811
    with portal_client() as client:
        before = client.post("/mcp/demo", json=_rpc("tools/list"), headers=MCP_HEADERS)
        assert before.status_code == 409
        assert before.json() == {"error": "setup required"}
        client.post("/api/portal/setup", json={"display_name": "Tess"})
        unknown = client.post("/mcp/nope", json=_rpc("tools/list"), headers=MCP_HEADERS)
        assert unknown.status_code == 404


# ---------------------------------------------------------------------------
# End to end over a real server
# ---------------------------------------------------------------------------


def test_mcp_client_gets_the_same_evidence_as_a_direct_call(
    env: SimpleNamespace,  # noqa: F811
) -> None:
    root = make_repo(env.shared, "demo")
    seed_codegraph(root)
    port = _free_port()
    with live_portal(port):
        with _http(port) as http:
            _setup_project(http, root)
            _seed_commits(root)
            # The bare URL is served directly - no 307 to a trailing slash.
            bare = http.post("/mcp/demo", json=_rpc("tools/list"), headers=MCP_HEADERS)
            assert bare.status_code == 200 and not bare.is_redirect

        async def _call():
            async with httpx.AsyncClient(
                trust_env=False, timeout=30, follow_redirects=False
            ) as ac:
                async with streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp/demo", http_client=ac
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return await session.call_tool(
                            "whygraph_evidence_for",
                            {"path": "sample.py", "line_start": 1, "line_end": 3},
                        )

        result = asyncio.run(_call())

    assert result.isError is False, result.content
    via_mcp = result.structuredContent
    if via_mcp is None or set(via_mcp) == {"result"}:
        via_mcp = (via_mcp or {}).get("result") or json.loads(result.content[0].text)
    with use_project(manual_ctx(root, slug="demo")):
        direct = whygraph_evidence_for(path="sample.py", line_start=1, line_end=3)
    assert via_mcp == direct
    assert len(direct["evidence"]) == 2


def test_slow_mcp_calls_do_not_block_the_ui(
    env: SimpleNamespace,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = make_repo(env.shared, "demo")
    port = _free_port()
    release = threading.Event()
    started: list[float] = []

    def _slow_resolve(**kwargs):
        started.append(time.monotonic())
        release.wait(10)  # a blocking body, e.g. git blame or an LLM call
        raise evidence.WhyGraphError("done waiting")

    monkeypatch.setattr(evidence, "resolve_target", _slow_resolve)
    calls = mcp_server.MCP_THREAD_TOKENS + 4

    with live_portal(port):
        with _http(port) as http:
            _setup_project(http, root)

        async def _scenario():
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30
            ) as ac:
                tool_calls = [
                    asyncio.create_task(
                        ac.post(
                            "/mcp/demo",
                            json=_rpc(
                                "tools/call",
                                {
                                    "name": "whygraph_evidence_for",
                                    "arguments": {"path": "a"},
                                },
                                id=i,
                            ),
                            headers=MCP_HEADERS,
                        )
                    )
                    for i in range(calls)
                ]
                deadline = time.monotonic() + 10
                while len(started) < mcp_server.MCP_THREAD_TOKENS:
                    assert time.monotonic() < deadline, "tool calls never started"
                    await asyncio.sleep(0.02)
                await asyncio.sleep(0.2)
                # Only the limiter's worth of bodies run; the rest wait.
                assert len(started) == mcp_server.MCP_THREAD_TOKENS

                t0 = time.monotonic()
                state = await ac.get("/api/portal/state", headers=CLIENT_HEADER)
                elapsed = time.monotonic() - t0
                release.set()
                responses = await asyncio.gather(*tool_calls)
                return state, elapsed, responses

        state, elapsed, responses = asyncio.run(_scenario())

    assert state.status_code == 200
    assert elapsed < 2.0, f"GET /api/portal/state took {elapsed:.2f}s"
    assert all(r.status_code == 200 for r in responses)
    assert len(started) == calls
