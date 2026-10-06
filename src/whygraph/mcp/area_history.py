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

from whygraph.core.context import current_project
from whygraph.core.remote import RemoteError, RemoteProject, platform_block
from whygraph.services.git import Repository

from .errors import WhyGraphError, log_tool_errors
from .evidence import (
    _evidence_dict,
    backfill_evidence_descriptions,
    default_remote_refs,
    path_tracked_at,
)
from .path_history import area_history_commits
from .targets import repo_root

_log = logging.getLogger(__name__)

_TOOL_DESCRIPTION = (
    "List the commits that have ever touched a given file path or any of "
    "its rename predecessors. Complements `whygraph_evidence_for` (which "
    "is line-blame-driven and HEAD-anchored): area_history reaches commits "
    "for code that has since been deleted, moved, or whose lines were all "
    "rewritten by a later refactor. Returns commits with their linked PRs "
    "and issues, newest first. Scan from the WhyGraph portal (or run "
    "`whygraph scan` outside it) first to populate the WhyGraph database."
)


def linked_area_history(
    remote: RemoteProject, path: str, limit: int, include_renames: bool
) -> dict:
    """``whygraph_area_history``'s answer for a project linked to a platform.

    Area history is a database question, so there is no local half to fall
    back on: the platform answers it against its server clone. ``path`` is
    sent only when a pushed revision tracks it (M2e plan section 0.2 #8),
    so a file that exists only in this checkout yields an empty list rather
    than naming itself to the platform.

    Parameters
    ----------
    remote : RemoteProject
        The project's platform.
    path, limit, include_renames
        The tool's arguments, unchanged.

    Returns
    -------
    dict
        ``{"path", "include_renames", "evidence", "platform"}``.

    Raises
    ------
    WhyGraphError
        The platform could not be reached, or refused.
    """
    repo = Repository(repo_root())
    items: list[dict] = []
    if path_tracked_at(repo, path, default_remote_refs(repo)):
        try:
            items = remote.history(path, limit, include_renames)
        except RemoteError as exc:
            raise WhyGraphError(str(exc)) from exc
    return {
        "path": path,
        "include_renames": include_renames,
        "evidence": items,
        "platform": platform_block(remote),
    }


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
        ``whygraph_evidence_for`` produces. A project linked to a WhyGraph
        platform is answered by :func:`linked_area_history`, which adds a
        ``platform`` block.
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

    ctx = current_project()
    if ctx is not None and ctx.remote is not None:
        return linked_area_history(ctx.remote, path, limit, include_renames)

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
