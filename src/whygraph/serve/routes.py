"""The ``/api/*`` routes for the Explorer panel.

Every handler is a **sync** ``def`` so FastAPI runs it in the threadpool — each
request gets its own thread, its own ``get_session()`` (a ``sqlmodel.Session`` is
not thread-safe), and its own read-only :class:`CodeGraph` handle. Handlers stay
thin: they delegate to the shared service functions
(:mod:`whygraph.mcp.rationale` / ``evidence`` / ``area_history`` / ``resources``)
and to the new traversal methods on :class:`CodeGraph`, then serialise.

Rationale is split (the resolved design decision): the **GET** is LLM-free — it
resolves the target, collects evidence, and reads the cache — while the **POST**
runs the full ``whygraph_rationale_brief`` generate-and-cache flow. Generation
therefore happens only on the explicit "Generate" button, never on passive view.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from fastapi import APIRouter, HTTPException, Query

from whygraph.core import get_config
from whygraph.mcp.area_history import whygraph_area_history
from whygraph.mcp.evidence import collect_evidence, whygraph_evidence_for
from whygraph.mcp.rationale import _format_response, whygraph_rationale_brief
from whygraph.mcp.rationale_cache import lookup_cached
from whygraph.mcp.resources import _commit_resource, _issue_resource, _pr_resource
from whygraph.mcp.targets import repo_root, resolve_target, target_dict
from whygraph.services.codegraph import CodeGraph, CodeGraphError

from . import graphdata

router = APIRouter()


@contextmanager
def _open_graph() -> Iterator[CodeGraph]:
    """Open a per-request read-only CodeGraph handle, or 503 if there is none.

    A missing/unopenable ``.codegraph/`` DB is a setup failure (the user must run
    ``whygraph scan``), surfaced as HTTP 503 so the UI can show a clear banner
    rather than a 500.
    """
    try:
        graph = CodeGraph.for_repository(
            repo_root(), codegraph_db=get_config().codegraph_db
        )
    except CodeGraphError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"CodeGraph index unavailable — run `whygraph scan`: {exc}",
        ) from exc
    try:
        yield graph
    finally:
        graph.close()


def _coverage_flag(symbol_file: str, start: int, end: int) -> bool:
    """Whether a cached rationale row matches this symbol's (path, line range).

    The rationale cache is keyed by line range, not qualified_name (§7.3), so a
    match means the symbol has been analysed. LLM-free — a pure cache read.
    """
    from whygraph.db import get_session
    from whygraph.db.models import RationaleCache
    from sqlmodel import select

    with get_session() as session:
        row = session.exec(
            select(RationaleCache.path)
            .where(RationaleCache.path == symbol_file)
            .where(RationaleCache.line_start == start)
            .where(RationaleCache.line_end == end)
        ).first()
    return row is not None


# ---- discovery -----------------------------------------------------------


@router.get("/search")
def search(q: str = Query(""), limit: int = Query(20, ge=1, le=100)) -> dict:
    """Cmd-K search — symbols whose name/qualified name contains ``q``."""
    if not q:
        return {"query": q, "results": []}
    with _open_graph() as graph:
        symbols = graph.search(q, limit=limit)
        results = [
            {
                **graphdata._symbol_dict(s),
                "analyzed": _coverage_flag(s.file_path, s.start_line, s.end_line),
            }
            for s in symbols
        ]
    return {"query": q, "results": results}


@router.get("/tree")
def tree(
    dir: str | None = Query(None),
    node: str | None = Query(None),
) -> dict:
    """One lazy level of the containment tree (root when no params)."""
    with _open_graph() as graph:
        entries = graphdata.tree_level(graph, directory=dir, node_id=node)
    return {"dir": dir, "node": node, "entries": entries}


# ---- graph ---------------------------------------------------------------


@router.get("/graph/overview")
def graph_overview(expanded: str = Query("")) -> dict:
    """Phase-2 LOD overview: directory super-nodes with weighted lifted edges.

    ``expanded`` is a comma-separated list of expanded directory paths (empty →
    only top-level directories). The landing view of the panel.
    """
    from . import coverage, lifting

    expanded_set = {d for d in expanded.split(",") if d}
    with _open_graph() as graph:
        cov = coverage.file_coverage(graph)
        return lifting.build_overview(graph, expanded_set, cov)


@router.get("/graph/ego")
def graph_ego(
    qualified_name: str = Query(...), hops: int = Query(1, ge=1, le=1)
) -> dict:
    """The one-hop ego graph of a symbol, with server-computed coordinates."""
    with _open_graph() as graph:
        symbol = graph.symbol(qualified_name)
        if symbol is None:
            raise HTTPException(status_code=404, detail=f"{qualified_name!r} not found")
        return graphdata.ego_graph(graph, symbol)


# ---- node detail ---------------------------------------------------------


@router.get("/node/{qualified_name}")
def node_detail(qualified_name: str) -> dict:
    """Identity + typed relationships for a symbol (the Relationships tab)."""
    with _open_graph() as graph:
        symbol = graph.symbol(qualified_name)
        if symbol is None:
            raise HTTPException(status_code=404, detail=f"{qualified_name!r} not found")
        return {
            "symbol": graphdata._symbol_dict(symbol),
            "analyzed": _coverage_flag(
                symbol.file_path, symbol.start_line, symbol.end_line
            ),
            "relations": graphdata.node_relations(graph, symbol),
        }


@router.get("/node/{qualified_name}/rationale")
def rationale_read(qualified_name: str) -> dict:
    """Cache-only rationale read — never calls an LLM (the resolved Q3 split).

    Returns ``{status: "cached", ...card}`` on a cache hit,
    ``{status: "no_evidence"}`` when the target has no historical evidence, or
    ``{status: "not_generated"}`` otherwise — the signal the UI uses to show a
    "Generate" button.
    """
    target = resolve_target(
        path=None, line_start=None, line_end=None, qualified_name=qualified_name
    )
    evidence = collect_evidence(target, limit=20)
    if not evidence:
        return {"status": "no_evidence", "target": target_dict(target)}
    cfg = get_config().rationale
    cached = lookup_cached(target, evidence, cfg.provider, cfg.model)
    if cached is None:
        return {"status": "not_generated", "target": target_dict(target)}
    rationale, cached_at = cached
    return {
        "status": "cached",
        **_format_response(target, rationale, evidence, cached_at),
    }


@router.post("/node/{qualified_name}/rationale")
def rationale_generate(qualified_name: str) -> dict:
    """Generate + cache a rationale card (the explicit "Generate" action).

    Runs :func:`whygraph_rationale_brief` verbatim — the same generate-and-cache
    flow the MCP tool performs — so the card can never drift from the MCP's. Slow
    (one LLM call); runs in the threadpool so the event loop is not blocked.
    """
    card = whygraph_rationale_brief(qualified_name=qualified_name)
    return {"status": "cached", **card}


@router.get("/node/{qualified_name}/evidence")
def evidence(qualified_name: str, limit: int = Query(20, ge=1, le=100)) -> dict:
    """Historical evidence for a symbol (the Evidence tab)."""
    return whygraph_evidence_for(qualified_name=qualified_name, limit=limit)


# ---- history (path-keyed; query param avoids a slash-in-path converter) ---


@router.get("/history")
def history(
    path: str = Query(...),
    limit: int = Query(20, ge=1, le=100),
    include_renames: bool = Query(True),
) -> dict:
    """Area history for a file path (the History tab)."""
    return whygraph_area_history(
        path=path, limit=limit, include_renames=include_renames
    )


# ---- evidence-link detail ------------------------------------------------


@router.get("/commit/{sha}")
def commit(sha: str) -> dict:
    """A commit and the PRs that contain it (mirrors the MCP resource)."""
    return _commit_resource(sha)


@router.get("/pr/{number}")
def pull_request(number: int) -> dict:
    """A pull request and the issues it closes (mirrors the MCP resource)."""
    return _pr_resource(number)


@router.get("/issue/{number}")
def issue(number: int) -> dict:
    """An issue and the PRs that close it (mirrors the MCP resource)."""
    return _issue_resource(number)
