"""Committed plans over REST (plan 61 section 9.2 case 17a).

``GET /api/projects/{slug}/plans`` and ``/{stem}`` through a production
portal with a member and a viewer on one project. The route inventories
(``ROUTE_ACTIONS``, ``ROUTE_REQUESTS``, ``REFUSED`` for a linked project)
live beside the other route tables.
"""

# ruff: noqa: F811 -- pytest fixtures (`api`, ...) are imported

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from test_mcp_plans_read import HARNESS, WIDGET, add_commit, add_plan, link
from test_portal_app import (  # noqa: F401 -- fixtures
    at,
    env,
    github_fake,
    manual_ctx,
    production_env,
)
from test_portal_identity_routes import audit_log  # noqa: F401 -- fixture
from test_portal_members import be, team  # noqa: F401 -- fixture
from test_portal_notes import api  # noqa: F401 -- fixture
from whygraph.core.context import use_project

P = "/api/projects/api/plans"


def call(t: SimpleNamespace, who: str, path: str, **kw: Any) -> Any:
    be(t, who)
    return t.client.get(at("acme") + path, **kw)


@pytest.fixture
def seeded(api: SimpleNamespace) -> SimpleNamespace:
    """Three plans: one shipped (a linked commit), two not implemented, one gone."""
    with use_project(manual_ctx(api.root, "api", "acme")):
        add_plan("tune-widget", WIDGET, updated="2026-01-03T00:00:00+00:00")
        add_commit("c" * 40, ["src/app/widget.py"])
        link("tune-widget", "commit", "c" * 40)
        add_plan("harness-plan", HARNESS, updated="2026-01-02T00:00:00+00:00")
        add_plan("old-100", WIDGET, present=0, updated="2026-01-09T00:00:00+00:00")
    return api


def test_list_filters_pages_and_orders(seeded: SimpleNamespace) -> None:
    t = seeded
    body = call(t, "dee", P).json()
    assert body["dir"] == "docs/plans" and body["total"] == 3
    # Present rows first, then newest; "old-100" is gone from HEAD so it is last.
    assert [p["stem"] for p in body["plans"]] == [
        "tune-widget",
        "harness-plan",
        "old-100",
    ]
    first = body["plans"][0]
    assert first["status"] == "shipped" and first["present"] is True
    assert first["uri"] == "whygraph://plan/tune-widget"
    assert {"title", "path", "objective", "updated_at"} <= first.keys()
    assert "link" not in first and "commits" not in first
    # Status filters before the page is sliced.
    shipped = call(t, "dee", P, params={"status": "shipped"}).json()
    assert [p["stem"] for p in shipped["plans"]] == ["tune-widget"]
    assert shipped["total"] == 1
    # Pages report the filtered total.
    page = call(t, "dee", P, params={"limit": 1, "offset": 1}).json()
    assert [p["stem"] for p in page["plans"]] == ["harness-plan"]
    assert page["total"] == 3
    # q is escaped: a literal underscore or percent matches nothing.
    assert call(t, "dee", P, params={"q": "widget"}).json()["total"] >= 1
    assert call(t, "dee", P, params={"q": "100%"}).json()["total"] == 0
    assert call(t, "dee", P, params={"q": "tune_widget"}).json()["total"] == 0
    assert call(t, "dee", P, params={"q": "tune-widget"}).json()["total"] == 1
    # Bad parameters are refused, not guessed.
    assert call(t, "dee", P, params={"status": "bogus"}).status_code == 422
    assert call(t, "dee", P, params={"limit": 0}).status_code == 422
    assert call(t, "dee", P, params={"q": "x" * 201}).status_code == 422


def test_an_empty_index_lists_nothing_and_names_the_dir(api: SimpleNamespace) -> None:
    body = call(api, "dee", P).json()
    assert body == {"plans": [], "total": 0, "dir": "docs/plans"}


def test_one_plan_carries_body_digest_outline_and_links(
    seeded: SimpleNamespace,
) -> None:
    t = seeded
    response = call(t, "dee", f"{P}/tune-widget")
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["stem"] == "tune-widget" and plan["status"] == "shipped"
    assert plan["body"] == WIDGET and plan["truncated"] is False
    assert plan["digest"]
    assert plan["implementing_commits"][0]["sha"] == "c" * 8
    assert {"n", "level", "title", "part", "step"} == set(plan["outline"][0])
    assert "contents" not in plan
    # Harness steps are outline entries with their step number.
    harness = call(t, "dee", f"{P}/harness-plan").json()
    assert any(e["step"] for e in harness["outline"])


def test_unknown_or_bad_stem_is_404_plan_not_found(seeded: SimpleNamespace) -> None:
    for stem in ("nope", "UPPER", "a" * 120):
        response = call(seeded, "dee", f"{P}/{stem}")
        assert response.status_code == 404, stem
        assert response.json()["code"] == "plan_not_found", response.text


def test_a_viewer_may_read(seeded: SimpleNamespace) -> None:
    assert call(seeded, "eve", P).status_code == 200
    assert call(seeded, "eve", f"{P}/tune-widget").status_code == 200


def test_plans_have_no_write_routes(seeded: SimpleNamespace) -> None:
    be(seeded, "ben")
    for method in ("post", "put", "patch", "delete"):
        r = getattr(seeded.client, method)(at("acme") + P)
        assert r.status_code in (404, 405)


def test_only_the_plans_route_asks_for_a_whole_plan() -> None:
    """No agent surface passes ``full=True`` (R15): the whole body is for people."""
    src = Path(__file__).parent.parent / "src" / "whygraph"
    pattern = re.compile(r"plan_record\([^)]*full\s*=\s*True", re.S)
    offenders = [
        str(p.relative_to(src))
        for p in src.rglob("*.py")
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == ["portal/plans_routes.py"]
