"""Tests for the CodeGraph bridge, ``/mcp/<slug>/code`` (portal-hosted CodeGraph plan, step 3a).

The shared child is ``tests/fixtures/fake_codegraph_mcp.py`` behind an
executable ``codegraph`` wrapper (its knobs are baked into the wrapper,
because the child's environment is the hosted allowlist). End-to-end cases
run a portal app under ``TestClient``; lifecycle cases drive
:class:`~whygraph.portal.codegraph_host.CodeGraphHost` directly.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import shlex
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from sqlmodel import select

from platform_fake import FakePlatform
from test_portal_app import (  # noqa: F401 -- `env` is a fixture
    env,
    init_project,
    make_repo,
    portal_client,
    seed_codegraph,
)
from test_portal_link import (  # noqa: F401 -- fixtures
    fake,
    link,
    linked,
    mirror,
    portal,
)
from test_portal_mcp import MCP_HEADERS, _rpc, _setup_project, _sse_json
from test_portal_runner import scanner  # noqa: F401 -- fixture
from whygraph.core import usage as core_usage
from whygraph.core.context import ProjectContext, use_project
from whygraph.portal import codegraph_host
from whygraph.portal import db as portal_db
from whygraph.portal.codegraph_host import (
    BRIDGED_TOOLS,
    BUSY,
    CODEGRAPH_INSTRUCTIONS,
    NO_BINARY,
    NOT_INDEXED,
    PAUSED,
    RESTARTED,
    BridgeTarget,
    CodeGraphHost,
    Refusal,
    police_arguments,
)
from whygraph.portal.models import AgentCallDay

FAKE = Path(__file__).parent / "fixtures" / "fake_codegraph_mcp.py"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_cg(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:  # noqa: F811
    """A fake ``codegraph`` the portal starts; ``configure(**knobs)`` rewrites it."""
    bin_dir = env.tmp / "cgbin"
    bin_dir.mkdir()
    ns = SimpleNamespace(
        bin=bin_dir / "codegraph",
        pids=env.tmp / "cg.pids",
        calls=env.tmp / "cg.calls",
    )

    def configure(**knobs: Any) -> None:
        values = {
            "FAKE_CG_PID_FILE": str(ns.pids),
            "FAKE_CG_CALL_LOG": str(ns.calls),
            **{f"FAKE_CG_{k.upper()}": str(v) for k, v in knobs.items()},
        }
        lines = ["#!/bin/sh"]
        lines += [f"export {k}={shlex.quote(v)}" for k, v in values.items()]
        lines.append(
            f'exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE))} "$@"'
        )
        ns.bin.write_text("\n".join(lines) + "\n")
        ns.bin.chmod(0o755)

    def pids() -> list[int]:
        if not ns.pids.exists():
            return []
        return [int(line) for line in ns.pids.read_text().split()]

    def calls() -> list[str]:
        return ns.calls.read_text().split() if ns.calls.exists() else []

    configure()
    ns.configure, ns.pid_list, ns.call_list = configure, pids, calls
    monkeypatch.setenv("WHYGRAPH_CODEGRAPH_CMD", str(ns.bin))
    return ns


def _code(client: Any, method: str, params: dict | None = None, *, slug: str = "demo"):
    return client.post(
        f"/mcp/{slug}/code", json=_rpc(method, params), headers=MCP_HEADERS
    )


def _call(
    client: Any, name: str, arguments: dict | None = None, *, slug: str = "demo"
) -> dict:
    """``tools/call`` on the bridge; the JSON-RPC message (``result`` or ``error``)."""
    response = _code(
        client, "tools/call", {"name": name, "arguments": arguments or {}}, slug=slug
    )
    assert response.status_code == 200, response.text
    return _sse_json(response.text)


def _text(message: dict) -> str:
    return "\n".join(c["text"] for c in message["result"]["content"])


def _forwarded(message: dict) -> dict:
    """What the fake saw for a forwarded call."""
    assert message["result"]["isError"] is False, message
    return json.loads(message["result"]["content"][0]["text"])


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _project(env: SimpleNamespace, client: Any, name: str = "demo") -> Path:  # noqa: F811
    root = make_repo(env.shared, name)
    seed_codegraph(root)
    _setup_project(client, root) if name == "demo" else _add_another(client, root)
    return root


def _add_another(client: Any, root: Path) -> None:
    added = client.post("/api/projects", json={"source": "local", "path": str(root)})
    assert added.status_code == 201, added.text
    init_project(client, added.json()["project"]["slug"])


def _kinds() -> dict[str, int]:
    totals: dict[str, int] = {}
    with portal_db.get_session() as session:
        for row in session.exec(select(AgentCallDay)).all():
            totals[row.kind] = totals.get(row.kind, 0) + row.calls
    return totals


def _tails(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "exited (pid" in r.getMessage()]


def _target(root: Path, slug: str = "demo", project_id: int = 1) -> BridgeTarget:
    return BridgeTarget(project_id, slug, root, os.path.realpath(root))


def _indexed_repo(tmp: Path, name: str = "repo") -> Path:
    root = tmp / name
    (root / ".codegraph").mkdir(parents=True)
    (root / ".codegraph" / "codegraph.db").write_text("")
    return Path(os.path.realpath(root))


def _host(cg: SimpleNamespace, data: Path, **kw: Any) -> CodeGraphHost:
    return CodeGraphHost(data, argv=[str(cg.bin), "serve", "--mcp"], **kw)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# Forwarding and the gate
# ---------------------------------------------------------------------------


def test_list_tools_and_instructions_come_from_the_packaged_schema(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No binary: tools/list and initialize must not need the child.
    monkeypatch.setenv("WHYGRAPH_CODEGRAPH_CMD", str(env.tmp / "absent"))
    with portal_client() as client:
        _project(env, client)
        init = _sse_json(
            _code(
                client,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            ).text
        )["result"]
        listed = _sse_json(_code(client, "tools/list").text)["result"]["tools"]
    assert fake_cg.pid_list() == []
    assert init["serverInfo"]["name"] == "codegraph"
    assert init["capabilities"].get("resources") is None
    assert init["capabilities"].get("prompts") is None
    instructions = init["instructions"]
    assert instructions == CODEGRAPH_INSTRUCTIONS and len(instructions) <= 2500
    for name in BRIDGED_TOOLS:
        assert f"`{name}`" in instructions
    for phrase in ("single tool", "watcher", "staleness banner"):
        assert phrase not in instructions.lower()
    assert [t["name"] for t in listed] == list(BRIDGED_TOOLS)
    for tool in listed:
        schema = tool["inputSchema"]
        assert schema["additionalProperties"] is False
        assert "projectPath" not in schema["properties"]
        assert "projectPath" not in schema.get("required", [])
        assert tool["annotations"] == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        assert not {"title", "outputSchema", "icons", "_meta"} & set(tool)
    by_name = {t["name"]: t for t in listed}
    assert by_name["codegraph_search"]["inputSchema"]["required"] == ["query"]
    assert "required" not in by_name["codegraph_node"]["inputSchema"]


def test_call_tool_is_forwarded_with_project_path_set_to_the_root(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
) -> None:
    with portal_client() as client:
        root = _project(env, client)
        seen = _forwarded(_call(client, "codegraph_search", {"query": "fn"}))
    assert seen["tool"] == "codegraph_search"
    assert seen["arguments"] == {"query": "fn", "projectPath": os.path.realpath(root)}
    assert os.path.realpath(seen["cwd"]) == os.path.realpath(
        env.data / "codegraph-home"
    )
    assert seen["client_roots"] is False  # test_initialize_sends_no_roots_capability


def test_unknown_slug_and_setup_gate(env: SimpleNamespace) -> None:  # noqa: F811
    with portal_client() as client:
        before = _code(client, "tools/list")
        assert before.status_code == 409
        assert before.json() == {"error": "setup required", "code": "setup_required"}
        client.post("/api/portal/setup", json={"display_name": "Tess"})
        assert _code(client, "tools/list", slug="nope").status_code == 404


def test_before_the_first_scan_calls_answer_success_shaped_text_and_no_forward(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
) -> None:
    # An ancestor's index must not be found by CodeGraph's walk-up.
    (env.shared / ".codegraph").mkdir()
    (env.shared / ".codegraph" / "codegraph.db").write_text("")
    root = make_repo(env.shared, "demo")
    with portal_client() as client:
        _setup_project(client, root)
        message = _call(client, "codegraph_search", {"query": "fn"})
    assert message["result"]["isError"] is False
    assert _text(message) == NOT_INDEXED
    assert fake_cg.call_list() == []


def test_no_codegraph_binary_is_a_tool_error(env: SimpleNamespace) -> None:  # noqa: F811
    with portal_client() as client:
        _project(env, client)
        assert client.app.state.portal.codegraph_host.state == "off"
        assert client.app.state.portal.codegraph_host.available is False
        message = _call(client, "codegraph_search", {"query": "fn"})
    assert message["result"]["isError"] is True
    assert _text(message) == NO_BINARY


def test_a_symlinked_index_swapped_after_start_is_409_unsafe_path(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
) -> None:
    elsewhere = env.tmp / "elsewhere"
    elsewhere.mkdir()
    with portal_client() as client:
        root = _project(env, client)
        assert _code(client, "tools/list").status_code == 200
        os.rename(root / ".codegraph", elsewhere / ".codegraph")
        (root / ".codegraph").symlink_to(elsewhere / ".codegraph")
        response = _code(client, "tools/list")
    assert response.status_code == 409
    assert response.json()["code"] == "unsafe_path"


def test_resources_and_prompts_list_answer_method_not_found(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
) -> None:
    with portal_client() as client:
        _project(env, client)
        for method in ("resources/list", "prompts/list"):
            message = _sse_json(_code(client, method).text)
            assert message["error"]["code"] == -32601, method


def test_body_over_1_mib_is_413(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
) -> None:
    big = _rpc(
        "tools/call",
        {"name": "codegraph_search", "arguments": {"query": "x" * (2 << 20)}},
    )
    with portal_client() as client:
        _project(env, client)
        for path in ("/mcp/demo", "/mcp/demo/code"):
            response = client.post(path, json=big, headers=MCP_HEADERS)
            assert response.status_code == 413, (path, response.text)
            # A body just under the cap is read and served.
            ok = client.post(path, json=_rpc("tools/list"), headers=MCP_HEADERS)
            assert ok.status_code == 200, path
    assert fake_cg.call_list() == []


def test_a_streamed_body_over_1_mib_is_413(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
) -> None:
    def chunks():
        yield b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"x": "'
        for _ in range(3):
            yield b"x" * (512 * 1024)
        yield b'"}}'

    with portal_client() as client:
        _project(env, client)
        response = client.post("/mcp/demo/code", content=chunks(), headers=MCP_HEADERS)
    assert response.status_code == 413


def test_a_session_id_header_is_answered_as_if_absent(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
) -> None:
    with portal_client() as client:
        _project(env, client)
        response = client.post(
            "/mcp/demo/code",
            json=_rpc("tools/list"),
            headers={**MCP_HEADERS, "mcp-session-id": "someone-elses"},
        )
    assert response.status_code == 200
    assert "mcp-session-id" not in response.headers
    assert len(_sse_json(response.text)["result"]["tools"]) == 4


# ---------------------------------------------------------------------------
# Policing
# ---------------------------------------------------------------------------


def test_project_path_is_refused_unless_it_is_the_root(tmp_path: Path) -> None:
    root = _indexed_repo(tmp_path)
    other = _indexed_repo(tmp_path, "other")
    (root / "sub").mkdir()
    link = tmp_path / "link-to-root"
    link.symlink_to(root)
    target = _target(root)
    for value in (
        str(other),
        "/",
        str(root / "sub"),
        str(root) + "/",
        f"file://{root}",
        str(root).upper(),
        "relative/path",
        str(root / "sub" / ".."),
        7,
    ):
        with pytest.raises(Refusal) as refused:
            police_arguments(
                "codegraph_search", {"query": "q", "projectPath": value}, target
            )
        assert refused.value.reason == "project_path", value
        assert refused.value.text.startswith("[WhyGraph] projectPath")
        assert "serves only demo" in refused.value.text
    for value in (str(root), str(link)):
        args = police_arguments(
            "codegraph_search", {"query": "q", "projectPath": value}, target
        )
        assert args == {"query": "q"}


def test_node_file_outside_the_root_is_refused_and_never_forwarded(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = env.tmp / "secret.txt"
    secret.write_text("SECRET_VALUE")
    caplog.set_level(logging.INFO, logger="whygraph.portal.codegraph")
    with portal_client() as client:
        root = _project(env, client)
        (root / "lnk").symlink_to(env.tmp)
        for file in (
            "/data/postgres.password",
            str(secret),
            "../x",
            "lnk/secret.txt",
            "a\x00b",
            "x" * 1025,
        ):
            message = _call(client, "codegraph_node", {"file": file})
            assert message["result"]["isError"] is True, file
            assert _text(message).startswith("[WhyGraph] file "), file
            assert "SECRET_VALUE" not in _text(message)
        # Inside the root - relative, absolute, a bare basename - is forwarded.
        for file in ("sample.py", str(root / "sample.py")):
            assert _forwarded(_call(client, "codegraph_node", {"file": file}))
    assert fake_cg.call_list() == ["codegraph_node", "codegraph_node"]
    # test_refusal_logs_carry_slug_and_reason_not_the_argument
    refusals = [r.getMessage() for r in caplog.records if "refused" in r.getMessage()]
    assert refusals and all("demo" in m and "(file)" in m for m in refusals)
    assert not any("postgres.password" in r.getMessage() for r in caplog.records)
    assert not any(str(secret) in r.getMessage() for r in caplog.records)


def test_impact_file_outside_the_root_is_refused(tmp_path: Path) -> None:
    root = _indexed_repo(tmp_path)
    target = _target(root)
    for file in ("/etc/passwd", "../outside.py", str(tmp_path)):
        with pytest.raises(Refusal) as refused:
            police_arguments("codegraph_impact", {"symbol": "f", "file": file}, target)
        assert refused.value.reason == "file"
    assert police_arguments("codegraph_impact", {"symbol": "f", "file": "a.py"}, target)


def test_impact_depth_is_capped_at_3(tmp_path: Path) -> None:
    target = _target(_indexed_repo(tmp_path))
    assert (
        police_arguments("codegraph_impact", {"symbol": "f", "depth": 3}, target)[
            "depth"
        ]
        == 3
    )
    for depth in (4, 0, 2.5):
        with pytest.raises(Refusal) as refused:
            police_arguments(
                "codegraph_impact", {"symbol": "f", "depth": depth}, target
            )
        assert refused.value.text.startswith("[WhyGraph] depth ")


def test_argument_caps(tmp_path: Path) -> None:
    target = _target(_indexed_repo(tmp_path))
    ok = police_arguments("codegraph_node", {"file": "a.py", "limit": 300}, target)
    assert ok["limit"] == 300
    assert (
        police_arguments("codegraph_search", {"query": "q", "limit": 10.0}, target)[
            "limit"
        ]
        == 10
    )
    assert isinstance(
        police_arguments("codegraph_search", {"query": "q", "limit": 10.0}, target)[
            "limit"
        ],
        int,
    )
    refused = [
        ("codegraph_node", {"file": "a.py", "limit": 2001}),
        ("codegraph_node", {"symbol": "s", "line": 0}),
        ("codegraph_node", {"file": "a.py", "offset": 0}),
        ("codegraph_search", {"query": "q", "limit": 101}),
        ("codegraph_search", {"query": "q", "limit": 10.5}),
        ("codegraph_search", {"query": "q", "limit": 10**400}),
        ("codegraph_search", {"query": "q", "limit": float("inf")}),
        ("codegraph_search", {"query": "x" * 2001}),
        ("codegraph_explore", {"query": "q", "maxFiles": 21}),
        ("codegraph_node", {"symbol": "s" * 513}),
        ("codegraph_search", {"query": "q", "kind": "file"}),  # outside the enum
        ("codegraph_search", {}),  # required
        ("codegraph_node", {"includeCode": "yes"}),  # a type
    ]
    for name, args in refused:
        with pytest.raises(Refusal) as caught:
            police_arguments(name, args, target)
        assert caught.value.text.startswith("[WhyGraph] "), (name, args)


def test_additional_properties_are_refused_by_the_schema_pass(tmp_path: Path) -> None:
    target = _target(_indexed_repo(tmp_path))
    with pytest.raises(Refusal) as refused:
        police_arguments("codegraph_search", {"query": "q", "rootUri": "/"}, target)
    assert refused.value.reason == "schema"
    assert "rootUri" in refused.value.text


def test_unknown_schema_property_withholds_a_listed_tool(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_cg.configure(extra_path_arg=1)
    with portal_client() as client:
        _project(env, client)
        searched = _call(
            client, "codegraph_search", {"query": "fn"}
        )  # starts the child
        assert _forwarded(searched)
        names = [
            t["name"]
            for t in _sse_json(_code(client, "tools/list").text)["result"]["tools"]
        ]
        node = _call(client, "codegraph_node", {"symbol": "fn"})
        assert client.app.state.portal.codegraph_host.withheld == frozenset(
            {"codegraph_node"}
        )
    assert "codegraph_node" not in names and len(names) == 3
    assert node["result"]["isError"] is True
    assert _text(node).startswith("[WhyGraph] codegraph_node is withheld")
    assert fake_cg.call_list() == ["codegraph_search"]
    assert any("withheld codegraph_node" in r.getMessage() for r in caplog.records)


def test_impact_is_served_when_the_child_hides_it_on_a_small_repo(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
) -> None:
    fake_cg.configure(list="codegraph_explore,codegraph_node,codegraph_search")
    with portal_client() as client:
        _project(env, client)
        seen = _forwarded(_call(client, "codegraph_impact", {"symbol": "fn"}))
        names = [
            t["name"]
            for t in _sse_json(_code(client, "tools/list").text)["result"]["tools"]
        ]
    assert seen["tool"] == "codegraph_impact"
    assert "codegraph_impact" in names


def test_an_unlisted_tool_name_is_a_json_rpc_error_not_forwarded_not_counted(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
) -> None:
    with portal_client() as client:
        _project(env, client)
        callers = _call(client, "codegraph_callers", {"symbol": "fn"})
        files = _call(client, "codegraph_files", {})
        long = _call(client, "x" * 100, {})
        assert _forwarded(_call(client, "codegraph_search", {"query": "fn"}))
        client.app.state.portal.agent_calls.flush()
    assert callers["error"]["code"] == -32602
    assert "codegraph_node" in callers["error"]["message"]
    assert "Called by" in callers["error"]["message"]
    assert files["error"] == {
        "code": -32602,
        "message": "Unknown tool: codegraph_files",
    }
    assert long["error"]["code"] == -32602
    assert fake_cg.call_list() == ["codegraph_search"]
    assert _kinds() == {"codegraph_search": 1}


def test_calls_count_under_bridged_tool_names(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
) -> None:
    with portal_client() as client:
        _project(env, client)
        _code(client, "tools/list")
        client.app.state.portal.agent_calls.flush()
        assert _kinds() == {}  # protocol chatter is not agent activity
        _call(client, "codegraph_search", {"query": "fn"})
        _call(client, "codegraph_explore", {"query": "fn"})
        # test_a_bridged_call_refused_for_its_arguments_is_counted
        refused = _call(client, "codegraph_impact", {"symbol": "fn", "depth": 9})
        assert refused["result"]["isError"] is True
        client.app.state.portal.agent_calls.flush()
    assert _kinds() == {
        "codegraph_search": 1,
        "codegraph_explore": 1,
        "codegraph_impact": 1,
    }


# ---------------------------------------------------------------------------
# Environment and the log
# ---------------------------------------------------------------------------


def test_child_env_has_the_switches_and_no_credentials(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "b" * 36)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("CODEGRAPH_MCP_DEBUG", "1")
    monkeypatch.setenv("CODEGRAPH_CATCHUP_GATE_TIMEOUT_MS", "5")
    with portal_client() as client:
        _project(env, client)
        seen = _forwarded(_call(client, "codegraph_search", {"query": "fn"}))["env"]
    assert seen["CODEGRAPH_MCP_TOOLS"] == "explore,node,search,impact"
    assert seen["CODEGRAPH_NO_WATCH"] == "1"
    assert seen["CODEGRAPH_NO_DAEMON"] == "1"
    assert seen["DO_NOT_TRACK"] == "1" and seen["CODEGRAPH_TELEMETRY"] == "0"
    assert seen["CODEGRAPH_NO_UPDATE_CHECK"] == "1"
    assert seen["CODEGRAPH_NO_DOWNLOAD"] == "1"
    assert seen["GIT_CONFIG_GLOBAL"] == os.devnull
    assert seen["GIT_CONFIG_NOSYSTEM"] == "1"
    assert seen["GIT_OPTIONAL_LOCKS"] == "0"
    assert os.path.realpath(seen["HOME"]) == os.path.realpath(
        env.data / "codegraph-home"
    )
    for name in (
        "GH_TOKEN",
        "OPENAI_API_KEY",
        "WHYGRAPH_DATABASE_URL",
        "WHYGRAPH_DATA",
        "CODEGRAPH_MCP_DEBUG",
        "CODEGRAPH_CATCHUP_GATE_TIMEOUT_MS",
    ):
        assert name not in seen, name


def test_stderr_goes_to_the_shared_log_with_a_start_banner_and_the_crash_tail_is_redacted(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_cg.configure(stderr_lines=4, exit_on="codegraph_node")
    logs = env.data / "logs"
    logs.mkdir(parents=True)
    (logs / "codegraph.log").write_text("old\n" * (300 * 1024))  # > 1 MiB
    with portal_client() as client:
        _project(env, client)
        crashed = _call(client, "codegraph_node", {"symbol": "fn"})
        deadline = time.monotonic() + 10
        while not _tails(caplog) and time.monotonic() < deadline:
            time.sleep(0.05)
    assert crashed["result"]["isError"] is True
    assert _text(crashed) == RESTARTED
    assert (logs / "codegraph.log.1").read_text().startswith("old\n")
    text = (logs / "codegraph.log").read_text()
    assert text.startswith("--- start ")
    assert "reason prewarm ---" in text
    assert "noise line 2" in text
    assert stat.S_IMODE((logs / "codegraph.log").stat().st_mode) == 0o600
    assert stat.S_IMODE((env.data / "codegraph-home").stat().st_mode) == 0o700
    tails = _tails(caplog)
    assert tails, [r.getMessage() for r in caplog.records]
    assert "noise line 2" in tails[0]
    assert "[mcp-debug]" not in tails[0]
    assert "ghp_***" in tails[0] and "a" * 36 not in tails[0]


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def test_host_survives_the_request_that_started_it(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
) -> None:
    with portal_client() as client:
        first = _project(env, client)
        second = _project(env, client, "other")
        host = client.app.state.portal.codegraph_host
        a = _forwarded(_call(client, "codegraph_search", {"query": "fn"}))
        pid = host.pid
        b = _forwarded(
            _call(client, "codegraph_explore", {"query": "fn"}, slug="other")
        )
        assert host.pid == pid and host.state == "live"
    # test_two_projects_in_parallel_each_get_their_own_root
    assert a["arguments"]["projectPath"] == os.path.realpath(first)
    assert b["arguments"]["projectPath"] == os.path.realpath(second)
    assert fake_cg.pid_list() == [pid]
    # Lifespan shutdown reaps every child.
    assert not any(_alive(p) for p in fake_cg.pid_list())


def test_two_projects_in_parallel_each_get_their_own_root(
    fake_cg: SimpleNamespace, tmp_path: Path
) -> None:
    fake_cg.configure(sleep=0.3)
    roots = [_indexed_repo(tmp_path, f"p{i}") for i in range(4)]

    async def main() -> list[dict]:
        host = _host(fake_cg, tmp_path / "data")
        await host.start()
        results: list[Any] = [None] * len(roots)

        async def one(i: int) -> None:
            target = _target(roots[i], slug=f"p{i}", project_id=i + 1)
            results[i] = await host.call_tool(
                target, "codegraph_search", {"query": "q"}
            )

        try:
            async with anyio.create_task_group() as tg:
                for i in range(len(roots)):
                    tg.start_soon(one, i)
        finally:
            await host.shutdown()
        return [json.loads(r.content[0].text) for r in results]

    seen = anyio.run(main)
    assert [s["arguments"]["projectPath"] for s in seen] == [str(r) for r in roots]
    assert len(fake_cg.pid_list()) == 1


def test_owner_task_does_not_inherit_request_context(
    fake_cg: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe: contextvars.ContextVar[str | None] = contextvars.ContextVar(
        "probe", default=None
    )
    seen: list[tuple] = []
    real = codegraph_host.hosted_env

    def spy(home: Path, base: Any = None) -> dict:
        from whygraph.core.context import _current  # noqa: PLC0415

        seen.append(
            (
                probe.get(),
                codegraph_host._bridge_target.get(),
                core_usage._sink.get(),
                _current.get(),
            )
        )
        return real(home, base)

    monkeypatch.setattr(codegraph_host, "hosted_env", spy)
    root = _indexed_repo(tmp_path)

    async def main() -> tuple[str, bool]:
        # No binary at start: nothing is pre-warmed outside the request.
        host = CodeGraphHost(tmp_path / "data", argv=[str(tmp_path / "absent")])
        await host.start()
        host.argv = [str(fake_cg.bin), "serve", "--mcp"]
        ctx = ProjectContext(slug="demo", root=root, config=None)  # type: ignore[arg-type]
        try:
            probe.set("request")
            with (
                use_project(ctx),
                core_usage.use_usage_sink(object()),  # type: ignore[arg-type]
                codegraph_host._bind_target(_target(root)),
            ):
                result = await host.call_tool(
                    _target(root), "codegraph_search", {"query": "q"}
                )
            alive = host.state == "live"
        finally:
            await host.shutdown()
        return result.content[0].text, alive

    text, alive = anyio.run(main)
    assert json.loads(text)["tool"] == "codegraph_search"
    assert alive
    assert seen == [(None, None, None, None)]


# ---------------------------------------------------------------------------
# Lifecycle and limits
# ---------------------------------------------------------------------------


def test_idle_stop_and_restart_on_the_next_call(
    fake_cg: SimpleNamespace, tmp_path: Path
) -> None:
    root = _indexed_repo(tmp_path)

    async def main() -> tuple[list[str], int | None]:
        host = _host(fake_cg, tmp_path / "data", idle_seconds=0.3, reap_every=0.05)
        await host.start()
        states = []
        try:
            await host.call_tool(_target(root), "codegraph_search", {"query": "q"})
            states.append(host.state)
            with anyio.fail_after(5):
                while host.state != "off":
                    await anyio.sleep(0.05)
            states.append(host.state)
            await host.call_tool(_target(root), "codegraph_search", {"query": "q"})
            states.append(host.state)
            return states, host.pid
        finally:
            await host.shutdown()

    states, pid = anyio.run(main)
    assert states == ["live", "off", "live"]
    pids = fake_cg.pid_list()
    assert len(pids) == 2 and pids[-1] == pid
    assert not any(_alive(p) for p in pids)


def test_a_crash_restarts_on_the_next_call_and_a_second_crash_pauses(
    fake_cg: SimpleNamespace, tmp_path: Path
) -> None:
    fake_cg.configure(exit_on="codegraph_node")
    root = _indexed_repo(tmp_path)
    clock = _Clock()

    async def main() -> list[str]:
        host = _host(fake_cg, tmp_path / "data", clock=clock)
        await host.start()
        out = []

        async def call(name: str, args: dict) -> str:
            result = await host.call_tool(_target(root), name, args)
            return result.content[0].text if result.isError else "ok"

        try:
            out.append(await call("codegraph_node", {"symbol": "s"}))  # crash
            out.append(await call("codegraph_search", {"query": "q"}))  # restarted
            out.append(await call("codegraph_node", {"symbol": "s"}))  # crash again
            clock.now += 10
            out.append(await call("codegraph_search", {"query": "q"}))  # paused
            out.append(host.state)
            clock.now += 25
            out.append(await call("codegraph_search", {"query": "q"}))  # window passed
        finally:
            await host.shutdown()
        return out

    out = anyio.run(main)
    assert out[0] == RESTARTED and out[1] == "ok" and out[2] == RESTARTED
    assert out[3] == PAUSED.format(seconds=20)
    assert out[4] == "restart_window"
    assert out[5] == "ok"
    pids = fake_cg.pid_list()
    assert len(pids) == 3
    assert not any(_alive(p) for p in pids)


def test_a_failed_start_counts_as_a_crash(
    fake_cg: SimpleNamespace, tmp_path: Path
) -> None:
    fake_cg.configure(protocol="1999-01-01")
    root = _indexed_repo(tmp_path)

    async def main() -> tuple[str, str]:
        host = _host(fake_cg, tmp_path / "data")
        await host.start()
        try:
            first = await host.call_tool(
                _target(root), "codegraph_search", {"query": "q"}
            )
            second = await host.call_tool(
                _target(root), "codegraph_search", {"query": "q"}
            )
        finally:
            await host.shutdown()
        return first.content[0].text, second.content[0].text

    first, second = anyio.run(main)
    # The pre-warm start failed; the first call restarts it, which fails too.
    assert first == RESTARTED
    assert second.startswith("[WhyGraph] CodeGraph is paused on this portal")


def test_the_forward_limiter_answers_busy(
    fake_cg: SimpleNamespace, tmp_path: Path
) -> None:
    fake_cg.configure(sleep=1.5)
    roots = [_indexed_repo(tmp_path, f"p{i}") for i in range(9)]

    async def main() -> list[str]:
        host = _host(fake_cg, tmp_path / "data", forward_wait=0.3)
        await host.start()
        texts: list[str] = []

        async def one(i: int) -> None:
            target = _target(roots[i], project_id=i + 1)
            result = await host.call_tool(target, "codegraph_search", {"query": "q"})
            texts.append(result.content[0].text if result.isError else "ok")

        try:
            async with anyio.create_task_group() as tg:
                for i in range(9):
                    tg.start_soon(one, i)
        finally:
            await host.shutdown()
        return texts

    texts = anyio.run(main)
    assert sorted(texts) == sorted(["ok"] * 8 + [BUSY])


def test_the_project_slot_is_taken_first(
    fake_cg: SimpleNamespace, tmp_path: Path
) -> None:
    fake_cg.configure(sleep=1.0)
    root = _indexed_repo(tmp_path)

    async def main() -> list[str]:
        host = _host(fake_cg, tmp_path / "data", forward_wait=5)
        await host.start()
        texts: list[str] = []

        async def one() -> None:
            result = await host.call_tool(
                _target(root), "codegraph_search", {"query": "q"}
            )
            texts.append(result.content[0].text if result.isError else "ok")

        try:
            async with anyio.create_task_group() as tg:
                for _ in range(17):
                    tg.start_soon(one)
                    await anyio.sleep(0)
        finally:
            await host.shutdown()
        return texts

    texts = anyio.run(main)
    assert texts[0] == BUSY  # the 17th, at once, before any forward finished
    assert sorted(texts) == sorted(["ok"] * 16 + [BUSY])


def test_two_timeouts_restart_the_child(
    fake_cg: SimpleNamespace, tmp_path: Path
) -> None:
    fake_cg.configure(sleep=2)
    root = _indexed_repo(tmp_path)

    async def main() -> list[str]:
        host = _host(fake_cg, tmp_path / "data", call_timeout=0.4)
        await host.start()
        out = []
        try:
            for _ in range(2):
                result = await host.call_tool(
                    _target(root), "codegraph_search", {"query": "q"}
                )
                out.append(result.content[0].text)
            with anyio.fail_after(10):
                while host.state != "off":
                    await anyio.sleep(0.05)
            out.append(host.state)
        finally:
            await host.shutdown()
        return out

    out = anyio.run(main)
    assert out[0] == out[1]
    assert out[0] == (
        "[WhyGraph] The CodeGraph call took longer than 0.4 s and was cancelled. "
        "Retry with a narrower query."
    )
    assert out[2] == "off"


def test_a_result_over_256_kib_gets_a_second_item(
    fake_cg: SimpleNamespace, tmp_path: Path
) -> None:
    fake_cg.configure(big=300 * 1024)
    root = _indexed_repo(tmp_path)

    async def main() -> Any:
        host = _host(fake_cg, tmp_path / "data")
        await host.start()
        try:
            return await host.call_tool(
                _target(root), "codegraph_search", {"query": "q"}
            )
        finally:
            await host.shutdown()

    result = anyio.run(main)
    assert result.isError is False
    assert len(result.content) == 2
    assert len(result.content[0].text) == 256 * 1024
    assert result.content[1].text == "[WhyGraph] Result truncated at 256 KiB."


def test_removal_retires_the_project_and_keeps_the_child(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
) -> None:
    with portal_client() as client:
        _project(env, client)
        _project(env, client, "other")
        host = client.app.state.portal.codegraph_host
        _forwarded(_call(client, "codegraph_search", {"query": "fn"}))
        pid = host.pid
        project_id = next(iter(host._slots))
        response = client.request("DELETE", "/api/projects/demo", json={})
        assert response.status_code == 200, response.text
        assert project_id in host._retired and project_id not in host._slots
        assert host.pid == pid
        _forwarded(_call(client, "codegraph_search", {"query": "fn"}, slug="other"))
        assert host.pid == pid


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def test_a_linked_project_is_served_and_makes_no_platform_request(
    fake_cg: SimpleNamespace,
    portal: Any,  # noqa: F811
    fake: FakePlatform,  # noqa: F811
    env: SimpleNamespace,  # noqa: F811
) -> None:
    root = mirror(env, "demo-checkout")
    assert link(portal, linked(portal, fake), root).status_code == 201
    init_project(portal, "demo")
    before = len(fake.requests)
    seen = _forwarded(_call(portal, "codegraph_search", {"query": "fn"}))
    assert seen["arguments"]["projectPath"] == os.path.realpath(root)
    assert len(fake.requests) == before
