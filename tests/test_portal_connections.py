"""Connection tokens and the ``/api/v1`` identity layer (M2e plan sections 4.2, 4.3, 5.3).

There are no ``/api/v1`` routes yet (they arrive with the consent and data
steps), so the ``net`` fixture runs a real production portal with a few
**test-only** routes put in front of its own (:data:`probe`): a project
route through :func:`~whygraph.portal.deps.v1_project_access`, a user route
through :func:`~whygraph.portal.deps.v1_user`, and two that only echo what
the guard resolved (``/api/v1/raw`` under the bearer prefix, ``/api/v1/meta``
the one public path). Everything else is the portal's: the guard, the
installed :class:`~whygraph.portal.deps.TokenIdentity`, the member,
project, org and admin routes that revoke tokens, the sweep.

The cast (``net``): Ada claims the instance (instance admin, password); Ben
signs in with GitHub and owns ``acme`` (projects ``api`` and ``web``); Cy is
a member of ``acme``; Dee owns ``bravo`` (project ``lib``). Tokens are made
with :func:`whygraph.portal.connections.issue` directly - the consent flow
is a later step.
"""

# ruff: noqa: F811 -- pytest fixtures (`env`, `production_env`, ...) are imported

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import anyio
import httpx
import pytest
from alembic import command
from fastapi import APIRouter, Depends, Request
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import select
from starlette.requests import Request as StarletteRequest

from conftest import builtin_org_id
from github_fake import FakeGitHub
from test_portal_app import (  # noqa: F401 -- fixtures
    CLIENT_HEADER,
    PORT,
    PROD_BASE,
    PROD_CLIENT,
    at,
    claim_instance,
    env,
    github_fake,
    github_sign_in,
    production_env,
    signed_in,
)
from test_portal_hosts_isolation import _insert_project
from test_portal_identity_routes import create_org
from whygraph.portal import connections
from whygraph.portal import db as portal_db
from whygraph.portal import orgs
from whygraph.portal.app import create_portal_app
from whygraph.portal.authz import Action
from whygraph.portal.deps import (
    ApiError,
    BoundProject,
    TokenIdentity,
    current_user,
    is_bearer_path,
    v1_project_access,
    v1_user,
)
from whygraph.portal.models import (
    REVOKED_REASONS,
    ConnectionToken,
    Organization,
    Project,
    ProjectGrant,
    User,
)
from whygraph.portal.security import Principal
from whygraph.services.git import redact_tokens

CONNECTIONS_REVISION = "c4e7a19b52d8"
PREVIOUS_REVISION = "73bf248ea6fd"
HEAD_REVISION = "e46b50366a4c"


# ---------------------------------------------------------------------------
# The test-only routes and the fixture
# ---------------------------------------------------------------------------

probe = APIRouter()
"""Test-only ``/api/v1`` routes, put in front of the portal's own routes."""


def _who(principal: Principal | None) -> dict | None:
    if principal is None:
        return None
    return {
        "uid": principal.uid,
        "token_id": principal.token_id,
        "token_project_id": principal.token_project_id,
        "session_id": principal.session_id,
        "is_instance_admin": principal.is_instance_admin,
    }


def _raw(request: Request) -> dict:
    state = request.scope.get("state", {})
    refusal = state.get("token_refusal")
    return {
        "principal": _who(state.get("principal")),
        "refusal": None if refusal is None else refusal.code,
    }


@probe.get("/api/v1/projects/{slug}/probe")
def _project_probe(
    project: BoundProject = Depends(v1_project_access(Action.PROJECT_READ)),
    principal: Principal = Depends(v1_user),
) -> dict:
    return {"project_id": project.id, "slug": project.slug, **(_who(principal) or {})}


@probe.get("/api/v1/probe")
def _user_probe(principal: Principal = Depends(v1_user)) -> dict:
    return _who(principal) or {}


@probe.get("/api/v1/raw")
def _raw_probe(request: Request) -> dict:
    return _raw(request)


@probe.get("/api/v1/meta")
def _meta_probe(request: Request) -> dict:
    return _raw(request)


@contextmanager
def probe_portal() -> Iterator[TestClient]:
    """A production portal (``TokenIdentity`` installed) with :data:`probe` first."""
    app = create_portal_app(port=PORT, instance_lock=True)
    before = len(app.router.routes)
    app.include_router(probe)
    added = app.router.routes[before:]
    del app.router.routes[before:]
    app.router.routes[0:0] = added
    with TestClient(
        app, base_url=PROD_BASE, headers=CLIENT_HEADER, client=PROD_CLIENT
    ) as client:
        yield client


@pytest.fixture
def net(github_fake: FakeGitHub, tmp_path: Path) -> Iterator[SimpleNamespace]:
    """Ada (instance admin), Ben (owns ``acme``), Cy (member), Dee (owns ``bravo``)."""
    with probe_portal() as client:
        claim_instance(client)
        client.cookies.clear()
        assert github_sign_in(client, "ben").status_code == 200
        assert create_org(client, "acme", "Acme").status_code == 201
        for login in ("cy", "dee"):
            assert github_sign_in(client, login).status_code == 200
        client.cookies.clear()
        ids, uids = {}, {}
        with portal_db.get_session() as session:
            people = [("ada", User.email == "ada@example.com")]
            people += [(n, User.github_login == n) for n in ("ben", "cy", "dee")]
            for name, where in people:
                user = session.exec(select(User).where(where)).one()
                ids[name], uids[name] = user.id, user.uid
            acme = session.exec(
                select(Organization.id).where(Organization.slug == "acme")
            ).one()
            bravo = orgs.create_org(session, slug="bravo", name="Bravo").id
            orgs.add_member(session, org_id=acme, user_id=ids["cy"], role="member")
            orgs.add_member(session, org_id=bravo, user_id=ids["dee"], role="owner")
        projects = {}
        for org_id, slug in ((acme, "api"), (acme, "web"), (bravo, "lib")):
            root = tmp_path / "roots" / slug
            root.mkdir(parents=True)
            projects[slug] = _insert_project(org_id, slug, slug, root, ids["ben"])
        yield SimpleNamespace(
            client=client,
            ids=ids,
            uids=uids,
            orgs={"acme": acme, "bravo": bravo},
            projects=projects,
            tmp=tmp_path,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def issue(n: SimpleNamespace, who: str, project: str, name: str = "laptop") -> str:
    """A fresh token for ``who`` on ``project``."""
    with portal_db.get_session() as session:
        return connections.issue(
            session,
            user=session.get(User, n.ids[who]),
            project=session.get(Project, n.projects[project]),
            client_name=name,
        )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def call(
    n: SimpleNamespace,
    token: str | None,
    slug: str = "api",
    org: str = "acme",
) -> httpx.Response:
    """GET the project probe on ``org``'s host with ``token`` and no cookie."""
    n.client.cookies.clear()
    return n.client.get(
        at(org) + f"/api/v1/projects/{slug}/probe",
        headers={} if token is None else bearer(token),
    )


def row(token: str) -> ConnectionToken:
    with portal_db.get_session() as session:
        found = session.exec(
            select(ConnectionToken).where(
                ConnectionToken.token_hash == connections.hash_token(token)
            )
        ).one()
        session.expunge(found)
        return found


def age(token: str, **fields: timedelta) -> None:
    """Set the token row's timestamps to ``now - delta``."""
    now = datetime.now(timezone.utc)
    values = {k: (now - v).isoformat(timespec="seconds") for k, v in fields.items()}
    with portal_db.get_session() as session:
        found = session.exec(
            select(ConnectionToken).where(
                ConnectionToken.token_hash == connections.hash_token(token)
            )
        ).one()
        for key, value in values.items():
            setattr(found, key, value)
        session.add(found)


def refused(response: httpx.Response, reason: str | None) -> None:
    """Assert the ``401`` of a refused token."""
    assert response.status_code == 401, response.text
    body = response.json()
    if reason is None:
        assert body["code"] == "invalid_token" and "reason" not in body
    else:
        assert (body["code"], body["reason"]) == ("token_revoked", reason)
    assert response.headers["www-authenticate"].startswith("Bearer")


# ---------------------------------------------------------------------------
# Identity: bearer on /api/v1, sessions elsewhere
# ---------------------------------------------------------------------------


def test_v1_requires_bearer_and_ignores_cookies(net: SimpleNamespace) -> None:
    c = net.client
    token = issue(net, "ben", "api")

    signed_in(c, net.ids["ben"])
    # A session cookie alone: the guard resolves no one on /api/v1 ...
    raw = c.get(at("acme") + "/api/v1/raw").json()
    assert raw == {"principal": None, "refusal": "invalid_token"}
    # ... and every v1 route answers 401 invalid_token, never login_required.
    refused(c.get(at("acme") + "/api/v1/projects/api/probe"), None)
    refused(c.get(at("acme") + "/api/v1/probe"), None)

    # Cy's cookie plus Ben's token: the token decides, the cookie is not read.
    c.cookies.clear()
    signed_in(c, net.ids["cy"])
    response = c.get(at("acme") + "/api/v1/projects/api/probe", headers=bearer(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["uid"] == net.uids["ben"] and body["session_id"] is None
    assert body["token_project_id"] == net.projects["api"]

    # Not a bearer header, or two of them: refused like none.
    c.cookies.clear()
    for headers in (
        {"Authorization": f"Basic {token}"},
        {"Authorization": token},
        [("Authorization", f"Bearer {token}"), ("Authorization", f"Bearer {token}")],
    ):
        refused(c.get(at("acme") + "/api/v1/probe", headers=headers), None)


def test_bearer_ignored_outside_v1(net: SimpleNamespace) -> None:
    c = net.client
    token = issue(net, "ben", "api")
    c.cookies.clear()
    for url in (at() + "/api/account", at("acme") + "/api/projects"):
        response = c.get(url, headers=bearer(token))
        assert response.status_code == 401, (url, response.text)
        assert response.json()["code"] == "login_required"

    # With Cy's session, Ben's token changes nothing outside /api/v1.
    signed_in(c, net.ids["cy"])
    account = c.get(at() + "/api/account", headers=bearer(token))
    assert account.status_code == 200 and account.json()["uid"] == net.uids["cy"]

    # /api/v1/meta (exactly) is public and session-resolved: a bearer is ignored.
    c.cookies.clear()
    meta = c.get(at("acme") + "/api/v1/meta", headers=bearer(token)).json()
    assert meta == {"principal": None, "refusal": None}


@pytest.mark.parametrize(
    ("path", "bearer_only"),
    [
        ("/api/v1", True),
        ("/api/v1/", True),
        ("/api/v1/raw", True),
        ("/api/v1/meta/", True),
        ("/api/v1/meta/x", True),
        ("/api/v1/meta", False),
        ("/api/v1meta", False),
        ("/API/v1/raw", False),
        ("//api/v1/raw", False),
        ("/api/v2", False),
        ("/api", False),
    ],
)
def test_is_bearer_path(path: str, bearer_only: bool) -> None:
    assert is_bearer_path(path) is bearer_only


def test_v1_path_tricks(net: SimpleNamespace) -> None:
    c = net.client
    token = issue(net, "ben", "api")
    ben = net.uids["ben"]
    acme = at("acme")

    def get(path: str, *, cookie: bool, with_token: bool) -> httpx.Response:
        c.cookies.clear()
        if cookie:
            signed_in(c, net.ids["ben"])
        return c.get(acme + path, headers=bearer(token) if with_token else {})

    # A percent-encoded slash routes and authenticates as the decoded path.
    encoded = "/api/v1%2Fraw"
    assert get(encoded, cookie=True, with_token=False).json()["principal"] is None
    as_token = get(encoded, cookie=False, with_token=True).json()["principal"]
    assert as_token["uid"] == ben and as_token["token_id"] is not None

    # Dot segments: whatever the path becomes, a cookie never authenticates it.
    dotted = get("/api/v1/meta/../raw", cookie=True, with_token=False)
    assert dotted.status_code in (200, 404)
    if dotted.status_code == 200:
        assert dotted.json()["principal"] is None

    # Neither prefix: never routed to a v1 route (the /api catch-all answers
    # 401 signed out, 404 signed in; the SPA fallback 404).
    for path in ("//api/v1/raw", "/API/v1/raw", "/api/v1meta"):
        for cookie, with_token in ((True, False), (False, True)):
            response = get(path, cookie=cookie, with_token=with_token)
            assert response.status_code in (401, 404), (path, response.text)
            assert "principal" not in response.text
            if with_token:
                assert response.json().get("code") != "invalid_token"

    # Only exactly /api/v1/meta is public: a trailing slash is a bearer path
    # (and no route), so neither a cookie nor a token reaches the meta route.
    for cookie, with_token in ((True, False), (False, True)):
        response = get("/api/v1/meta/", cookie=cookie, with_token=with_token)
        assert response.status_code in (401, 404), response.text
        assert "principal" not in response.text
    assert (
        get("/api/v1/meta", cookie=True, with_token=False).json()["principal"]["uid"]
        == ben
    )


def test_token_principal_never_instance_admin(net: SimpleNamespace) -> None:
    with portal_db.get_session() as session:
        orgs.add_member(
            session, org_id=net.orgs["acme"], user_id=net.ids["ada"], role="member"
        )
    token = issue(net, "ada", "api")
    response = call(net, token)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["uid"] == net.uids["ada"]
    assert body["is_instance_admin"] is False and body["session_id"] is None

    # Without a membership the reader fallback never applies to a token.
    stray = issue(net, "ada", "lib")
    refused(call(net, stray, "lib", "bravo"), "member_removed")

    # A token never reaches a session route either (current_user refuses it).
    principal = Principal(user_id=1, uid="u", display_name="U", token_id=7)
    request = StarletteRequest({"type": "http", "state": {"principal": principal}})
    with pytest.raises(ApiError) as info:
        anyio.run(current_user, request)
    assert (info.value.status, info.value.code) == (401, "login_required")


def test_token_bound_to_project_id(net: SimpleNamespace) -> None:
    token = issue(net, "ben", "api")
    ok = call(net, token)
    assert ok.status_code == 200 and ok.json()["project_id"] == net.projects["api"]
    # Ben owns acme and may read web - but this token reaches api only.
    other = call(net, token, "web")
    assert other.status_code == 404, other.text
    # Another org's host: not found either.
    assert call(net, token, "lib", "bravo").status_code == 404
    assert call(net, token, "nope").status_code == 404


def test_recreated_project_same_slug_unreachable(net: SimpleNamespace) -> None:
    token = issue(net, "ben", "api")
    assert call(net, token).status_code == 200
    signed_in(net.client, net.ids["ben"])
    deleted = net.client.request(
        "DELETE", at("acme") + "/api/projects/api", json={"confirm_name": "api"}
    )
    assert deleted.status_code == 200, deleted.text
    root = net.tmp / "roots" / "api-again"
    root.mkdir()
    again = _insert_project(net.orgs["acme"], "api", "api", root, net.ids["ben"])
    assert again != net.projects["api"]

    refused(call(net, token), "project_deleted")
    # Even un-revoked by hand, the token names no project any more.
    with portal_db.get_session() as session:
        session.exec(
            text(
                "UPDATE connection_tokens SET revoked_at = NULL, revoked_reason = NULL"
            )
        )
    assert row(token).project_id is None
    refused(call(net, token), "project_deleted")


@pytest.mark.parametrize("reason", REVOKED_REASONS)
def test_revoked_token_401_with_reason(net: SimpleNamespace, reason: str) -> None:
    token = issue(net, "cy", "api")
    assert call(net, token).status_code == 200
    with portal_db.get_session() as session:
        assert connections.revoke(session, token_id=row(token).id, reason=reason)
    refused(call(net, token), reason)
    refused(net.client.get(at("acme") + "/api/v1/probe", headers=bearer(token)), reason)


def test_unknown_and_malformed_tokens_are_invalid(net: SimpleNamespace) -> None:
    good = issue(net, "cy", "api")
    for token in (
        None,
        "",
        "wgc_" + "A" * 43,
        good[:-1],
        good + "x",
        "ghp_" + good[4:],
        good.upper(),
    ):
        refused(call(net, token), None)


def test_idle_token_expires(net: SimpleNamespace) -> None:
    token = issue(net, "cy", "api")
    assert call(net, token).status_code == 200
    assert row(token).last_used_at is not None
    age(token, last_used_at=connections.IDLE - timedelta(minutes=1))
    assert call(net, token).status_code == 200
    age(token, last_used_at=connections.IDLE + timedelta(seconds=1))
    refused(call(net, token), "idle")
    stored = row(token)
    assert stored.revoked_reason == "idle" and stored.revoked_at is not None
    # And it stays revoked (no comeback once the row is touched again).
    age(token, last_used_at=timedelta(0))
    refused(call(net, token), "idle")


def test_unused_token_expires_after_an_hour(net: SimpleNamespace) -> None:
    fresh = issue(net, "cy", "api")
    age(fresh, created_at=connections.UNUSED - timedelta(minutes=1))
    assert call(net, fresh).status_code == 200
    assert row(fresh).last_used_at is not None  # the first use is always written

    stale = issue(net, "cy", "api")
    age(stale, created_at=connections.UNUSED + timedelta(seconds=1))
    refused(call(net, stale), "idle")
    assert row(stale).revoked_reason == "idle"


def test_touch_writes_first_use_then_every_five_minutes(net: SimpleNamespace) -> None:
    token = issue(net, "cy", "api")
    token_id = row(token).id
    assert connections.touch(token_id) is True
    assert connections.touch(token_id) is False  # fresh enough
    age(token, last_used_at=connections.TOUCH_EVERY + timedelta(seconds=1))
    assert connections.touch(token_id) is True


def test_lookup_rechecks_membership_without_revocation(net: SimpleNamespace) -> None:
    token = issue(net, "cy", "api")
    assert call(net, token).status_code == 200
    with portal_db.get_session() as session:
        session.exec(
            text("DELETE FROM memberships WHERE org_id = :o AND user_id = :u"),
            params={"o": net.orgs["acme"], "u": net.ids["cy"]},
        )
    refused(call(net, token), "member_removed")
    assert row(token).revoked_at is None  # refused on every call, not revoked


# ---------------------------------------------------------------------------
# Revocation follows membership, projects and orgs
# ---------------------------------------------------------------------------


def test_member_removed_revokes_tokens(net: SimpleNamespace) -> None:
    with portal_db.get_session() as session:
        orgs.add_member(
            session, org_id=net.orgs["bravo"], user_id=net.ids["cy"], role="member"
        )
    acme_token = issue(net, "cy", "api")
    bravo_token = issue(net, "cy", "lib")
    ben_token = issue(net, "ben", "api")
    signed_in(net.client, net.ids["ben"])
    removed = net.client.delete(at("acme") + f"/api/org/members/{net.uids['cy']}")
    assert removed.status_code == 204, removed.text

    assert row(acme_token).revoked_reason == "member_removed"
    refused(call(net, acme_token), "member_removed")
    assert call(net, bravo_token, "lib", "bravo").status_code == 200
    assert call(net, ben_token).status_code == 200


def test_member_left_and_user_disabled_reasons(net: SimpleNamespace) -> None:
    cy_token = issue(net, "cy", "api")
    signed_in(net.client, net.ids["cy"])
    left = net.client.delete(at("acme") + "/api/org/membership")
    assert left.status_code == 204, left.text
    refused(call(net, cy_token), "member_left")

    dee_token = issue(net, "dee", "lib")
    assert call(net, dee_token, "lib", "bravo").status_code == 200
    signed_in(net.client, net.ids["ada"])
    patch = net.client.patch(
        at() + f"/api/admin/users/{net.uids['dee']}", json={"disabled": True}
    )
    assert patch.status_code == 200, patch.text
    refused(call(net, dee_token, "lib", "bravo"), "user_disabled")
    # Enabling the account does not bring the token back.
    signed_in(net.client, net.ids["ada"])
    enable = net.client.patch(
        at() + f"/api/admin/users/{net.uids['dee']}", json={"disabled": False}
    )
    assert enable.status_code == 200, enable.text
    refused(call(net, dee_token, "lib", "bravo"), "user_disabled")


def test_disabled_user_refused_without_revocation(net: SimpleNamespace) -> None:
    token = issue(net, "cy", "api")
    with portal_db.get_session() as session:
        user = session.get(User, net.ids["cy"])
        user.disabled_at = "2026-10-05T00:00:00+00:00"
        session.add(user)
    refused(call(net, token), "user_disabled")
    assert row(token).revoked_at is None


def test_project_delete_revokes_with_reason(net: SimpleNamespace) -> None:
    ben = issue(net, "ben", "api")
    cy = issue(net, "cy", "api")
    web = issue(net, "cy", "web")
    signed_in(net.client, net.ids["ben"])
    deleted = net.client.request(
        "DELETE", at("acme") + "/api/projects/api", json={"confirm_name": "api"}
    )
    assert deleted.status_code == 200, deleted.text

    for token in (ben, cy):
        stored = row(token)
        assert stored.revoked_reason == "project_deleted"
        assert stored.project_id is None and stored.org_id == net.orgs["acme"]
        refused(call(net, token), "project_deleted")
    assert call(net, web, "web").status_code == 200
    with portal_db.get_session() as session:
        listed = connections.list_for_user(session, net.ids["cy"])
    assert [(t.project_slug, t.revoked_reason) for t in listed] == [
        ("web", None),
        (None, "project_deleted"),
    ]


def test_org_delete_revokes_with_reason(net: SimpleNamespace) -> None:
    acme = [issue(net, "ben", "api"), issue(net, "cy", "web")]
    bravo = issue(net, "dee", "lib")
    signed_in(net.client, net.ids["ben"])
    deleted = net.client.request(
        "DELETE", at("acme") + "/api/org", json={"confirm_slug": "acme"}
    )
    assert deleted.status_code == 200, deleted.text

    for token in acme:
        stored = row(token)
        assert stored.revoked_reason == "org_deleted"
        assert stored.org_id is None and stored.project_id is None
        # The org's host is gone; the token's refusal still says why.
        refused(
            net.client.get(at("bravo") + "/api/v1/probe", headers=bearer(token)),
            "org_deleted",
        )
    assert call(net, bravo, "lib", "bravo").status_code == 200


# ---------------------------------------------------------------------------
# Tokens follow project access (M2f-1 plan section 4.7)
# ---------------------------------------------------------------------------


def _grant(n: SimpleNamespace, who: str, slug: str, role: str | None) -> None:
    """Set (or, with ``None``, drop) ``who``'s grant on ``slug``."""
    key = (n.projects[slug], n.ids[who])
    with portal_db.get_session() as session:
        found = session.get(ProjectGrant, key)
        if role is None:
            if found is not None:
                session.delete(found)
            return
        if found is None:
            found = ProjectGrant(
                org_id=session.get(Project, key[0]).org_id,
                project_id=key[0],
                user_id=key[1],
                role=role,
            )
        found.role = role
        session.add(found)


def _restrict(n: SimpleNamespace, slug: str, restricted: bool = True) -> None:
    with portal_db.get_session() as session:
        session.get(Project, n.projects[slug]).restricted = restricted


def _default(n: SimpleNamespace, org: str, value: str) -> None:
    with portal_db.get_session() as session:
        session.get(Organization, n.orgs[org]).default_project_role = value


def test_lookup_refuses_a_restricted_project_without_revocation(
    net: SimpleNamespace,
) -> None:
    cy, ben = issue(net, "cy", "api"), issue(net, "ben", "api")
    web = issue(net, "cy", "web")
    _restrict(net, "api")
    refused(call(net, cy), "project_access_removed")
    assert row(cy).revoked_at is None  # refused on every call, not revoked
    assert call(net, ben).status_code == 200  # the owner is a project admin
    assert call(net, web, "web").status_code == 200
    # A grant restores access, and the token works again.
    _grant(net, "cy", "api", "viewer")
    assert call(net, cy).status_code == 200
    # Removing the grant takes it away again.
    _grant(net, "cy", "api", None)
    refused(call(net, cy), "project_access_removed")
    _restrict(net, "api", False)
    assert call(net, cy).status_code == 200


def test_lookup_refuses_under_the_default_none_until_granted(
    net: SimpleNamespace,
) -> None:
    cy = issue(net, "cy", "api")
    _default(net, "acme", "none")
    refused(call(net, cy), "project_access_removed")
    assert row(cy).revoked_at is None
    _grant(net, "cy", "api", "contributor")
    assert call(net, cy).status_code == 200
    _grant(net, "cy", "api", None)
    refused(call(net, cy), "project_access_removed")
    # A viewer default keeps tokens working: viewers may connect agents.
    _default(net, "acme", "viewer")
    assert call(net, cy).status_code == 200


def test_member_removed_wins_over_project_access(net: SimpleNamespace) -> None:
    cy = issue(net, "cy", "api")
    _restrict(net, "api")
    with portal_db.get_session() as session:
        session.exec(
            text("DELETE FROM memberships WHERE org_id = :o AND user_id = :u"),
            params={"o": net.orgs["acme"], "u": net.ids["cy"]},
        )
    refused(call(net, cy), "member_removed")


def test_revoke_for_project_user_is_scoped(net: SimpleNamespace) -> None:
    api, web = issue(net, "cy", "api"), issue(net, "cy", "web")
    ben = issue(net, "ben", "api")
    with portal_db.get_session() as session:
        count = connections.revoke_for_project_user(
            session, net.projects["api"], net.ids["cy"], "project_access_removed"
        )
    assert count == 1
    assert row(api).revoked_reason == "project_access_removed"
    refused(call(net, api), "project_access_removed")
    assert call(net, web, "web").status_code == 200
    assert call(net, ben).status_code == 200


def test_revoke_lost_access_revokes_at_once_and_stays_revoked(
    net: SimpleNamespace,
) -> None:
    with portal_db.get_session() as session:
        orgs.add_member(
            session, org_id=net.orgs["acme"], user_id=net.ids["dee"], role="member"
        )
    cy_api, cy_web = issue(net, "cy", "api"), issue(net, "cy", "web")
    dee_api, ben_api = issue(net, "dee", "api"), issue(net, "ben", "api")
    lib = issue(net, "dee", "lib")
    _grant(net, "dee", "api", "viewer")
    # The change and the eager revoke in one transaction.
    with portal_db.get_session() as session:
        session.get(Project, net.projects["api"]).restricted = True
        session.flush()
        count = connections.revoke_lost_access(
            session, net.orgs["acme"], project_id=net.projects["api"]
        )
    assert count == 1
    assert row(cy_api).revoked_reason == "project_access_removed"
    refused(call(net, cy_api), "project_access_removed")
    for token, slug, org in (
        (cy_web, "web", "acme"),  # another project
        (dee_api, "api", "acme"),  # granted
        (ben_api, "api", "acme"),  # the owner
        (lib, "lib", "bravo"),  # another org
    ):
        assert row(token).revoked_at is None
        assert call(net, token, slug, org).status_code == 200
    # Revoked is final: a grant does not bring the revoked token back.
    _grant(net, "cy", "api", "contributor")
    refused(call(net, cy_api), "project_access_removed")
    # Nothing left to revoke: a second pass is a no-op.
    with portal_db.get_session() as session:
        assert connections.revoke_lost_access(session, net.orgs["acme"]) == 0


def test_revoke_lost_access_after_the_default_none(net: SimpleNamespace) -> None:
    cy_api, cy_web = issue(net, "cy", "api"), issue(net, "cy", "web")
    ben = issue(net, "ben", "web")
    _grant(net, "cy", "web", "viewer")
    with portal_db.get_session() as session:
        session.get(Organization, net.orgs["acme"]).default_project_role = "none"
        session.flush()
        count = connections.revoke_lost_access(
            session, net.orgs["acme"], user_id=net.ids["cy"]
        )
    assert count == 1
    assert row(cy_api).revoked_reason == "project_access_removed"
    assert row(cy_web).revoked_at is None and row(ben).revoked_at is None


def test_revoke_lost_access_skips_former_members(net: SimpleNamespace) -> None:
    """A token whose user left the org is the member routes' to revoke."""
    cy = issue(net, "cy", "api")
    with portal_db.get_session() as session:
        session.exec(
            text("DELETE FROM memberships WHERE org_id = :o AND user_id = :u"),
            params={"o": net.orgs["acme"], "u": net.ids["cy"]},
        )
        assert connections.revoke_lost_access(session, net.orgs["acme"]) == 0
    assert row(cy).revoked_at is None


def test_lists(net: SimpleNamespace) -> None:
    first = issue(net, "cy", "api", "laptop")
    issue(net, "ben", "api", "desk top-1.local")
    issue(net, "cy", "web", "laptop")
    with portal_db.get_session() as session:
        connections.revoke(session, token_id=row(first).id, reason="user_revoked")
        api = connections.list_for_project(session, net.projects["api"])
        mine = connections.list_for_user(session, net.ids["cy"])
    assert [(t.user_login, t.client_name) for t in api] == [("ben", "desk top-1.local")]
    assert [(t.project_slug, t.revoked_reason) for t in mine] == [
        ("web", None),
        ("api", "user_revoked"),
    ]
    assert not any(hasattr(t, "token_hash") for t in mine)


def test_revoke_is_scoped(net: SimpleNamespace) -> None:
    token = issue(net, "cy", "api")
    uid = row(token).uid
    with portal_db.get_session() as session:
        # Another user's uid, or another project's: nothing matches.
        assert not connections.revoke(
            session, uid=uid, user_id=net.ids["ben"], reason="user_revoked"
        )
        assert not connections.revoke(
            session, uid=uid, project_id=net.projects["web"], reason="admin_revoked"
        )
        assert connections.revoke(
            session, uid=uid, project_id=net.projects["api"], reason="admin_revoked"
        )
        # Already revoked: the reason is kept.
        assert not connections.revoke(session, uid=uid, reason="user_revoked")
    assert row(token).revoked_reason == "admin_revoked"
    with pytest.raises(ValueError):
        with portal_db.get_session() as session:
            connections.revoke(session, reason="idle")


# ---------------------------------------------------------------------------
# Issue, redaction, throttle
# ---------------------------------------------------------------------------


def test_issue_shape_hash_and_client_name(net: SimpleNamespace) -> None:
    token = issue(net, "cy", "api", "Cys laptop")
    assert connections.TOKEN_RE.fullmatch(token)
    stored = row(token)
    assert stored.token_hash == connections.hash_token(token) != token
    assert stored.client_name == "Cys laptop" and stored.last_used_at is None
    assert (
        redact_tokens(f"sent {token} to the platform") == "sent wgc_*** to the platform"
    )
    for bad in ("", "x" * 65, "laptop\n", "lap/top", "<b>"):
        with pytest.raises(ValueError):
            issue(net, "cy", "api", bad)


def test_valid_token_not_throttled_by_failures(net: SimpleNamespace) -> None:
    token = issue(net, "cy", "api")
    bad = "wgc_" + "B" * 43
    limit = net.client.app.state.portal.v1_auth_ip.limit
    for _ in range(limit):
        refused(call(net, bad), None)
    throttled = call(net, bad)
    assert throttled.status_code == 429, throttled.text
    assert throttled.json()["code"] == "throttled"
    assert int(throttled.headers["retry-after"]) >= 1
    # A valid token from the same address is never refused by it.
    for _ in range(3):
        assert call(net, token).status_code == 200
    # A request without a token is not a failed lookup and is not throttled.
    refused(call(net, None), None)


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


def test_sweep(net: SimpleNamespace) -> None:
    live = issue(net, "cy", "api")
    assert call(net, live).status_code == 200
    unused = issue(net, "cy", "api")
    age(unused, created_at=timedelta(hours=2))
    idle = issue(net, "cy", "api")
    age(idle, created_at=timedelta(days=200), last_used_at=timedelta(days=91))
    recent = issue(net, "cy", "api")
    old = issue(net, "cy", "api")
    with portal_db.get_session() as session:
        for token in (recent, old):
            connections.revoke(session, token_id=row(token).id, reason="user_revoked")
    age(old, revoked_at=timedelta(days=31))
    age(recent, revoked_at=timedelta(days=29))

    assert connections.sweep() == (2, 1)
    assert row(live).revoked_at is None
    assert row(unused).revoked_reason == row(idle).revoked_reason == "idle"
    assert row(recent).revoked_reason == "user_revoked"
    with pytest.raises(Exception):
        row(old)
    assert connections.sweep() == (0, 0)


def test_production_start_sweeps(github_fake: FakeGitHub, tmp_path: Path) -> None:
    with probe_portal() as client:
        claim_instance(client)
        admin = client.cookies.get("whygraph_session")
        assert admin
    with portal_db.get_session() as session:
        org = orgs.create_org(session, slug="acme", name="Acme")
        user = session.exec(select(User)).one()
        orgs.add_member(session, org_id=org.id, user_id=user.id, role="owner")
        org_id, user_id = org.id, user.id
    root = tmp_path / "root"
    root.mkdir()
    project_id = _insert_project(org_id, "api", "api", root, user_id)
    with portal_db.get_session() as session:
        token = connections.issue(
            session,
            user=session.get(User, user_id),
            project=session.get(Project, project_id),
            client_name="laptop",
        )
    age(token, created_at=timedelta(hours=2))
    with probe_portal():
        deadline = time.monotonic() + 10
        while row(token).revoked_at is None and time.monotonic() < deadline:
            time.sleep(0.05)
    assert row(token).revoked_reason == "idle"


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


def _version() -> str:
    with portal_db.get_engine().connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _source_check() -> str:
    with portal_db.get_engine().connect() as conn:
        return conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_projects_source'"
            )
        ).scalar_one()


def test_connections_migration_round_trip(empty_portal_database: str) -> None:
    portal_db.ensure_initialized()
    assert _version() == HEAD_REVISION
    assert "platform" in _source_check()
    with portal_db.get_engine().connect() as conn:
        index = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'ix_connection_tokens_live_project_id'"
            )
        ).scalar_one()
    assert "WHERE (revoked_at IS NULL)" in index
    portal_db._reset_engine()
    command.downgrade(portal_db.alembic_config(), PREVIOUS_REVISION)
    portal_db._reset_engine()
    from sqlalchemy import inspect

    tables = set(inspect(portal_db.get_engine()).get_table_names())
    assert not tables & {"connection_tokens", "platform_links"}
    assert "platform" not in _source_check() and "github" in _source_check()
    command.upgrade(portal_db.alembic_config(), "head")
    portal_db._reset_engine()
    assert _version() == HEAD_REVISION


_PROJECT = (
    "INSERT INTO projects (org_id, slug, name, source, root, created_at, restricted) "
    "VALUES (:org, :slug, :slug, :source, :root, 'now', false) RETURNING id"
)


@pytest.mark.parametrize("seed", ["platform project", "platform link", "token"])
def test_connections_downgrade_refuses_m2e_data(
    empty_portal_database: str, seed: str
) -> None:
    portal_db.ensure_initialized()
    with portal_db.get_session() as s:
        org = builtin_org_id(s)
        s.commit()
    with portal_db.get_engine().begin() as conn:
        local = conn.execute(
            text(_PROJECT),
            {"org": org, "slug": "api", "source": "local", "root": "/r/api"},
        ).scalar_one()
        if seed == "platform project":
            conn.execute(
                text(_PROJECT),
                {"org": org, "slug": "lnk", "source": "platform", "root": "/r/lnk"},
            )
        elif seed == "platform link":
            conn.execute(
                text(
                    "INSERT INTO platform_links (project_id, platform_origin, "
                    "api_origin, org_slug, remote_slug, remote_name, clone_url, "
                    "default_branch, token_ciphertext, token_hint, status, status_at) "
                    "VALUES (:p, 'https://w.example', 'https://a.w.example', 'a', "
                    "'api', 'API', 'https://github.com/a/api.git', 'main', 'c', "
                    "'...abcd', 'ok', 'now')"
                ),
                {"p": local},
            )
        else:
            user = conn.execute(
                text(
                    "INSERT INTO users (uid, display_name, is_instance_admin, "
                    "created_at) VALUES ('u-1', 'U', false, 'now') RETURNING id"
                )
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO connection_tokens (uid, user_id, org_id, project_id, "
                    "token_hash, client_name, created_at) "
                    "VALUES ('t-1', :u, :o, :p, 'h', 'laptop', 'now')"
                ),
                {"u": user, "o": org, "p": local},
            )
    portal_db._reset_engine()
    with pytest.raises(RuntimeError, match="connections revision"):
        command.downgrade(portal_db.alembic_config(), PREVIOUS_REVISION)
    portal_db._reset_engine()
    assert _version() == HEAD_REVISION


def test_token_identity_is_a_session_identity() -> None:
    """Production's identity still answers every non-v1 path as sessions do."""
    from whygraph.portal.deps import SessionIdentity

    assert issubclass(TokenIdentity, SessionIdentity)
