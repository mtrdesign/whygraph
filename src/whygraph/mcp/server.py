"""The WhyGraph MCP server — assembly point and ``whygraph-mcp`` entry.

Owns the single ``FastMCP("whygraph")`` instance, attaches every feature
module's tools to it at import time (so ``mcp.list_tools()`` works without
running the server), and exposes :func:`main` for the ``whygraph-mcp``
console script.

Adding a feature: create a ``whygraph/mcp/<feature>.py`` with a
``register(mcp)`` function, then import it and call ``register`` below.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from whygraph.core import configure_logging, get_config

from . import area_history, evidence, prompts, rationale, resources

mcp = FastMCP("whygraph")

evidence.register(mcp)
rationale.register(mcp)
area_history.register(mcp)
resources.register(mcp)
prompts.register(mcp)


def main() -> None:
    """Run the WhyGraph MCP server on stdio. Entry point for ``whygraph-mcp``."""
    cfg = get_config()
    configure_logging(cfg.log_level, file_config=cfg.logging)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
