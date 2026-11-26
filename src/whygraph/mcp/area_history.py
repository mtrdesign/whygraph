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
from typing import Annotated, Any, TypedDict

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from whygraph.core.context import current_project
from whygraph.core.remote import RemoteError, RemoteProject, platform_block
from whygraph.db import get_session
from whygraph.services.git import Repository

from .descriptions import (
    AREA_HISTORY_ANNOTATIONS,
    AREA_HISTORY_DESCRIPTION,
)
from .structured import structured
from .errors import WhyGraphError, log_tool_errors
from .evidence import (
    MAX_BACKFILL_PER_CALL,
    Detail,
    backfill_evidence_descriptions,
    default_remote_refs,
    evidence_dicts,
    path_tracked_at,
    review_scope,
    shape_evidence,
)
from .path_history import area_history_commits
from .plans import attach_linked_plans, guarded_plans_for
from .targets import repo_root

_log = logging.getLogger(__name__)


def linked_area_history(
    remote: RemoteProject,
    path: str,
    limit: int,
    include_renames: bool,
    *,
    detail: Detail = "compact",
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
    detail : {"compact", "full"}, optional
        The form of the ``plans`` refs.

    Returns
    -------
    dict
        ``{"path", "include_renames", "evidence", "describing", "platform"}``
        with items in the nested shape; ``describing`` is always ``0``
        (descriptions are the platform's). When the path was sent, the
        platform is also asked for its plans (file scope) with only the
        SHAs of its own answer and that path (plan 61 section 4.8):
        ``plans`` when any, ``platform.plans = "unsupported"`` on an older
        platform.

    Raises
    ------
    WhyGraphError
        The platform could not be reached, or refused.
    """
    repo = Repository(repo_root())
    items: list[dict] = []
    sent = path_tracked_at(repo, path, default_remote_refs(repo))
    if sent:
        try:
            items = remote.history(path, limit, include_renames)
        except RemoteError as exc:
            raise WhyGraphError(str(exc)) from exc
    result = {
        "path": path,
        "include_renames": include_renames,
        "evidence": items,
        "describing": 0,
        "platform": platform_block(remote),
    }
    if sent:
        # Only the platform's own SHAs and the path it was just sent.
        shas = [
            item["commit"]["sha"]
            for item in items
            if isinstance(item.get("commit"), dict)
            and isinstance(item["commit"].get("sha"), str)
        ]
        attach_linked_plans(result, remote, shas, [path], scope="file", detail=detail)
    return result


class _AreaHistoryRequired(TypedDict):
    path: str
    include_renames: bool
    evidence: list[dict[str, Any]]
    describing: int


class AreaHistoryResult(_AreaHistoryRequired, total=False):
    """The ``whygraph_area_history`` result, for FastMCP's output schema.

    ``evidence`` items are the ``compact`` or ``full`` shape of
    :func:`~whygraph.mcp.evidence.shape_evidence_item`; ``pull_requests``
    / ``issues`` are the ``detail="full"`` prose maps of
    :func:`~whygraph.mcp.evidence.shape_evidence`; ``platform`` is set for
    a linked project; ``plans`` lists the committed plans that name this
    file (file scope, omitted when empty).
    """

    pull_requests: dict[str, dict[str, Any]]
    issues: dict[str, dict[str, Any]]
    platform: dict[str, Any]
    plans: list[dict[str, Any]]


def area_history_payload(
    path: str,
    limit: int = 20,
    include_renames: bool = True,
    describe: bool = True,
    *,
    detail: Detail = "compact",
) -> dict:
    """Area-history commits for a file path, items in the nested shape.

    The body of :func:`whygraph_area_history` before its items are shaped,
    and what the Explorer's History tab reads.

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
    describe : bool, optional
        Backfill missing commit descriptions, bounded (default). ``False``
        never calls an LLM.
    detail : {"compact", "full"}, optional
        The form of a linked project's ``plans`` refs (only
        :func:`linked_area_history` reads it).

    Returns
    -------
    dict
        ``{"path", "include_renames", "evidence", "describing"}`` — the
        ``evidence`` items use :func:`~whygraph.mcp.evidence.evidence_dicts`'
        shape (review threads on ``path`` and, with ``include_renames``, its
        earlier names), ``describing`` counts the commits left undescribed. A
        project linked to a WhyGraph platform is answered by
        :func:`linked_area_history`, which adds a ``platform`` block.

    Raises
    ------
    WhyGraphError
        An empty ``path`` or a bad ``limit``.
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
        return linked_area_history(
            ctx.remote, path, limit, include_renames, detail=detail
        )

    items = area_history_commits(path, limit=limit, include_renames=include_renames)
    describing = backfill_evidence_descriptions(
        items, target_path=path, max_commits=MAX_BACKFILL_PER_CALL if describe else 0
    ).left
    with get_session() as session:
        scope = review_scope(
            session, repo_root(), path, include_renames=include_renames
        )
    return {
        "path": path,
        "include_renames": include_renames,
        "evidence": evidence_dicts(items, scope),
        "describing": describing,
    }


def whygraph_area_history(
    path: str,
    limit: Annotated[int, Field(ge=1, le=100)] = 10,
    include_renames: bool = True,
    detail: Detail = "compact",
    describe: bool = True,
) -> AreaHistoryResult:
    """MCP tool — area-history commits for a file path.

    See :data:`~whygraph.mcp.descriptions.AREA_HISTORY_DESCRIPTION` for the agent-facing summary.

    :func:`area_history_payload` with its items passed through
    :func:`~whygraph.mcp.evidence.shape_evidence` at ``detail``
    (``lines`` is ``null``: area items are not blame-derived).
    """
    result = area_history_payload(
        path,
        limit=limit,
        include_renames=include_renames,
        describe=describe,
        detail=detail,
    )
    shaped = {**result, **shape_evidence(result["evidence"], detail)}
    ctx = current_project()
    if ctx is None or ctx.remote is None:
        shas = list(dict.fromkeys(item["commit"]["sha"] for item in result["evidence"]))
        plans = guarded_plans_for(shas, [path], scope="file", detail=detail)
        if plans:
            shaped["plans"] = plans
    return shaped


def register(mcp: FastMCP) -> None:
    """Attach the area-history tool to an MCP server."""
    mcp.tool(
        name="whygraph_area_history",
        title=AREA_HISTORY_ANNOTATIONS.title,
        description=AREA_HISTORY_DESCRIPTION,
        annotations=AREA_HISTORY_ANNOTATIONS,
    )(log_tool_errors(structured(AreaHistoryResult)(whygraph_area_history)))
