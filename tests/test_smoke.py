"""Smoke tests for the package and its MCP server.

Asserts the package imports and that the MCP server registers exactly the
tools this iteration ships. The tool names are part of the agent-callable
contract — renaming one is a public API break.
"""

from __future__ import annotations

import asyncio


def test_imports() -> None:
    from whygraph import cli  # noqa: F401
    from whygraph.mcp import server  # noqa: F401


def test_mcp_server_name() -> None:
    from whygraph.mcp.server import mcp

    assert mcp.name == "whygraph"


def test_mcp_server_registers_evidence_and_rationale_tools() -> None:
    from whygraph.mcp.server import mcp

    tools = asyncio.run(mcp.list_tools())
    assert {t.name for t in tools} == {
        "whygraph_evidence_for",
        "whygraph_rationale_brief",
    }
