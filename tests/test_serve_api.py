"""Integration tests for the Explorer HTTP API — :mod:`whygraph.serve`.

Each test drives a FastAPI ``TestClient`` over :func:`create_app`, backed by a fake
CodeGraph DB (a ``file → class → method`` tree plus a caller) and an initialised,
empty WhyGraph DB. The rationale-split tests monkeypatch the service functions so
they can assert the LLM path is taken **only** on ``POST`` — never on a passive
``GET`` — which is the whole point of the resolved Q3 design.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from whygraph import core
from whygraph.core.config import Config
from whygraph.db import engine as db_engine
from whygraph.serve import routes
from whygraph.serve.app import create_app

# A small graph: file a.py contains class A contains method m; b.py's `caller`
# calls m and imports A.
_NODES = [
    {
        "id": "n_file_a",
        "kind": "file",
        "name": "a.py",
        "qualified_name": "src/pkg/a.py",
        "file_path": "src/pkg/a.py",
        "language": "python",
        "start_line": 1,
        "end_line": 40,
        "docstring": None,
        "signature": None,
    },
    {
        "id": "n_cls",
        "kind": "class",
        "name": "A",
        "qualified_name": "pkg.a.A",
        "file_path": "src/pkg/a.py",
        "language": "python",
        "start_line": 3,
        "end_line": 30,
        "docstring": None,
        "signature": "class A",
    },
    {
        "id": "n_m",
        "kind": "method",
        "name": "m",
        "qualified_name": "pkg.a.A.m",
        "file_path": "src/pkg/a.py",
        "language": "python",
        "start_line": 5,
        "end_line": 10,
        "docstring": "does m",
        "signature": "def m(self)",
    },
    {
        "id": "n_file_b",
        "kind": "file",
        "name": "b.py",
        "qualified_name": "src/pkg/b.py",
        "file_path": "src/pkg/b.py",
        "language": "python",
        "start_line": 1,
        "end_line": 20,
        "docstring": None,
        "signature": None,
    },
    {
        "id": "n_caller",
        "kind": "function",
        "name": "caller",
        "qualified_name": "pkg.b.caller",
        "file_path": "src/pkg/b.py",
        "language": "python",
        "start_line": 2,
        "end_line": 8,
        "docstring": None,
        "signature": "def caller()",
    },
]
_EDGES = [
    ("n_file_a", "n_cls", "contains"),
    ("n_cls", "n_m", "contains"),
    ("n_caller", "n_m", "calls"),
    ("n_caller", "n_cls", "imports"),
]


@pytest.fixture
def serve_client(tmp_path, monkeypatch, codegraph_db_factory):
    """A TestClient over ``create_app``, with a fake CodeGraph + empty WhyGraph DB.

    Points the app's static dir at an empty path so the API tests are independent
    of whether ``make playground`` has been run (the built bundle is gitignored).
    """
    cg_path = codegraph_db_factory(nodes=_NODES, edges=_EDGES)
    wdb = tmp_path / "whygraph.db"
    monkeypatch.setattr(core, "_config", Config(whygraph_db=wdb, codegraph_db=cg_path))
    monkeypatch.setattr("whygraph.serve.app._STATIC_DIR", tmp_path / "nostatic")
    db_engine._reset_engine()
    try:
        with TestClient(create_app(core._config)) as client:
            yield client
    finally:
        db_engine._reset_engine()
        core._reset_config()


# ---- tree ----------------------------------------------------------------


def test_tree_root_lists_top_directory(serve_client) -> None:
    entries = serve_client.get("/api/tree").json()["entries"]
    assert [e["label"] for e in entries] == ["src"]
    assert entries[0]["kind"] == "directory"
    assert entries[0]["dir"] == "src"


def test_tree_directory_lists_files(serve_client) -> None:
    entries = serve_client.get("/api/tree", params={"dir": "src/pkg"}).json()["entries"]
    labels = {e["label"] for e in entries}
    assert labels == {"a.py", "b.py"}
    assert all(e["kind"] == "file" for e in entries)


def test_tree_node_lists_symbol_children(serve_client) -> None:
    entries = serve_client.get("/api/tree", params={"node": "n_file_a"}).json()[
        "entries"
    ]
    assert [e["qualified_name"] for e in entries] == ["pkg.a.A"]


# ---- search --------------------------------------------------------------


def test_search_finds_symbol_with_coverage_flag(serve_client) -> None:
    results = serve_client.get("/api/search", params={"q": "A.m"}).json()["results"]
    assert any(r["qualified_name"] == "pkg.a.A.m" for r in results)
    assert all(r["analyzed"] is False for r in results)  # nothing cached yet


def test_search_empty_query_returns_no_results(serve_client) -> None:
    assert serve_client.get("/api/search", params={"q": ""}).json()["results"] == []


# ---- ego graph -----------------------------------------------------------


def test_ego_graph_has_focus_neighbours_and_coords(serve_client) -> None:
    body = serve_client.get(
        "/api/graph/ego", params={"qualified_name": "pkg.a.A.m"}
    ).json()
    assert body["focus"] == "pkg.a.A.m"
    ids = {n["id"] for n in body["nodes"]}
    assert ids == {"n_m", "n_caller", "n_cls"}  # focus + caller + container
    focus = next(n for n in body["nodes"] if n["data"]["is_focus"])
    assert focus["position"] == {"x": 0.0, "y": 0.0}
    edge_kinds = {(e["source"], e["target"], e["kind"]) for e in body["edges"]}
    assert ("n_caller", "n_m", "calls") in edge_kinds
    assert ("n_cls", "n_m", "contains") in edge_kinds


def test_ego_graph_404_for_unknown_symbol(serve_client) -> None:
    r = serve_client.get("/api/graph/ego", params={"qualified_name": "pkg.nope"})
    assert r.status_code == 404


def test_overview_lifts_to_directory_supernode(serve_client) -> None:
    # Nothing expanded → both files collapse into the top-level `src` super-node.
    body = serve_client.get("/api/graph/overview").json()
    assert {n["id"] for n in body["nodes"]} == {"dir:src"}
    assert all("coverage" in n for n in body["nodes"])


def test_overview_expanded_reveals_files(serve_client) -> None:
    body = serve_client.get(
        "/api/graph/overview", params={"expanded": "src,src/pkg"}
    ).json()
    ids = {n["id"] for n in body["nodes"]}
    assert "file:src/pkg/a.py" in ids and "file:src/pkg/b.py" in ids
    # b.py's caller calls/imports into a.py → a directional lifted edge exists.
    assert any(
        e["source"] == "file:src/pkg/b.py" and e["target"] == "file:src/pkg/a.py"
        for e in body["edges"]
    )


# ---- node detail ---------------------------------------------------------


def test_node_detail_groups_relations(serve_client) -> None:
    body = serve_client.get("/api/node?qualified_name=pkg.a.A.m").json()
    assert body["symbol"]["qualified_name"] == "pkg.a.A.m"
    rel = body["relations"]
    assert [c["qualified_name"] for c in rel["callers"]] == ["pkg.b.caller"]
    assert rel["container"]["qualified_name"] == "pkg.a.A"
    assert body["analyzed"] is False


def test_node_detail_404_for_unknown(serve_client) -> None:
    assert serve_client.get("/api/node?qualified_name=pkg.nope").status_code == 404


def test_node_detail_handles_file_node_with_slashes_in_qn(serve_client) -> None:
    # A `file` node's qualified_name is a path with slashes (e.g. "src/pkg/a.py").
    # As a query param this must resolve cleanly and return JSON — not fall through
    # to the SPA (which previously returned index.html with a 200).
    r = serve_client.get("/api/node?qualified_name=src/pkg/a.py")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["symbol"]["qualified_name"] == "src/pkg/a.py"
    assert body["symbol"]["kind"] == "file"
    # A file contains its class(es); no callers/callees.
    assert [c["qualified_name"] for c in body["relations"]["children"]] == ["pkg.a.A"]


# ---- rationale split (the resolved Q3 design) ----------------------------


def _fake_evidence() -> SimpleNamespace:
    return SimpleNamespace(pull_requests=[], issues=[])


def test_rationale_get_no_evidence_makes_no_llm_call(serve_client, monkeypatch) -> None:
    monkeypatch.setattr(routes, "collect_evidence", lambda target, limit=20: [])
    gen = mock.Mock()
    monkeypatch.setattr(routes, "whygraph_rationale_brief", gen)

    body = serve_client.get("/api/node/rationale?qualified_name=pkg.a.A.m").json()

    assert body["status"] == "no_evidence"
    gen.assert_not_called()


def test_rationale_get_not_generated_makes_no_llm_call(
    serve_client, monkeypatch
) -> None:
    monkeypatch.setattr(
        routes, "collect_evidence", lambda t, limit=20: [_fake_evidence()]
    )
    monkeypatch.setattr(routes, "lookup_cached", lambda *a, **k: None)
    gen = mock.Mock()
    monkeypatch.setattr(routes, "whygraph_rationale_brief", gen)

    body = serve_client.get("/api/node/rationale?qualified_name=pkg.a.A.m").json()

    assert body["status"] == "not_generated"
    gen.assert_not_called()


def test_rationale_get_returns_cached_card(serve_client, monkeypatch) -> None:
    from whygraph.analyze import Rationale

    rationale = Rationale(
        purpose="the purpose",
        why="the why",
        constraints=("c1",),
        tradeoffs=(),
        risks=(),
        model="test-model",
        provider="test",
        input_tokens=1,
        output_tokens=2,
    )
    monkeypatch.setattr(
        routes, "collect_evidence", lambda t, limit=20: [_fake_evidence()]
    )
    monkeypatch.setattr(
        routes,
        "lookup_cached",
        lambda *a, **k: (rationale, "2026-01-01T00:00:00+00:00"),
    )
    gen = mock.Mock()
    monkeypatch.setattr(routes, "whygraph_rationale_brief", gen)

    body = serve_client.get("/api/node/rationale?qualified_name=pkg.a.A.m").json()

    assert body["status"] == "cached"
    assert body["purpose"] == "the purpose"
    assert body["constraints"] == ["c1"]
    gen.assert_not_called()  # cache read is still LLM-free


def test_rationale_post_calls_brief_verbatim(serve_client, monkeypatch) -> None:
    card = {
        "target": {"path": "src/pkg/a.py", "line_start": 5, "line_end": 10},
        "purpose": "generated purpose",
    }
    gen = mock.Mock(return_value=card)
    monkeypatch.setattr(routes, "whygraph_rationale_brief", gen)

    body = serve_client.post("/api/node/rationale?qualified_name=pkg.a.A.m").json()

    assert body["status"] == "cached"
    assert body["purpose"] == "generated purpose"
    gen.assert_called_once_with(qualified_name="pkg.a.A.m")


# ---- static fallback -----------------------------------------------------


def test_root_reports_ui_not_built(serve_client) -> None:
    # No static bundle in a source checkout — the API must still serve, and `/`
    # returns the guidance message rather than 500.
    r = serve_client.get("/")
    assert r.status_code == 200
    assert "ui is not built" in r.text.lower()
    assert "make playground" in r.text


def test_serves_spa_when_built(tmp_path, monkeypatch, codegraph_db_factory) -> None:
    # With a built bundle, `/` serves index.html, unknown client routes fall back
    # to it (SPA routing), and /api still wins over the catch-all.
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>WG-BUILT</title>")
    monkeypatch.setattr("whygraph.serve.app._STATIC_DIR", static)
    cg_path = codegraph_db_factory(nodes=_NODES, edges=_EDGES)
    monkeypatch.setattr(
        core, "_config", Config(whygraph_db=tmp_path / "w.db", codegraph_db=cg_path)
    )
    db_engine._reset_engine()
    try:
        with TestClient(create_app(core._config)) as client:
            assert "WG-BUILT" in client.get("/").text
            assert "WG-BUILT" in client.get("/some/client/route").text
            assert client.get("/api/tree").status_code == 200
    finally:
        db_engine._reset_engine()
        core._reset_config()
