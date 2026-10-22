"""Every ``whygraph_<tool>(...)`` argument named in the bundled assets must exist.

FastMCP's argument model ignores unknown keys, so an asset that tells an
agent to pass a parameter the tool lacks "succeeds" silently (the
``force_refresh`` defect). This reads each call-looking mention and checks
its keyword names against the tool's ``inputSchema``.
"""

from __future__ import annotations

import asyncio
import re
from importlib.resources import files

from whygraph.mcp.server import mcp

_CALL = re.compile(
    r"(whygraph_(?:evidence_for|rationale_brief|area_history))\(([^)]*)\)"
)
_KEY = re.compile(r"(?<![\w.])([a-z_][a-z0-9_]*)\s*=")


def _asset_texts() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    stack = [files("whygraph") / "assets"]
    while stack:
        node = stack.pop()
        for child in node.iterdir():
            if child.is_dir():
                stack.append(child)
            else:
                try:
                    out.append((str(child), child.read_text(encoding="utf-8")))
                except UnicodeDecodeError:
                    pass
    return out


def test_asset_tool_arguments_exist_in_the_tool_schemas() -> None:
    schemas = {
        t.name: set(t.inputSchema.get("properties", {}))
        for t in asyncio.run(mcp.list_tools())
    }
    bad: list[str] = []
    seen = 0
    for name, text in _asset_texts():
        for match in _CALL.finditer(text):
            seen += 1
            for key in _KEY.findall(match.group(2)):
                if key not in schemas[match.group(1)]:
                    bad.append(f"{name}: {match.group(1)}({key}=...)")
    assert seen > 0
    assert not bad, bad


def test_force_refresh_is_in_the_rationale_schema() -> None:
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    props = tools["whygraph_rationale_brief"].inputSchema["properties"]
    assert "force_refresh" in props
