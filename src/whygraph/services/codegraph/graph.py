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
from .relation import Relation
from .symbol import NODE_COLUMNS, Symbol

# Location of the CodeGraph database within a repository.
_DB_RELPATH = Path(".codegraph") / "codegraph.db"

# ``SELECT`` of the node columns Symbol.from_row needs — bare for plain node
# queries, and table-qualified as ``n`` for the edge joins.
_NODE_SELECT = f"SELECT {', '.join(NODE_COLUMNS)} FROM nodes"
_NODE_SELECT_JOINED = ", ".join(f"n.{c}" for c in NODE_COLUMNS)

# Hard cap on the neighbour-walk depth — keeps a pathological request bounded.
_MAX_DEPTH = 3

# The edge kind that records one symbol invoking another.
_CALLS = "calls"


class CodeGraph:
    """Read-only view of a CodeGraph knowledge graph.

    Construct directly from a database path, or via :meth:`for_repository` to
    resolve ``<root>/.codegraph/codegraph.db``. The connection is opened
    read-only; the instance is a context manager, so the idiomatic use is::

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
    def for_repository(cls, root: Path) -> "CodeGraph":
        """Open the CodeGraph database for a repository.

        Parameters
        ----------
        root : Path
            The repository root — the directory that contains ``.codegraph/``.

        Returns
        -------
        CodeGraph
            A view bound to ``<root>/.codegraph/codegraph.db``.

        Raises
        ------
        CodeGraphError
            If ``.codegraph/codegraph.db`` does not exist under ``root`` — most
            often because ``codegraph init`` has not been run there.
        """
        return cls(root / _DB_RELPATH)

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
        return self._calls_relations(node_id, incoming=True)

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
        return self._calls_relations(node_id, incoming=False)

    def _calls_relations(self, node_id: str, *, incoming: bool) -> list[Relation]:
        """Resolve the ``calls`` edges on one side of a symbol.

        ``incoming`` selects callers — the edge ``target`` is ``node_id`` and
        the neighbour is the edge ``source``; otherwise callees, the mirror.
        The edge's ``kind`` and ``line`` are aliased to ``edge_kind`` /
        ``edge_line`` so they do not collide with the node's own ``kind``.
        """
        anchor, neighbour = ("target", "source") if incoming else ("source", "target")
        rows = self._conn.execute(
            f"SELECT {_NODE_SELECT_JOINED}, "
            "e.kind AS edge_kind, e.line AS edge_line "
            f"FROM edges e JOIN nodes n ON n.id = e.{neighbour} "
            f"WHERE e.{anchor} = ? AND e.kind = ?",
            (node_id, _CALLS),
        ).fetchall()
        return [Relation.from_row(r) for r in rows]

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
