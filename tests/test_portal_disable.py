"""Disabling an account (M2d-1 plan sections 4.6 and 5.4).

Driven through ``PATCH /api/admin/users/{uid}`` on the ``world`` of
``test_portal_identity_routes.py``: Ada (the password instance admin), Ann
(GitHub, owner of ``quokka``), Bob (GitHub, owner of ``narwhal``) and Pat (a
password account in no org).
"""

# ruff: noqa: F811 -- pytest fixtures are imported from the sibling test modules

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from test_portal_app import (  # noqa: F401 -- fixtures
    PROD_PASSWORD,
    at,
    env,
    github_fake,
    github_sign_in,
    log_in,
    production_env,
)
from test_portal_identity_routes import (  # noqa: F401 -- fixtures
    COOKIE,
    LOGIN_REQUIRED,
    audit_log,
    claimed,
    cookie_of,
    events,
    fresh,
    reset_link,
    session_count,
    signed_in_admin,
    token_in,
    user_row,
    world,
)
from whygraph.portal import db as portal_db
from whygraph.portal.models import Membership, User

DISABLED = {"error": "this account is disabled", "code": "account_disabled"}


def admin_patch(client: TestClient, uid: str, **body) -> httpx.Response:
    """``PATCH /api/admin/users/{uid}`` (the caller must be an instance admin)."""
    return client.patch(at() + f"/api/admin/users/{uid}", json=body)


def disable(world: SimpleNamespace, uid: str, disabled: bool = True) -> dict:
    """Disable (or enable) ``uid`` as Ada; leave the client signed out."""
    response = admin_patch(signed_in_admin(world.client), uid, disabled=disabled)
    assert response.status_code == 200, response.text
    world.client.cookies.clear()
    return response.json()


def get_with(client: TestClient, token: str, url: str) -> httpx.Response:
    """GET ``url`` with ``token`` as the only session cookie."""
    client.cookies.clear()
    return client.get(url, headers={"Cookie": f"{COOKIE}={token}"})


def test_disabling_ends_every_session_on_the_next_request(
    world: SimpleNamespace,
) -> None:
    client = world.client
    tokens = []
    for _ in range(2):  # two browsers
        client.cookies.clear()
        tokens.append(cookie_of(github_sign_in(client, "ann")))
    assert session_count(login="ann") == 2
    for token in tokens:
        assert (
            get_with(client, token, at("quokka") + "/api/projects").status_code == 200
        )
    row = disable(world, world.ann)
    assert row["disabled"] is True and row["github_login"] == "ann"
    assert session_count(login="ann") == 0
    for token in tokens:
        for url in (at() + "/api/account", at("quokka") + "/api/projects"):
            response = get_with(client, token, url)
            assert response.status_code == 401, (url, response.text)
            assert response.json() == LOGIN_REQUIRED


def test_a_surviving_session_of_a_disabled_user_is_signed_out(
    world: SimpleNamespace,
) -> None:
    """A session made in a race with the disable still resolves to nobody."""
    client = world.client
    token = cookie_of(github_sign_in(client, "ann"))
    with portal_db.get_session() as session:
        user = session.exec(select(User).where(User.uid == world.ann)).one()
        user.disabled_at = "2026-10-04T00:00:00+00:00"  # no revoke
    assert session_count(login="ann") == 1
    response = get_with(client, token, at() + "/api/account")
    assert response.status_code == 401 and response.json() == LOGIN_REQUIRED


def test_a_disabled_github_account_cannot_sign_in(world: SimpleNamespace) -> None:
    disable(world, world.ann)
    response = github_sign_in(world.client, "ann")
    assert response.status_code == 403 and response.json() == DISABLED
    assert session_count(login="ann") == 0


def test_a_disabled_password_account_is_refused_only_after_its_password(
    world: SimpleNamespace,
) -> None:
    client = world.client
    disable(world, world.pat)
    wrong = log_in(client, "pat@example.com", "not pat's passphrase at all")
    assert wrong.status_code == 401 and wrong.json()["code"] == "bad_credentials"
    right = log_in(client, "pat@example.com", PROD_PASSWORD)
    assert right.status_code == 403 and right.json() == DISABLED
    assert "set-cookie" not in right.headers
    assert session_count("pat@example.com") == 0


def test_a_reset_is_refused_for_a_disabled_account(world: SimpleNamespace) -> None:
    client = world.client
    # A link issued before the disable dies with it.
    killed = token_in(reset_link(signed_in_admin(client), world.pat).json()["url"])
    disable(world, world.pat)
    body = {"token": killed, "password": "pat's brand new passphrase"}
    dead = client.post(at() + "/api/auth/reset", json=body)
    assert dead.status_code == 400 and dead.json()["code"] == "bad_token"
    # The admin cannot issue one while it is disabled.
    refused = reset_link(signed_in_admin(client), world.pat)
    assert refused.status_code == 409 and refused.json()["code"] == "user_disabled"
    # A live link racing the disable is refused by the reset itself.
    disable(world, world.pat, False)
    raw = token_in(reset_link(signed_in_admin(client), world.pat).json()["url"])
    client.cookies.clear()
    with portal_db.get_session() as session:
        user = session.exec(select(User).where(User.uid == world.pat)).one()
        user.disabled_at = "2026-10-04T00:00:00+00:00"
    body["token"] = raw
    response = client.post(at() + "/api/auth/reset", json=body)
    assert response.status_code == 403 and response.json() == DISABLED
    assert user_row("pat@example.com").password_changed_at is None


def test_disabling_keeps_memberships_and_enabling_restores_access(
    world: SimpleNamespace,
) -> None:
    client = world.client
    disable(world, world.ann)
    with portal_db.get_session() as session:
        user_id = session.exec(select(User.id).where(User.uid == world.ann)).one()
        roles = session.exec(
            select(Membership.role).where(Membership.user_id == user_id)
        ).all()
    assert roles == ["owner"]
    assert user_row(login="ann").github_id is not None
    users = signed_in_admin(client).get(at() + "/api/admin/users").json()
    assert {u["uid"]: u["disabled"] for u in users}[world.ann] is True
    client.cookies.clear()
    row = disable(world, world.ann, False)
    assert row["disabled"] is False
    assert github_sign_in(client, "ann").status_code == 200
    assert client.get(at("quokka") + "/api/projects").status_code == 200


def test_the_admin_patch_needs_at_least_one_known_field(
    world: SimpleNamespace,
) -> None:
    client = signed_in_admin(world.client)
    for body in ({}, {"is_instance_admin": None, "disabled": None}, {"email": "x"}):
        response = admin_patch(client, world.pat, **body)
        assert response.status_code == 422, (body, response.text)
    both = admin_patch(client, world.pat, is_instance_admin=True, disabled=True)
    assert both.status_code == 200, both.text
    assert (both.json()["is_instance_admin"], both.json()["disabled"]) == (True, True)


def test_you_cannot_disable_yourself(world: SimpleNamespace) -> None:
    client = signed_in_admin(world.client)
    response = admin_patch(client, world.ada, disabled=True)
    assert response.status_code == 409 and response.json()["code"] == "self_disable"
    assert client.get(at() + "/api/admin/users").status_code == 200


def test_the_last_enabled_admin_counts_only_enabled_admins(
    world: SimpleNamespace,
) -> None:
    client = signed_in_admin(world.client)
    assert admin_patch(client, world.pat, is_instance_admin=True).status_code == 200
    assert admin_patch(client, world.pat, disabled=True).status_code == 200
    # Pat is still flagged admin, but disabled: Ada is the last enabled one.
    for body in ({"is_instance_admin": False}, {"disabled": True}):
        response = admin_patch(client, world.ada, **body)
        assert response.status_code == 409, (body, response.text)
    demote = admin_patch(client, world.ada, is_instance_admin=False)
    assert demote.json()["code"] == "last_admin"
    assert user_row("ada@example.com").is_instance_admin is True
    # Re-enabled, Pat is an admin again and Ada may step down.
    assert admin_patch(client, world.pat, disabled=False).status_code == 200
    assert admin_patch(client, world.ada, is_instance_admin=False).status_code == 200


def test_disable_and_enable_are_audited(
    world: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    client = signed_in_admin(world.client)
    audit_log.clear()
    admin_patch(client, world.bob, disabled=True)
    admin_patch(client, world.bob, disabled=True)  # unchanged: no event
    admin_patch(client, world.bob, disabled=False)
    records = [r for r in events(audit_log) if r["event"].startswith("user_")]
    assert [(r["event"], r["target"]) for r in records] == [
        ("user_disabled", world.bob),
        ("user_enabled", world.bob),
    ]
    assert all(r["uid"] == world.ada for r in records)
