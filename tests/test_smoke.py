def test_imports() -> None:
    from whygraph import cli, mcp_server  # noqa: F401


def test_mcp_server_name() -> None:
    from whygraph.mcp_server import mcp

    assert mcp.name == "whygraph"
