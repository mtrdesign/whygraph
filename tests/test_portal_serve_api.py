"""Integration tests for the Explorer HTTP API on the portal app.

Ported from the 1.x ``test_serve_api.py`` (``whygraph serve`` is gone): each
test drives the portal (:func:`whygraph.portal.app.create_portal_app`) with
one initialized local project, ``demo``, whose ``.codegraph/codegraph.db`` is
a fake ``file -> class -> method`` tree plus a caller, and an initialised,
empty WhyGraph DB. :class:`ScopedClient` rewrites ``/api/<x>`` to the
project-scoped ``/api/projects/demo/<x>`` so the test bodies read as before.
The rationale-split tests monkeypatch the service functions so they can
assert the LLM path is taken **only** on ``POST`` - never on a passive
``GET`` - which is the whole point of the resolved Q3 design.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from conftest import build_fake_codegraph_db
from test_portal_app import (  # noqa: F401 -- `env` is a fixture
    add_local,
    env,
    init_project,
    make_repo,
    portal_client,
)
from whygraph.core.config import Config
from whygraph.core.context import ProjectContext
from whygraph.db import engine as db_engine
from whygraph.serve import routes

SLUG = "demo"


class ScopedClient:
    """A portal ``TestClient`` whose ``/api/<x>`` calls go to one project.

    ``/api/tree`` becomes ``/api/projects/<slug>/tree``; every other path
    (``/``, ``/p/...``, ``/api/projects/...``) is sent unchanged.

    Attributes
    ----------
    client : TestClient
        The underlying client (``X-WhyGraph-Client: 1``, loopback Host).
    slug : str
        The project slug.
    root : Path
        The project's repository root.
    """

    def __init__(self, client: TestClient, slug: str, root: Path) -> None:
        self.client = client
        self.slug = slug
        self.root = root

    @property
    def app(self):
        return self.client.app

    def _url(self, url: str) -> str:
        if url.startswith("/api/") and not url.startswith("/api/projects/"):
            return f"/api/projects/{self.slug}/{url.removeprefix('/api/')}"
        return url

    def request(self, method: str, url: str, **kwargs):
        return self.client.request(method, self._url(url), **kwargs)

    def get(self, url: str, **kwargs):
        return self.client.get(self._url(url), **kwargs)

    def post(self, url: str, **kwargs):
        return self.client.post(self._url(url), **kwargs)

    def put(self, url: str, **kwargs):
        return self.client.put(self._url(url), **kwargs)

    def patch(self, url: str, **kwargs):
        return self.client.patch(self._url(url), **kwargs)

    def delete(self, url: str, **kwargs):
        return self.client.delete(self._url(url), **kwargs)

    def context(self) -> ProjectContext:
        """The project's current (cached) context."""
        state = self.app.state.portal
        return state.contexts.get(self._project_id())

    def _project_id(self) -> int:
        from sqlmodel import select

        from whygraph.portal import db as portal_db
        from whygraph.portal.models import Project

        with portal_db.get_session() as session:
            return session.exec(
                select(Project.id).where(Project.slug == self.slug)
            ).one()


def use_project_config(
    client: ScopedClient, monkeypatch: pytest.MonkeyPatch, config: Config
) -> ProjectContext:
    """Make the project's bound context carry ``config`` (keeping its DB paths).

    The 1.x tests swapped ``core._config``; in the portal the config comes
    from the project context, so the portal's context cache is patched
    instead. The project's own ``whygraph_db`` / ``codegraph_db`` are always
    kept, so the test stays hermetic.
    """
    real = client.context()
    ctx = ProjectContext(
        slug=real.slug,
        root=real.root,
        config=replace(
            config,
            whygraph_db=real.config.whygraph_db,
            codegraph_db=real.config.codegraph_db,
        ),
    )
    contexts = client.app.state.portal.contexts

    async def _aget(project_id: int) -> ProjectContext:
        return ctx

    monkeypatch.setattr(contexts, "get", lambda project_id: ctx)
    monkeypatch.setattr(contexts, "aget", _aget)
    return ctx


@contextmanager
def portal_with_project(
    env: SimpleNamespace,  # noqa: F811
    *,
    nodes: list[dict] | None = None,
    edges: list | None = None,
    codegraph: bool = True,
) -> Iterator[ScopedClient]:
    """A portal past setup with the initialized local project ``demo``."""
    db_engine._reset_engine()
    try:
        with portal_client() as client:
            setup = client.post("/api/portal/setup", json={"display_name": "Tess"})
            assert setup.status_code == 201, setup.text
            root = make_repo(env.shared, SLUG)
            if codegraph:
                (root / ".codegraph").mkdir()
                build_fake_codegraph_db(
                    root / ".codegraph" / "codegraph.db", nodes=nodes, edges=edges
                )
            assert add_local(client, root)["project"]["slug"] == SLUG
            assert init_project(client, SLUG)["initialized"] is True
            yield ScopedClient(client, SLUG, root)
    finally:
        db_engine._reset_engine()


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
def serve_client(env: SimpleNamespace) -> Iterator[ScopedClient]:  # noqa: F811
    """The portal, scoped to ``demo``, with a fake CodeGraph + empty WhyGraph DB.

    The ``env`` fixture points the static dir at an empty path, so the API
    tests are independent of whether ``make playground`` has been run.
    """
    with portal_with_project(env, nodes=_NODES, edges=_EDGES) as client:
        yield client


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
    assert r.json() == {"error": "'pkg.nope' not found", "code": "symbol_not_found"}


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
    # No static bundle in a source checkout - the API must still serve, and `/`
    # returns the guidance message rather than 500.
    r = serve_client.get("/")
    assert r.status_code == 200
    assert "ui is not built" in r.text.lower()
    assert "make playground" in r.text


def test_serves_spa_when_built(
    env: SimpleNamespace,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With a built bundle, `/` serves index.html, unknown client routes fall back
    # to it (SPA routing), and the scoped /api still wins over the catch-all.
    static = env.tmp / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>WG-BUILT</title>")
    monkeypatch.setattr("whygraph.serve.app._STATIC_DIR", static)
    with portal_with_project(env, nodes=_NODES, edges=_EDGES) as client:
        assert "WG-BUILT" in client.get("/").text
        assert "WG-BUILT" in client.get("/some/client/route").text
        assert client.get("/api/tree").status_code == 200
