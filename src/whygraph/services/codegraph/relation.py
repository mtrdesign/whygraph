"""In-memory value object for one CodeGraph edge — a relation between symbols.

Exposes :class:`Relation`: a neighbouring :class:`Symbol` paired with the edge
``kind`` that connects it and the line the edge was observed on. The row parser
lives here, mirroring :meth:`Symbol.from_row`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .symbol import Symbol


@dataclass(frozen=True, slots=True)
class Relation:
    """A directed edge in the code graph, resolved to its neighbour symbol.

    A :class:`Relation` is always read relative to some *anchor* symbol — the
    one passed to :meth:`CodeGraph.callers` or :meth:`CodeGraph.callees`.
    :attr:`symbol` is the symbol at the *other* end of the edge; :attr:`kind`
    is what the edge means.

    Attributes
    ----------
    symbol : Symbol
        The neighbour — the caller (from :meth:`CodeGraph.callers`) or the
        callee (from :meth:`CodeGraph.callees`) of the anchor symbol.
    kind : str
        Edge kind — ``"calls"``, ``"contains"``, or ``"imports"``. This is the
        relation's "action": what the anchor and the neighbour do to each other.
    line : int or None
        1-based line of the edge's site (e.g. the call site), or ``None`` when
        CodeGraph did not record one.
    """

    symbol: Symbol
    kind: str
    line: int | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Relation":
        """Build a :class:`Relation` from a joined ``edges`` + ``nodes`` row.

        The row must carry the
        :data:`~whygraph.services.codegraph.symbol.NODE_COLUMNS` of the
        neighbour node plus the edge's ``edge_kind`` and ``edge_line`` columns.
        Those two are aliased (rather than the bare ``kind`` / ``line``) so they
        do not collide with the node's own ``kind`` column on the joined row.

        Parameters
        ----------
        row : sqlite3.Row
            A row produced by a callers/callees query in
            :mod:`whygraph.services.codegraph.graph`.

        Returns
        -------
        Relation
            The parsed relation.
        """
        line = row["edge_line"]
        return cls(
            symbol=Symbol.from_row(row),
            kind=row["edge_kind"],
            line=int(line) if line is not None else None,
        )
