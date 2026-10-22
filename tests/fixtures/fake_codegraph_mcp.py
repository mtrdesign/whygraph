"""A stand-in for ``codegraph``, for the CodeGraph bridge tests.

Run as ``<python> fake_codegraph_mcp.py serve --mcp`` (the tests put an
executable ``codegraph`` wrapper on ``PATH`` or point
``WHYGRAPH_CODEGRAPH_CMD`` at it). ``serve --mcp`` is a low-level MCP server
named ``codegraph`` over stdio that lists the real 1.4.1 records from
``src/whygraph/portal/codegraph_tools.json`` (``projectPath`` required, as a
shared child without a default project lists it) and answers every call
with ``json.dumps({"tool", "arguments", "cwd", "env"})``; ``env`` holds only
the switches and the credentials a test asserts are absent.

Knobs (environment):

``FAKE_CG_LIST``          comma-separated tool names to list (default all four)
``FAKE_CG_PID_FILE``      append this process's pid, one per line
``FAKE_CG_EXIT_ON``       exit 3 when this tool is called
``FAKE_CG_EXTRA_PATH_ARG`` ``1``: list ``codegraph_node`` with an extra property
``FAKE_CG_SLEEP``         seconds to sleep before answering a call
``FAKE_CG_STDERR_LINES``  write N noise lines to stderr at start (one
                          ``[mcp-debug]`` line and one token-shaped string)
``FAKE_CG_PROTOCOL``      the protocol version to answer ``initialize`` with
``FAKE_CG_BIG``           answer with this many bytes of text
``FAKE_CG_CALL_LOG``      append each called tool's name, one per line

``sync`` / ``index`` / ``init`` (the bridge's freshness sync; it must never
run the last two) append one JSON line per event to ``FAKE_CG_SYNC_LOG``:
``{"event": "start", "argv", "cwd", "pid", "pgid", "no_watch", "at"}``, then
``{"event": "end", "at"}``. Knobs:

``FAKE_CG_SYNC_SLEEP``    seconds a ``sync`` takes (default 0)
``FAKE_CG_SYNC_EXIT``     the exit code of a ``sync`` (default 0)
``FAKE_CG_SYNC_STDERR``   a line written to stderr by a ``sync``

Each answer also says whether the client advertised the ``roots``
capability (``client_roots``).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

RECORDS = (
    Path(__file__).resolve().parents[2] / "src/whygraph/portal/codegraph_tools.json"
)
ENV_KEYS = (
    "HOME",
    "DO_NOT_TRACK",
    "CODEGRAPH_TELEMETRY",
    "CODEGRAPH_NO_UPDATE_CHECK",
    "CODEGRAPH_NO_DOWNLOAD",
    "CODEGRAPH_NO_DAEMON",
    "CODEGRAPH_NO_WATCH",
    "CODEGRAPH_MCP_TOOLS",
    "CODEGRAPH_CATCHUP_GATE_TIMEOUT_MS",
    "CODEGRAPH_MCP_DEBUG",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_TERMINAL_PROMPT",
    "GIT_OPTIONAL_LOCKS",
    "GH_TOKEN",
    "OPENAI_API_KEY",
    "WHYGRAPH_DATABASE_URL",
    "WHYGRAPH_DATA",
)


def _tools() -> list[types.Tool]:
    records = json.loads(RECORDS.read_text())["tools"]
    names = os.environ.get("FAKE_CG_LIST")
    wanted = names.split(",") if names else list(records)
    tools = []
    for name in wanted:
        record = records[name]
        schema = json.loads(json.dumps(record["inputSchema"]))
        if name == "codegraph_node" and os.environ.get("FAKE_CG_EXTRA_PATH_ARG"):
            schema["properties"]["rootPath"] = {"type": "string"}
        tools.append(
            types.Tool(
                name=name,
                description=record["description"],
                inputSchema=schema,
                annotations=types.ToolAnnotations(**record["annotations"]),
            )
        )
    return tools


def serve() -> None:
    if os.environ.get("FAKE_CG_PID_FILE"):
        with open(os.environ["FAKE_CG_PID_FILE"], "a") as fh:
            fh.write(f"{os.getpid()}\n")
    for i in range(int(os.environ.get("FAKE_CG_STDERR_LINES") or 0)):
        if i == 0:
            print("[mcp-debug] a debug line", file=sys.stderr)
        elif i == 1:
            print("token ghp_" + "a" * 36, file=sys.stderr)
        else:
            print(f"noise line {i}", file=sys.stderr)
    sys.stderr.flush()
    protocol = os.environ.get("FAKE_CG_PROTOCOL")
    if protocol:
        import mcp.server.session as server_session

        server_session.SUPPORTED_PROTOCOL_VERSIONS = [protocol]
        types.LATEST_PROTOCOL_VERSION = protocol

    server: Server = Server("codegraph", instructions="upstream instructions")

    @server.list_tools()
    async def _list() -> list[types.Tool]:
        return _tools()

    @server.call_tool(validate_input=False)
    async def _call(name: str, arguments: dict) -> list[types.TextContent]:
        if os.environ.get("FAKE_CG_CALL_LOG"):
            with open(os.environ["FAKE_CG_CALL_LOG"], "a") as fh:
                fh.write(f"{name}\n")
        if name == os.environ.get("FAKE_CG_EXIT_ON"):
            os._exit(3)
        delay = float(os.environ.get("FAKE_CG_SLEEP") or 0)
        if delay:
            await anyio.sleep(delay)
        big = int(os.environ.get("FAKE_CG_BIG") or 0)
        if big:
            return [types.TextContent(type="text", text="x" * big)]
        params = server.request_context.session.client_params
        payload = {
            "client_roots": bool(params and params.capabilities.roots),
            "tool": name,
            "arguments": arguments,
            "cwd": os.getcwd(),
            "env": {k: os.environ[k] for k in ENV_KEYS if k in os.environ},
            "at": time.time(),
        }
        return [types.TextContent(type="text", text=json.dumps(payload))]

    async def _run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(_run)


def _sync_log(event: dict) -> None:
    path = os.environ.get("FAKE_CG_SYNC_LOG")
    if path:
        with open(path, "a") as fh:
            fh.write(json.dumps({**event, "at": time.time()}) + "\n")


def writer(argv: list[str]) -> int:
    _sync_log(
        {
            "event": "start",
            "argv": argv,
            "cwd": os.getcwd(),
            "pid": os.getpid(),
            "pgid": os.getpgid(0),
            "no_watch": os.environ.get("CODEGRAPH_NO_WATCH"),
        }
    )
    if argv[0] != "sync":
        _sync_log({"event": "end"})
        return 0
    if os.environ.get("FAKE_CG_SYNC_STDERR"):
        print(os.environ["FAKE_CG_SYNC_STDERR"], file=sys.stderr)
    time.sleep(float(os.environ.get("FAKE_CG_SYNC_SLEEP") or 0))
    _sync_log({"event": "end"})
    return int(os.environ.get("FAKE_CG_SYNC_EXIT") or 0)


def main(argv: list[str]) -> int:
    if argv[:2] == ["serve", "--mcp"]:
        serve()
        return 0
    if argv[:1] in (["sync"], ["index"], ["init"]):
        return writer(argv)
    print(f"fake codegraph: unsupported {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
