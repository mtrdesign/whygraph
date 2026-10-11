"""Linking a local checkout to a platform project (M2e step 6, plan section 4.8).

The flow is ``POST /api/platform/connect`` -> the platform's consent page ->
``POST /api/platform/callback`` -> ``GET /api/platform/pending/{link_id}`` ->
``POST /api/projects {"source": "platform", ...}`` -> Initialize -> a
CodeGraph-only first scan. The platform is ``tests/platform_fake.py``, plugged
into ``PortalState.platform_transport`` as an ``httpx.MockTransport``, so no
test touches the network.

What these tests pin: the RFC 9207 ``iss`` check before anything else, the org
origin computed locally (never taken from the reply), the origin-or-history
match rule (plan section 0.1 #12), the slug rule, the browser's port in the
loopback ``redirect_uri``, the revoke of an abandoned link, the token never
leaving the portal DB, and the ``404`` of every ``/api/platform/*`` route in
production.
"""

# ruff: noqa: F811 -- pytest fixtures imported from test_portal_app / test_portal_runner

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from platform_fake import ORG, SLUG, TOKEN, FakePlatform, status_body
from test_portal_app import (  # noqa: F401 -- fixtures
    LOCAL_ONLY_ROUTES,
    _git,
    add_local,
    at,
    env,
    init_project,
    make_repo,
    prod_portal,
    production_env,
    seed_codegraph,
)
from test_portal_runner import (  # noqa: F401 -- `scanner` is a fixture
    client_for,
    runner_flags,
    scanner,
    wait_run,
)
from whygraph.portal import db as portal_db
from whygraph.portal.models import PlatformLink, Project
from whygraph.portal.platform_pending import PendingLinks
from whygraph.portal.secrets import decrypt

CLONE_URL = "https://github.com/acme/demo.git"
"""What :func:`platform_fake.status_body` clones from."""

CODE = "the-code"
LINK_EXPIRED = {
    "error": "this link expired or was used; connect again",
    "code": "link_expired",
}


# ---------------------------------------------------------------------------
# Fixtures and flow helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def fake() -> FakePlatform:
    """A platform at ``https://wg.example.com`` holding the ``demo`` project."""
    return FakePlatform()


def _transport(*platforms: FakePlatform) -> httpx.MockTransport:
    """One transport that routes each request to the platform owning its host."""

    def handle(request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host") or str(request.url.netloc)
        for platform in platforms:
            if host == platform.host or host.endswith("." + platform.host):
                return platform.handle(request)
        return httpx.Response(404, json={"error": "no platform", "code": "not_found"})

    return httpx.MockTransport(handle)


@pytest.fixture
def portal(
    env: SimpleNamespace, scanner: SimpleNamespace, fake: FakePlatform
) -> Iterator[TestClient]:
    """A portal past setup whose platform client talks to :func:`fake`."""
    with client_for() as client:
        assert (
            client.post("/api/portal/setup", json={"display_name": "Tess"}).status_code
            == 201
        )
        client.app.state.portal.platform_transport = _transport(fake)
        yield client


def connect(client: TestClient, fake: FakePlatform, **over: Any) -> dict:
    """``POST /api/platform/connect`` for ``fake``; the body it answered with."""
    body = {"platform_url": fake.platform_origin, "client_name": "laptop"}
    body.update(over)
    response = client.post("/api/platform/connect", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def query_of(started: dict) -> dict[str, str]:
    """The consent URL's query (``state``, the PKCE challenge, ``redirect_uri``)."""
    parts = urlsplit(started["authorize_url"])
    assert f"{parts.scheme}://{parts.netloc}" == started["platform_origin"]
    assert parts.path == "/connect"
    return dict(parse_qsl(parts.query))


def allow(
    fake: FakePlatform,
    started: dict,
    *,
    code: str = CODE,
    org: str = ORG,
    slug: str = SLUG,
    token: str = TOKEN,
) -> None:
    """Register the code the platform's **Allow** would hand back.

    The challenge is taken from the consent URL the portal built, so the
    exchange only succeeds if the portal kept the matching verifier.
    """
    params = query_of(started)
    assert params["code_challenge_method"] == "S256"
    fake.codes[code] = {
        "challenge": params["code_challenge"],
        "redirect_uri": params["redirect_uri"],
        "org": org,
        "slug": slug,
        "token": token,
    }


def callback(
    client: TestClient, started: dict, *, code: str | None = CODE, **over: Any
) -> httpx.Response:
    """``POST /api/platform/callback`` as the ``/connect/callback`` page would."""
    body: dict[str, Any] = {
        "state": query_of(started)["state"],
        "iss": started["platform_origin"],
    }
    if code is not None:
        body["code"] = code
    body.update(over)
    return client.post("/api/platform/callback", json=body)


def linked(client: TestClient, fake: FakePlatform) -> str:
    """Connect, allow and exchange; the ``link_id`` of the pending link."""
    started = connect(client, fake)
    allow(fake, started)
    response = callback(client, started)
    assert response.status_code == 200, response.text
    return response.json()["link_id"]


def link(client: TestClient, link_id: str, root: Path) -> httpx.Response:
    """``POST /api/projects`` consuming ``link_id`` over ``root``."""
    return client.post(
        "/api/projects",
        json={"source": "platform", "link_id": link_id, "path": str(root)},
    )


def pending_of(client: TestClient, link_id: str) -> dict:
    """``GET /api/platform/pending/{link_id}``: what the picker is given."""
    response = client.get(f"/api/platform/pending/{link_id}")
    assert response.status_code == 200, response.text
    return response.json()


def mirror(env: SimpleNamespace, name: str, remote: str | None = CLONE_URL) -> Path:
    """A fresh checkout under the shared folder with ``remote`` as its ``origin``."""
    root = make_repo(env.shared, name, remote=remote)
    seed_codegraph(root)
    return root


def head_of(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").strip()


def project_id(slug: str) -> int:
    """The id of the project called ``slug`` (the add response names no id)."""
    with portal_db.get_session() as session:
        return session.exec(select(Project.id).where(Project.slug == slug)).one()


def link_row(slug: str) -> PlatformLink:
    """The :class:`PlatformLink` of the project called ``slug``."""
    with portal_db.get_session() as session:
        row = session.get(PlatformLink, project_id(slug))
        assert row is not None
        session.expunge(row)
        return row


# ---------------------------------------------------------------------------
# Connect and callback
# ---------------------------------------------------------------------------


def test_connect_checks_the_platform_and_names_the_machine(
    portal: TestClient, fake: FakePlatform
) -> None:
    started = connect(portal, fake)
    assert started["platform_origin"] == fake.platform_origin
    assert started["redirect_uri"] == "http://127.0.0.1:8765/connect/callback"
    assert started["known_platform"] is False  # nothing links to it yet
    params = query_of(started)
    assert params["client_name"] == "laptop"
    assert params["redirect_uri"] == started["redirect_uri"]
    assert len(params["state"]) >= 32
    # The version check happened, and no credential was sent with it.
    meta = fake.requests[0]
    assert (meta.method, meta.url.path) == ("GET", "/api/v1/meta")
    assert "authorization" not in meta.headers
    # The prefilled machine name comes from the portal state.
    hostname = portal.get("/api/portal/state").json()["hostname"]
    assert 1 <= len(hostname) <= 64


def test_connect_refuses_a_bad_url_or_machine_name(
    portal: TestClient, fake: FakePlatform
) -> None:
    for url, code in (
        ("http://wg.example.com", "bad_platform_url"),  # no dev switch
        ("https://wg.example.com/x", "bad_platform_url"),
        ("https://user:pw@wg.example.com", "bad_platform_url"),
    ):
        response = portal.post(
            "/api/platform/connect", json={"platform_url": url, "client_name": "a"}
        )
        assert response.status_code == 422, (url, response.text)
        assert response.json()["code"] == code, url
    bad_name = portal.post(
        "/api/platform/connect",
        json={"platform_url": fake.platform_origin, "client_name": "laptop/1"},
    )
    assert bad_name.status_code == 422
    assert bad_name.json()["code"] == "bad_client_name"
    assert fake.requests == []  # neither reached the platform


def test_callback_refuses_iss_mismatch(portal: TestClient, fake: FakePlatform) -> None:
    """RFC 9207: an answer from another platform is refused before the exchange."""
    started = connect(portal, fake)
    allow(fake, started)
    seen = len(fake.requests)
    for iss in ("https://evil.example", fake.platform_origin + ".evil.example", None):
        response = callback(portal, started, iss=iss)
        assert response.status_code == 422, (iss, response.text)
        assert response.json() == {
            "error": (
                "the answer came from another platform than the one you connected to"
            ),
            "code": "issuer_mismatch",
        }
        assert len(fake.requests) == seen, iss  # the code was never exchanged
        # The connect is single use, so the next attempt is already expired.
        started = connect(portal, fake)
        allow(fake, started)
        seen = len(fake.requests)


def test_callback_unknown_state_expired(portal: TestClient, fake: FakePlatform) -> None:
    unknown = portal.post(
        "/api/platform/callback",
        json={"state": "nostate", "code": CODE, "iss": fake.platform_origin},
    )
    assert unknown.status_code == 410
    assert unknown.json() == {
        "error": "this connect expired or was already used; connect again",
        "code": "connect_expired",
    }
    # A replayed callback is the same refusal: the connect is single use.
    started = connect(portal, fake)
    allow(fake, started)
    assert callback(portal, started).status_code == 200
    replay = callback(portal, started)
    assert replay.status_code == 410
    assert replay.json()["code"] == "connect_expired"


def test_access_denied_callback(portal: TestClient, fake: FakePlatform) -> None:
    """Cancel on the platform is ``409 access_denied``; another error is generic."""
    started = connect(portal, fake)
    denied = callback(portal, started, code=None, error="access_denied")
    assert denied.status_code == 409
    assert denied.json() == {"error": "the link was cancelled", "code": "access_denied"}

    started = connect(portal, fake)
    other = callback(portal, started, code=None, error="server_error")
    assert other.status_code == 409
    assert other.json()["code"] == "connect_failed"

    started = connect(portal, fake)
    nothing = callback(portal, started, code=None)
    assert nothing.status_code == 422
    assert nothing.json()["code"] == "connect_failed"


def test_link_refuses_foreign_api_origin(
    portal: TestClient, fake: FakePlatform
) -> None:
    """The org origin is computed locally; a reply naming another one is refused."""
    fake.responses["token"] = (
        200,
        {
            "token": TOKEN,
            "org": ORG,
            "project": status_body(),
            "api_version": 1,
            "api_origin": "https://acme.evil.example",
        },
    )
    started = connect(portal, fake)
    allow(fake, started)
    response = callback(portal, started)
    assert response.status_code == 502, response.text
    assert response.json() == {
        "error": "the platform named an unexpected address for its org",
        "code": "bad_platform_reply",
    }
    # The token the platform just handed out is revoked, best effort.
    assert [r.method for r in fake.requests if r.url.path.endswith("/token")] == [
        "POST",
        "DELETE",
    ]


def test_redirect_uri_follows_browser_port(
    env: SimpleNamespace,
    scanner: SimpleNamespace,
    fake: FakePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loopback port is the browser's (Vite in development), not the portal's."""
    monkeypatch.setenv("WHYGRAPH_DEV_ORIGINS", "http://127.0.0.1:5174")
    with client_for() as client:
        assert (
            client.post("/api/portal/setup", json={"display_name": "Tess"}).status_code
            == 201
        )
        client.app.state.portal.platform_transport = _transport(fake)
        # No Origin header: the portal's own port.
        started = connect(client, fake)
        assert started["redirect_uri"] == "http://127.0.0.1:8765/connect/callback"
        dev = client.post(
            "/api/platform/connect",
            json={"platform_url": fake.platform_origin, "client_name": "laptop"},
            headers={"Origin": "http://127.0.0.1:5174"},
        )
        assert dev.status_code == 200, dev.text
        assert dev.json()["redirect_uri"] == "http://127.0.0.1:5174/connect/callback"
        assert query_of(dev.json())["redirect_uri"] == dev.json()["redirect_uri"]
        # An origin the portal does not serve never becomes a redirect target.
        refused = client.post(
            "/api/platform/connect",
            json={"platform_url": fake.platform_origin, "client_name": "laptop"},
            headers={"Origin": "http://127.0.0.1:9999"},
        )
        assert refused.status_code == 403, refused.text


# ---------------------------------------------------------------------------
# The pending link
# ---------------------------------------------------------------------------


def test_pending_lists_the_matching_checkouts(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    root = mirror(env, "demo-checkout")
    other = mirror(env, "unrelated", remote="https://git.example.com/acme/other.git")
    link_id = linked(portal, fake)
    body = portal.get(f"/api/platform/pending/{link_id}")
    assert body.status_code == 200, body.text
    pending = body.json()
    assert pending["link_id"] == link_id
    assert pending["org"] == ORG
    assert pending["clone_url"] == CLONE_URL
    assert pending["clone_command"] == f"git clone {CLONE_URL}"
    assert pending["slug_taken"] is False
    assert pending["reconnect"] is None  # nothing is linked yet
    assert [c["path"] for c in pending["candidates"]] == [str(root)]
    assert pending["candidates"][0]["match"] == "origin"
    assert str(other) in [r["path"] for r in pending["other_repos"]]
    assert str(root) not in [r["path"] for r in pending["other_repos"]]


def test_pending_link_expiry_revokes(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    """An expired or abandoned link's token is revoked on the platform."""
    now = [1000.0]
    state = portal.app.state.portal
    state.pending_links = PendingLinks(clock=lambda: now[0], ttl=1800)
    link_id = linked(portal, fake)
    assert fake.tokens[TOKEN]["revoked"] is None
    now[0] += 1801
    expired = portal.get(f"/api/platform/pending/{link_id}")
    assert expired.status_code == 410
    assert expired.json() == LINK_EXPIRED
    assert fake.tokens[TOKEN]["revoked"] == "removed_locally"
    # An unknown link is the same refusal, and the link cannot be consumed.
    assert link(portal, link_id, mirror(env, "demo-checkout")).status_code == 410


def test_deleting_a_pending_link_revokes_it_now(
    portal: TestClient, fake: FakePlatform
) -> None:
    link_id = linked(portal, fake)
    dropped = portal.delete(f"/api/platform/pending/{link_id}")
    assert dropped.status_code == 200, dropped.text
    assert dropped.json() == {"revoked": True}
    assert fake.tokens[TOKEN]["revoked"] == "removed_locally"
    again = portal.delete(f"/api/platform/pending/{link_id}")
    assert again.status_code == 410
    assert again.json() == LINK_EXPIRED


# ---------------------------------------------------------------------------
# The match rule (plan section 0.1 #12)
# ---------------------------------------------------------------------------


def test_link_refuses_origin_mismatch(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    """Another repository's checkout, with none of the platform's history."""
    root = mirror(env, "elsewhere", remote="https://git.example.com/acme/other.git")
    response = link(portal, linked(portal, fake), root)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "origin_mismatch"
    assert body["clone_url"] == CLONE_URL
    assert body["origin"] == "https://git.example.com/acme/other.git"
    assert "does not hold the platform's last scanned commit" in body["error"]
    with portal_db.get_session() as session:
        assert session.exec(select(Project.id)).all() == []


def test_link_accepts_shared_history_with_other_origin(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    """A mirror or same-history fork: the platform's head exists locally."""
    root = mirror(env, "fork", remote="git@github.com:someone/demo-fork.git")
    fake.projects[SLUG] = status_body(last_scanned_head=head_of(root))
    response = link(portal, linked(portal, fake), root)
    assert response.status_code == 201, response.text
    assert response.json()["project"]["slug"] == SLUG


def test_link_without_platform_scan_needs_origin_match(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    """No ``last_scanned_head``: there is no history to compare, so origin rules."""
    fake.projects[SLUG] = status_body(last_scanned_head=None)
    fork = mirror(env, "fork", remote="git@github.com:someone/demo-fork.git")
    refused = link(portal, linked(portal, fake), fork)
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "origin_mismatch"
    assert "has not scanned the project yet" in refused.json()["error"]
    # The same checkout with the project's own origin is accepted.
    same = mirror(env, "demo-checkout")
    assert link(portal, linked(portal, fake), same).status_code == 201


def test_link_matches_non_github_host_clone_url(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    """Host, owner and name match across URL forms, cases and ssh ports."""
    fake.projects[SLUG] = status_body(
        clone_url="https://git.acme.internal/ACME/Demo.git", last_scanned_head=None
    )
    root = mirror(
        env, "demo-checkout", remote="ssh://git@Git.Acme.Internal:2222/acme/demo"
    )
    link_id = linked(portal, fake)
    pending = portal.get(f"/api/platform/pending/{link_id}").json()
    assert [c["path"] for c in pending["candidates"]] == [str(root)]
    response = link(portal, link_id, root)
    assert response.status_code == 201, response.text
    assert link_row(SLUG).clone_url == "https://git.acme.internal/ACME/Demo.git"


def test_link_refuses_slug_collision(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    """The local slug is the platform slug (plan section 0.2 #7)."""
    add_local(portal, mirror(env, SLUG, remote=None))
    root = mirror(env, "demo-checkout")
    link_id = linked(portal, fake)
    # A *local* project of that name is a genuine collision, never a reconnect.
    pending = pending_of(portal, link_id)
    assert pending["slug_taken"] is True
    assert pending["reconnect"] is None
    response = link(portal, link_id, root)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "slug_taken"
    # The link survives the refusal, so the user can remove the project and retry.
    assert portal.get(f"/api/platform/pending/{link_id}").status_code == 200


def test_link_refuses_a_path_outside_the_shared_folders(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    outside = make_repo(env.tmp / "outside", "demo-checkout", remote=CLONE_URL)
    response = link(portal, linked(portal, fake), outside)
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "not_shared"


# ---------------------------------------------------------------------------
# The whole flow
# ---------------------------------------------------------------------------


def test_link_full_flow(
    portal: TestClient,
    fake: FakePlatform,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
) -> None:
    """Connect -> callback -> pending -> link -> Initialize -> CodeGraph-only scan."""
    root = mirror(env, "demo-checkout")
    link_id = linked(portal, fake)
    added = link(portal, link_id, root)
    assert added.status_code == 201, added.text
    project = added.json()["project"]
    assert (project["slug"], project["source"]) == (SLUG, "platform")
    assert project["initialized_at"] is None

    with portal_db.get_session() as session:
        row = session.exec(select(Project).where(Project.slug == SLUG)).one()
        assert (row.source, row.root) == ("platform", str(root))
        assert row.remote_url == CLONE_URL
    saved = link_row(SLUG)
    assert saved.platform_origin == fake.platform_origin
    assert saved.api_origin == f"https://{ORG}.wg.example.com"
    assert (saved.org_slug, saved.remote_slug, saved.remote_name) == (ORG, SLUG, "Demo")
    assert (saved.default_branch, saved.status) == ("main", "ok")
    assert saved.last_platform_head == status_body()["last_scanned_head"]
    assert decrypt(saved.token_ciphertext) == TOKEN
    assert saved.token_hint == "…" + TOKEN[-4:]

    # The pending link is consumed, and its token was not revoked.
    assert portal.get(f"/api/platform/pending/{link_id}").status_code == 410
    assert fake.tokens[TOKEN]["revoked"] is None

    # The repo marks as registered, so it is not offered again.
    repos = {
        r["path"]: r["registered"]
        for r in portal.get("/api/portal/repos").json()["repos"]
    }
    assert repos[str(root)] is True

    # Initialize: no local DB, and the preview names no leftover.
    init = init_project(portal, SLUG)
    assert init["initialized"] is True
    assert init["ignored_db"] is None
    assert not (root / ".whygraph" / "whygraph.db").exists()

    # The first scan is CodeGraph-only.
    run_id = portal.post(f"/api/projects/{SLUG}/scans", json={}).json()["run_id"]
    run = wait_run(portal, SLUG, run_id)
    assert run["status"] == "ok", run
    assert "--codegraph-only" in runner_flags(scanner.calls()[-1])
    assert not (root / ".whygraph" / "whygraph.db").exists()


def test_relinking_a_former_local_checkout_notes_its_leftover_db(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    """A leftover ``whygraph.db`` is never opened, and the preview says so."""
    root = mirror(env, "demo-checkout")
    (root / ".whygraph").mkdir(exist_ok=True)
    leftover = root / ".whygraph" / "whygraph.db"
    leftover.write_bytes(b"not a database")
    assert link(portal, linked(portal, fake), root).status_code == 201
    for dry_run in (True, False):
        init = init_project(portal, SLUG, dry_run=dry_run)
        assert init["ignored_db"] == ".whygraph/whygraph.db", dry_run
    assert leftover.read_bytes() == b"not a database"


def test_link_token_never_exposed(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    """The token lives encrypted in the portal DB and in no response or log."""
    root = mirror(env, "demo-checkout")
    link_id = linked(portal, fake)
    texts = [portal.get(f"/api/platform/pending/{link_id}").text]
    texts.append(link(portal, link_id, root).text)
    texts.append(portal.get("/api/projects").text)
    texts.append(portal.get(f"/api/projects/{SLUG}").text)
    texts.append(portal.get(f"/api/projects/{SLUG}/config").text)
    texts.append(init_project(portal, SLUG) and portal.get("/api/portal/state").text)
    run_id = portal.post(f"/api/projects/{SLUG}/scans", json={}).json()["run_id"]
    assert wait_run(portal, SLUG, run_id)["status"] == "ok"
    texts.append(portal.get(f"/api/projects/{SLUG}/scans").text)
    texts.append(portal.get(f"/api/projects/{SLUG}/scans/{run_id}/log").text)
    for text in texts:
        assert TOKEN not in text
        assert "wgc_" not in text
    # Only the ciphertext holds it, and the child never saw it.
    with portal_db.get_session() as session:
        row = session.exec(select(PlatformLink)).one()
        assert TOKEN not in row.token_ciphertext
        assert decrypt(row.token_ciphertext) == TOKEN


def test_two_platforms_linked_side_by_side(
    env: SimpleNamespace, scanner: SimpleNamespace, fake: FakePlatform
) -> None:
    """Two platforms, two projects, two tokens: nothing is shared between them."""
    second = FakePlatform("https://wg2.example.com")
    second.projects = {
        "api": status_body(
            "api", name="API", clone_url="https://github.com/acme/api.git"
        )
    }
    first_root = None
    with client_for() as client:
        assert (
            client.post("/api/portal/setup", json={"display_name": "Tess"}).status_code
            == 201
        )
        client.app.state.portal.platform_transport = _transport(fake, second)
        first_root = mirror(env, "demo-checkout")
        api_root = mirror(env, "api-checkout", remote="https://github.com/acme/api.git")

        demo = link(client, linked(client, fake), first_root)
        assert demo.status_code == 201, demo.text
        started = connect(client, second)
        assert started["known_platform"] is False  # a different platform
        allow(second, started, slug="api", token="wgc_" + "B" * 40)
        api_link = callback(client, started)
        assert api_link.status_code == 200, api_link.text
        api = link(client, api_link.json()["link_id"], api_root)
        assert api.status_code == 201, api.text

        rows = {r.remote_slug: r for r in (link_row(SLUG), link_row("api"))}
        assert rows[SLUG].platform_origin == "https://wg.example.com"
        assert rows["api"].platform_origin == "https://wg2.example.com"
        assert rows[SLUG].api_origin == "https://acme.wg.example.com"
        assert rows["api"].api_origin == "https://acme.wg2.example.com"
        assert decrypt(rows[SLUG].token_ciphertext) == TOKEN
        assert decrypt(rows["api"].token_ciphertext) == "wgc_" + "B" * 40
        # Connecting to a platform this portal already links to is known.
        assert connect(client, fake)["known_platform"] is True


# ---------------------------------------------------------------------------
# Production has no link routes
# ---------------------------------------------------------------------------


def test_platform_routes_404_in_production(production_env: SimpleNamespace) -> None:
    """``/api/platform/*`` is local mode's own; production answers ``404``."""
    assert LOCAL_ONLY_ROUTES  # the inventory names them
    with prod_portal() as client:
        for path, method in sorted(LOCAL_ONLY_ROUTES):
            url = path.replace("{link_id}", "nolink").replace("{slug}", "api")
            for prefix in (at(), at("quokka")):
                response = client.request(method, prefix + url, json={})
                assert response.status_code == 404, (
                    prefix,
                    method,
                    path,
                    response.text,
                )
                assert response.json() == {"error": "not found"}, (prefix, method, path)
