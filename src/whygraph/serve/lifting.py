"""Phase-2 edge-lifting for the directory-level LOD overview (§8).

Every low-level ``calls`` / ``imports`` edge is projected onto the **deepest
currently-visible ancestor** of each endpoint, given an ``expanded`` set of
directory paths. Cross-container edges become one **weighted, directional** edge
between super-nodes; edges internal to a collapsed super-node are hidden (counted
as ``internal_edges`` on the node). Lifting for a given expansion state is a cheap
group-by over :meth:`CodeGraph.file_edges` — no per-request graph walk.
"""

from __future__ import annotations

from whygraph.services.codegraph import CodeGraph

# The low-level edge kinds rolled up into the overview. `contains` is the tree
# structure itself, so it is deliberately excluded.
LIFTED_KINDS = ("calls", "imports")


def _dirs_of(file_path: str) -> list[str]:
    """Cumulative directory paths of ``file_path``, shallowest first.

    ``"a/b/c/foo.py"`` → ``["a", "a/b", "a/b/c"]`` (the filename is dropped).
    """
    parts = file_path.split("/")[:-1]
    dirs: list[str] = []
    acc = ""
    for part in parts:
        acc = f"{acc}/{part}" if acc else part
        dirs.append(acc)
    return dirs


def _representative(file_path: str, expanded: set[str]) -> str:
    """The visible node id that ``file_path`` rolls up to under ``expanded``.

    Descends the directory chain through expanded ancestors; the first collapsed
    directory (its parent expanded, itself not) is the super-node. If the whole
    chain is expanded, the file itself is the visible node.
    """
    for directory in _dirs_of(file_path):
        if directory not in expanded:
            return f"dir:{directory}"
    return f"file:{file_path}"


def _rep_coverage(rep: str, coverage: dict[str, tuple[int, int]]) -> dict:
    """Aggregate ``(analyzed, total)`` coverage for a representative node."""
    if rep.startswith("file:"):
        analyzed, total = coverage.get(rep[len("file:") :], (0, 0))
    else:
        prefix = rep[len("dir:") :] + "/"
        analyzed = total = 0
        for file_path, (file_analyzed, file_total) in coverage.items():
            if file_path.startswith(prefix):
                analyzed += file_analyzed
                total += file_total
    return {
        "analyzed": analyzed,
        "total": total,
        "fraction": analyzed / total if total else 0.0,
    }


def _node_meta(rep: str) -> dict:
    """Identity fields for a representative node (kind, label, path)."""
    if rep.startswith("file:"):
        path = rep[len("file:") :]
        return {"id": rep, "kind": "file", "label": path.split("/")[-1], "path": path}
    path = rep[len("dir:") :]
    return {"id": rep, "kind": "directory", "label": path.split("/")[-1], "path": path}


def build_overview(
    graph: CodeGraph,
    expanded: set[str],
    coverage: dict[str, tuple[int, int]],
) -> dict:
    """Assemble the LOD overview graph for a given expansion state.

    Parameters
    ----------
    graph : CodeGraph
        An open graph handle.
    expanded : set of str
        Directory paths that are currently expanded (top-level dirs are always
        visible regardless).
    coverage : dict
        ``file_path -> (analyzed, total)`` from :func:`coverage.file_coverage`.

    Returns
    -------
    dict
        ``{"expanded": [...], "nodes": [...], "edges": [...]}``. Nodes carry a
        ``coverage`` block and an ``internal_edges`` count; edges are weighted and
        directional (``X→Y`` and ``Y→X`` stay distinct — asymmetry is signal).
    """
    # Visible node set: the representative of every file under the current state.
    nodes: dict[str, dict] = {}
    for file_symbol in graph.files():
        rep = _representative(file_symbol.file_path, expanded)
        nodes.setdefault(rep, {**_node_meta(rep), "internal_edges": 0})

    weights: dict[tuple[str, str, str], int] = {}
    for src_file, tgt_file, kind in graph.file_edges(LIFTED_KINDS):
        src_rep = _representative(src_file, expanded)
        tgt_rep = _representative(tgt_file, expanded)
        # An edge may touch a file with no file node listed (defensive): make sure
        # both endpoints are present as nodes.
        nodes.setdefault(src_rep, {**_node_meta(src_rep), "internal_edges": 0})
        nodes.setdefault(tgt_rep, {**_node_meta(tgt_rep), "internal_edges": 0})
        if src_rep == tgt_rep:
            nodes[src_rep]["internal_edges"] += 1
            continue
        key = (src_rep, tgt_rep, kind)
        weights[key] = weights.get(key, 0) + 1

    for node in nodes.values():
        node["coverage"] = _rep_coverage(node["id"], coverage)

    edges = [
        {
            "id": f"{src}->{tgt}:{kind}",
            "source": src,
            "target": tgt,
            "kind": kind,
            "weight": weight,
        }
        for (src, tgt, kind), weight in weights.items()
    ]
    return {"expanded": sorted(expanded), "nodes": list(nodes.values()), "edges": edges}
