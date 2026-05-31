"""The ``whygraph_area_history`` MCP tool.

Where ``whygraph_evidence_for`` asks "which commits authored *these
specific lines* of code", ``whygraph_area_history`` asks "which commits
have ever touched *this file* (or anything that ever became this file)".
The two answer different questions and reinforce each other — evidence
keeps line-level precision; area-history reaches code that no longer
exists at HEAD by walking the rename chain.

The tool is keyed by path, not by symbol or line range, on purpose: it
exists to surface commits that blame physically cannot, including
commits whose touched paths have since been deleted or renamed away.
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from .errors import WhyGraphError, log_tool_errors
from .evidence import (
    _evidence_dict,
    backfill_evidence_descriptions,
)
from .path_history import area_history_commits

_log = logging.getLogger(__name__)

_TOOL_DESCRIPTION = (
    "List the commits that have ever touched a given file path or any of "
    "its rename predecessors. Complements `whygraph_evidence_for` (which "
    "is line-blame-driven and HEAD-anchored): area_history reaches commits "
    "for code that has since been deleted, moved, or whose lines were all "
    "rewritten by a later refactor. Returns commits with their linked PRs "
    "and issues, newest first. Run `whygraph scan` first to populate the "
    "WhyGraph database."
)


def whygraph_area_history(
    path: str,
    limit: int = 20,
    include_renames: bool = True,
) -> dict:
    """MCP tool — area-history commits for a file path.

    See :data:`_TOOL_DESCRIPTION` for the agent-facing summary.

    Parameters
    ----------
    path : str
        The file path the caller cares about, as it appears at HEAD (or
        at any commit — the rename chain is bidirectional).
    limit : int, optional
        Cap on the number of commits returned, newest first. Default 20.
    include_renames : bool, optional
        When ``True`` (default), walk the ``renamed_from`` chain and
        include commits that touched historical names. When ``False``,
        only commits that touched the literal ``path`` are returned.

    Returns
    -------
    dict
        ``{"path": str, "include_renames": bool, "evidence": [...]}`` —
        the ``evidence`` list uses the same JSON shape that
        ``whygraph_evidence_for`` produces.
    """
    _log.debug(
        "whygraph_area_history called: path=%r limit=%d include_renames=%s",
        path,
        limit,
        include_renames,
    )
    if not path:
        raise WhyGraphError("path is required")
    if limit < 1:
        raise WhyGraphError("limit must be >= 1")

    items = area_history_commits(path, limit=limit, include_renames=include_renames)
    backfill_evidence_descriptions(items, target_path=path)
    return {
        "path": path,
        "include_renames": include_renames,
        "evidence": [_evidence_dict(item) for item in items],
    }


def register(mcp: FastMCP) -> None:
    """Attach the area-history tool to an MCP server."""
    mcp.tool(name="whygraph_area_history", description=_TOOL_DESCRIPTION)(
        log_tool_errors(whygraph_area_history)
    )
