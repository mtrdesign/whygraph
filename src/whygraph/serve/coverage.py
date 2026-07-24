"""Phase-2 rationale-coverage counting for the LOD overview heatmap.

"Analyzed" for a symbol means a ``rationale_cache`` row whose
``(path, line_start, line_end)`` matches the symbol's ``file_path`` +
``start_line`` / ``end_line`` — the cache is keyed by line range, **not** by
qualified_name (§2.6/§7.3). Per-file coverage is the fraction of a file's
definable symbols that have such a match; per-directory coverage aggregates its
files. Because the cache is populated lazily (only when a user clicks Generate),
most symbols read "unexplored" until browsed — which is the point of the heatmap.
"""

from __future__ import annotations

from sqlmodel import select

from whygraph.db import get_session
from whygraph.db.models import RationaleCache
from whygraph.services.codegraph import CodeGraph


def _analyzed_keys() -> set[tuple[str, int, int]]:
    """The ``(path, line_start, line_end)`` of every cached rationale row."""
    with get_session() as session:
        rows = session.exec(
            select(
                RationaleCache.path,
                RationaleCache.line_start,
                RationaleCache.line_end,
            )
        ).all()
    return {(r[0], r[1], r[2]) for r in rows}


def file_coverage(graph: CodeGraph) -> dict[str, tuple[int, int]]:
    """Per-file ``(analyzed, total)`` counts over definable symbols.

    Parameters
    ----------
    graph : CodeGraph
        An open graph handle.

    Returns
    -------
    dict
        ``file_path -> (analyzed_count, total_count)``. A file with no definable
        symbols is absent from the mapping.
    """
    analyzed = _analyzed_keys()
    counts: dict[str, list[int]] = {}
    for file_path, start, end in graph.definition_ranges():
        entry = counts.setdefault(file_path, [0, 0])
        entry[1] += 1  # total
        if (file_path, start, end) in analyzed:
            entry[0] += 1  # analyzed
    return {path: (a, t) for path, (a, t) in counts.items()}
