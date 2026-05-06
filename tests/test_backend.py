from __future__ import annotations

from pathlib import Path

from whygraph.backend import GraphBackend, SqliteCodegraphBackend, SymbolNode


def test_get_node_returns_node_for_known_qname(fake_codegraph_db: Path) -> None:
    backend = SqliteCodegraphBackend(fake_codegraph_db)
    try:
        node = backend.get_node("pkg.a")
        assert node is not None
        assert node.id == "n_a"
        assert node.qualified_name == "pkg.a"
        assert node.file_path == "src/pkg/a.py"
        assert node.docstring == "doc-a"
    finally:
        backend.close()


def test_get_node_returns_none_for_unknown_qname(
    fake_codegraph_db: Path,
) -> None:
    backend = SqliteCodegraphBackend(fake_codegraph_db)
    try:
        assert backend.get_node("pkg.missing") is None
    finally:
        backend.close()


def test_get_node_by_id_roundtrip(fake_codegraph_db: Path) -> None:
    backend = SqliteCodegraphBackend(fake_codegraph_db)
    try:
        node = backend.get_node_by_id("n_b")
        assert node is not None
        assert node.qualified_name == "pkg.b"
    finally:
        backend.close()


def test_get_node_by_id_returns_none_for_unknown(
    fake_codegraph_db: Path,
) -> None:
    backend = SqliteCodegraphBackend(fake_codegraph_db)
    try:
        assert backend.get_node_by_id("nope") is None
    finally:
        backend.close()


def test_get_callers_returns_sources_of_call_edges(
    fake_codegraph_db: Path,
) -> None:
    backend = SqliteCodegraphBackend(fake_codegraph_db)
    try:
        callers = backend.get_callers("n_b")
        assert [c.id for c in callers] == ["n_a"]
    finally:
        backend.close()


def test_get_callers_empty_for_no_callers(fake_codegraph_db: Path) -> None:
    backend = SqliteCodegraphBackend(fake_codegraph_db)
    try:
        assert backend.get_callers("n_a") == []
    finally:
        backend.close()


def test_get_callees_returns_targets_of_call_edges(
    fake_codegraph_db: Path,
) -> None:
    backend = SqliteCodegraphBackend(fake_codegraph_db)
    try:
        callees = backend.get_callees("n_a")
        assert [c.id for c in callees] == ["n_b"]
    finally:
        backend.close()


def test_get_callers_ignores_non_call_edges(codegraph_db_factory) -> None:
    path = codegraph_db_factory(
        edges=[("n_a", "n_b", "imports"), ("n_a", "n_b", "calls")],
    )
    backend = SqliteCodegraphBackend(path)
    try:
        callers = backend.get_callers("n_b")
        assert [c.id for c in callers] == ["n_a"]
        assert len(callers) == 1
    finally:
        backend.close()


def test_find_symbols_substring_match(fake_codegraph_db: Path) -> None:
    backend = SqliteCodegraphBackend(fake_codegraph_db)
    try:
        results = backend.find_symbols("pkg.")
        assert {r.id for r in results} == {"n_a", "n_b", "n_c"}
    finally:
        backend.close()


def test_find_symbols_respects_limit(fake_codegraph_db: Path) -> None:
    backend = SqliteCodegraphBackend(fake_codegraph_db)
    try:
        results = backend.find_symbols("pkg.", limit=2)
        assert len(results) == 2
    finally:
        backend.close()


def test_find_symbols_returns_empty_for_no_match(
    fake_codegraph_db: Path,
) -> None:
    backend = SqliteCodegraphBackend(fake_codegraph_db)
    try:
        assert backend.find_symbols("zzz") == []
    finally:
        backend.close()


def test_walk_neighbors_depth_1_returns_immediate_neighbors(
    fake_codegraph_db: Path,
) -> None:
    backend = SqliteCodegraphBackend(fake_codegraph_db)
    try:
        neighbors = backend.walk_neighbors("n_b", depth=1)
        assert {n.id for n in neighbors} == {"n_a", "n_c"}
    finally:
        backend.close()


def test_walk_neighbors_depth_0_is_empty(fake_codegraph_db: Path) -> None:
    backend = SqliteCodegraphBackend(fake_codegraph_db)
    try:
        assert backend.walk_neighbors("n_b", depth=0) == []
    finally:
        backend.close()


def test_walk_neighbors_depth_2_reaches_2_hops(fake_codegraph_db: Path) -> None:
    backend = SqliteCodegraphBackend(fake_codegraph_db)
    try:
        neighbors = backend.walk_neighbors("n_a", depth=2)
        assert {n.id for n in neighbors} == {"n_b", "n_c"}
    finally:
        backend.close()


def test_walk_neighbors_caps_at_max_depth(codegraph_db_factory) -> None:
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
    backend = SqliteCodegraphBackend(path)
    try:
        neighbors = backend.walk_neighbors("n_a", depth=10)
        ids = {n.id for n in neighbors}
        assert ids == {"n_b", "n_c", "n_d"}
        assert "n_e" not in ids
    finally:
        backend.close()


def test_backend_implements_protocol_shape(fake_codegraph_db: Path) -> None:
    backend = SqliteCodegraphBackend(fake_codegraph_db)
    try:
        for method in (
            "get_node",
            "get_node_by_id",
            "get_callers",
            "get_callees",
            "find_symbols",
            "walk_neighbors",
            "close",
        ):
            assert hasattr(backend, method), method
        assert isinstance(backend.get_node("pkg.a"), SymbolNode)
    finally:
        backend.close()


def test_protocol_is_typing_only() -> None:
    assert GraphBackend is not None
