"""Capture or check CodeGraph's MCP tool records (``codegraph_tools.json``).

The portal's ``/mcp/<slug>/code`` bridge answers ``tools/list`` from
``src/whygraph/portal/codegraph_tools.json`` without starting CodeGraph, and
refuses a tool whose live schema differs from that pin. This script talks to
``codegraph serve --mcp`` over stdio exactly as the portal's shared child is
started (no ``--path``, an empty working directory, the hosted environment)
and either writes the records or compares them.

Capture against the pinned CodeGraph from a host with Docker (the npm install
goes into a scratch directory; behind a corporate CA pass ``--cafile``)::

    npm i --prefix /tmp/cg @colbymchenry/codegraph@1.4.1
    .venv/bin/python scripts/codegraph_mcp_probe.py --version 1.4.1 \\
        --server-cmd "docker run --rm -i -v /tmp/cg:/cg -w /tmp node:22.21.1 \\
        /cg/node_modules/.bin/codegraph serve --mcp"

A ``docker run`` server command gets ``-e NAME`` for every variable of the
hosted environment that is not inherited anyway, so the container sees the
same switches as the portal's child.

Check a built image (the release gate)::

    docker run --rm -v "$PWD/scripts:/probe:ro" <image> \\
        python /probe/codegraph_mcp_probe.py --check

``--check`` fails (exit 1) when the listed tools, a property set, ``required``
or the annotations differ from the packaged file, when a tool gains
``title`` / ``outputSchema`` / ``_meta`` / ``icons`` / ``execution``, when an
advertised schema is not a valid JSON Schema 2020-12 document, or when the
server's protocol version is not one the SDK supports.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys
import tempfile
from importlib import resources
from pathlib import Path

import jsonschema
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

from whygraph.portal.codegraph_host import (
    BRIDGED_TOOLS,
    advertised_tools,
    tool_record,
)
from whygraph.services.codegraph.bootstrap import hosted_env

OUTPUT = (
    Path(__file__).resolve().parent.parent / "src/whygraph/portal/codegraph_tools.json"
)
FORBIDDEN_FIELDS = ("title", "outputSchema", "_meta", "icons", "execution")
"""Tool fields the bridge does not forward; a server that adds one needs a review."""


def _server_argv(command: str, env: dict[str, str]) -> list[str]:
    """The server argv; a ``docker run`` forwards the hosted switches by name."""
    argv = shlex.split(command)
    if argv[:2] == ["docker", "run"]:
        forwarded: list[str] = []
        for name in sorted(env):
            if name.startswith(("CODEGRAPH_", "DO_NOT_TRACK", "GIT_")):
                forwarded += ["-e", name]
        argv = argv[:2] + forwarded + argv[2:]
    return argv


async def _probe(argv: list[str], home: Path) -> dict:
    env = hosted_env(home)
    params = StdioServerParameters(
        command=argv[0], args=argv[1:], cwd=str(home), env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            listed = await session.list_tools()
    return {
        "protocol_version": str(init.protocolVersion),
        "server": init.serverInfo.model_dump(exclude_none=True),
        "instructions": init.instructions or "",
        "tools": [t.model_dump(by_alias=True, exclude_none=True) for t in listed.tools],
    }


def _records(probe: dict, version: str) -> dict:
    tools = {}
    for raw in probe["tools"]:
        if raw["name"] in BRIDGED_TOOLS:
            tools[raw["name"]] = {
                **tool_record(raw),
                "description": raw.get("description", ""),
                "inputSchema": raw.get("inputSchema", {}),
            }
    return {
        "codegraph_version": version,
        "protocol_version": probe["protocol_version"],
        "upstream_instructions": probe["instructions"],
        "tools": tools,
    }


def _check(probe: dict, pinned: dict) -> list[str]:
    problems: list[str] = []
    if probe["protocol_version"] not in SUPPORTED_PROTOCOL_VERSIONS:
        problems.append(
            f"protocol version {probe['protocol_version']} is not supported"
        )
    listed = {t["name"]: t for t in probe["tools"]}
    if sorted(n for n in listed if n in BRIDGED_TOOLS) != sorted(pinned["tools"]):
        problems.append(
            f"listed tools {sorted(listed)} differ from {sorted(pinned['tools'])}"
        )
    for name, raw in listed.items():
        if name not in pinned["tools"]:
            continue
        for field in FORBIDDEN_FIELDS:
            if field in raw:
                problems.append(f"{name} gained {field!r}")
        live, pin = tool_record(raw), pinned["tools"][name]
        for key in ("properties", "required", "annotations"):
            if live[key] != pin[key]:
                problems.append(f"{name}.{key}: {live[key]!r} != {pin[key]!r}")
    for tool in advertised_tools(pinned):
        try:
            jsonschema.Draft202012Validator.check_schema(tool.inputSchema)
        except jsonschema.SchemaError as exc:
            problems.append(f"{tool.name}: invalid advertised schema: {exc.message}")
    return problems


def main(argv: list[str] | None = None) -> int:
    """Run the probe.

    Parameters
    ----------
    argv : list[str], optional
        Command-line arguments (default ``sys.argv[1:]``).

    Returns
    -------
    int
        ``0`` on success, ``1`` when ``--check`` found a difference.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--server-cmd",
        default="codegraph serve --mcp",
        help="the MCP server command (default: %(default)s)",
    )
    parser.add_argument("--check", action="store_true", help="compare, do not write")
    parser.add_argument(
        "--json",
        type=Path,
        help="the records file (default: the packaged one for --check, "
        "src/whygraph/portal/codegraph_tools.json otherwise)",
    )
    parser.add_argument(
        "--version", default="", help="the CodeGraph version being captured"
    )
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="codegraph-home-") as tmp:
        home = Path(tmp)
        probe = asyncio.run(
            _probe(_server_argv(args.server_cmd, hosted_env(home)), home)
        )
    if args.check:
        path = args.json or resources.files("whygraph.portal") / "codegraph_tools.json"
        pinned = json.loads(path.read_text(encoding="utf-8"))
        problems = _check(probe, pinned)
        for problem in problems:
            print(f"codegraph probe: {problem}", file=sys.stderr)
        if not problems:
            print(f"codegraph probe: {len(pinned['tools'])} tools match the pin")
        return 1 if problems else 0
    out = args.json or OUTPUT
    out.write_text(
        json.dumps(_records(probe, args.version), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"codegraph probe: wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
