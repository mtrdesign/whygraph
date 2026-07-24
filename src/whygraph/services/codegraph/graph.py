"""Read-only query layer over a CodeGraph SQLite database.

Exposes :class:`CodeGraph` — the entry point of the codegraph service. It opens
CodeGraph's ``.codegraph/codegraph.db`` read-only and answers structural
questions about the code: look a symbol up by qualified name, search by
substring, walk callers and callees, and assemble a :class:`SymbolContext`.

WhyGraph reads CodeGraph's SQLite directly — no subprocess, no MCP roundtrip.
The schema (tables ``nodes`` and ``edges``) is owned upstream by CodeGraph
(``colbymchenry/codegraph``); this module only reads it.
"""

from __future__ import annotations

import sqlite3
from collections import deque
from pathlib import Path

from .context import SymbolContext
from .exceptions import CodeGraphError
from .paths import CODEGRAPH_DB_RELPATH
from .relation import Relation
from .symbol import NODE_COLUMNS, Symbol

# ``SELECT`` of the node columns Symbol.from_row needs — bare for plain node
# queries, and table-qualified as ``n`` for the edge joins.
_NODE_SELECT = f"SELECT {', '.join(NODE_COLUMNS)} FROM nodes"
_NODE_SELECT_JOINED = ", ".join(f"n.{c}" for c in NODE_COLUMNS)

# Hard cap on the neighbour-walk depth — keeps a pathological request bounded.
_MAX_DEPTH = 3

# The edge kind that records one symbol invoking another.
_CALLS = "calls"
# The edge kind that records one symbol importing another.
_IMPORTS = "imports"
# The edge kind that records structural containment (file → class → method).
_CONTAINS = "contains"
# The node kind for a source file — the roots of the containment tree.
_FILE = "file"
# Node kinds a rationale card can be generated for — used by coverage counting.
_DEFINABLE_KINDS = ("function", "method", "class")


class CodeGraph:
    """Read-only view of a CodeGraph knowledge graph.

    Construct directly from a database path, or via :meth:`for_repository`,
    which resolves ``<root>/.codegraph/codegraph.db`` (or a ``whygraph.toml``
    override). The connection is opened read-only; the instance is a context
    manager, so the idiomatic use is::

        with CodeGraph.for_repository(repo_root) as graph:
            symbol = graph.symbol("pkg.module.func")

    Parameters
    ----------
    db_path : Path
        Path to a CodeGraph SQLite database.

    Raises
    ------
    CodeGraphError
        If the database does not exist, or cannot be opened.

    Attributes
    ----------
    db_path : Path
        The database path the connection was opened against.
    """

    def __init__(self, db_path: Path) -> None:
        if not db_path.exists():
            raise CodeGraphError(
                f"CodeGraph database not found at {db_path} — run `codegraph init`"
            )
        self.db_path = db_path
        try:
            self._conn = sqlite3.connect(
                f"file:{db_path}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise CodeGraphError(
                f"failed to open CodeGraph database at {db_path}"
            ) from exc
        self._conn.row_factory = sqlite3.Row

    @classmethod
    def for_repository(
        cls, root: Path, *, codegraph_db: Path | None = None
    ) -> "CodeGraph":
        """Open the CodeGraph database for a repository.

        Parameters
        ----------
        root : Path
            The repository root — the directory that contains ``.codegraph/``.
        codegraph_db : Path, optional
            Explicit database path that overrides the default location. Pass
            :attr:`whygraph.core.config.Config.codegraph_db` here to honour a
            ``codegraph_db`` entry in ``whygraph.toml``. When ``None``
            (default), the project-relative ``<root>/.codegraph/codegraph.db``
            is used.

        Returns
        -------
        CodeGraph
            A view bound to the resolved database path.

        Raises
        ------
        CodeGraphError
            If the resolved database does not exist — most often because
            ``codegraph init`` has not been run.
        """
        return cls(
            codegraph_db if codegraph_db is not None else root / CODEGRAPH_DB_RELPATH
        )

    def __repr__(self) -> str:
        return f"CodeGraph(db_path={self.db_path!r})"

    def __enter__(self) -> "CodeGraph":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()

    def symbol(self, qualified_name: str) -> Symbol | None:
        """Look a symbol up by its fully-qualified name.

        Parameters
        ----------
        qualified_name : str
            The dotted name, e.g. ``"pkg.module.Class.method"``.

        Returns
        -------
        Symbol or None
            The matching symbol, or ``None`` when the graph has no such name.
            If several rows share the name, the first is returned.
        """
        row = self._conn.execute(
            f"{_NODE_SELECT} WHERE qualified_name = ? LIMIT 1",
            (qualified_name,),
        ).fetchone()
        return Symbol.from_row(row) if row else None

    def symbol_by_id(self, node_id: str) -> Symbol | None:
        """Look a symbol up by its CodeGraph node id.

        Parameters
        ----------
        node_id : str
            CodeGraph's internal node id — see :attr:`Symbol.id`.

        Returns
        -------
        Symbol or None
            The matching symbol, or ``None`` when no node has that id.
        """
        row = self._conn.execute(
            f"{_NODE_SELECT} WHERE id = ?",
            (node_id,),
        ).fetchone()
        return Symbol.from_row(row) if row else None

    def search(self, query: str, limit: int = 20) -> list[Symbol]:
        """Find symbols whose name or qualified name contains ``query``.

        Parameters
        ----------
        query : str
            Substring matched, case-sensitively, against both ``name`` and
            ``qualified_name``.
        limit : int, optional
            Maximum number of results (default 20). Shorter qualified names
            rank first, so the closest matches lead.

        Returns
        -------
        list[Symbol]
            Matching symbols, best-first; empty when nothing matches.
        """
        like = f"%{query}%"
        rows = self._conn.execute(
            f"{_NODE_SELECT} "
            "WHERE qualified_name LIKE ? OR name LIKE ? "
            "ORDER BY length(qualified_name) ASC "
            "LIMIT ?",
            (like, like, limit),
        ).fetchall()
        return [Symbol.from_row(r) for r in rows]

    def callers(self, node_id: str) -> list[Relation]:
        """Symbols that call the given symbol — its fan-in.

        Parameters
        ----------
        node_id : str
            The :attr:`Symbol.id` of the called symbol.

        Returns
        -------
        list[Relation]
            One :class:`Relation` per incoming ``calls`` edge; each
            :attr:`Relation.symbol` is a caller. Empty when nothing calls it.
        """
        return self.relations(node_id, _CALLS, incoming=True)

    def callees(self, node_id: str) -> list[Relation]:
        """Symbols the given symbol calls — its fan-out.

        Parameters
        ----------
        node_id : str
            The :attr:`Symbol.id` of the calling symbol.

        Returns
        -------
        list[Relation]
            One :class:`Relation` per outgoing ``calls`` edge; each
            :attr:`Relation.symbol` is a callee. Empty when it calls nothing.
        """
        return self.relations(node_id, _CALLS, incoming=False)

    def relations(self, node_id: str, kind: str, *, incoming: bool) -> list[Relation]:
        """Resolve the edges of one ``kind`` on one side of a symbol.

        Generalises callers/callees to any edge ``kind`` (``calls``,
        ``imports``, ``contains``). ``incoming`` selects the edges whose
        ``target`` is ``node_id`` (the neighbour is then the edge ``source``);
        otherwise the mirror — edges whose ``source`` is ``node_id``. The
        edge's ``kind`` and ``line`` are aliased to ``edge_kind`` / ``edge_line``
        so they do not collide with the node's own ``kind``.

        Parameters
        ----------
        node_id : str
            The :attr:`Symbol.id` to anchor the traversal on.
        kind : str
            The edge kind to filter on, e.g. ``"calls"`` or ``"contains"``.
        incoming : bool
            When ``True``, return edges pointing *at* ``node_id`` (fan-in);
            when ``False``, edges pointing *away* from it (fan-out).

        Returns
        -------
        list[Relation]
            One :class:`Relation` per matching edge; empty when there are none.
        """
        anchor, neighbour = ("target", "source") if incoming else ("source", "target")
        rows = self._conn.execute(
            f"SELECT {_NODE_SELECT_JOINED}, "
            "e.kind AS edge_kind, e.line AS edge_line "
            f"FROM edges e JOIN nodes n ON n.id = e.{neighbour} "
            f"WHERE e.{anchor} = ? AND e.kind = ?",
            (node_id, kind),
        ).fetchall()
        return [Relation.from_row(r) for r in rows]

    def imports_(self, node_id: str) -> list[Relation]:
        """Symbols the given symbol imports — its outgoing ``imports`` edges.

        Parameters
        ----------
        node_id : str
            The :attr:`Symbol.id` of the importing symbol.

        Returns
        -------
        list[Relation]
            One :class:`Relation` per outgoing ``imports`` edge; empty when it
            imports nothing.
        """
        return self.relations(node_id, _IMPORTS, incoming=False)

    def container(self, node_id: str) -> Symbol | None:
        """The symbol that structurally contains the given symbol.

        A ``file`` node *contains* a ``class``; a ``class`` *contains* a
        ``method``. The parent is therefore the source of an **incoming**
        ``contains`` edge (this symbol is the target).

        Parameters
        ----------
        node_id : str
            The :attr:`Symbol.id` of the contained symbol.

        Returns
        -------
        Symbol or None
            The containing symbol, or ``None`` when the symbol has no
            ``contains`` parent (e.g. a ``file`` node).
        """
        rels = self.relations(node_id, _CONTAINS, incoming=True)
        return rels[0].symbol if rels else None

    def children(self, node_id: str) -> list[Symbol]:
        """The symbols the given symbol structurally contains.

        The mirror of :meth:`container` — the targets of **outgoing**
        ``contains`` edges (a file's classes/functions, a class's methods).

        Parameters
        ----------
        node_id : str
            The :attr:`Symbol.id` of the containing symbol.

        Returns
        -------
        list[Symbol]
            The contained symbols, in edge order; empty when it contains none.
        """
        return [r.symbol for r in self.relations(node_id, _CONTAINS, incoming=False)]

    def files(self) -> list[Symbol]:
        """All ``file`` nodes — the roots of the containment tree.

        Returns
        -------
        list[Symbol]
            Every symbol whose ``kind`` is ``"file"``, ordered by ``file_path``.
        """
        rows = self._conn.execute(
            f"{_NODE_SELECT} WHERE kind = ? ORDER BY file_path",
            (_FILE,),
        ).fetchall()
        return [Symbol.from_row(r) for r in rows]

    def file_edges(self, kinds: tuple[str, ...]) -> list[tuple[str, str, str]]:
        """Every edge of the given kinds, projected onto endpoint file paths.

        Joins each edge's ``source`` and ``target`` to their nodes and returns
        the *defining file paths* of both ends. The Phase-2 edge-lifting
        aggregation (:mod:`whygraph.serve.lifting`) group-bys over these to roll
        low-level ``calls`` / ``imports`` edges up to directory/file super-nodes.

        Parameters
        ----------
        kinds : tuple of str
            Edge kinds to include, e.g. ``("calls", "imports")``.

        Returns
        -------
        list of (str, str, str)
            ``(source_file_path, target_file_path, edge_kind)`` per matching edge.
        """
        if not kinds:
            return []
        placeholders = ",".join("?" * len(kinds))
        rows = self._conn.execute(
            "SELECT s.file_path AS src, t.file_path AS tgt, e.kind AS k "
            "FROM edges e "
            "JOIN nodes s ON s.id = e.source "
            "JOIN nodes t ON t.id = e.target "
            f"WHERE e.kind IN ({placeholders})",
            kinds,
        ).fetchall()
        return [(r["src"], r["tgt"], r["k"]) for r in rows]

    def definition_ranges(self) -> list[tuple[str, int, int]]:
        """The ``(file_path, start_line, end_line)`` of every definable symbol.

        "Definable" means a ``function``, ``method``, or ``class`` — the symbols
        a rationale card can be generated for. The Phase-2 coverage aggregation
        (:mod:`whygraph.serve.coverage`) joins these line ranges against the
        ``rationale_cache`` to compute per-file/dir "analyzed" fractions.

        Returns
        -------
        list of (str, int, int)
            One tuple per definable symbol.
        """
        placeholders = ",".join("?" * len(_DEFINABLE_KINDS))
        rows = self._conn.execute(
            f"SELECT file_path, start_line, end_line FROM nodes "
            f"WHERE kind IN ({placeholders})",
            _DEFINABLE_KINDS,
        ).fetchall()
        return [
            (r["file_path"], int(r["start_line"]), int(r["end_line"])) for r in rows
        ]

    def neighbors(self, node_id: str, depth: int = 1) -> list[Symbol]:
        """Walk outward from a symbol over edges of any kind.

        Performs an undirected breadth-first walk — following every edge
        direction and every edge kind — and returns the symbols reached,
        nearest first. The starting symbol is not included.

        Parameters
        ----------
        node_id : str
            The :attr:`Symbol.id` to start from.
        depth : int, optional
            How many hops to walk (default 1). Clamped to ``0``–``3``; a depth
            of ``0`` returns an empty list.

        Returns
        -------
        list[Symbol]
            Reached symbols, in breadth-first order.
        """
        depth = min(max(depth, 0), _MAX_DEPTH)
        if depth == 0:
            return []
        seen: set[str] = {node_id}
        order: list[str] = []
        frontier: deque[tuple[str, int]] = deque([(node_id, 0)])
        while frontier:
            current, d = frontier.popleft()
            if d >= depth:
                continue
            rows = self._conn.execute(
                "SELECT target AS other FROM edges WHERE source = ? "
                "UNION "
                "SELECT source AS other FROM edges WHERE target = ?",
                (current, current),
            ).fetchall()
            for row in rows:
                other = row["other"]
                if other in seen:
                    continue
                seen.add(other)
                order.append(other)
                frontier.append((other, d + 1))

        if not order:
            return []
        placeholders = ",".join("?" * len(order))
        rows = self._conn.execute(
            f"{_NODE_SELECT} WHERE id IN ({placeholders})",
            order,
        ).fetchall()
        by_id = {r["id"]: Symbol.from_row(r) for r in rows}
        return [by_id[i] for i in order if i in by_id]

    def context(self, qualified_name: str) -> SymbolContext | None:
        """Assemble the structural context of a symbol.

        Resolves ``qualified_name`` to a symbol, then gathers its callers and
        callees — the bundle the rationale generator consumes as structural
        evidence.

        Parameters
        ----------
        qualified_name : str
            The dotted name of the symbol to describe.

        Returns
        -------
        SymbolContext or None
            The target symbol with its callers and callees, or ``None`` when
            the graph has no symbol with that name.
        """
        target = self.symbol(qualified_name)
        if target is None:
            return None
        return SymbolContext(
            target=target,
            callers=tuple(self.callers(target.id)),
            callees=tuple(self.callees(target.id)),
        )
