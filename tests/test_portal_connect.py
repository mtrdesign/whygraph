"""Consent and code exchange for connected portals (M2e plan sections 4.4, 5.3).

The ``cn`` fixture runs a real production portal (no injected identity:
``TokenIdentity`` resolves sessions and bearer tokens) with Ada, the
instance admin who claimed it (no memberships); Ben, owner of ``acme``
(projects ``api`` and ``web``); Cy, a member of ``acme``; Fay, an admin of
``acme``; Dee, owner of ``bravo`` (project ``lib``). Accounts, orgs and
projects are inserted directly and sessions are made with
:func:`~test_portal_app.signed_in` - the sign-in itself is
``test_portal_identity_routes.py``'s. Everything under test goes through the
HTTP routes: validate, authorize, the exchange (with no cookie, as the local
portal sends it), the token lists and the revocations.
"""

# ruff: noqa: F811 -- pytest fixtures (`env`, `production_env`, `audit_log`) are imported

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.routing import iter_route_contexts
from sqlmodel import select

from test_portal_app import (  # noqa: F401 -- fixtures
    PROD_BASE,
    V1_ROUTES,
    _calls,
    at,
    claim_instance,
    env,
    portal_client,
    prod_portal,
    production_env,
    signed_in,
)
from test_portal_hosts_isolation import _insert_project
from test_portal_identity_routes import audit_log, events  # noqa: F401 -- fixture
from whygraph.api_v1 import API_VERSION, MetaOut, TokenReply
from whygraph.portal import connections
from whygraph.portal import db as portal_db
from whygraph.portal import orgs
from whygraph.portal.connect_routes import MIN_CLIENT_VERSION, redirect_port
from whygraph.portal.deps import current_user, v1_user
from whygraph.portal.github_auth import AccessLogRedactor, pkce_challenge
from whygraph.portal.models import (
    ConnectionToken,
    Membership,
    Organization,
    Project,
    ProjectGrant,
    User,
)
from whygraph.portal.throttle import Throttle
from whygraph.portal.v1_status import load_status

NOT_FOUND = {"error": "not found"}
LOCAL_PORT = 8765
REDIRECT = f"http://127.0.0.1:{LOCAL_PORT}/connect/callback"
VERIFIER = "v" * 20 + "-._~" + "A1b2C3d4e5F6g7H8i9J0k" + "z" * 20  # 65 chars
STATE = "state_0123456789-abcdef"
CLIENT_NAME = "ben-laptop.local"
GIT_URL = "http://127.0.0.1:9"
"""``production_env``'s GitHub (the platform's clone host)."""


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


def _user(session, login: str, github_id: int) -> User:  # noqa: ANN001
    user = User(display_name=login.title(), github_id=github_id, github_login=login)
    session.add(user)
    session.flush()
    return user


@pytest.fixture
def cn(production_env: SimpleNamespace, tmp_path: Path) -> Iterator[SimpleNamespace]:
    """Ada (instance admin), Ben / Fay / Cy in ``acme``, Dee in ``bravo``."""
    with prod_portal() as client:
        claim_instance(client)
        client.cookies.clear()
        ids, uids, org_ids = {}, {}, {}
        with portal_db.get_session() as session:
            ada = session.exec(select(User).where(User.email == "ada@example.com"))
            ada = ada.one()
            ids["ada"], uids["ada"] = ada.id, ada.uid
            for n, login in enumerate(("ben", "cy", "dee", "fay", "eve")):
                user = _user(session, login, 5100 + n)
                ids[login], uids[login] = user.id, user.uid
            for slug in ("acme", "bravo"):
                org_ids[slug] = orgs.create_org(
                    session, slug=slug, name=slug.title()
                ).id
            for org, login, role in (
                ("acme", "ben", "owner"),
                ("acme", "cy", "member"),
                ("acme", "fay", "admin"),
                ("bravo", "dee", "owner"),
            ):
                orgs.add_member(
                    session, org_id=org_ids[org], user_id=ids[login], role=role
                )
        projects = {}
        for n, (org, slug) in enumerate(
            (("acme", "api"), ("acme", "web"), ("bravo", "lib"))
        ):
            root = tmp_path / "roots" / org / slug
            root.mkdir(parents=True)
            projects[slug] = _insert_project(
                org_ids[org],
                slug,
                f"{slug.title()} project",
                root,
                ids["ben"],
                github=(70 + n, 7000 + n),
                remote_url=f"{GIT_URL}/{org}/{slug}",
                default_branch="main",
            )
        yield SimpleNamespace(
            client=client,
            state=client.app.state.portal,
            ids=ids,
            uids=uids,
            orgs=org_ids,
            projects=projects,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def be(n: SimpleNamespace, who: str | None) -> None:
    """Make the next requests ``who``'s (``None``: no cookie at all)."""
    n.client.cookies.clear()
    if who is not None:
        signed_in(n.client, n.ids[who])


def consent(**over) -> dict:
    """A valid consent query, with ``over`` replacing fields."""
    body = {
        "redirect_uri": REDIRECT,
        "code_challenge": pkce_challenge(VERIFIER),
        "code_challenge_method": "S256",
        "state": STATE,
        "client_name": CLIENT_NAME,
    }
    body.update(over)
    return body


def validate(n: SimpleNamespace, **over) -> httpx.Response:
    return n.client.post(at() + "/api/connect/validate", json=consent(**over))


def authorize(
    n: SimpleNamespace, org: str = "acme", project: str = "api", **over
) -> httpx.Response:
    return n.client.post(
        at() + "/api/connect/authorize",
        json=consent(org=org, project=project, **over),
    )


def query_of(url: str) -> dict[str, str]:
    values = parse_qs(urlsplit(url).query, keep_blank_values=True, strict_parsing=True)
    assert all(len(v) == 1 for v in values.values()), url
    return {k: v[0] for k, v in values.items()}


def code_for(n: SimpleNamespace, who: str = "ben", **kwargs) -> str:
    """Authorize as ``who`` and return the code of the redirect."""
    be(n, who)
    response = authorize(n, **kwargs)
    assert response.status_code == 200, response.text
    return query_of(response.json()["redirect"])["code"]


def exchange(
    n: SimpleNamespace,
    code: str,
    verifier: str = VERIFIER,
    redirect_uri: str = REDIRECT,
) -> httpx.Response:
    """``POST /api/connect/token`` with no cookie, as the local portal sends it."""
    n.client.cookies.clear()
    return n.client.post(
        at() + "/api/connect/token",
        json={"code": code, "code_verifier": verifier, "redirect_uri": redirect_uri},
    )


def invalid_grant(response: httpx.Response) -> None:
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "invalid_grant"
    assert "token" not in response.json()


def token_rows() -> list[ConnectionToken]:
    with portal_db.get_session() as session:
        rows = session.exec(select(ConnectionToken)).all()
        for row in rows:
            session.expunge(row)
        return rows


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def issue(n: SimpleNamespace, who: str, project: str, name: str = "laptop") -> str:
    with portal_db.get_session() as session:
        return connections.issue(
            session,
            user=session.get(User, n.ids[who]),
            project=session.get(Project, n.projects[project]),
            client_name=name,
        )


def uid_of_token(token: str) -> str:
    with portal_db.get_session() as session:
        return session.exec(
            select(ConnectionToken.uid).where(
                ConnectionToken.token_hash == connections.hash_token(token)
            )
        ).one()


def revoked_reason(token: str) -> str | None:
    with portal_db.get_session() as session:
        return session.exec(
            select(ConnectionToken.revoked_reason).where(
                ConnectionToken.token_hash == connections.hash_token(token)
            )
        ).one()


def self_revoke(
    n: SimpleNamespace, token: str | None, org: str = "acme", slug: str = "api"
) -> httpx.Response:
    n.client.cookies.clear()
    return n.client.delete(
        at(org) + f"/api/v1/projects/{slug}/token",
        headers={} if token is None else bearer(token),
    )


# ---------------------------------------------------------------------------
# The code: single use, PKCE, redirect_uri
# ---------------------------------------------------------------------------


def test_connect_code_single_use_and_pkce(cn: SimpleNamespace) -> None:
    # A code with the wrong verifier gets nothing (and is spent).
    wrong = code_for(cn)
    invalid_grant(exchange(cn, wrong, verifier="w" * 43))
    assert token_rows() == []

    code = code_for(cn)
    response = exchange(cn, code)
    assert response.status_code == 200, response.text
    reply = TokenReply.model_validate(response.json())
    assert connections.TOKEN_RE.fullmatch(reply.token)
    assert reply.org == "acme" and reply.api_version == API_VERSION == 1
    project = reply.project
    assert (project.slug, project.name, project.role) == ("api", "Api project", "owner")
    assert project.github_full_name == "acme/api"
    assert project.clone_url == f"{GIT_URL}/acme/api.git"
    assert project.default_branch == "main" and project.access_lost is False
    (row,) = token_rows()
    assert row.project_id == cn.projects["api"] and row.user_id == cn.ids["ben"]
    assert row.client_name == CLIENT_NAME and row.token_hash != reply.token

    # The same code a second time, even with the right verifier: refused.
    invalid_grant(exchange(cn, code))
    assert len(token_rows()) == 1
    assert len(cn.state.connect_codes) == 0


def test_code_consumed_by_failed_pkce(cn: SimpleNamespace) -> None:
    code = code_for(cn)
    for verifier in ("w" * 43, VERIFIER[:-1] + "Q", "short", VERIFIER + "\n"):
        invalid_grant(exchange(cn, code, verifier=verifier))
    invalid_grant(exchange(cn, code))  # the first failure spent it
    assert token_rows() == []


def test_exchange_redirect_uri_mismatch(cn: SimpleNamespace) -> None:
    for other in (
        f"http://127.0.0.1:{LOCAL_PORT + 1}/connect/callback",
        REDIRECT + "/",
        REDIRECT.replace("127.0.0.1", "localhost"),
        REDIRECT.upper(),
    ):
        code = code_for(cn)
        invalid_grant(exchange(cn, code, redirect_uri=other))
        invalid_grant(exchange(cn, code))  # spent by the mismatch
    assert token_rows() == []


GOOD_REDIRECTS = [
    "http://127.0.0.1:1/connect/callback",
    "http://127.0.0.1:8765/connect/callback",
    "http://127.0.0.1:65535/connect/callback",
]

BAD_REDIRECTS = [
    "",
    "http://localhost:8765/connect/callback",
    "http://LOCALHOST:8765/connect/callback",
    "http://[::1]:8765/connect/callback",
    "http://127.0.0.2:8765/connect/callback",
    "http://127.1:8765/connect/callback",
    "http://0x7f000001:8765/connect/callback",
    "https://127.0.0.1:8765/connect/callback",
    "HTTP://127.0.0.1:8765/connect/callback",
    "//127.0.0.1:8765/connect/callback",
    "http://127.0.0.1/connect/callback",  # no port
    "http://127.0.0.1:/connect/callback",
    "http://127.0.0.1:0/connect/callback",
    "http://127.0.0.1:65536/connect/callback",
    "http://127.0.0.1:99999/connect/callback",
    "http://127.0.0.1:08765/connect/callback",
    "http://127.0.0.1:+8765/connect/callback",
    "http://127.0.0.1:8765",
    "http://127.0.0.1:8765/",
    "http://127.0.0.1:8765/connect",
    "http://127.0.0.1:8765/connect/callback/",
    "http://127.0.0.1:8765/connect/callbackx",
    "http://127.0.0.1:8765/Connect/callback",
    "http://127.0.0.1:8765//connect/callback",
    "http://127.0.0.1:8765/x/../connect/callback",
    "http://127.0.0.1:8765/connect/%63allback",
    "http://127.0.0.1:8765/connect/callback?x=1",
    "http://127.0.0.1:8765/connect/callback?",
    "http://127.0.0.1:8765/connect/callback#frag",
    "http://127.0.0.1:8765/connect/callback#",
    "http://user@127.0.0.1:8765/connect/callback",
    "http://user:pw@127.0.0.1:8765/connect/callback",
    "http://127.0.0.1:8765@evil.example/connect/callback",
    "http://evil.example/connect/callback",
    "http://127.0.0.1.evil.example:8765/connect/callback",
    " http://127.0.0.1:8765/connect/callback",
    "http://127.0.0.1:8765/connect/callback ",
    "http://127.0.0.1:8765/connect/callback\n",
    "http://127.0.0.1:8765/connect/callback\x00",
]


@pytest.mark.parametrize("uri", GOOD_REDIRECTS)
def test_redirect_port_accepts_exactly_the_loopback_literal(uri: str) -> None:
    assert redirect_port(uri) == int(uri.split(":")[2].split("/")[0])


@pytest.mark.parametrize("uri", BAD_REDIRECTS)
def test_redirect_port_refuses(uri: str) -> None:
    assert redirect_port(uri) is None


def test_connect_redirect_must_be_exact_loopback(cn: SimpleNamespace) -> None:
    cn.state.connect_user = Throttle(1000, 3600)  # more attempts than 30 / hour
    be(cn, "ben")
    for uri in BAD_REDIRECTS:
        for response in (
            validate(cn, redirect_uri=uri),
            authorize(cn, redirect_uri=uri),
        ):
            assert response.status_code == 422, (uri, response.text)
            body = response.json()
            assert body["code"] == "bad_connect_request", uri
            assert body["field"] == "redirect_uri", uri
            assert "redirect" not in body and "cancel_url" not in body, uri
    assert len(cn.state.connect_codes) == 0
    for uri in GOOD_REDIRECTS:
        assert validate(cn, redirect_uri=uri).status_code == 200, uri
        response = authorize(cn, redirect_uri=uri)
        assert response.status_code == 200, (uri, response.text)
        assert response.json()["redirect"].startswith(uri + "?")


# ---------------------------------------------------------------------------
# The URLs: built server-side, iss, never for an invalid request
# ---------------------------------------------------------------------------


def test_authorize_redirect_carries_iss(cn: SimpleNamespace) -> None:
    be(cn, "ben")
    state = "Ab-_" * 8
    response = authorize(cn, state=state)
    assert response.status_code == 200, response.text
    redirect = response.json()["redirect"]
    head, _, raw_query = redirect.partition("?")
    assert head == REDIRECT
    # Every value percent-encoded (the issuer's ":" and "/" included).
    assert "iss=http%3A%2F%2Fwhygraph.localhost%3A8765" in raw_query
    query = query_of(redirect)
    assert set(query) == {"code", "state", "iss"}
    assert query["state"] == state and query["iss"] == PROD_BASE
    assert len(query["code"]) == 43

    cancel = validate(cn, state=state).json()["cancel_url"]
    head, _, raw_query = cancel.partition("?")
    assert head == REDIRECT
    assert query_of(cancel) == {
        "error": "access_denied",
        "state": state,
        "iss": PROD_BASE,
    }
    assert "iss=http%3A%2F%2Fwhygraph.localhost%3A8765" in raw_query


INVALID_FIELDS = [
    ("code_challenge_method", "plain"),
    ("code_challenge_method", "s256"),
    ("code_challenge", "A" * 42),
    ("code_challenge", "A" * 44),
    ("code_challenge", "A" * 42 + "="),
    ("code_challenge", "A" * 42 + "+"),
    ("state", "short"),
    ("state", "s" * 129),
    ("state", "has space in it!!"),
    ("state", "x" * 15 + "&"),
    ("client_name", ""),
    ("client_name", "evil‮name"),
]


def test_connect_invalid_redirect_never_redirects(cn: SimpleNamespace) -> None:
    """An invalid request yields an error and no URL, from either route."""
    be(cn, "ben")
    cases = [("redirect_uri", "http://localhost:1/connect/callback"), *INVALID_FIELDS]
    for field, value in cases:
        for response in (
            validate(cn, **{field: value}),
            authorize(cn, **{field: value}),
        ):
            assert response.status_code == 422, (field, value, response.text)
            body = response.json()
            assert (body["code"], body["field"]) == ("bad_connect_request", field)
            assert "redirect" not in body and "cancel_url" not in body
            assert value not in body["error"] or not value
    # Missing fields and unknown ones are the body validator's 422.
    for body in ({}, {**consent(), "extra": 1}, {**consent(), "redirect_uri": 5}):
        response = cn.client.post(at() + "/api/connect/validate", json=body)
        assert response.status_code == 422 and "cancel_url" not in response.json()
    assert len(cn.state.connect_codes) == 0


def test_client_name_charset_enforced(cn: SimpleNamespace) -> None:
    be(cn, "ben")
    for name in (
        "",
        "a" * 65,
        "laptop<script>",
        "näme",
        "two\nlines",
        "tab\there",
        "semi;colon",
        "quote'd",
        'quote"d',
        "slash/name",
    ):
        for response in (
            validate(cn, client_name=name),
            authorize(cn, client_name=name),
        ):
            assert response.status_code == 422, (name, response.text)
            assert response.json()["field"] == "client_name", name
    for name in ("my-laptop.local", "A B_c-1.2", "a" * 64, "x"):
        assert validate(cn, client_name=name).json()["client_name"] == name
        code = query_of(authorize(cn, client_name=name).json()["redirect"])["code"]
        assert exchange(cn, code).status_code == 200, name
        be(cn, "ben")
    with portal_db.get_session() as session, pytest.raises(ValueError):
        connections.issue(
            session,
            user=session.get(User, cn.ids["ben"]),
            project=session.get(Project, cn.projects["api"]),
            client_name="bad<name>",
        )


# ---------------------------------------------------------------------------
# Who may consent
# ---------------------------------------------------------------------------


def test_reader_cannot_authorize(cn: SimpleNamespace) -> None:
    """An instance admin reads every org as ``reader``, but never consents."""
    be(cn, "ada")
    # The reader access exists (a browser-only administrative view) ...
    assert cn.client.get(at("acme") + "/api/projects/api").status_code == 200
    # ... and gives no consent, no projects and no code.
    assert cn.client.get(at() + "/api/connect/projects").json() == []
    hinted = validate(cn, org="acme", project="api")
    assert hinted.status_code == 422 and hinted.json()["field"] == "org"
    plain = validate(cn)
    assert plain.status_code == 200 and plain.json()["orgs"] == []
    response = authorize(cn)
    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert len(cn.state.connect_codes) == 0


def test_consent_lists_member_orgs_only(cn: SimpleNamespace) -> None:
    be(cn, "cy")
    listed = cn.client.get(at() + "/api/connect/projects")
    assert listed.status_code == 200
    assert [(p["org"], p["slug"]) for p in listed.json()] == [
        ("acme", "api"),
        ("acme", "web"),
    ]
    assert listed.json()[0] == {
        "org": "acme",
        "org_name": "Acme",
        "slug": "api",
        "name": "Api project",
        "github_full_name": "acme/api",
        "access_lost": False,
    }
    body = validate(cn, org="acme", project="web").json()
    assert body["ok"] is True and body["port"] == LOCAL_PORT
    assert body["orgs"] == [{"slug": "acme", "name": "Acme", "role": "member"}]
    for hint, field in (
        ({"org": "bravo"}, "org"),
        ({"org": "nosuch"}, "org"),
        ({"org": "acme", "project": "lib"}, "project"),
        ({"project": "api"}, "project"),
    ):
        response = validate(cn, **hint)
        assert response.status_code == 422, hint
        assert response.json()["field"] == field, hint
    for org, project in (("bravo", "lib"), ("acme", "lib"), ("nosuch", "api")):
        response = authorize(cn, org=org, project=project)
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    # An access-lost project may be linked; the page warns.
    with portal_db.get_session() as session:
        row = session.get(Project, cn.projects["web"])
        row.access_lost_at = "2026-10-01T00:00:00+00:00"
        session.add(row)
    response = authorize(cn, project="web")
    assert response.status_code == 200 and response.json()["access_lost"] is True
    # Eve belongs to no org: nothing to list.
    be(cn, "eve")
    assert cn.client.get(at() + "/api/connect/projects").json() == []


def test_exchange_rechecks_membership(
    cn: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    code = code_for(cn, "cy")
    with portal_db.get_session() as session:
        session.delete(session.get(Membership, (cn.orgs["acme"], cn.ids["cy"])))
    audit_log.clear()
    invalid_grant(exchange(cn, code))
    (record,) = [
        r for r in events(audit_log) if r["event"] == "connection_token_refused"
    ]
    assert record["reason"] == "member_removed"

    # A disabled account is refused the same way.
    code = code_for(cn, "fay")
    with portal_db.get_session() as session:
        fay = session.get(User, cn.ids["fay"])
        fay.disabled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        session.add(fay)
    audit_log.clear()
    invalid_grant(exchange(cn, code))
    (record,) = [
        r for r in events(audit_log) if r["event"] == "connection_token_refused"
    ]
    assert record["reason"] == "user_disabled"
    assert token_rows() == []


# ---------------------------------------------------------------------------
# Restricted projects never leak (M2f-1 plan section 4.4)
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


def _consent_slugs(n: SimpleNamespace) -> list[str]:
    listed = n.client.get(at() + "/api/connect/projects")
    assert listed.status_code == 200, listed.text
    return [p["slug"] for p in listed.json()]


def test_restricted_absent_from_the_consent_list(cn: SimpleNamespace) -> None:
    _restrict(cn, "api")
    be(cn, "cy")
    assert _consent_slugs(cn) == ["web"]
    _grant(cn, "cy", "api", "viewer")  # viewers may connect agents
    assert _consent_slugs(cn) == ["api", "web"]
    # The org default `none` hides everything not granted.
    _default(cn, "acme", "none")
    assert _consent_slugs(cn) == ["api"]
    # Org owners and admins see every project, Restricted included.
    for who in ("ben", "fay"):
        be(cn, who)
        assert _consent_slugs(cn) == ["api", "web"]


def test_restricted_validate_hint_is_not_found(cn: SimpleNamespace) -> None:
    _restrict(cn, "api")
    be(cn, "cy")
    hidden = validate(cn, org="acme", project="api")
    missing = validate(cn, org="acme", project="nosuch")
    assert hidden.status_code == missing.status_code == 422, hidden.text
    assert hidden.json() == missing.json()
    assert hidden.json()["field"] == "project"
    assert validate(cn, org="acme", project="web").status_code == 200
    _grant(cn, "cy", "api", "viewer")
    assert validate(cn, org="acme", project="api").status_code == 200
    be(cn, "fay")
    _grant(cn, "cy", "api", None)
    assert validate(cn, org="acme", project="api").status_code == 200


def test_restricted_authorize_is_not_found(cn: SimpleNamespace) -> None:
    _restrict(cn, "api")
    be(cn, "cy")
    for project in ("api", "nosuch"):
        response = authorize(cn, project=project)
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert len(cn.state.connect_codes) == 0
    _default(cn, "acme", "none")
    response = authorize(cn, project="web")
    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    _grant(cn, "cy", "api", "contributor")
    assert authorize(cn, project="api").status_code == 200


def test_restricted_after_consent_refuses_the_exchange(
    cn: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    code = code_for(cn, "cy")
    _restrict(cn, "api")
    audit_log.clear()
    invalid_grant(exchange(cn, code))
    (record,) = [
        r for r in events(audit_log) if r["event"] == "connection_token_refused"
    ]
    assert record["reason"] == "project_access_removed"
    assert record["target"] == cn.uids["cy"]
    # The default `none` is refused the same way.
    _restrict(cn, "api", False)
    code = code_for(cn, "cy")
    _default(cn, "acme", "none")
    audit_log.clear()
    invalid_grant(exchange(cn, code))
    (record,) = [
        r for r in events(audit_log) if r["event"] == "connection_token_refused"
    ]
    assert record["reason"] == "project_access_removed"
    assert token_rows() == []
    # With a grant the exchange answers the project role.
    _grant(cn, "cy", "api", "viewer")
    reply = exchange(cn, code_for(cn, "cy"))
    assert reply.status_code == 200, reply.text
    project = TokenReply.model_validate(reply.json()).project
    assert (project.role, project.project_role) == ("member", "viewer")


def test_v1_status_carries_the_project_role_and_hides_restricted(
    cn: SimpleNamespace,
) -> None:
    def status(token: str) -> httpx.Response:
        cn.client.cookies.clear()
        return cn.client.get(at("acme") + "/api/v1/projects/api", headers=bearer(token))

    ben, cy, fay = (issue(cn, who, "api") for who in ("ben", "cy", "fay"))
    roles = {}
    for who, token in (("ben", ben), ("cy", cy), ("fay", fay)):
        response = status(token)
        assert response.status_code == 200, response.text
        roles[who] = (response.json()["role"], response.json()["project_role"])
    assert roles == {
        "ben": ("owner", "admin"),
        "cy": ("member", "contributor"),
        "fay": ("admin", "admin"),
    }
    _grant(cn, "cy", "api", "viewer")
    assert status(cy).json()["project_role"] == "viewer"
    # Restricted without a grant: the token is refused, and the status that
    # every v1 answer carries is never built.
    _grant(cn, "cy", "api", None)
    _restrict(cn, "api")
    response = status(cy)
    assert response.status_code == 401, response.text
    assert (response.json()["code"], response.json()["reason"]) == (
        "token_revoked",
        "project_access_removed",
    )
    assert status(ben).json()["project_role"] == "admin"
    with portal_db.get_session() as session:
        api = cn.projects["api"]
        assert load_status(session, api, cn.ids["cy"]) is None
        assert load_status(session, api, cn.ids["ada"]) is None  # no membership
        assert load_status(session, api, cn.ids["fay"]).project_role == "admin"
        _grant(cn, "cy", "api", "admin")
        assert load_status(session, api, cn.ids["cy"]).project_role == "admin"


# ---------------------------------------------------------------------------
# Lists and revocation
# ---------------------------------------------------------------------------


def test_user_lists_only_own_tokens(cn: SimpleNamespace) -> None:
    ben_api, ben_web = issue(cn, "ben", "api", "one"), issue(cn, "ben", "web")
    cy_api = issue(cn, "cy", "api", "cys-box")
    be(cn, "ben")
    listed = cn.client.get(at() + "/api/connect/tokens")
    assert listed.status_code == 200
    rows = listed.json()
    assert {r["uid"] for r in rows} == {uid_of_token(ben_api), uid_of_token(ben_web)}
    for token in (ben_api, ben_web, cy_api):
        assert token not in listed.text
    assert {(r["org"], r["project"]) for r in rows} == {
        ("acme", "api"),
        ("acme", "web"),
    }
    assert "token_hash" not in listed.text

    other = cn.client.delete(at() + f"/api/connect/tokens/{uid_of_token(cy_api)}")
    assert other.status_code == 404 and other.json()["code"] == "not_found"
    assert revoked_reason(cy_api) is None
    mine = cn.client.delete(at() + f"/api/connect/tokens/{uid_of_token(ben_api)}")
    assert mine.status_code == 204, mine.text
    assert revoked_reason(ben_api) == "user_revoked"
    again = cn.client.delete(at() + f"/api/connect/tokens/{uid_of_token(ben_api)}")
    assert again.status_code == 404
    (row,) = [
        r
        for r in cn.client.get(at() + "/api/connect/tokens").json()
        if r["uid"] == uid_of_token(ben_api)
    ]
    assert row["revoked_reason"] == "user_revoked" and row["revoked_at"]
    refused = self_revoke(cn, ben_api)
    assert refused.status_code == 401
    assert refused.json()["reason"] == "user_revoked"


def test_admin_lists_and_revokes_project_tokens(
    cn: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    cy_api, ben_api = issue(cn, "cy", "api", "cys-box"), issue(cn, "ben", "api")
    issue(cn, "cy", "web")  # another project: never listed under api
    for who in ("ben", "fay"):  # the owner and an admin
        be(cn, who)
        listed = cn.client.get(at("acme") + "/api/projects/api/connections")
        assert listed.status_code == 200, listed.text
        assert {(r["user_login"], r["client_name"]) for r in listed.json()} == {
            ("cy", "cys-box"),
            ("ben", "laptop"),
        }
        assert set(listed.json()[0]) == {
            "uid",
            "user_login",
            "user_name",
            "client_name",
            "created_at",
            "last_used_at",
        }
        assert cy_api not in listed.text and ben_api not in listed.text
    audit_log.clear()
    uid = uid_of_token(cy_api)
    revoked = cn.client.delete(at("acme") + f"/api/projects/api/connections/{uid}")
    assert revoked.status_code == 204, revoked.text
    assert revoked_reason(cy_api) == "admin_revoked"
    (record,) = [r for r in events(audit_log) if r["event"] == "connection_revoked"]
    assert (record["uid"], record["target"]) == (cn.uids["fay"], cn.uids["cy"])
    assert (record["reason"], record["token_uid"]) == ("admin_revoked", uid)
    assert (record["org"], record["project"]) == ("acme", "api")
    listed = cn.client.get(at("acme") + "/api/projects/api/connections").json()
    assert [r["user_login"] for r in listed] == ["ben"]  # live tokens only
    refused = self_revoke(cn, cy_api)
    assert (refused.status_code, refused.json()["reason"]) == (401, "admin_revoked")


def test_member_cannot_list_project_tokens(cn: SimpleNamespace) -> None:
    token = issue(cn, "ben", "api")
    be(cn, "cy")
    for method, path in (
        ("GET", "/api/projects/api/connections"),
        ("DELETE", f"/api/projects/api/connections/{uid_of_token(token)}"),
    ):
        response = cn.client.request(method, at("acme") + path)
        assert response.status_code == 403, (method, response.text)
        assert response.json()["code"] == "forbidden"
    assert revoked_reason(token) is None
    # Not on the base host, and not in an org one is no member of.
    be(cn, "ben")
    for prefix in (at(), at("bravo")):
        response = cn.client.get(prefix + "/api/projects/api/connections")
        assert (response.status_code, response.json()) == (404, NOT_FOUND)


def test_admin_cannot_revoke_other_projects_token(cn: SimpleNamespace) -> None:
    web, lib = issue(cn, "cy", "web"), issue(cn, "dee", "lib")
    be(cn, "ben")
    for token in (web, lib):  # another project of the org, another org's
        uid = uid_of_token(token)
        response = cn.client.delete(at("acme") + f"/api/projects/api/connections/{uid}")
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "not_found"
        assert revoked_reason(token) is None
    # Through its own project, web's token is Ben's to revoke.
    uid = uid_of_token(web)
    assert (
        cn.client.delete(
            at("acme") + f"/api/projects/web/connections/{uid}"
        ).status_code
        == 204
    )
    # Bravo's host is not Ben's at all.
    uid = uid_of_token(lib)
    response = cn.client.delete(at("bravo") + f"/api/projects/lib/connections/{uid}")
    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert revoked_reason(lib) is None


def test_self_revoke_removed_locally(
    cn: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    token = issue(cn, "cy", "api")
    # Only its own project, on its own org host, with the bearer itself.
    for org, slug in (("acme", "web"), ("bravo", "lib"), ("bravo", "api")):
        response = self_revoke(cn, token, org, slug)
        assert response.status_code == 404, (org, slug, response.text)
    cn.client.cookies.clear()
    base = cn.client.delete(at() + "/api/v1/projects/api/token", headers=bearer(token))
    assert base.status_code == 404
    signed_in(cn.client, cn.ids["cy"])  # a session never counts on /api/v1
    session_only = cn.client.delete(at("acme") + "/api/v1/projects/api/token")
    assert session_only.status_code == 401
    assert session_only.json()["code"] == "invalid_token"
    assert revoked_reason(token) is None

    audit_log.clear()
    response = self_revoke(cn, token)
    assert response.status_code == 204, response.text
    assert revoked_reason(token) == "removed_locally"
    (record,) = [r for r in events(audit_log) if r["event"] == "connection_revoked"]
    assert (record["uid"], record["reason"]) == (cn.uids["cy"], "removed_locally")
    assert record["token_uid"] == uid_of_token(token)
    again = self_revoke(cn, token)
    assert again.status_code == 401
    assert (again.json()["code"], again.json()["reason"]) == (
        "token_revoked",
        "removed_locally",
    )


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------


def test_meta_is_public_on_every_host(cn: SimpleNamespace) -> None:
    cn.client.cookies.clear()
    for prefix in (at(), at("acme"), at("nosuchorg")):
        response = cn.client.get(prefix + "/api/v1/meta")
        assert response.status_code == 200, (prefix, response.text)
        assert response.json() == {
            "api_version": 1,
            "min_client": MIN_CLIENT_VERSION,
            "capabilities": ["evidence", "rationale", "history", "resources"],
        }
        MetaOut.model_validate(response.json())


def test_meta_public_and_404_in_local_mode(env: SimpleNamespace) -> None:
    """Local mode serves none of the platform's routes, before setup or after."""
    with portal_client() as client:
        for setup in (False, True):
            if setup:
                done = client.post("/api/portal/setup", json={"display_name": "Tess"})
                assert done.status_code == 201
            for method, path in (
                ("GET", "/api/v1/meta"),
                ("POST", "/api/connect/validate"),
                ("POST", "/api/connect/token"),
                ("GET", "/api/connect/tokens"),
                ("GET", "/api/projects/demo/connections"),
                ("DELETE", "/api/v1/projects/demo/token"),
            ):
                response = client.request(method, path, json={})
                assert response.status_code == 404, (method, path, response.text)
                assert response.json() == NOT_FOUND


# ---------------------------------------------------------------------------
# Throttles and the pending codes
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_throttle_takes_a_per_call_limit() -> None:
    clock = Clock()
    rule = Throttle(2, 60, clock=clock)
    assert [rule.hit("k", limit=4) for _ in range(5)][-1] is not None
    assert rule.check("k", limit=4) is not None
    assert rule.check("k", limit=5) is None
    assert rule.hit("k") is not None  # the default limit (2) is long past
    assert rule.hit("other") is None and rule.hit("other") is None
    assert rule.hit("other") == 60
    # 0 refuses everything for a whole window, and counts nothing.
    assert rule.hit("zero", limit=0) == 60 and rule.check("zero", limit=0) == 60
    assert rule.check("zero") is None
    clock.now += 61
    assert rule.hit("k", limit=1) is None


def test_authorize_is_throttled_per_user(cn: SimpleNamespace) -> None:
    cn.state.connect_user = Throttle(2, 3600)
    be(cn, "ben")
    for _ in range(2):
        assert authorize(cn).status_code == 200
    refused = authorize(cn)
    assert refused.status_code == 429 and refused.json()["code"] == "throttled"
    assert int(refused.headers["retry-after"]) > 0
    # Invalid requests count too; another user is not affected.
    be(cn, "cy")
    assert authorize(cn).status_code == 200


def test_token_exchange_throttles_failures_per_ip(cn: SimpleNamespace) -> None:
    cn.state.connect_ip = Throttle(1, 600)
    # A successful exchange is never counted ...
    assert exchange(cn, code_for(cn)).status_code == 200
    assert exchange(cn, code_for(cn)).status_code == 200
    # ... a failed one is, and then the address is refused, whatever the code.
    invalid_grant(exchange(cn, "x" * 43))
    refused = exchange(cn, code_for(cn))
    assert refused.status_code == 429 and refused.json()["code"] == "throttled"


def test_pending_codes_expire_are_single_use_and_capped() -> None:
    clock = Clock()
    codes = connections.PendingCodes(clock=clock, ttl=60, max_entries=2)
    fields = dict(
        user_id=1,
        org_id=2,
        project_id=3,
        redirect_uri=REDIRECT,
        code_challenge="c" * 43,
        client_name="box",
    )
    first = codes.put(**fields)
    assert first not in codes._entries  # only the hash is held
    assert codes.pop(first).project_id == 3
    assert codes.pop(first) is None
    late = codes.put(**fields)
    clock.now += 60
    assert codes.pop(late) is None
    a, b, c = (codes.put(**fields) for _ in range(3))
    assert len(codes) == 2 and codes.pop(a) is None  # the oldest was evicted
    assert codes.pop(b) is not None and codes.pop(c) is not None


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/connect?redirect_uri=x&state=s&code_challenge=c", "/connect?<redacted>"),
        ("/connect/callback?code=abc&state=xyz&iss=i", "/connect/callback?<redacted>"),
        ("/connect", "/connect"),
        ("/api/connect/token", "/api/connect/token"),
    ],
)
def test_access_log_redacts_the_connect_queries(path: str, expected: str) -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", path, "1.1", 200),
        None,
    )
    assert AccessLogRedactor().filter(record) is True
    assert f'"GET {expected} HTTP/1.1"' in record.getMessage()


# ---------------------------------------------------------------------------
# End to end, and the bearer sweep
# ---------------------------------------------------------------------------


def test_connect_end_to_end(
    cn: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    """validate -> authorize -> token -> the token on ``/api/v1``."""
    c = cn.client
    be(cn, "ben")
    audit_log.clear()
    checked = validate(cn, org="acme", project="api")
    assert checked.status_code == 200, checked.text
    assert checked.json()["client_name"] == CLIENT_NAME
    assert c.get(at() + "/api/connect/projects").status_code == 200
    allowed = authorize(cn)
    assert allowed.status_code == 200, allowed.text
    query = query_of(allowed.json()["redirect"])
    assert query["iss"] == PROD_BASE and query["state"] == STATE

    reply = exchange(cn, query["code"])  # no cookie: the local portal
    assert reply.status_code == 200, reply.text
    token = TokenReply.model_validate(reply.json()).token
    assert reply.headers["cache-control"] == "no-store"
    be(cn, "ben")
    (listed,) = c.get(at() + "/api/connect/tokens").json()
    assert (listed["client_name"], listed["revoked_at"]) == (CLIENT_NAME, None)

    assert self_revoke(cn, token).status_code == 204
    assert revoked_reason(token) == "removed_locally"

    records = events(audit_log)
    names = [r["event"] for r in records]
    assert names == [
        "connection_authorized",
        "connection_token_issued",
        "connection_revoked",
    ]
    authorized, issued, _ = records
    assert authorized["client_name"] == repr(CLIENT_NAME) == "'ben-laptop.local'"
    assert issued["client_name"] == repr(CLIENT_NAME)
    assert (authorized["org"], authorized["project"]) == ("acme", "api")
    assert authorized["port"] == LOCAL_PORT
    logged = json.dumps(records)
    assert token not in logged and query["code"] not in logged


_V1 = "/api/v1/projects/{slug}"
_SWEEP_SHA = "a" * 40
_TARGET = {"path": "sample.py", "line_start": 1, "line_end": 2}

V1_REQUESTS: dict[tuple[str, str], tuple[int, str | None, dict]] = {
    (f"{_V1}/token", "DELETE"): (204, None, {}),
    # The data routes (M2e plan section 4.5). ``cn``'s projects are
    # initialized but empty and their roots hold no git repository, so the
    # reads answer over nothing: a rationale finds no evidence and an
    # unknown commit / PR / issue is ``not_found``. Each carries a ``code``,
    # which the tenancy ``404``s above never do.
    (_V1, "GET"): (200, None, {}),
    (f"{_V1}/evidence", "POST"): (200, None, {"json": {"target": _TARGET}}),
    (f"{_V1}/rationale", "POST"): (
        404,
        "no_evidence",
        {"json": {"target": _TARGET}},
    ),
    (f"{_V1}/history", "GET"): (200, None, {"params": {"path": "sample.py"}}),
    (f"{_V1}/commits/{{sha}}", "GET"): (404, "not_found", {}),
    (f"{_V1}/prs/{{number}}", "GET"): (404, "not_found", {}),
    (f"{_V1}/issues/{{number}}", "GET"): (404, "not_found", {}),
    (f"{_V1}/overview", "GET"): (200, None, {}),
}
"""How the bearer sweep calls each ``/api/v1`` project route, and the
``(status, code, request kwargs)`` of a call that gets through."""


def _v1_url(path: str, slug: str) -> str:
    """Fill a v1 route's path parameters for the sweep."""
    for placeholder, value in (
        ("{slug}", slug),
        ("{sha}", _SWEEP_SHA),
        ("{number}", "4242"),
    ):
        path = path.replace(placeholder, value)
    assert "{" not in path, path
    return path


def _v1_routes(app) -> set[tuple[str, str]]:  # noqa: ANN001
    return {
        (rc.path, method)
        for rc in iter_route_contexts(app.routes)
        if (rc.path or "").startswith("/api/v1/projects/")
        and getattr(rc, "dependant", None) is not None
        for method in rc.methods
        if method != "HEAD"
    }


def test_v1_routes_resolve_a_bearer_never_a_session(cn: SimpleNamespace) -> None:
    assert _v1_routes(cn.client.app) == V1_ROUTES == set(V1_REQUESTS)
    for rc in iter_route_contexts(cn.client.app.routes):
        if any((rc.path, m) in V1_ROUTES for m in getattr(rc, "methods", None) or ()):
            calls = set(_calls(rc.dependant))
            assert v1_user in calls and current_user not in calls, rc.path


@pytest.mark.parametrize(
    ("path", "method"),
    sorted(V1_ROUTES),
    ids=[f"{m} {p}" for p, m in sorted(V1_ROUTES)],
)
def test_v1_bearer_sweep_over_two_orgs(
    cn: SimpleNamespace, path: str, method: str
) -> None:
    """Each token reaches only its own project, on its own org host.

    Ben's token (``acme/api``) and Dee's (``bravo/lib``): the other org's
    host, the other project of the same org, the base host and a host naming
    no org are each a ``404``; no token, a malformed one and a session alone
    are ``401``, and so is a revoked token (with its reason); the token's own
    project answers, naming its own project at the user's role and nothing of
    the other org.
    """
    tokens = {"acme": issue(cn, "ben", "api"), "bravo": issue(cn, "dee", "lib")}
    own = {"acme": "api", "bravo": "lib"}
    owner = {"acme": "ben", "bravo": "dee"}

    expected_status, expected_code, kwargs = V1_REQUESTS[(path, method)]

    def call(org: str | None, slug: str, headers: dict) -> httpx.Response:
        cn.client.cookies.clear()
        return cn.client.request(
            method, at(org) + _v1_url(path, slug), headers=headers, **kwargs
        )

    for org, other in (("acme", "bravo"), ("bravo", "acme")):
        mine = bearer(tokens[org])
        for host, slug in (
            (other, own[other]),
            (other, own[org]),
            (org, own[other]),
            (org, "web" if org == "acme" else "nosuch"),
            (None, own[org]),
            ("nosuchorg", own[org]),
        ):
            response = call(host, slug, mine)
            assert response.status_code == 404, (org, host, slug, response.text)
            assert response.json().get("code") is None, response.text
        for headers in ({}, bearer("wgc_" + "x" * 43), {"Authorization": "Bearer"}):
            response = call(org, own[org], headers)
            assert response.status_code == 401, (org, headers, response.text)
            assert response.json()["code"] == "invalid_token"
        signed_in(cn.client, cn.ids[owner[org]])
        response = cn.client.request(
            method, at(org) + _v1_url(path, own[org]), **kwargs
        )
        assert (
            response.status_code == 401 and response.json()["code"] == "invalid_token"
        )
        # A token the owner revoked is refused here too, with the reason -
        # never by letting the request through to the route body.
        spent = issue(cn, owner[org], own[org], name="old-laptop")
        be(cn, owner[org])
        gone = cn.client.delete(
            at(org) + f"/api/projects/{own[org]}/connections/{uid_of_token(spent)}"
        )
        assert gone.status_code == 204, gone.text
        response = call(org, own[org], bearer(spent))
        assert response.status_code == 401, (org, response.text)
        assert (response.json()["code"], response.json()["reason"]) == (
            "token_revoked",
            "admin_revoked",
        )
    for org in ("acme", "bravo"):
        other = own["bravo" if org == "acme" else "acme"]
        response = call(org, own[org], bearer(tokens[org]))
        assert response.status_code == expected_status, response.text
        if expected_code is not None:
            assert response.json()["code"] == expected_code, response.text
        if response.status_code == 200:
            # Every answer names the token's own project, at its user's role,
            # and carries nothing of the other org (as the session sweeps'
            # ``assert_no_leak``; ``cn``'s projects differ only by name). The
            # status route *is* that block; every other answer carries it.
            body = response.json()
            project = body if (path, method) == (_V1, "GET") else body["project"]
            assert project["slug"] == own[org], response.text
            assert project["name"] == f"{own[org].title()} project", response.text
            assert project["role"] == "owner", response.text
            assert f"{other.title()} project" not in response.text, response.text
