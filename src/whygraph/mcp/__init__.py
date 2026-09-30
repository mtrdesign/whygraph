"""WhyGraph's MCP server — rationale and evidence cards over the scan DB.

The server is assembled in :mod:`whygraph.mcp.server`: a single
``FastMCP("whygraph")`` instance with one feature module per domain
(:mod:`whygraph.mcp.evidence`, :mod:`whygraph.mcp.rationale`). Each feature
module exposes a ``register(mcp)`` function that attaches its tools — so
new features land as new modules without growing a monolith.

The WhyGraph portal serves it over HTTP, one endpoint per project
(``/mcp/<slug>``, :mod:`whygraph.portal.mcp_mount`).
"""
