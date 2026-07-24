"""Tests for the Phase-2 LOD overview — edge-lifting + coverage.

The lifting tests exercise the three cases from §8.1 (internal / cross-container /
mixed expansion) against a hand-built graph: three files in two directories with
two ``calls`` edges. Coverage is tested against a seeded ``rationale_cache``.
"""

from __future__ import annotations

import pytest

from whygraph.services.codegraph import CodeGraph
from whygraph.serve import coverage, lifting


def _node(
    nid: str, kind: str, name: str, file_path: str, start: int = 1, end: int = 5
) -> dict:
    return {
        "id": nid,
        "kind": kind,
        "name": name,
        "qualified_name": name,
        "file_path": file_path,
        "language": "python",
        "start_line": start,
        "end_line": end,
        "docstring": None,
        "signature": None,
    }


# src/a/foo.py::foo calls src/b/bar.py::bar (cross dir) and src/a/baz.py::baz (same dir).
_NODES = [
    _node("file:src/a/foo.py", "file", "foo.py", "src/a/foo.py"),
    _node("file:src/a/baz.py", "file", "baz.py", "src/a/baz.py"),
    _node("file:src/b/bar.py", "file", "bar.py", "src/b/bar.py"),
    _node("fn_foo", "function", "foo", "src/a/foo.py", 1, 10),
    _node("fn_baz", "function", "baz", "src/a/baz.py", 1, 10),
    _node("fn_bar", "function", "bar", "src/b/bar.py", 1, 10),
]
_EDGES = [
    ("fn_foo", "fn_bar", "calls"),
    ("fn_foo", "fn_baz", "calls"),
]


@pytest.fixture
def overview_db(codegraph_db_factory):
    return codegraph_db_factory(nodes=_NODES, edges=_EDGES)


def _overview(db, expanded: set[str], cov=None):
    with CodeGraph(db) as graph:
        return lifting.build_overview(graph, expanded, cov or {})


def test_lifting_internal_when_nothing_expanded(overview_db) -> None:
    # Both edges collapse into the single top-level `dir:src` super-node → hidden.
    ov = _overview(overview_db, set())
    assert {n["id"] for n in ov["nodes"]} == {"dir:src"}
    assert ov["edges"] == []
    assert ov["nodes"][0]["internal_edges"] == 2


def test_lifting_cross_container_edge(overview_db) -> None:
    # Expand `src`: foo/baz roll up to dir:src/a, bar to dir:src/b.
    ov = _overview(overview_db, {"src"})
    ids = {n["id"] for n in ov["nodes"]}
    assert ids == {"dir:src/a", "dir:src/b"}
    assert ov["edges"] == [
        {
            "id": "dir:src/a->dir:src/b:calls",
            "source": "dir:src/a",
            "target": "dir:src/b",
            "kind": "calls",
            "weight": 1,
        }
    ]
    # foo→baz is internal to dir:src/a.
    src_a = next(n for n in ov["nodes"] if n["id"] == "dir:src/a")
    assert src_a["internal_edges"] == 1


def test_lifting_mixed_expansion(overview_db) -> None:
    # Expand `src` and `src/a`: foo/baz become file nodes; bar stays dir:src/b.
    ov = _overview(overview_db, {"src", "src/a"})
    ids = {n["id"] for n in ov["nodes"]}
    assert ids == {"file:src/a/foo.py", "file:src/a/baz.py", "dir:src/b"}
    edge_tuples = {(e["source"], e["target"], e["weight"]) for e in ov["edges"]}
    assert ("file:src/a/foo.py", "dir:src/b", 1) in edge_tuples  # mixed
    assert ("file:src/a/foo.py", "file:src/a/baz.py", 1) in edge_tuples  # cross file


def test_lifting_edges_are_directional(overview_db) -> None:
    # X→Y and Y→X must never collapse into one undirected edge.
    ov = _overview(overview_db, {"src"})
    dirs = {(e["source"], e["target"]) for e in ov["edges"]}
    assert ("dir:src/a", "dir:src/b") in dirs  # foo(src/a) → bar(src/b)
    assert ("dir:src/b", "dir:src/a") not in dirs  # no reverse edge exists


def test_coverage_counts_analyzed_over_total(
    overview_db, whygraph_db_initialized
) -> None:
    from whygraph.db import get_session
    from whygraph.db.models import RationaleCache

    # Seed one cached rationale that matches fn_foo's (path, line range).
    with get_session() as session:
        session.add(
            RationaleCache(
                path="src/a/foo.py",
                line_start=1,
                line_end=10,
                provider="test",
                model="default",
                evidence_fingerprint="fp",
                cached_at="2026-01-01T00:00:00+00:00",
                purpose="p",
                why="w",
                constraints="[]",
                tradeoffs="[]",
                risks="[]",
            )
        )
        session.commit()

    with CodeGraph(overview_db) as graph:
        cov = coverage.file_coverage(graph)

    assert cov["src/a/foo.py"] == (1, 1)  # analyzed
    assert cov["src/a/baz.py"] == (0, 1)  # not analyzed
    assert cov["src/b/bar.py"] == (0, 1)


def test_coverage_feeds_overview_node(overview_db) -> None:
    ov = _overview(
        overview_db, {"src"}, cov={"src/a/foo.py": (1, 2), "src/a/baz.py": (0, 1)}
    )
    src_a = next(n for n in ov["nodes"] if n["id"] == "dir:src/a")
    # dir:src/a aggregates foo (1/2) + baz (0/1) = 1/3.
    assert src_a["coverage"]["analyzed"] == 1
    assert src_a["coverage"]["total"] == 3
    assert src_a["coverage"]["fraction"] == pytest.approx(1 / 3)
