"""The WhyGraph Explorer HTTP server — a second transport over the service layer.

``whygraph serve`` (see :mod:`whygraph.cli.commands.serve`) runs the FastAPI app
built here as a long-lived, loopback-only container. The app is a **thin adapter**:
its ``/api`` routes call the *same* plain functions the MCP tools call
(:func:`whygraph.mcp.rationale.whygraph_rationale_brief`,
:func:`whygraph.mcp.evidence.whygraph_evidence_for`,
:func:`whygraph.mcp.area_history.whygraph_area_history`, the resource readers) plus
the kind-aware traversal methods on
:class:`whygraph.services.codegraph.CodeGraph`, so the panel's rationale / evidence
/ history can never drift from the MCP's.

The panel is **read-only** except for one explicit, user-initiated action: the
``POST .../rationale`` endpoint, which generates and caches a rationale card exactly
as the MCP tool does. Passive viewing never calls an LLM.

Public API
----------
* :func:`whygraph.serve.app.create_app` — the FastAPI application factory.
"""

from __future__ import annotations
