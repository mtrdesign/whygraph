"""The update-notice routes (plan 43 section 4.5, cases 9-11).

``GET /api/portal/state``'s ``update`` block, ``PUT /api/portal/update-check``
(local mode) and ``GET /api/admin/update`` (production, instance admin).
The loop is never relied on for the data: the switch and the releases are set
on ``state.updates`` directly.
"""

# ruff: noqa: F811 -- pytest fixtures imported from test_portal_app

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from github_fake import FakeGitHub
from test_portal_app import (  # noqa: F401 -- fixtures
    client,
    env,
    github_fake,
    github_sign_in,
    portal_client,
    prod_portal,
    production_env,
    ready,
)
from test_portal_app import at, claim_instance, log_in
from whygraph.core.releases import Release
from whygraph.portal import db as portal_db
from whygraph.portal import updates
from whygraph.portal.models import Setting

NOT_FOUND = {"error": "not found"}


def _wait_resolved(client: TestClient) -> None:
    """Wait for the loop's first tick, so it cannot overwrite a test's switch."""
    state = client.app.state.portal
    deadline = time.monotonic() + 10
    while state.updates.switch is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert state.updates.switch is not None


def _stored() -> bool:
    with portal_db.get_session() as session:
        return bool(session.get(Setting, 1).update_check)


def _inject(client: TestClient, version: tuple[int, int, int]) -> None:
    book = client.app.state.portal.updates
    book.record(
        book.begin(),
        [Release(version, datetime.now(UTC) - timedelta(days=2), False)],
        time.monotonic(),
        datetime.now(UTC),
    )


def test_the_state_block_is_null_before_setup(client: TestClient) -> None:
    assert client.get("/api/portal/state").json()["update"] is None


def test_the_state_block_after_setup_is_disabled_when_the_env_says_so(
    ready: TestClient,
) -> None:
    _wait_resolved(ready)
    block = ready.get("/api/portal/state").json()["update"]
    assert block["enabled"] is False
    assert block["source"] == "env"
    assert block["available"] is False
    assert block["latest"] is None


def test_the_put_toggles_persists_and_shows_an_available_release(
    ready: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wait_resolved(ready)
    monkeypatch.delenv("WHYGRAPH_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    on = ready.put("/api/portal/update-check", json={"enabled": True})
    assert on.status_code == 200, on.text
    assert on.json()["enabled"] is True
    assert on.json()["source"] == "setting"
    assert _stored() is True
    assert ready.app.state.portal.updates.switch == (True, "setting")
    _inject(ready, (999, 0, 0))
    block = ready.get("/api/portal/state").json()["update"]
    assert block["available"] is True
    assert block["latest"] == "999.0.0"
    assert block["notes_url"].endswith("/releases/999.0.0/")
    assert block["upgrading_url"] == updates.UPGRADING_URL
    off = ready.put("/api/portal/update-check", json={"enabled": False})
    assert off.status_code == 200
    assert off.json()["enabled"] is False
    assert off.json()["latest"] is None
    assert _stored() is False
    assert ready.app.state.portal.updates.switch == (False, "setting")


@pytest.mark.parametrize(
    "body",
    [{"enabled": "true"}, {"enabled": 1}, {}, {"enabled": True, "extra": 1}, []],
)
def test_the_put_refuses_a_bad_body(ready: TestClient, body: object) -> None:
    assert ready.put("/api/portal/update-check", json=body).status_code == 422
    assert _stored() is True


def test_the_put_needs_setup(client: TestClient) -> None:
    assert (
        client.put("/api/portal/update-check", json={"enabled": False}).status_code
        == 409
    )


@pytest.fixture
def claimed(github_fake: FakeGitHub, production_env: SimpleNamespace):
    github_fake.add_user("ann")
    with prod_portal() as prod:
        claim_instance(prod)
        prod.cookies.clear()
        yield prod


def test_production_state_is_null_even_for_an_instance_admin(
    claimed: TestClient,
) -> None:
    assert claimed.get(at() + "/api/portal/state").json()["update"] is None
    assert log_in(claimed, "ada@example.com").status_code == 200
    state = claimed.get(at() + "/api/portal/state").json()
    assert state["user"]["is_instance_admin"] is True
    assert state["update"] is None


def test_the_production_put_is_404_signed_in_or_not(claimed: TestClient) -> None:
    body = {"enabled": False}
    out = claimed.put(at() + "/api/portal/update-check", json=body)
    assert (out.status_code, out.json()) == (404, NOT_FOUND)
    log_in(claimed, "ada@example.com")
    out = claimed.put(at() + "/api/portal/update-check", json=body)
    assert (out.status_code, out.json()) == (404, NOT_FOUND)


def test_the_admin_update_status_matrix(claimed: TestClient) -> None:
    url = "/api/admin/update"
    nobody = claimed.get(at() + url)
    assert nobody.status_code == 401
    assert nobody.json()["code"] == "login_required"
    assert github_sign_in(claimed, "ann").status_code == 200
    assert claimed.get(at() + url).status_code == 403
    claimed.cookies.clear()
    assert log_in(claimed, "ada@example.com").status_code == 200
    ok = claimed.get(at() + url)
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert set(body) == {
        "enabled",
        "source",
        "current",
        "latest",
        "available",
        "manual_steps",
        "notes_url",
        "upgrading_url",
        "checked_at",
        "error",
    }
    assert body["upgrading_url"] == updates.UPGRADING_URL
    assert body["available"] is False
    # An org host does not serve the admin routes.
    assert claimed.get(at("quokka") + url).status_code == 404


def test_the_admin_update_route_is_404_in_local_mode(ready: TestClient) -> None:
    out = ready.get("/api/admin/update")
    assert out.status_code == 404
