"""Platform configuration: the ``instance_settings`` row and the sign-up allowlist.

Step 21 of plan 59 (§4.13): the config routes, the cache, the allowlist routes
with GitHub id resolution, and Erase deleting the allowlist rows. Step 22 adds
the policy cases here.
"""

# ruff: noqa: F811 -- pytest fixtures are imported from other test modules

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Iterator

import httpx
import pytest
from sqlalchemy import text
from sqlmodel import select

from github_fake import FakeGitHub
from test_portal_app import (  # noqa: F401 -- fixtures
    GitServer,
    admin_request,
    at,
    claim_instance,
    env,
    github_admin,
    github_app_key,
    github_fake,
    github_git_server,
    github_sign_in,
    portal_client,
    prod_portal,
    production_env,
    wait_scan,
)
from test_portal_github_import import (  # noqa: F401 -- `app_env` is a fixture
    API_REPO,
    POISONED,
    World,
    app_env,
    connected,
    import_repo,
    project_row,
)
from test_portal_identity_routes import audit_log, create_org, events  # noqa: F401
from whygraph.portal import auth_routes
from whygraph.portal import db as portal_db
from whygraph.portal import instance_settings
from whygraph.portal.github_auth import GitHubRateLimited
from whygraph.portal.models import (
    InstanceSetting,
    Invitation,
    Membership,
    Organization,
    Project,
    SignupAllowlist,
    User,
)
from whygraph.portal.runner import RunnerUnavailable

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


# ---------------------------------------------------------------------------
# Policy (step 22): sign-up, invitations, org creation, quotas, portal state
# ---------------------------------------------------------------------------


def user_row(login: str) -> User | None:
    with portal_db.get_session() as db:
        row = db.exec(select(User).where(User.github_login == login)).first()
        if row is not None:
            db.expunge(row)
        return row


def sign_in_as(w: SimpleNamespace, login: str) -> httpx.Response:
    """A fresh browser (no cookies) signing ``login`` in with GitHub."""
    w.client.cookies.clear()
    return github_sign_in(w.client, login)


def org_with_invitation(w: SimpleNamespace, slug: str, login: str) -> int:
    """Root creates ``slug`` and invites ``login`` (no account); the org id."""
    assert sign_in_as(w, "root").status_code == 200
    assert create_org(w.client, slug, slug.title()).status_code == 201
    invited = w.client.post(
        at(slug) + "/api/org/members", json={"github_login": login, "role": "member"}
    )
    assert invited.status_code == 201, invited.text
    assert invited.json()["pending"] is True
    w.client.cookies.clear()
    with portal_db.get_session() as db:
        return db.exec(select(Organization.id).where(Organization.slug == slug)).one()


def test_policy_closed_refuses_a_new_github_id_and_admits_an_existing_one(
    world: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    assert sign_in_as(world, "cy").status_code == 200
    assert put(world, {"signup_policy": "closed"}).status_code == 200

    refused = sign_in_as(world, "dee")
    assert_code(refused, 403, "signups_closed")
    assert "whygraph_session" not in refused.cookies
    assert user_row("dee") is None  # nothing was written
    (event,) = [e for e in events(audit_log) if e["event"] == "github_signin_refused"]
    assert event["reason"] == "signups_closed" and event["github_login"] == "dee"

    assert sign_in_as(world, "cy").status_code == 200  # an existing account
    assert sign_in_as(world, "root").status_code == 200


def test_policy_closed_still_answers_account_deleted_to_a_deleted_user(
    world: SimpleNamespace,
) -> None:
    assert sign_in_as(world, "cy").status_code == 200
    cy = user_row("cy")
    assert cy is not None
    deleted = call(world, "DELETE", f"/api/admin/users/{cy.uid}", {"confirm": "cy"})
    assert deleted.status_code == 200, deleted.text
    assert put(world, {"signup_policy": "closed"}).status_code == 200
    assert_code(sign_in_as(world, "cy"), 403, "account_deleted")


def test_policy_invite_only_admits_an_invited_id(world: SimpleNamespace) -> None:
    org_id = org_with_invitation(world, "acme", "dee")
    assert put(world, {"signup_policy": "invite_only"}).status_code == 200
    assert sign_in_as(world, "dee").status_code == 200
    dee = user_row("dee")
    assert dee is not None
    with portal_db.get_session() as db:
        assert db.get(Membership, (org_id, dee.id)) is not None  # redeemed


def test_policy_invite_only_refuses_an_uninvited_id(world: SimpleNamespace) -> None:
    assert put(world, {"signup_policy": "invite_only"}).status_code == 200
    assert_code(sign_in_as(world, "cy"), 403, "signups_closed")
    assert user_row("cy") is None


def test_policy_invite_only_admits_an_allowlisted_id_once(
    world: SimpleNamespace,
) -> None:
    assert add(world, "dee").status_code == 200
    assert add(world, "cy").status_code == 200
    assert put(world, {"signup_policy": "invite_only"}).status_code == 200
    assert sign_in_as(world, "dee").status_code == 200
    with portal_db.get_session() as db:
        entries = {
            e.github_login: e.used_at for e in db.exec(select(SignupAllowlist)).all()
        }
    assert entries["dee"] is not None and entries["cy"] is None
    listed = {e["github_login"]: e for e in call(world, "GET", ALLOWLIST).json()}
    assert listed["dee"]["used_at"] is not None
    # A used entry admits nobody.
    with portal_db.get_session() as db:
        db.exec(
            text("UPDATE signup_allowlist SET used_at = '2026-11-01T00:00:00+00:00'")
        )  # type: ignore[call-overload]
    assert_code(sign_in_as(world, "cy"), 403, "signups_closed")
    assert user_row("cy") is None


def test_policy_the_allowlisted_login_under_another_id_is_refused(
    world: SimpleNamespace,
) -> None:
    """P17: the entry names a GitHub id; a login re-registered by someone else is not it."""
    assert add(world, "cy").status_code == 200
    world.fake.remove_user("cy")
    impostor = world.fake.add_user("cy")
    assert put(world, {"signup_policy": "invite_only"}).status_code == 200
    assert_code(sign_in_as(world, "cy"), 403, "signups_closed")
    assert user_row("cy") is None
    with portal_db.get_session() as db:
        github_id, used_at = db.exec(
            select(SignupAllowlist.github_id, SignupAllowlist.used_at)
        ).one()
    assert github_id != impostor.id and used_at is None


def test_policy_an_invitation_of_a_deleted_or_suspended_org_does_not_admit(
    world: SimpleNamespace,
) -> None:
    org_id = org_with_invitation(world, "acme", "dee")
    assert put(world, {"signup_policy": "invite_only"}).status_code == 200
    for column in ("suspended_at", "deleted_at"):
        with portal_db.get_session() as db:
            db.exec(  # type: ignore[call-overload]
                text(
                    f"UPDATE organizations SET {column} = '2026-11-01T00:00:00+00:00' "
                    "WHERE id = :id"
                ).bindparams(id=org_id)
            )
        assert_code(sign_in_as(world, "dee"), 403, "signups_closed")
        assert user_row("dee") is None
        with portal_db.get_session() as db:
            db.exec(  # type: ignore[call-overload]
                text(
                    f"UPDATE organizations SET {column} = NULL WHERE id = :id"
                ).bindparams(id=org_id)
            )
    assert sign_in_as(world, "dee").status_code == 200  # the org is live again


def test_policy_a_refused_sign_in_leaves_another_rows_login_unchanged(
    world: SimpleNamespace,
) -> None:
    assert sign_in_as(world, "cy").status_code == 200
    first = user_row("cy")
    assert first is not None
    # Cy renames on GitHub; someone new registers the freed login.
    world.fake.rename_user("cy", "cy-renamed")
    world.fake.add_user("cy")
    assert put(world, {"signup_policy": "closed"}).status_code == 200
    assert_code(sign_in_as(world, "cy"), 403, "signups_closed")
    with portal_db.get_session() as db:
        logins = db.exec(select(User.github_login).where(User.id == first.id)).one()
        rows = db.exec(select(User.id).where(User.github_login == "cy")).all()
    assert logins == "cy"  # not released
    assert rows == [first.id]


def test_policy_closed_refuses_a_new_account_invitation(
    world: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    """D32: under ``closed`` a person with no account could never redeem it."""
    assert sign_in_as(world, "cy").status_code == 200  # cy has an account
    assert sign_in_as(world, "root").status_code == 200
    assert create_org(world.client, "acme", "Acme").status_code == 201
    assert put(world, {"signup_policy": "closed"}).status_code == 200

    def member(login: str) -> httpx.Response:
        return world.client.post(
            at("acme") + "/api/org/members",
            json={"github_login": login, "role": "member"},
        )

    assert_code(member("dee"), 409, "signups_closed")
    with portal_db.get_session() as db:
        assert db.exec(select(Invitation)).all() == []
    (event,) = [e for e in events(audit_log) if e["event"] == "member_add_refused"]
    assert event["reason"] == "signups_closed" and event["github_login"] == "dee"
    # An existing account is still added.
    added = member("cy")
    assert added.status_code == 201, added.text
    assert "pending" not in added.json()
    # Under invite_only the invitation is what admits them.
    assert put(world, {"signup_policy": "invite_only"}).status_code == 200
    invited = member("dee")
    assert invited.status_code == 201, invited.text
    assert invited.json()["pending"] is True


def test_policy_org_creation_restricted_on_both_routes(world: SimpleNamespace) -> None:
    assert put(world, {"org_creation": "admins"}).status_code == 200
    assert sign_in_as(world, "cy").status_code == 200
    assert_code(create_org(world.client, "acme"), 403, "org_creation_restricted")
    assert_code(
        world.client.get(at() + "/api/orgs/slug-check", params={"slug": "acme"}),
        403,
        "org_creation_restricted",
    )
    with portal_db.get_session() as db:
        assert db.exec(select(Organization)).all() == []
    # An instance admin still may, on both routes.
    assert sign_in_as(world, "root").status_code == 200
    check = world.client.get(at() + "/api/orgs/slug-check", params={"slug": "acme"})
    assert check.status_code == 200 and check.json()["available"] is True
    assert create_org(world.client, "acme").status_code == 201


def test_policy_org_quota_counts_live_owned_orgs(world: SimpleNamespace) -> None:
    assert put(world, {"max_orgs_per_user": 1}).status_code == 200
    assert sign_in_as(world, "cy").status_code == 200
    assert create_org(world.client, "first").status_code == 201
    refused = create_org(world.client, "second")
    assert_code(refused, 409, "org_quota_reached")
    assert (refused.json()["limit"], refused.json()["current"]) == (1, 1)
    # A soft-deleted org no longer counts (D43: live orgs only).
    with portal_db.get_session() as db:
        db.exec(  # type: ignore[call-overload]
            text(
                "UPDATE organizations SET deleted_at = '2026-11-01T00:00:00+00:00' "
                "WHERE slug = 'first'"
            )
        )
    assert create_org(world.client, "second").status_code == 201
    # Instance admins are not exempt on this page (D43).
    assert sign_in_as(world, "root").status_code == 200
    assert create_org(world.client, "root-one").status_code == 201
    assert_code(create_org(world.client, "root-two"), 409, "org_quota_reached")


def test_policy_org_quota_holds_under_two_concurrent_creates(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller's row is locked once, FOR UPDATE: one create wins, no deadlock."""
    assert put(world, {"max_orgs_per_user": 1}).status_code == 200
    assert sign_in_as(world, "cy").status_code == 200
    barrier = threading.Barrier(2, timeout=30)
    real = auth_routes.validate_org_slug

    def validate(slug: str) -> None:
        barrier.wait()  # both requests are about to open their transaction
        real(slug)

    monkeypatch.setattr(auth_routes, "validate_org_slug", validate)
    results: list[httpx.Response] = []
    threads = [
        threading.Thread(
            target=lambda s=slug: results.append(create_org(world.client, s))
        )
        for slug in ("one", "two")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(r.status_code for r in results) == [201, 409], [
        r.text for r in results
    ]
    (refused,) = [r for r in results if r.status_code == 409]
    assert refused.json()["code"] == "org_quota_reached"
    with portal_db.get_session() as db:
        assert len(db.exec(select(Organization)).all()) == 1


def test_policy_portal_state_carries_the_banner_and_can_create_org(
    world: SimpleNamespace,
) -> None:
    def state() -> dict:
        return world.client.get(at() + "/api/portal/state").json()

    world.client.cookies.clear()
    body = state()
    assert body["announcement"] is None
    assert body["org_creation"] == "everyone"
    assert body["can_create_org"] is False  # signed out

    assert sign_in_as(world, "cy").status_code == 200
    assert state()["can_create_org"] is True
    text_with_markup = "Maintenance <b>tonight</b>"
    changed = put(
        world,
        {
            "banner_text": text_with_markup,
            "banner_level": "warning",
            "org_creation": "admins",
        },
    )
    assert changed.status_code == 200, changed.text
    body = state()
    assert body["announcement"] == {"text": text_with_markup, "level": "warning"}
    assert body["org_creation"] == "admins" and body["can_create_org"] is False
    assert sign_in_as(world, "root").status_code == 200
    assert state()["can_create_org"] is True
    world.client.cookies.clear()
    signed_out = state()
    assert signed_out["announcement"]["text"] == text_with_markup  # public
    assert signed_out["can_create_org"] is False


def test_policy_local_portal_state_has_no_policy_fields(env: SimpleNamespace) -> None:
    with portal_client() as client:
        body = client.get("/api/portal/state").json()
    for key in ("announcement", "org_creation", "can_create_org"):
        assert key not in body


# ---------------------------------------------------------------------------
# Policy: the project quota (production imports through the GitHub App)
# ---------------------------------------------------------------------------


@pytest.fixture
def importer(app_env: GitServer, production_env: SimpleNamespace) -> Iterator[World]:
    """Ada claims the instance; Ben signs in with GitHub, creates ``acme`` and connects."""
    with prod_portal() as client:
        claim_instance(client)
        client.cookies.clear()
        assert github_sign_in(client, "ben").status_code == 200
        assert create_org(client, "acme", "Acme").status_code == 201
        w = World(client, app_env, production_env)
        connected(w)
        yield w


def set_project_quota(w: World, limit: int | None) -> None:
    response = admin_request(w.client, "PUT", CONFIG, {"max_projects_per_org": limit})
    assert response.status_code == 200, response.text


def refuse_runs(w: World, patched: pytest.MonkeyPatch) -> None:
    async def refuse(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RunnerUnavailable("the scan runner is not running")

    patched.setattr(w.state.runner, "request_import", refuse)


def test_policy_project_quota_is_enforced_on_import_and_spares_a_retry(
    importer: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = importer
    set_project_quota(w, 1)
    with monkeypatch.context() as patched:
        refuse_runs(w, patched)
        first = import_repo(w, API_REPO)
        assert first.status_code == 201, first.text
        refused = import_repo(w, POISONED)
    assert_code(refused, 409, "project_quota_reached")
    assert (refused.json()["limit"], refused.json()["current"]) == (1, 1)
    assert project_row("acme", "poisoned") is None

    # The org is at its limit, and the Retry of the existing import still runs.
    url = at("acme") + "/api/projects/api"
    retry = w.client.post(f"{url}/scans", json={})
    assert retry.status_code == 202, retry.text
    run = wait_scan(w.client, url, retry.json()["run_id"])
    assert run["status"] == "ok", run

    # A soft-deleted project no longer counts.
    with portal_db.get_session() as db:
        db.exec(  # type: ignore[call-overload]
            text("UPDATE projects SET deleted_at = '2026-11-01T00:00:00+00:00'")
        )
    with monkeypatch.context() as patched:
        refuse_runs(w, patched)
        assert import_repo(w, POISONED).status_code == 201


def test_policy_project_quota_holds_under_two_concurrent_imports(
    importer: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = importer
    set_project_quota(w, 1)
    refuse_runs(w, monkeypatch)
    barrier = threading.Barrier(2, timeout=30)
    real_hit = w.state.import_org.hit

    def hit(key, **kwargs):  # noqa: ANN001, ANN202
        barrier.wait()  # both requests are about to insert their row
        return real_hit(key, **kwargs)

    monkeypatch.setattr(w.state.import_org, "hit", hit)
    results: list[httpx.Response] = []
    threads = [
        threading.Thread(target=lambda r=repo: results.append(import_repo(w, r)))
        for repo in (API_REPO, POISONED)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(r.status_code for r in results) == [201, 409], [
        r.text for r in results
    ]
    (refused,) = [r for r in results if r.status_code == 409]
    assert refused.json()["code"] == "project_quota_reached"
    with portal_db.get_session() as db:
        assert len(db.exec(select(Project.id)).all()) == 1
