"""Platform configuration: the ``instance_settings`` row and the sign-up allowlist.

Step 21 of plan 59 (§4.13): the config routes, the cache, the allowlist routes
with GitHub id resolution, and Erase deleting the allowlist rows. Step 22 adds
the policy cases here.
"""

# ruff: noqa: F811 -- pytest fixtures are imported from other test modules

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text
from sqlmodel import select

from github_fake import FakeGitHub
from test_portal_app import (  # noqa: F401 -- fixtures
    admin_request,
    claim_instance,
    env,
    github_admin,
    github_fake,
    github_sign_in,
    prod_portal,
    production_env,
)
from test_portal_identity_routes import audit_log, events  # noqa: F401 -- fixture
from whygraph.portal import db as portal_db
from whygraph.portal import instance_settings
from whygraph.portal.github_auth import GitHubRateLimited
from whygraph.portal.models import InstanceSetting, SignupAllowlist, User

CONFIG = "/api/admin/config"
ALLOWLIST = "/api/admin/config/signup-allowlist"

DEFAULTS = {
    "signup_policy": "open",
    "org_creation": "everyone",
    "max_orgs_per_user": None,
    "max_projects_per_org": None,
    "banner_text": None,
    "banner_level": "info",
    "updated_at": None,
    "updated_by": None,
}


@pytest.fixture
def world(github_fake: FakeGitHub) -> SimpleNamespace:
    """A production portal with one GitHub administrator ("root") and a few known accounts."""
    for login in ("root", "cy", "dee"):
        github_fake.add_user(login)
    with prod_portal() as client:
        claim_instance(client)
        client.cookies.clear()
        admin_uid = github_admin(client, "root")
        client.cookies.clear()
        with portal_db.get_session() as db:
            admin_id = db.exec(select(User.id).where(User.uid == admin_uid)).one()
        yield SimpleNamespace(
            client=client, fake=github_fake, admin_uid=admin_uid, admin_id=admin_id
        )


def call(
    w: SimpleNamespace, method: str, path: str, body: dict | None = None
) -> httpx.Response:
    return admin_request(w.client, method, path, body, user_id=w.admin_id)


def put(w: SimpleNamespace, body: dict) -> httpx.Response:
    return call(w, "PUT", CONFIG, body)


def assert_code(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["code"] == code, response.text


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_with_no_row_answers_the_defaults(world: SimpleNamespace) -> None:
    with portal_db.get_session() as db:
        assert db.get(InstanceSetting, 1) is None
    response = call(world, "GET", CONFIG)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["settings"] == DEFAULTS
    assert world.client.app.state.portal.instance_settings == (
        instance_settings.InstanceSettings()
    )


def test_config_get_has_no_secret_and_no_file_path(world: SimpleNamespace) -> None:
    body = call(world, "GET", CONFIG).json()
    instance = body["instance"]
    assert set(instance) == {
        "base_url",
        "base_check",
        "version",
        "update",
        "github_app",
        "oauth_app",
        "data_disk",
        "postgres_bytes",
    }
    assert set(instance["github_app"]) == {"slug", "client_id"}
    assert set(instance["oauth_app"]) == {"client_id"}
    assert instance["data_disk"]["total"] >= instance["data_disk"]["free"] > 0
    assert isinstance(instance["postgres_bytes"], int)
    dumped = call(world, "GET", CONFIG).text.lower()
    for word in ("secret", "private_key", "_file", ".pem", "password", "webhook"):
        assert word not in dumped, word


def test_config_put_upserts_and_swaps_the_cache(
    world: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    response = put(world, {"signup_policy": "closed", "max_orgs_per_user": 3})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["signup_policy"] == "closed" and body["max_orgs_per_user"] == 3
    assert body["updated_by"] == world.admin_uid  # a uid, not a row id
    assert body["updated_at"]
    cached = world.client.app.state.portal.instance_settings
    assert cached.signup_policy == "closed" and cached.max_orgs_per_user == 3
    with portal_db.get_session() as db:
        row = db.get(InstanceSetting, 1)
        assert row is not None and row.signup_policy == "closed"
    # A second write updates the one row.
    assert put(world, {"org_creation": "admins"}).status_code == 200
    with portal_db.get_session() as db:
        assert len(db.exec(select(InstanceSetting)).all()) == 1
    got = call(world, "GET", CONFIG).json()["settings"]
    assert got["signup_policy"] == "closed" and got["org_creation"] == "admins"
    changed = [
        e for e in events(audit_log) if e["event"] == "instance_settings_changed"
    ]
    assert changed[0]["changes"] == {
        "max_orgs_per_user": [None, 3],
        "signup_policy": ["open", "closed"],
    }
    assert changed[1]["changes"] == {"org_creation": ["everyone", "admins"]}


def test_config_put_tells_an_absent_key_from_null(world: SimpleNamespace) -> None:
    assert (
        put(world, {"max_projects_per_org": 5, "banner_text": "hi"}).status_code == 200
    )
    assert put(world, {"signup_policy": "invite_only"}).status_code == 200
    state = world.client.app.state.portal.instance_settings
    assert (state.max_projects_per_org, state.banner_text) == (5, "hi")  # absent: kept
    assert put(world, {"max_projects_per_org": None}).status_code == 200
    state = world.client.app.state.portal.instance_settings
    assert state.max_projects_per_org is None and state.banner_text == "hi"
    assert state.signup_policy == "invite_only"


def test_config_put_validates(world: SimpleNamespace) -> None:
    assert put(world, {}).status_code == 422
    for bad in (
        {"signup_policy": "everyone"},
        {"signup_policy": None},
        {"org_creation": "nobody"},
        {"banner_level": "error"},
        {"max_orgs_per_user": 0},
        {"max_projects_per_org": -1},
        {"max_orgs_per_user": 1.5},
        {"max_orgs_per_user": True},
        {"banner_text": "x" * 501},
        {"nope": 1},
    ):
        assert put(world, bad).status_code == 422, bad
    assert put(world, {"banner_text": "x" * 500}).status_code == 200
    # Nothing refused was written.
    assert world.client.app.state.portal.instance_settings.signup_policy == "open"


def test_config_a_blank_banner_is_stored_as_null_and_the_event_truncates(
    world: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    assert put(world, {"banner_text": "y" * 200}).status_code == 200
    assert put(world, {"banner_text": "   "}).status_code == 200
    assert world.client.app.state.portal.instance_settings.banner_text is None
    with portal_db.get_session() as db:
        row = db.get(InstanceSetting, 1)
        assert row is not None and row.banner_text is None
    changed = [
        e for e in events(audit_log) if e["event"] == "instance_settings_changed"
    ]
    assert changed[0]["changes"]["banner_text"] == [None, "y" * 80]
    assert changed[1]["changes"]["banner_text"] == ["y" * 80, None]


def test_config_the_database_refuses_what_the_route_would(
    world: SimpleNamespace,
) -> None:
    with portal_db.get_session() as db:
        db.add(InstanceSetting(id=1))
    with pytest.raises(Exception):  # noqa: B017, PT011 -- an IntegrityError
        with portal_db.get_session() as db:
            db.exec(text("UPDATE instance_settings SET signup_policy = 'x'"))  # type: ignore[call-overload]
    with pytest.raises(Exception):  # noqa: B017, PT011
        with portal_db.get_session() as db:
            db.exec(text("INSERT INTO instance_settings (id) VALUES (2)"))  # type: ignore[call-overload]


def test_config_is_loaded_at_start_up(world: SimpleNamespace) -> None:
    assert (
        put(world, {"signup_policy": "closed", "banner_text": "down soon"}).status_code
        == 200
    )
    world.client.__exit__(None, None, None)  # release the instance lock
    with prod_portal() as second:
        loaded = second.app.state.portal.instance_settings
        assert loaded.signup_policy == "closed" and loaded.banner_text == "down soon"


def test_config_a_failed_load_at_start_up_degrades_and_never_opens(
    github_fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(db):  # noqa: ANN001
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(instance_settings, "load", boom)
    with prod_portal() as client:
        state = client.app.state.portal
        assert state.degraded is not None
        assert "platform configuration" in state.degraded
        assert "settings unreadable" in state.degraded
        assert "spend" not in state.degraded  # its own message, not _seed_usage's


def test_config_routes_need_an_admin_session(world: SimpleNamespace) -> None:
    for method, path in (
        ("GET", CONFIG),
        ("PUT", CONFIG),
        ("GET", ALLOWLIST),
        ("POST", ALLOWLIST),
        ("DELETE", f"{ALLOWLIST}/1"),
    ):
        response = world.client.request(
            method, f"{world.client.base_url}{path}", json={}
        )
        assert response.status_code == 401, (method, path)
        assert response.json()["code"] == "admin_login_required"


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


def add(w: SimpleNamespace, login: str, note: str | None = None) -> httpx.Response:
    return call(w, "POST", ALLOWLIST, {"github_login": login, "note": note})


def test_allowlist_add_resolves_the_github_id(
    world: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    response = add(world, "@Cy", "contractor, ACME")
    assert response.status_code == 200, response.text
    entry = response.json()
    assert entry["added"] is True
    assert entry["github_login"] == "cy" and entry["note"] == "contractor, ACME"
    assert entry["used_at"] is None and entry["created_by"] == world.admin_uid
    with portal_db.get_session() as db:
        github_id = db.exec(select(SignupAllowlist.github_id)).one()
    assert github_id == world.fake.users["cy"].id  # the id, not the login
    # A second add returns the existing entry.
    again = add(world, "cy").json()
    assert again["added"] is False and again["id"] == entry["id"]
    listed = call(world, "GET", ALLOWLIST).json()
    assert [e["github_login"] for e in listed] == ["cy"]
    assert "github_id" not in listed[0]
    added = [e for e in events(audit_log) if e["event"] == "signup_allowlist_changed"]
    assert len(added) == 1 and added[0]["added"] == "cy"


def test_allowlist_an_unknown_note_length_and_login_are_refused(
    world: SimpleNamespace,
) -> None:
    assert add(world, "cy", "n" * 201).status_code == 422
    assert_code(add(world, "not a login!"), 422, "bad_login")


def test_allowlist_maps_githubs_errors(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert_code(add(world, "nobody"), 404, "no_such_github_user")
    world.fake.force("users", status=500)
    assert_code(add(world, "dee"), 502, "github_unavailable")

    def limited(login: str, **_: object) -> None:
        raise GitHubRateLimited(42)

    monkeypatch.setattr(world.client.app.state.portal.github, "user_by_login", limited)
    refused = add(world, "dee")
    assert_code(refused, 503, "github_rate_limited")
    assert refused.headers["Retry-After"] == "42"
    with portal_db.get_session() as db:
        assert db.exec(select(SignupAllowlist)).all() == []


def test_allowlist_remove_audits_and_404s_when_gone(
    world: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    entry_id = add(world, "dee").json()["id"]
    assert call(world, "DELETE", f"{ALLOWLIST}/{entry_id}").status_code == 200
    assert call(world, "GET", ALLOWLIST).json() == []
    assert call(world, "DELETE", f"{ALLOWLIST}/{entry_id}").status_code == 404
    changed = [e for e in events(audit_log) if e["event"] == "signup_allowlist_changed"]
    assert [(e.get("added"), e.get("removed")) for e in changed] == [
        ("dee", None),
        (None, "dee"),
    ]


def test_allowlist_erase_deletes_the_erased_ids_entries(world: SimpleNamespace) -> None:
    assert github_sign_in(world.client, "cy").status_code == 200
    world.client.cookies.clear()
    with portal_db.get_session() as db:
        cy_uid = db.exec(select(User.uid).where(User.github_login == "cy")).one()
    add(world, "cy")
    add(world, "dee")
    deleted = call(world, "DELETE", f"/api/admin/users/{cy_uid}", {"confirm": "cy"})
    assert deleted.status_code == 200, deleted.text
    erased = call(world, "POST", f"/api/admin/users/{cy_uid}/erase", {"confirm": "cy"})
    assert erased.status_code == 200, erased.text
    assert [e["github_login"] for e in call(world, "GET", ALLOWLIST).json()] == ["dee"]
