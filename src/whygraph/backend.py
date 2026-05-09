from __future__ import annotations

import sqlite3
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Protocol


@dataclass(frozen=True)
class SymbolNode:
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


class GraphBackend(Protocol):
    def get_node(self, qualified_name: str) -> SymbolNode | None: ...
    def get_node_by_id(self, node_id: str) -> SymbolNode | None: ...
    def get_callers(self, node_id: str) -> list[SymbolNode]: ...
    def get_callees(self, node_id: str) -> list[SymbolNode]: ...
    def find_symbols(self, query: str, limit: int = 20) -> list[SymbolNode]: ...
    def walk_neighbors(self, node_id: str, depth: int = 1) -> list[SymbolNode]: ...
    def close(self) -> None: ...


_NODE_COLUMNS = (
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
_NODE_SELECT = f"SELECT {', '.join(_NODE_COLUMNS)} FROM nodes"
_NODE_SELECT_N = f"SELECT {', '.join('n.' + c for c in _NODE_COLUMNS)}"

_MAX_DEPTH = 3


def _row_to_node(row: sqlite3.Row) -> SymbolNode:
    return SymbolNode(
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


def _rows_to_nodes(rows: Iterable[sqlite3.Row]) -> list[SymbolNode]:
    return [_row_to_node(r) for r in rows]


class SqliteCodegraphBackend:
    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    def get_node(self, qualified_name: str) -> SymbolNode | None:
        row = self._conn.execute(
            f"{_NODE_SELECT} WHERE qualified_name = ? LIMIT 1",
            (qualified_name,),
        ).fetchone()
        return _row_to_node(row) if row else None

    def get_node_by_id(self, node_id: str) -> SymbolNode | None:
        row = self._conn.execute(
            f"{_NODE_SELECT} WHERE id = ?",
            (node_id,),
        ).fetchone()
        return _row_to_node(row) if row else None

    def get_callers(self, node_id: str) -> list[SymbolNode]:
        rows = self._conn.execute(
            f"{_NODE_SELECT_N} FROM nodes n JOIN edges e ON e.source = n.id "
            "WHERE e.target = ? AND e.kind = 'calls'",
            (node_id,),
        ).fetchall()
        return _rows_to_nodes(rows)

    def get_callees(self, node_id: str) -> list[SymbolNode]:
        rows = self._conn.execute(
            f"{_NODE_SELECT_N} FROM nodes n JOIN edges e ON e.target = n.id "
            "WHERE e.source = ? AND e.kind = 'calls'",
            (node_id,),
        ).fetchall()
        return _rows_to_nodes(rows)

    def find_symbols(self, query: str, limit: int = 20) -> list[SymbolNode]:
        like = f"%{query}%"
        rows = self._conn.execute(
            f"{_NODE_SELECT} "
            "WHERE qualified_name LIKE ? OR name LIKE ? "
            "ORDER BY length(qualified_name) ASC "
            "LIMIT ?",
            (like, like, limit),
        ).fetchall()
        return _rows_to_nodes(rows)

    def iter_nodes(self) -> Iterator[SymbolNode]:
        """Stream every node in the graph. Used by the renderer to build a
        full node list — protocol-level callers should still go through
        ``get_node`` / ``find_symbols``."""
        cur = self._conn.execute(_NODE_SELECT)
        for row in cur:
            yield _row_to_node(row)

    def iter_edges(self) -> Iterator[tuple[str, str, str]]:
        """Stream every edge as ``(source_id, target_id, kind)``. Used by
        the renderer."""
        cur = self._conn.execute("SELECT source, target, kind FROM edges")
        for row in cur:
            yield (row["source"], row["target"], row["kind"])

    def walk_neighbors(self, node_id: str, depth: int = 1) -> list[SymbolNode]:
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
            neighbors = self._conn.execute(
                "SELECT target AS other FROM edges WHERE source = ? "
                "UNION "
                "SELECT source AS other FROM edges WHERE target = ?",
                (current, current),
            ).fetchall()
            for row in neighbors:
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
        by_id = {r["id"]: _row_to_node(r) for r in rows}
        return [by_id[i] for i in order if i in by_id]
