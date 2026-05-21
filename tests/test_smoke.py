import pytest

_MCP_BROKEN = pytest.mark.xfail(
    reason="mcp_server imports mcp_queries, still on the removed whygraph.scan.db "
    "layer; whygraph-mcp left non-functional pending the III Iteration migration",
    raises=ImportError,
)


@_MCP_BROKEN
def test_imports() -> None:
    from whygraph import cli, mcp_server  # noqa: F401


@_MCP_BROKEN
def test_mcp_server_name() -> None:
    from whygraph.mcp_server import mcp

    assert mcp.name == "whygraph"


@_MCP_BROKEN
def test_mcp_server_registers_full_surface() -> None:
    """One assertion per registered tool/resource/prompt name.

    These names are part of the agent-callable contract — renaming any of
    them is a public API break.
    """
    import asyncio

    from whygraph.mcp_server import mcp

    tools = asyncio.run(mcp.list_tools())
    tool_names = {t.name for t in tools}
    assert {
        "whygraph_evidence_for",
        "whygraph_search",
        "whygraph_velocity_summary",
        "whygraph_rationale_brief",
        "whygraph_window",
    } <= tool_names

    resources = asyncio.run(mcp.list_resource_templates()) + asyncio.run(
        mcp.list_resources()
    )
    resource_uris = {str(r.uriTemplate) if hasattr(r, "uriTemplate") else str(r.uri) for r in resources}
    # Static URIs and templates both surface; check membership loosely.
    joined = " ".join(resource_uris)
    assert "whygraph://repo/overview" in joined
    assert "whygraph://commit/" in joined
    assert "whygraph://pr/" in joined
    assert "whygraph://issue/" in joined

    prompts = asyncio.run(mcp.list_prompts())
    prompt_names = {p.name for p in prompts}
    assert {
        "explain_change",
        "debug_history",
        "team_pulse",
        "changelog",
        "feature_timeline",
        "user_profile",
        "whygraph_plan",
    } <= prompt_names
