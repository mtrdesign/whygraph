"""Tests for the codegraph service — :mod:`whygraph.services.codegraph`.

Every test runs against a fake CodeGraph-shaped SQLite database built by the
``fake_codegraph_db`` / ``codegraph_db_factory`` fixtures (see ``conftest.py``).
The default graph is three function symbols ``pkg.a``, ``pkg.b``, ``pkg.c``
where ``a`` calls ``b`` and ``b`` calls ``c``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from whygraph.services.codegraph import (
    CodeGraph,
    CodeGraphError,
    Relation,
    Symbol,
    SymbolContext,
)

# ---- construction & lifecycle -------------------------------------------


def test_init_raises_for_missing_database(tmp_path: Path) -> None:
    with pytest.raises(CodeGraphError, match="not found"):
        CodeGraph(tmp_path / "nope.db")


def test_for_repository_raises_when_codegraph_absent(tmp_path: Path) -> None:
    with pytest.raises(CodeGraphError, match="not found"):
        CodeGraph.for_repository(tmp_path)


def test_for_repository_opens_db_at_conventional_path(
    tmp_path: Path, fake_codegraph_db: Path
) -> None:
    cg_dir = tmp_path / ".codegraph"
    cg_dir.mkdir()
    fake_codegraph_db.rename(cg_dir / "codegraph.db")

    with CodeGraph.for_repository(tmp_path) as graph:
        assert graph.symbol("pkg.a") is not None


def test_context_manager_closes_the_connection(fake_codegraph_db: Path) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        pass
    with pytest.raises(sqlite3.ProgrammingError):
        graph.symbol("pkg.a")


# ---- symbol / symbol_by_id ----------------------------------------------


def test_symbol_returns_symbol_for_known_qname(fake_codegraph_db: Path) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        symbol = graph.symbol("pkg.a")

    assert isinstance(symbol, Symbol)
    assert symbol.id == "n_a"
    assert symbol.qualified_name == "pkg.a"
    assert symbol.file_path == "src/pkg/a.py"
    assert symbol.docstring == "doc-a"
    assert symbol.signature == "def a()"


def test_symbol_returns_none_for_unknown_qname(fake_codegraph_db: Path) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        assert graph.symbol("pkg.missing") is None


def test_symbol_by_id_roundtrip(fake_codegraph_db: Path) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        symbol = graph.symbol_by_id("n_b")

    assert symbol is not None
    assert symbol.qualified_name == "pkg.b"


def test_symbol_by_id_returns_none_for_unknown(fake_codegraph_db: Path) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        assert graph.symbol_by_id("nope") is None


# ---- callers / callees ---------------------------------------------------


def test_callers_returns_relations_to_calling_symbols(
    fake_codegraph_db: Path,
) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        callers = graph.callers("n_b")

    assert [r.symbol.id for r in callers] == ["n_a"]
    assert all(isinstance(r, Relation) for r in callers)


def test_callees_returns_relations_to_called_symbols(
    fake_codegraph_db: Path,
) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        callees = graph.callees("n_a")

    assert [r.symbol.id for r in callees] == ["n_b"]


def test_callers_empty_for_symbol_with_no_callers(
    fake_codegraph_db: Path,
) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        assert graph.callers("n_a") == []


def test_callers_ignore_non_call_edges(codegraph_db_factory) -> None:
    path = codegraph_db_factory(
        edges=[("n_a", "n_b", "imports"), ("n_a", "n_b", "calls")],
    )
    with CodeGraph(path) as graph:
        callers = graph.callers("n_b")

    assert [r.symbol.id for r in callers] == ["n_a"]
    assert [r.kind for r in callers] == ["calls"]


def test_relation_carries_edge_kind_and_line(codegraph_db_factory) -> None:
    path = codegraph_db_factory(edges=[("n_a", "n_b", "calls", 42)])
    with CodeGraph(path) as graph:
        (caller,) = graph.callers("n_b")

    assert caller.kind == "calls"
    assert caller.line == 42
    assert caller.symbol.qualified_name == "pkg.a"


def test_relation_line_is_none_when_edge_records_none(
    fake_codegraph_db: Path,
) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        (caller,) = graph.callers("n_b")

    assert caller.line is None


# ---- search --------------------------------------------------------------


def test_search_substring_match(fake_codegraph_db: Path) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        results = graph.search("pkg.")

    assert {r.id for r in results} == {"n_a", "n_b", "n_c"}


def test_search_respects_limit(fake_codegraph_db: Path) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        results = graph.search("pkg.", limit=2)

    assert len(results) == 2


def test_search_returns_empty_for_no_match(fake_codegraph_db: Path) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        assert graph.search("zzz") == []


# ---- neighbors -----------------------------------------------------------


def test_neighbors_depth_1_returns_immediate_neighbors(
    fake_codegraph_db: Path,
) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        neighbors = graph.neighbors("n_b", depth=1)

    assert {n.id for n in neighbors} == {"n_a", "n_c"}


def test_neighbors_depth_0_is_empty(fake_codegraph_db: Path) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        assert graph.neighbors("n_b", depth=0) == []


def test_neighbors_depth_2_reaches_two_hops(fake_codegraph_db: Path) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        neighbors = graph.neighbors("n_a", depth=2)

    assert {n.id for n in neighbors} == {"n_b", "n_c"}


def test_neighbors_caps_at_max_depth(codegraph_db_factory) -> None:
    nodes = [
        {
            "id": f"n_{ch}",
            "kind": "function",
            "name": ch,
            "qualified_name": f"pkg.{ch}",
            "file_path": f"src/{ch}.py",
            "language": "python",
            "start_line": 1,
            "end_line": 1,
            "docstring": None,
            "signature": None,
        }
        for ch in "abcde"
    ]
    edges = [
        ("n_a", "n_b", "calls"),
        ("n_b", "n_c", "calls"),
        ("n_c", "n_d", "calls"),
        ("n_d", "n_e", "calls"),
    ]
    path = codegraph_db_factory(nodes=nodes, edges=edges)
    with CodeGraph(path) as graph:
        ids = {n.id for n in graph.neighbors("n_a", depth=10)}

    assert ids == {"n_b", "n_c", "n_d"}
    assert "n_e" not in ids


# ---- context -------------------------------------------------------------


def test_context_bundles_target_with_callers_and_callees(
    fake_codegraph_db: Path,
) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        context = graph.context("pkg.b")

    assert isinstance(context, SymbolContext)
    assert context.target.qualified_name == "pkg.b"
    assert [r.symbol.qualified_name for r in context.callers] == ["pkg.a"]
    assert [r.symbol.qualified_name for r in context.callees] == ["pkg.c"]


def test_context_returns_none_for_unknown_qname(fake_codegraph_db: Path) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        assert graph.context("pkg.missing") is None


def test_context_has_empty_tuples_for_leaf_symbol(fake_codegraph_db: Path) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        context = graph.context("pkg.a")

    assert context is not None
    assert context.callers == ()
    assert [r.symbol.qualified_name for r in context.callees] == ["pkg.b"]


# ---- value objects -------------------------------------------------------


def test_symbol_is_frozen(fake_codegraph_db: Path) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        symbol = graph.symbol("pkg.a")

    assert symbol is not None
    with pytest.raises(Exception):  # FrozenInstanceError
        symbol.kind = "class"  # type: ignore[misc]


def test_relation_is_frozen(fake_codegraph_db: Path) -> None:
    with CodeGraph(fake_codegraph_db) as graph:
        (caller,) = graph.callers("n_b")

    with pytest.raises(Exception):  # FrozenInstanceError
        caller.kind = "imports"  # type: ignore[misc]
