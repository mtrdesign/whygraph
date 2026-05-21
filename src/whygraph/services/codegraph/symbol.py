"""In-memory value object for one CodeGraph node — a code symbol.

Exposes :class:`Symbol` plus the row parser that builds it. The parser lives
here (not on :class:`~whygraph.services.codegraph.CodeGraph`) so that "what one
``nodes`` row looks like" is owned by the class that represents it — the same
pattern :meth:`whygraph.services.git.Commit.from_git_log` follows.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

NODE_COLUMNS = (
    "id",
    "kind",
    "name",
    "qualified_name",
    "file_path",
    "language",
    "start_line",
    "end_line",
    "docstring",
    "signature",
)
"""Columns selected from CodeGraph's ``nodes`` table, in the order the
``SELECT`` statements in :mod:`whygraph.services.codegraph.graph` list them.
:meth:`Symbol.from_row` reads exactly these keys."""


@dataclass(frozen=True, slots=True)
class Symbol:
    """One symbol in the code graph — a function, method, class, file, ….

    Mirrors a row of CodeGraph's ``nodes`` table, narrowed to the columns
    WhyGraph needs. Immutable; constructed from a query row by
    :meth:`from_row`.

    Attributes
    ----------
    id : str
        CodeGraph's internal node id. Opaque, but required to traverse the
        ``edges`` table — see :meth:`CodeGraph.callers` / :meth:`CodeGraph.callees`.
    kind : str
        Symbol kind — ``"function"``, ``"method"``, ``"class"``, ``"file"``,
        ``"variable"``, ``"import"``, ….
    name : str
        Unqualified symbol name.
    qualified_name : str
        Fully-qualified dotted name — the stable handle callers use to look a
        symbol up via :meth:`CodeGraph.symbol`.
    file_path : str
        Path to the defining file, relative to the repository root.
    language : str
        Source-language tag (e.g. ``"python"``).
    start_line : int
        1-based first line of the symbol's definition.
    end_line : int
        1-based last line of the symbol's definition.
    docstring : str or None
        The symbol's docstring, when CodeGraph extracted one.
    signature : str or None
        The symbol's signature, when CodeGraph extracted one.
    """

    id: str
    kind: str
    name: str
    qualified_name: str
    file_path: str
    language: str
    start_line: int
    end_line: int
    docstring: str | None
    signature: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Symbol":
        """Build a :class:`Symbol` from a ``nodes`` row.

        Parameters
        ----------
        row : sqlite3.Row
            A query row carrying at least the columns in :data:`NODE_COLUMNS`.

        Returns
        -------
        Symbol
            The parsed symbol.
        """
        return cls(
            id=row["id"],
            kind=row["kind"],
            name=row["name"],
            qualified_name=row["qualified_name"],
            file_path=row["file_path"],
            language=row["language"],
            start_line=int(row["start_line"]),
            end_line=int(row["end_line"]),
            docstring=row["docstring"],
            signature=row["signature"],
        )
