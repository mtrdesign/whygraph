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
        "whygraph_area_history",
    }


def test_mcp_server_registers_resources() -> None:
    from whygraph.mcp.server import mcp

    resources = asyncio.run(mcp.list_resources())
    templates = asyncio.run(mcp.list_resource_templates())
    assert {r.name for r in resources} == {"whygraph_repo_overview"}
    assert {t.uriTemplate for t in templates} == {
        "whygraph://commit/{sha}",
        "whygraph://pr/{number}",
        "whygraph://issue/{number}",
    }


def test_mcp_server_registers_prompts() -> None:
    from whygraph.mcp.server import mcp

    prompts = asyncio.run(mcp.list_prompts())
    assert {p.name for p in prompts} == {
        "whygraph_pre_edit_brief",
        "whygraph_why_was_this_written",
        "whygraph_triage_commit",
    }
