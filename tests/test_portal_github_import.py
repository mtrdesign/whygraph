"""Production imports through the GitHub App (M2d-2 plan sections 4.3-4.5, 5.1).

A production portal whose two GitHub apps (sign-in and the GitHub App) both
talk to one :class:`~github_fake.FakeGitHub`: its API through
``httpx.MockTransport``, its git over dumb HTTP on a real socket
(``github_git_server``), so the import really clones with a repo-scoped
installation token. Ben signs in with GitHub and owns ``acme``; Ada is the
password instance admin.

Covered: the authorize / install / callback flow (state, PKCE on authorize
and none on install, one cookie, the account and org re-checks, the
server-built return URL, ``start_from_portal``, ``setup_action=request``,
``iss``), the listings, the import (happy path, every ``no_access`` case,
duplicates per org, the same repo in two orgs, slug retries, concurrency,
the tracked-state refusal, the production Initialize, the forge layer),
the user tokens' lifetime, the production config allowlist, removal, and
the ``503`` / ``404`` of an unconfigured app and of local mode.
"""

# ruff: noqa: F811 -- pytest fixtures imported from test_portal_app

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import select

from github_fake import FakeGitHub
from test_portal_app import (  # noqa: F401 -- fixtures
    GITHUB_APP_CLIENT_ID,
    GITHUB_APP_CLIENT_SECRET,
    GITHUB_APP_SLUG,
    GITHUB_WEBHOOK_SECRET,
    PROD_BASE,
    PROD_DOMAIN,
    GitServer,
    at,
    claim_instance,
    env,
    github_app_key,
    github_git_server,
    github_sign_in,
    log_in,
    portal_client,
    prod_portal,
    production_env,
)
from test_portal_identity_routes import (  # noqa: F401 -- `audit_log` is a fixture
    audit_log,
    create_org,
    events,
)
from whygraph.portal import db as portal_db
from whygraph.portal import sessions
from whygraph.portal.github_app import GitHubApp, UserTokens
from whygraph.portal.github_auth import GitHubOAuth
from whygraph.portal.models import Membership, Organization, Project, User
from whygraph.portal.routes import _clone_dir_is_safe

API = 2**31  # repository ids above 2^31, as on github.com (spike #9)
API_REPO, PULLLESS_REPO, HIDDEN_REPO, OTHER_PUBLIC, OWN_REPO, POISONED = (
    API + 1,
    API + 2,
    API + 3,
    API + 4,
    API + 5,
    API + 6,
)
INSTALLATION, OTHER_INSTALLATION = 7, 8
TOKEN_SHAPE = re.compile(r"gh[opsur]_[A-Za-z0-9]{8,}")


def _pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _set_app_env(
    tmp: Path, key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The five ``WHYGRAPH_GITHUB_APP_*`` variables, files in ``tmp``."""
    secret = tmp / "app-secret"
    secret.write_text(GITHUB_APP_CLIENT_SECRET + "\n")
    pem = tmp / "app-key.pem"
    pem.write_text(_pem(key))
    hook = tmp / "webhook-secret"
    hook.write_text(GITHUB_WEBHOOK_SECRET + "\n")
    monkeypatch.setenv("WHYGRAPH_GITHUB_APP_SLUG", GITHUB_APP_SLUG)
    monkeypatch.setenv("WHYGRAPH_GITHUB_APP_CLIENT_ID", GITHUB_APP_CLIENT_ID)
    monkeypatch.setenv("WHYGRAPH_GITHUB_APP_CLIENT_SECRET_FILE", str(secret))
    monkeypatch.setenv("WHYGRAPH_GITHUB_APP_PRIVATE_KEY_FILE", str(pem))
    monkeypatch.setenv("WHYGRAPH_GITHUB_APP_WEBHOOK_SECRET_FILE", str(hook))


@dataclass
class World:
    """A production portal with the GitHub App, over the fake's git server."""

    client: TestClient
    server: GitServer
    env: SimpleNamespace

    @property
    def fake(self) -> FakeGitHub:
        return self.server.fake

    @property
    def state(self):  # noqa: ANN201
        return self.client.app.state.portal

    @property
    def repos(self) -> Path:
        return self.env.data / "repos"


@pytest.fixture
def app_env(
    production_env: SimpleNamespace,
    github_git_server: GitServer,
    github_app_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> GitServer:
    """``production_env`` with the GitHub App, both apps on the fake.

    The fake's repositories: ``acme/api`` (Ben reads it), ``acme/web`` (Ben
    sees it with ``pull`` false), ``acme/hidden`` (private, Ben has no
    access), ``acme/poisoned`` (tracks ``.whygraph/``) - all covered by
    installation 7 on the ``acme`` organization -; ``other/lib``, public,
    covered by installation 8 that Ben cannot see; and ``ben/tool``, Ben's
    own repository, covered by no installation.
    """
    server = github_git_server
    for var in list(os.environ):
        if var.lower().endswith("_proxy"):
            monkeypatch.delenv(var)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("WHYGRAPH_GITHUB_URL", server.url)
    monkeypatch.setenv("WHYGRAPH_GITHUB_API_URL", server.url + "/api/v3")
    _set_app_env(production_env.tmp, github_app_key, monkeypatch)
    transport = httpx.MockTransport(server.fake.handle)
    monkeypatch.setattr(
        "whygraph.portal.app.GitHubOAuth",
        lambda config: GitHubOAuth(config, transport=transport),
    )
    monkeypatch.setattr(
        "whygraph.portal.app.GitHubApp",
        lambda config: GitHubApp(config, transport=transport),
    )
    fake = server.fake
    fake.add_installation(INSTALLATION, "acme", account_type="Organization")
    fake.add_installation(OTHER_INSTALLATION, "other", account_type="Organization")
    ben_reads = {"ben": {"pull": True}}
    server.add_repo(
        API_REPO,
        "acme/api",
        INSTALLATION,
        readers=ben_reads,
        files={"README.md": "api\n"},
    )
    server.add_repo(
        PULLLESS_REPO, "acme/web", INSTALLATION, readers={"ben": {"pull": False}}
    )
    server.add_repo(HIDDEN_REPO, "acme/hidden", INSTALLATION)
    server.add_repo(
        POISONED,
        "acme/poisoned",
        INSTALLATION,
        readers=ben_reads,
        files={"README.md": "x\n", ".whygraph/whygraph.db": "not a db\n"},
    )
    server.add_repo(OTHER_PUBLIC, "other/lib", OTHER_INSTALLATION, public=True)
    server.add_repo(OWN_REPO, "ben/tool", None)
    return server


@pytest.fixture
def world(app_env: GitServer, production_env: SimpleNamespace) -> Iterator[World]:
    """Ada claims the instance; Ben signs in with GitHub and creates ``acme``."""
    with prod_portal() as client:
        claim_instance(client)
        client.cookies.clear()
        assert github_sign_in(client, "ben").status_code == 200
        assert create_org(client, "acme", "Acme").status_code == 201
        yield World(client, app_env, production_env)


def start(w: World, org: str = "acme", *, install: bool = False) -> str:
    """``POST /api/github/app/authorize`` (asserted ``200``); the URL to follow."""
    response = w.client.post(
        at(org) + "/api/github/app/authorize", json={"install": install}
    )
    assert response.status_code == 200, response.text
    return response.json()["url"]


def follow(w: World, url: str, **params: str) -> dict[str, str]:
    """Send the browser through the fake GitHub; return the redirect's query."""
    response = w.state.github_app._request("GET", url + "&" + urlencode(params))
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(f"{PROD_BASE}/auth/github-app?"), location
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def callback(w: World, query: dict) -> httpx.Response:
    return w.client.post(at() + "/api/github/app/callback", json=query)


def connect(
    w: World, org: str = "acme", *, login: str = "ben", install: bool = False
) -> httpx.Response:
    """The whole authorize (or install) round trip, as the SPA drives it."""
    return callback(w, follow(w, start(w, org, install=install), login=login))


def connected(w: World, org: str = "acme") -> None:
    response = connect(w, org)
    assert response.status_code == 200, response.text


def import_repo(
    w: World, repo_id: int, *, org: str = "acme", installation: int = INSTALLATION
) -> httpx.Response:
    return w.client.post(
        at(org) + "/api/projects",
        json={"source": "github", "installation_id": installation, "repo_id": repo_id},
    )


def project_row(org: str, slug: str) -> Project | None:
    with portal_db.get_session() as session:
        row = session.exec(
            select(Project)
            .join(Organization, Organization.id == Project.org_id)
            .where(Organization.slug == org, Project.slug == slug)
        ).first()
        if row is not None:
            session.expunge(row)
        return row


def clone_dirs(w: World, org: str = "acme") -> list[str]:
    folder = w.repos / org
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def jar_get(w: World, name: str) -> str | None:
    """A cookie's value from the jar, whatever domain form it was stored with."""
    values = [c.value for c in w.client.cookies.jar if c.name == name]
    assert len(values) <= 1, values
    return values[0] if values else None


def ben_session_id(w: World) -> int:
    token = jar_get(w, "whygraph_session")
    row = sessions.lookup(token)
    assert row is not None
    return row.session_id


def ben_id() -> int:
    with portal_db.get_session() as session:
        return session.exec(select(User.id).where(User.github_login == "ben")).one()


def assert_no_token(response: httpx.Response) -> None:
    assert not TOKEN_SHAPE.search(response.text), response.text


# ---------------------------------------------------------------------------
# Authorize, install and the callback (plan section 4.4)
# ---------------------------------------------------------------------------


def test_authorize_uses_pkce_and_the_callback_stores_a_session_token(
    world: World, audit_log: pytest.LogCaptureFixture
) -> None:
    w = world
    url = start(w)
    query = parse_qs(urlsplit(url).query)
    assert url.startswith(f"{w.server.url}/login/oauth/authorize?")
    assert query["client_id"] == [GITHUB_APP_CLIENT_ID]
    assert query["code_challenge_method"] == ["S256"] and query["code_challenge"]
    cookie = jar_get(w, "whygraph_ghapp")
    assert cookie == query["state"][0]

    back = follow(w, url, login="ben")
    assert back["iss"] == f"{w.server.url}/login/oauth"
    response = callback(w, back)
    assert response.status_code == 200, response.text
    # The return URL is built by the server, from the org the state named.
    assert response.json() == {
        "return_to": "http://acme.whygraph.localhost:8765/projects/new"
    }
    assert_no_token(response)
    assert "whygraph_ghapp" not in w.client.cookies  # cleared
    (exchange,) = w.fake.calls("POST", "/login/oauth/access_token")[-1:]
    assert b"code_verifier=" in exchange.content
    entry = w.state.user_tokens.get(ben_session_id(w), ben_id())
    assert entry is not None and entry.github_id == 1001
    assert entry.token.startswith("ghu_") and not w.fake.is_revoked(entry.token)
    (event,) = [e for e in events(audit_log) if e["event"] == "github_app_authorized"]
    assert (event["org"], event["github_login"], event["arrival"]) == (
        "acme",
        "ben",
        "authorize",
    )
    # The state is single use.
    again = callback(w, back)
    assert (again.status_code, again.json()["code"]) == (400, "oauth_state")


def test_install_exchanges_without_pkce(world: World) -> None:
    w = world
    url = start(w, install=True)
    assert url.startswith(
        f"{w.server.url}/apps/{GITHUB_APP_SLUG}/installations/new?state="
    )
    assert "code_challenge" not in url
    back = follow(w, url, login="ben", account="ben")
    assert back["setup_action"] == "install" and "iss" not in back
    response = callback(w, back)
    assert response.status_code == 200, response.text
    assert response.json()["return_to"].endswith(
        "acme.whygraph.localhost:8765/projects/new"
    )
    exchange = w.fake.calls("POST", "/login/oauth/access_token")[-1]
    assert b"code_verifier" not in exchange.content
    installations = w.client.get(at("acme") + "/api/github/installations")
    ids = [i["id"] for i in installations.json()["installations"]]
    assert int(back["installation_id"]) in ids


def test_the_callback_needs_exactly_the_one_matching_cookie(world: World) -> None:
    w = world
    # A second whygraph_ghapp cookie (another host tossed one in) refuses.
    back = follow(w, start(w), login="ben")
    w.client.cookies.set(
        "whygraph_ghapp", "tossed", domain=PROD_DOMAIN, path="/api/github"
    )
    response = callback(w, back)
    assert (response.status_code, response.json()["code"]) == (400, "oauth_state")
    w.client.cookies.delete("whygraph_ghapp", domain=PROD_DOMAIN, path="/api/github")
    # No cookie at all (another browser) refuses too, and spends nothing.
    back = follow(w, start(w), login="ben")
    w.client.cookies.delete("whygraph_ghapp")
    response = callback(w, back)
    assert (response.status_code, response.json()["code"]) == (400, "oauth_state")
    # A cookie that is not the posted state.
    back = follow(w, start(w), login="ben")
    response = callback(w, {**back, "state": "not-the-state"})
    assert (response.status_code, response.json()["code"]) == (400, "oauth_state")
    assert len(w.state.user_tokens) == 0


def test_the_state_belongs_to_the_session_that_started_it(world: World) -> None:
    w = world
    back = follow(w, start(w), login="ben")
    cookie = jar_get(w, "whygraph_ghapp")
    # Ben signs in again: a new session, same browser cookie.
    assert github_sign_in(w.client, "ben").status_code == 200
    assert jar_get(w, "whygraph_ghapp") == cookie == back["state"]
    response = callback(w, back)
    assert (response.status_code, response.json()["code"]) == (400, "oauth_state")


def test_an_iss_other_than_githubs_is_refused_before_the_exchange(
    world: World,
) -> None:
    w = world
    back = follow(w, start(w), login="ben")
    before = len(w.fake.calls("POST", "/login/oauth/access_token"))
    response = callback(w, {**back, "iss": "https://evil.example/login/oauth"})
    assert (response.status_code, response.json()["code"]) == (
        400,
        "github_auth_failed",
    )
    assert len(w.fake.calls("POST", "/login/oauth/access_token")) == before


def test_another_github_account_is_refused_and_revoked(
    world: World, audit_log: pytest.LogCaptureFixture
) -> None:
    w = world
    response = connect(w, login="cy")
    assert (response.status_code, response.json()["code"]) == (
        403,
        "github_account_mismatch",
    )
    (token,) = w.fake.tokens_for("cy")
    assert w.fake.is_revoked(token)
    assert len(w.state.user_tokens) == 0
    assert any(
        e["event"] == "github_account_mismatch" and e["github_login"] == "cy"
        for e in events(audit_log)
    )


def test_a_password_account_cannot_connect_github(world: World) -> None:
    w = world
    w.client.cookies.clear()
    assert log_in(w.client, "ada@example.com").status_code == 200
    assert create_org(w.client, "adas", "Ada's").status_code == 201
    response = w.client.post(at("adas") + "/api/github/app/authorize", json={})
    assert (response.status_code, response.json()["code"]) == (403, "github_required")
    # And at the callback: an account that lost its GitHub id meanwhile.
    w.client.cookies.clear()
    assert github_sign_in(w.client, "ben").status_code == 200
    url = start(w)
    with portal_db.get_session() as session:
        session.exec(
            text(
                "UPDATE users SET github_id = NULL, github_login = NULL "
                "WHERE github_login = 'ben'"
            )
        )
    response = callback(w, follow(w, url, login="ben"))
    assert (response.status_code, response.json()["code"]) == (403, "github_required")


def test_the_org_is_rechecked_at_the_callback(world: World) -> None:
    w = world
    url = start(w)
    with portal_db.get_session() as session:
        org_id = session.exec(
            select(Organization.id).where(Organization.slug == "acme")
        ).one()
        membership = session.get(Membership, (org_id, ben_id()))
        membership.role = "member"
        session.add(membership)
    response = callback(w, follow(w, url, login="ben"))
    assert (response.status_code, response.json()["code"]) == (403, "forbidden")
    (token,) = w.fake.tokens_for("ben")[-1:]
    assert w.fake.is_revoked(token)
    assert len(w.state.user_tokens) == 0


def test_an_install_not_started_from_the_portal(world: World) -> None:
    w = world
    # An install started from GitHub's own page: no state, no cookie.
    response = w.state.github_app._request(
        "GET",
        f"{w.server.url}/apps/{GITHUB_APP_SLUG}/installations/new?login=ben",
    )
    query = {
        k: v[0]
        for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()
    }
    assert "state" not in query
    answer = callback(w, query)
    assert (answer.status_code, answer.json()["code"]) == (409, "start_from_portal")
    # An unknown state is the same answer for an install.
    answer = callback(w, {**query, "state": "unknown"})
    assert (answer.status_code, answer.json()["code"]) == (409, "start_from_portal")


def test_a_request_to_the_owners_exchanges_nothing(world: World) -> None:
    w = world
    back = follow(w, start(w, install=True), login="ben", request="1")
    assert back["setup_action"] == "request"
    before = len(w.fake.calls("POST", "/login/oauth/access_token"))
    response = callback(w, back)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "requested": True,
        "return_to": "http://acme.whygraph.localhost:8765/projects/new",
    }
    assert len(w.fake.calls("POST", "/login/oauth/access_token")) == before
    assert len(w.state.user_tokens) == 0


def test_the_return_url_cannot_be_steered(world: World) -> None:
    w = world
    back = follow(w, start(w), login="ben")
    response = callback(w, {**back, "return_to": "https://evil.example/"})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# User tokens (plan section 4.3)
# ---------------------------------------------------------------------------


def _listing(w: World) -> httpx.Response:
    return w.client.get(at("acme") + "/api/github/installations")


def _needs_authorization(response: httpx.Response) -> None:
    assert (response.status_code, response.json()["code"]) == (
        401,
        "github_authorization_required",
    ), response.text


def test_without_a_token_the_listing_asks_to_connect(world: World) -> None:
    _needs_authorization(_listing(world))


def test_sign_out_ends_and_revokes_the_token(world: World) -> None:
    w = world
    connected(w)
    entry = w.state.user_tokens.get(ben_session_id(w), ben_id())
    assert _listing(w).status_code == 200
    assert w.client.post(at() + "/api/auth/logout").status_code == 200
    assert len(w.state.user_tokens) == 0 and w.fake.is_revoked(entry.token)
    assert github_sign_in(w.client, "ben").status_code == 200
    _needs_authorization(_listing(w))


def test_a_token_serves_only_its_own_session(world: World) -> None:
    w = world
    connected(w)
    old = jar_get(w, "whygraph_session")
    # Another browser of Ben's: its own session, no token.
    w.client.cookies.clear()
    assert github_sign_in(w.client, "ben").status_code == 200
    _needs_authorization(_listing(w))
    # Back in the first browser, the token serves its session again.
    w.client.cookies.clear()
    w.client.cookies.set("whygraph_session", old, domain=PROD_DOMAIN)
    assert _listing(w).status_code == 200


def test_rotating_and_disabling_end_the_token(world: World) -> None:
    w = world
    connected(w)
    sid, uid = ben_session_id(w), ben_id()
    # rotate (a credential change) replaces the session: the old one is gone,
    # and the new one does not inherit the token.
    with portal_db.get_session() as session:
        fresh = sessions.rotate(session, sid, uid, "pytest")
    gone = _listing(w)
    assert (gone.status_code, gone.json()["code"]) == (401, "login_required")
    w.client.cookies.clear()
    w.client.cookies.set("whygraph_session", fresh, domain=PROD_DOMAIN)
    _needs_authorization(_listing(w))

    # Disabling (revoke_user) drops and revokes every token of the user.
    connected(w)
    sid = ben_session_id(w)
    entry = w.state.user_tokens.get(sid, uid)
    assert entry is not None
    w.client.cookies.clear()
    assert log_in(w.client, "ada@example.com").status_code == 200
    (ben,) = [
        u
        for u in w.client.get(at() + "/api/admin/users").json()
        if u["github_login"] == "ben"
    ]
    patched = w.client.patch(
        at() + f"/api/admin/users/{ben['uid']}", json={"disabled": True}
    )
    assert patched.status_code == 200, patched.text
    assert w.state.user_tokens.get(sid, uid) is None
    assert len(w.state.user_tokens) == 0 and w.fake.is_revoked(entry.token)


def test_a_token_expires(world: World) -> None:
    w = world
    clock = SimpleNamespace(now=time.time())
    w.state.user_tokens = UserTokens(clock=lambda: clock.now)
    connected(w)
    assert _listing(w).status_code == 200
    clock.now += 8 * 3600 + 60
    _needs_authorization(_listing(w))
    w.state.user_tokens.sweep()
    assert len(w.state.user_tokens) == 0


def test_a_token_github_rejects_is_forgotten(world: World) -> None:
    w = world
    connected(w)
    entry = w.state.user_tokens.get(ben_session_id(w), ben_id())
    assert w.state.github_app.revoke(entry.token)
    _needs_authorization(_listing(w))
    assert len(w.state.user_tokens) == 0


# ---------------------------------------------------------------------------
# Listing (plan section 4.5)
# ---------------------------------------------------------------------------


def test_the_listings_show_what_the_user_sees(world: World) -> None:
    w = world
    connected(w)
    installations = _listing(w)
    assert installations.status_code == 200, installations.text
    assert installations.json() == {
        "installations": [
            {
                "id": INSTALLATION,
                "account_login": "acme",
                "account_type": "Organization",
                "avatar_url": "https://avatars.example.test/a/acme",
                "repository_selection": "selected",
            }
        ]
    }
    url = at("acme") + f"/api/github/installations/{INSTALLATION}/repos"
    repos = w.client.get(url, params={"page": 1})
    assert repos.status_code == 200, repos.text
    body = repos.json()
    assert body["total_count"] == 3 and body["page"] == 1
    assert [(r["full_name"], r["imported"]) for r in body["repos"]] == [
        ("acme/api", False),
        ("acme/web", False),
        ("acme/poisoned", False),
    ]
    assert body["repos"][0] == {
        "id": API_REPO,
        "full_name": "acme/api",
        "private": True,
        "default_branch": "main",
        "imported": False,
    }
    assert import_repo(w, API_REPO).status_code == 201
    body = w.client.get(url).json()
    assert [r["imported"] for r in body["repos"]] == [True, False, False]
    # An installation the user cannot see.
    hidden = w.client.get(
        at("acme") + f"/api/github/installations/{OTHER_INSTALLATION}/repos"
    )
    assert (hidden.status_code, hidden.json()["code"]) == (404, "no_access")
    assert_no_token(repos)


def test_the_listings_need_the_add_project_role(world: World) -> None:
    w = world
    with portal_db.get_session() as session:
        org_id = session.exec(
            select(Organization.id).where(Organization.slug == "acme")
        ).one()
        membership = session.get(Membership, (org_id, ben_id()))
        membership.role = "member"
        session.add(membership)
    for method, path in (
        ("POST", "/api/github/app/authorize"),
        ("GET", "/api/github/installations"),
        ("GET", f"/api/github/installations/{INSTALLATION}/repos"),
    ):
        response = w.client.request(method, at("acme") + path, json={})
        assert (response.status_code, response.json()["code"]) == (403, "forbidden")


# ---------------------------------------------------------------------------
# Import (plan section 4.5)
# ---------------------------------------------------------------------------


def test_import_clones_initializes_and_saves_the_forge_layer(
    world: World, audit_log: pytest.LogCaptureFixture
) -> None:
    w = world
    connected(w)
    response = import_repo(w, API_REPO)
    assert response.status_code == 201, response.text
    assert_no_token(response)
    body = response.json()
    assert body["project"]["slug"] == "api" and body["project"]["initialized"]
    assert body["project"]["remote_url"] == f"{w.server.url}/acme/api"
    assert (
        body["project"]["github_full_name"],
        body["project"]["installation_account"],
    ) == ("acme/api", "acme")
    listed = w.client.get(at("acme") + "/api/projects").json()["projects"]
    assert [(p["github_full_name"], p["installation_account"]) for p in listed] == [
        ("acme/api", "acme")
    ]
    root = w.repos / "acme" / "api"
    assert body["project"]["root"] == str(root)
    assert (root / "README.md").read_text() == "api\n"
    assert (root / ".whygraph" / "whygraph.db").is_file()
    assert clone_dirs(w) == ["api"]  # no .clone-* left behind
    assert w.server.url in (root / ".git" / "config").read_text()
    assert "ghs_" not in (root / ".git" / "config").read_text()

    row = project_row("acme", "api")
    assert row is not None
    assert (row.source, row.root, row.github_repo_id) == (
        "github",
        "repos/acme/api",
        API_REPO,
    )
    assert (row.github_installation_id, row.default_branch) == (INSTALLATION, "main")
    assert row.created_by == ben_id() and row.initialized_at is not None
    config = w.client.get(at("acme") + "/api/projects/api/config").json()
    assert config["config"] == {"scan": {"forge": "auto"}}
    # Initialized for real: the data routes open the project.
    assert w.client.get(at("acme") + "/api/projects/api/scans").status_code == 200
    (event,) = [e for e in events(audit_log) if e["event"] == "project_imported"]
    assert (event["org"], event["repo_id"], event["full_name"]) == (
        "acme",
        API_REPO,
        "acme/api",
    )
    # The mint was scoped to the one repository.
    (mint,) = w.fake.calls("POST", f"/api/v3/app/installations/{INSTALLATION}/")
    assert b'"repository_ids":[' + str(API_REPO).encode() + b"]" in mint.content


def test_import_takes_an_optional_name(world: World) -> None:
    w = world
    connected(w)
    response = w.client.post(
        at("acme") + "/api/projects",
        json={
            "source": "github",
            "installation_id": INSTALLATION,
            "repo_id": API_REPO,
            "name": "The API",
        },
    )
    assert response.status_code == 201, response.text
    assert (response.json()["project"]["slug"], response.json()["project"]["name"]) == (
        "the-api",
        "The API",
    )


@pytest.mark.parametrize(
    ("installation", "repo_id"),
    [
        # A public repo the user can read, through an installation the user
        # cannot see (another org's): visibility is checked (spike #4).
        (OTHER_INSTALLATION, OTHER_PUBLIC),
        # A repo the user cannot read: private and not shared with them,
        # and one they see without pull permission.
        (INSTALLATION, HIDDEN_REPO),
        (INSTALLATION, PULLLESS_REPO),
        # A repo the user owns that the installation does not cover.
        (INSTALLATION, OWN_REPO),
    ],
    ids=["unseen-installation", "unreadable", "no-pull", "outside-installation"],
)
def test_no_access_is_one_answer(world: World, installation: int, repo_id: int) -> None:
    w = world
    connected(w)
    response = import_repo(w, repo_id, installation=installation)
    assert response.status_code == 404, response.text
    assert response.json() == {
        "error": "that repository is not available to you through that installation",
        "code": "no_access",
    }
    assert clone_dirs(w) == []
    with portal_db.get_session() as session:
        assert session.exec(select(Project.id)).first() is None


def test_import_needs_a_token(world: World) -> None:
    _needs_authorization(import_repo(world, API_REPO))


def test_a_repo_imports_once_per_org_and_into_two_orgs(world: World) -> None:
    w = world
    assert create_org(w.client, "bravo", "Bravo").status_code == 201
    connected(w)
    assert import_repo(w, API_REPO).status_code == 201
    again = import_repo(w, API_REPO)
    assert (again.status_code, again.json()["code"]) == (409, "duplicate")
    # The same repository in another org: its own clone at repos/bravo/api.
    other = import_repo(w, API_REPO, org="bravo")
    assert other.status_code == 201, other.text
    assert other.json()["project"]["slug"] == "api"
    assert other.json()["project"]["root"] == str(w.repos / "bravo" / "api")
    assert clone_dirs(w, "acme") == ["api"] and clone_dirs(w, "bravo") == ["api"]
    assert project_row("bravo", "api").root == "repos/bravo/api"


def test_a_taken_folder_retries_the_slug(world: World) -> None:
    w = world
    connected(w)
    leftover = w.repos / "acme" / "api"
    leftover.mkdir(parents=True)
    (leftover / "stray").write_text("x")
    response = import_repo(w, API_REPO)
    assert response.status_code == 201, response.text
    assert response.json()["project"]["slug"] == "api-2"
    assert (leftover / "stray").exists()  # never merged into or removed
    assert (w.repos / "acme" / "api-2" / "README.md").is_file()


def test_a_slug_race_in_the_database_retries(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent import committed the slug first: ``uq_projects_org_slug``."""
    w = world
    connected(w)
    with portal_db.get_session() as session:
        org_id = session.exec(
            select(Organization.id).where(Organization.slug == "acme")
        ).one()
        session.add(
            Project(
                org_id=org_id,
                slug="api",
                name="api",
                source="github",
                root=str(w.env.tmp / "elsewhere"),
            )
        )
    from whygraph.portal import routes

    real = routes.unique_slug
    calls = []

    def stale(session, name, *, org_id, exclude=()):  # noqa: ANN001, ANN202
        calls.append(set(exclude))
        return (
            "api"
            if len(calls) == 1
            else real(session, name, org_id=org_id, exclude=exclude)
        )

    monkeypatch.setattr(routes, "unique_slug", stale)
    response = import_repo(w, API_REPO)
    assert response.status_code == 201, response.text
    assert response.json()["project"]["slug"] == "api-2"
    assert calls == [set(), {"api"}]
    assert clone_dirs(w) == ["api-2"]


def test_concurrent_imports_of_one_repo_keep_one_clone(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    connected(w)
    from whygraph.portal import routes

    barrier = threading.Barrier(2, timeout=30)
    real_clone = routes.Repository.clone

    def clone(url, dest, **kwargs):  # noqa: ANN001, ANN202
        barrier.wait()  # both requests passed the duplicate pre-check
        return real_clone(url, dest, **kwargs)

    monkeypatch.setattr(routes.Repository, "clone", staticmethod(clone))
    results: list[httpx.Response] = []

    def run() -> None:
        results.append(import_repo(w, API_REPO))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(r.status_code for r in results) == [201, 409], [
        r.text for r in results
    ]
    (refused,) = [r for r in results if r.status_code == 409]
    assert refused.json()["code"] == "duplicate"
    assert clone_dirs(w) == ["api"]
    with portal_db.get_session() as session:
        assert len(session.exec(select(Project.id)).all()) == 1


def test_a_repo_tracking_whygraph_state_is_refused(world: World) -> None:
    w = world
    connected(w)
    response = import_repo(w, POISONED)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "tracked_whygraph_state"
    assert response.json()["paths"] == [".whygraph/whygraph.db"]
    assert clone_dirs(w) == []
    assert project_row("acme", "poisoned") is None


def test_imports_are_throttled_per_org(world: World) -> None:
    w = world
    connected(w)
    for _ in range(30):
        w.state.import_org.record(project_org_id("acme"))
    response = import_repo(w, API_REPO)
    assert (response.status_code, response.json()["code"]) == (429, "throttled")
    assert clone_dirs(w) == []


def project_org_id(slug: str) -> int:
    with portal_db.get_session() as session:
        return session.exec(
            select(Organization.id).where(Organization.slug == slug)
        ).one()


def test_production_initialize_is_rerunnable(world: World) -> None:
    w = world
    connected(w)
    assert import_repo(w, API_REPO).status_code == 201
    with portal_db.get_session() as session:
        session.exec(text("UPDATE projects SET initialized_at = NULL"))
    w.state.contexts.invalidate(None)
    init = w.client.post(at("acme") + "/api/projects/api/init", json={"agents": []})
    assert init.status_code == 200, init.text
    assert init.json()["initialized"] is True and init.json()["agent_files"] == []
    assert project_row("acme", "api").initialized_at is not None
    again = w.client.post(at("acme") + "/api/projects/api/init", json={})
    assert again.status_code == 200, again.text
    agents = w.client.post(
        at("acme") + "/api/projects/api/init", json={"agents": ["claude"]}
    )
    assert (agents.status_code, agents.json()["code"]) == (422, "not_in_production")
    assert not (w.repos / "acme" / "api" / ".mcp.json").exists()
    assert not (w.repos / "acme" / "api" / ".whygraph" / "portal.json").exists()


def test_production_config_fixes_the_branch_remote_and_hooks(world: World) -> None:
    w = world
    connected(w)
    assert import_repo(w, API_REPO).status_code == 201
    url = at("acme") + "/api/projects/api/config"
    for key, value in (
        ("hooks", True),
        ("remote", "upstream"),
        ("default_branch", "develop"),
    ):
        response = w.client.put(url, json={"config": {"scan": {key: value}}})
        assert response.status_code == 422, (key, response.text)
        assert response.json()["keys"] == [f"scan.{key}"], key
    response = w.client.put(url, json={"config": {"scan": {"forge": "off"}}})
    assert response.status_code == 200, response.text
    assert response.json()["config"] == {"scan": {"forge": "off"}}


def test_removing_an_imported_project_deletes_its_clone(world: World) -> None:
    w = world
    connected(w)
    assert import_repo(w, API_REPO).status_code == 201
    response = w.client.request(
        "DELETE", at("acme") + "/api/projects/api", json={"confirm_name": "api"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["checkout_deleted"] is True
    assert clone_dirs(w) == [] and (w.repos / "acme").is_dir()


# ---------------------------------------------------------------------------
# The clone folder guard (plan section 0.2 #3)
# ---------------------------------------------------------------------------


def test_the_clone_guard_wants_exactly_the_layout_of_the_row(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "repos" / "acme" / "api").mkdir(parents=True)
    (data / "repos" / "legacy").mkdir()
    assert _clone_dir_is_safe(data / "repos" / "acme" / "api", data, depth=2)
    assert _clone_dir_is_safe(data / "repos" / "acme" / ".clone-x-1", data, depth=2)
    assert not _clone_dir_is_safe(data / "repos" / "acme", data, depth=2)
    assert not _clone_dir_is_safe(data / "repos" / "acme" / "api", data, depth=1)
    assert _clone_dir_is_safe(data / "repos" / "legacy", data, depth=1)
    assert not _clone_dir_is_safe(tmp_path / "elsewhere" / "x" / "y", data, depth=2)
    # A symlinked org folder or project folder is refused, wherever it points.
    outside = tmp_path / "outside"
    (outside / "api").mkdir(parents=True)
    (data / "repos" / "evil").symlink_to(outside)
    assert not _clone_dir_is_safe(data / "repos" / "evil" / "api", data, depth=2)
    (data / "repos" / "acme" / "link").symlink_to(data / "repos" / "acme" / "api")
    assert not _clone_dir_is_safe(data / "repos" / "acme" / "link", data, depth=2)


# ---------------------------------------------------------------------------
# Without the app, and in local mode
# ---------------------------------------------------------------------------


GITHUB_ROUTES = (
    ("POST", "/api/github/app/authorize", "acme"),
    ("GET", "/api/github/installations", "acme"),
    ("GET", f"/api/github/installations/{INSTALLATION}/repos", "acme"),
    ("POST", "/api/github/app/callback", None),
)


def test_every_route_is_503_without_the_app(
    production_env: SimpleNamespace,
    github_git_server: GitServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = httpx.MockTransport(github_git_server.fake.handle)
    monkeypatch.setattr(
        "whygraph.portal.app.GitHubOAuth",
        lambda config: GitHubOAuth(config, transport=transport),
    )
    with prod_portal() as client:
        claim_instance(client)
        client.cookies.clear()
        assert github_sign_in(client, "ben").status_code == 200
        assert create_org(client, "acme", "Acme").status_code == 201
        for method, path, org in GITHUB_ROUTES:
            body = {"code": "c", "state": "s"} if org is None else {}
            response = client.request(method, at(org) + path, json=body)
            assert response.status_code == 503, (path, response.text)
            assert response.json()["code"] == "github_app_not_configured", path
        response = client.post(
            at("acme") + "/api/projects",
            json={"source": "github", "installation_id": 7, "repo_id": 1},
        )
        assert (response.status_code, response.json()["code"]) == (
            503,
            "github_app_not_configured",
        )


def test_local_mode_has_none_of_the_routes(env: SimpleNamespace) -> None:
    with portal_client() as client:
        assert (
            client.post("/api/portal/setup", json={"display_name": "T"}).status_code
            == 201
        )
        for method, path, _ in GITHUB_ROUTES:
            response = client.request(method, path, json={})
            assert (response.status_code, response.json()) == (
                404,
                {"error": "not found"},
            ), path
        response = client.post(
            "/api/projects",
            json={"source": "github", "installation_id": 7, "repo_id": 1},
        )
        assert (response.status_code, response.json()["code"]) == (
            403,
            "source_not_allowed",
        )
