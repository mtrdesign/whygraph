"""Server-side graph + tree assembly for the Explorer panel.

Two jobs, both computed in Python so the browser never runs a layout engine
(the direct fix for the old viewer being "slow and glitchy"):

* :func:`ego_graph` — the focus symbol plus its immediate typed neighbours
  (callers, callees, imports, container, children), rendered as React-Flow-ready
  ``nodes`` / ``edges`` with **precomputed layered coordinates**. The browser only
  pans, zooms, and re-fetches on expansion.
* :func:`tree_level` — one lazy level of the ``dir → file → class → method``
  containment tree, with directories synthesised from file-node ``file_path``.

Both take an open :class:`~whygraph.services.codegraph.CodeGraph`; neither opens or
closes it (the caller owns the per-request handle — see :mod:`whygraph.serve.routes`).
"""

from __future__ import annotations

from whygraph.services.codegraph import CodeGraph, Relation, Symbol

# Layout constants for the layered ego-graph. Rows are stacked vertically; nodes
# within a row are spread horizontally and centred on the focus at (0, 0).
_ROW_GAP = 170
_COL_GAP = 240

# Symbol kinds that may contain other symbols — used to decide, cheaply and
# without an extra query per row, whether a tree node shows an expand affordance.
_EXPANDABLE_KINDS = {"file", "class", "module", "namespace", "interface"}


def _symbol_dict(symbol: Symbol) -> dict:
    """The identity fields the UI needs for a symbol, in one flat dict."""
    return {
        "id": symbol.id,
        "qualified_name": symbol.qualified_name,
        "name": symbol.name,
        "kind": symbol.kind,
        "file_path": symbol.file_path,
        "start_line": symbol.start_line,
        "end_line": symbol.end_line,
        "signature": symbol.signature,
    }


def _relation_dict(relation: Relation) -> dict:
    """A relationship-list row: the neighbour symbol plus the edge kind/line."""
    return {
        **_symbol_dict(relation.symbol),
        "edge_kind": relation.kind,
        "edge_line": relation.line,
    }


def node_relations(graph: CodeGraph, symbol: Symbol) -> dict:
    """Every typed relationship of ``symbol``, grouped for the detail panel.

    Parameters
    ----------
    graph : CodeGraph
        An open graph handle.
    symbol : Symbol
        The resolved focus symbol.

    Returns
    -------
    dict
        ``{callers, callees, imports, container, children}`` — each a list of
        relation/symbol dicts (``container`` is a single dict or ``None``).
    """
    container = graph.container(symbol.id)
    return {
        "callers": [_relation_dict(r) for r in graph.callers(symbol.id)],
        "callees": [_relation_dict(r) for r in graph.callees(symbol.id)],
        "imports": [_relation_dict(r) for r in graph.imports_(symbol.id)],
        "container": _symbol_dict(container) if container else None,
        "children": [_symbol_dict(s) for s in graph.children(symbol.id)],
    }


def _row_positions(count: int, y: float) -> list[dict]:
    """``count`` positions on row ``y``, centred on x = 0."""
    return [{"x": (i - (count - 1) / 2) * _COL_GAP, "y": y} for i in range(count)]


def ego_graph(graph: CodeGraph, symbol: Symbol) -> dict:
    """Assemble the one-hop ego graph of ``symbol`` with layered coordinates.

    Layout is three rows: things that point *into* / contain the focus above it
    (callers, container), the focus in the middle, and things it points *at* /
    contains below it (callees, imports, children). Coordinates are final — the
    client renders them verbatim, never running a force simulation.

    Parameters
    ----------
    graph : CodeGraph
        An open graph handle.
    symbol : Symbol
        The resolved focus symbol.

    Returns
    -------
    dict
        ``{focus, nodes, edges}``. ``nodes`` carry ``position`` + ``data``;
        ``edges`` carry ``source`` / ``target`` (CodeGraph node ids) and ``kind``.
        A duplicate neighbour (e.g. a symbol that both calls and is called by the
        focus) appears once as a node but keeps both directed edges.
    """
    container = graph.container(symbol.id)
    callers = graph.callers(symbol.id)
    callees = graph.callees(symbol.id)
    imports = graph.imports_(symbol.id)
    children = graph.children(symbol.id)

    # Upper row: callers + the container. Lower row: callees + imports + children.
    upper: list[tuple[Symbol, str, bool]] = [(r.symbol, "calls", True) for r in callers]
    if container is not None:
        upper.append((container, "contains", True))
    lower: list[tuple[Symbol, str, bool]] = (
        [(r.symbol, "calls", False) for r in callees]
        + [(r.symbol, "imports", False) for r in imports]
        + [(s, "contains", False) for s in children]
    )

    nodes: dict[str, dict] = {
        symbol.id: {
            "id": symbol.id,
            "position": {"x": 0.0, "y": 0.0},
            "data": {**_symbol_dict(symbol), "is_focus": True},
        }
    }
    edges: list[dict] = []

    def _place(items: list[tuple[Symbol, str, bool]], y: float) -> None:
        for (neighbour, kind, incoming), pos in zip(
            items, _row_positions(len(items), y)
        ):
            if neighbour.id not in nodes:
                nodes[neighbour.id] = {
                    "id": neighbour.id,
                    "position": pos,
                    "data": {**_symbol_dict(neighbour), "is_focus": False},
                }
            src, tgt = (
                (neighbour.id, symbol.id) if incoming else (symbol.id, neighbour.id)
            )
            edges.append(
                {
                    "id": f"{src}->{tgt}:{kind}",
                    "source": src,
                    "target": tgt,
                    "kind": kind,
                }
            )

    _place(upper, -_ROW_GAP)
    _place(lower, _ROW_GAP)

    return {
        "focus": symbol.qualified_name,
        "nodes": list(nodes.values()),
        "edges": edges,
    }


def _tree_entry_for_symbol(symbol: Symbol) -> dict:
    """A containment-tree row for a symbol node (file / class / method / …)."""
    return {
        "id": f"node:{symbol.id}",
        "label": symbol.name,
        "kind": symbol.kind,
        "node_id": symbol.id,
        "qualified_name": symbol.qualified_name,
        "path": symbol.file_path,
        "has_children": symbol.kind in _EXPANDABLE_KINDS,
    }


def _tree_entry_for_dir(path: str, label: str) -> dict:
    """A containment-tree row for a synthesised directory."""
    return {
        "id": f"dir:{path}",
        "label": label,
        "kind": "directory",
        "dir": path,
        "has_children": True,
    }


def tree_level(
    graph: CodeGraph,
    *,
    directory: str | None = None,
    node_id: str | None = None,
) -> list[dict]:
    """Return one lazy level of the containment tree.

    Exactly one expansion mode applies:

    * ``node_id`` given — the symbol children of that file/class node
      (``CodeGraph.children``).
    * otherwise — the entries directly under ``directory`` (root when ``None``):
      immediate sub-directories (synthesised from file ``file_path``) then the
      file nodes that live directly in it.

    Parameters
    ----------
    graph : CodeGraph
        An open graph handle.
    directory : str, optional
        Directory path to list, relative to the repo root. ``None`` lists root.
    node_id : str, optional
        A file/class node id whose symbol children to list. Wins over
        ``directory`` when both are given.

    Returns
    -------
    list[dict]
        Directory rows first, then file/symbol rows.
    """
    if node_id is not None:
        return [_tree_entry_for_symbol(s) for s in graph.children(node_id)]

    prefix = f"{directory.rstrip('/')}/" if directory else ""
    subdirs: dict[str, None] = {}  # ordered set of immediate sub-directory names
    files: list[Symbol] = []
    for file_symbol in graph.files():
        file_path = file_symbol.file_path
        if not file_path.startswith(prefix):
            continue
        rest = file_path[len(prefix) :]
        head, _, tail = rest.partition("/")
        if tail:
            subdirs.setdefault(head, None)
        else:
            files.append(file_symbol)

    dir_entries = [_tree_entry_for_dir(f"{prefix}{name}", name) for name in subdirs]
    file_entries = [_tree_entry_for_symbol(f) for f in files]
    return dir_entries + file_entries
