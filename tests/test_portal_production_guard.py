"""The request guard in production mode (M2c plan sections 4.5 and 5.4).

``Host`` is classified against ``WHYGRAPH_BASE_URL`` (``421`` for anything
that is neither the base host nor one org label under it), an ``Origin`` must
be exactly the request's own host, every response carries the security
headers, and the identity is only resolved on ``/api`` and ``/mcp``.
"""

# ruff: noqa: F811 -- pytest fixtures (`env`, `production_env`) imported from test_portal_app

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
from starlette.middleware.cors import CORSMiddleware

from test_portal_app import (  # noqa: F401 -- fixtures
    PORT,
    at,
    env,
    prod_portal,
    production_env,
    signed_in,
)
from test_portal_mcp import MCP_HEADERS, _rpc
from whygraph.portal import db as portal_db
from whygraph.portal import sessions
from whygraph.portal.models import User
from whygraph.portal.orgs import add_member, create_org

LOGIN_REQUIRED = {"error": "sign-in required", "code": "login_required"}
SECURITY_HEADERS = {
    "x-frame-options": "DENY",
    "content-security-policy": (
        "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
    ),
    "referrer-policy": "same-origin",
    "x-content-type-options": "nosniff",
}


@pytest.fixture
def spa(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tiny built SPA (index.html plus one asset)."""
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>WhyGraph</title>")
    (static / "assets" / "app.js").write_text("console.log('hi')")
    monkeypatch.setattr("whygraph.serve.app._STATIC_DIR", static)
    return static


@pytest.fixture
def world(production_env: SimpleNamespace, spa: Path) -> Iterator[SimpleNamespace]:
    """A production portal with orgs ``quokka`` (ann) and ``narwhal`` (bob)."""
    with prod_portal() as client:
        with portal_db.get_session() as session:
            ids = {}
            for slug, name in (("quokka", "ann"), ("narwhal", "bob")):
                org = create_org(session, slug=slug, name=slug.title())
                user = User(display_name=name.title(), email=f"{name}@example.com")
                session.add(user)
                session.flush()
                add_member(session, org_id=org.id, user_id=user.id, role="owner")
                ids[name] = user.id
        yield SimpleNamespace(client=client, **ids)


def _assert_security_headers(response, *, https: bool = False) -> None:  # noqa: ANN001
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get(name) == value, (name, response.headers)
    hsts = response.headers.get("strict-transport-security")
    if https:
        assert hsts == "max-age=31536000; includeSubDomains"
    else:
        assert hsts is None


# ---------------------------------------------------------------------------
# Host
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "evil.example.com:8765",
        "127.0.0.1:8765",  # local mode's hosts mean nothing in production
        "localhost:8765",
        "www.whygraph.localhost:8765",  # never an org host
        "api.whygraph.localhost:8765",
        "a.b.whygraph.localhost:8765",  # two labels deep
        "whygraph.localhost.evil.com:8765",  # lookalike suffix
        "quokka.whygraph.localhost.evil.com:8765",
        "evilwhygraph.localhost:8765",
        "whygraph.localhost:9999",  # another port
        "whygraph.localhost",  # a missing port
        "Not_A_Slug.whygraph.localhost:8765",
    ],
)
@pytest.mark.parametrize(
    "path", ["/api/portal/state", "/", "/p/api/explorer", "/assets/app.js", "/mcp/api"]
)
def test_an_unknown_host_is_421_on_every_path(
    world: SimpleNamespace, host: str, path: str
) -> None:
    response = world.client.get(f"http://{host}{path}")
    assert response.status_code == 421, response.text
    assert response.json() == {"error": "unknown Host"}
    _assert_security_headers(response)


@pytest.mark.parametrize("prefix", [at(), at("quokka"), at("nosuchorg")])
def test_the_base_and_org_hosts_are_served(world: SimpleNamespace, prefix: str) -> None:
    assert world.client.get(prefix + "/api/portal/state").status_code == 200
    page = world.client.get(prefix + "/p/api/explorer")
    assert page.status_code == 200 and "WhyGraph" in page.text


# ---------------------------------------------------------------------------
# Origin, Sec-Fetch-Site, the custom header
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        at("narwhal"),  # another org's page
        at(),  # the base host's page writing to an org
        "http://evil.example.com",
        "https://quokka.whygraph.localhost:8765",  # another scheme
        "http://quokka.whygraph.localhost:9999",
        "http://quokka.whygraph.localhost",
        "null",
    ],
)
def test_a_write_from_another_origin_is_refused(
    world: SimpleNamespace, origin: str
) -> None:
    signed_in(world.client, world.ann)
    response = world.client.post(
        at("quokka") + "/api/projects",
        json={"source": "github", "url": "https://github.com/o/r"},
        headers={"Origin": origin},
    )
    assert response.status_code == 403
    assert response.json() == {"error": "origin not allowed"}


def test_the_base_host_accepts_only_its_own_origin(world: SimpleNamespace) -> None:
    ok = world.client.get(at() + "/api/portal/state", headers={"Origin": at()})
    assert ok.status_code == 200
    other = world.client.get(
        at() + "/api/portal/state", headers={"Origin": at("quokka")}
    )
    assert other.status_code == 403


def test_an_uppercase_host_with_its_lowercase_origin_is_allowed(
    world: SimpleNamespace,
) -> None:
    signed_in(world.client, world.ann)
    response = world.client.get(
        at("quokka") + "/api/portal/state",
        headers={
            "Host": "QUOKKA.WhyGraph.LOCALHOST:8765",
            "Origin": "http://quokka.whygraph.localhost:8765",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["host_kind"] == "org"


@pytest.mark.parametrize(
    ("site", "status"),
    [("same-site", 403), ("cross-site", 403), ("same-origin", 200), ("none", 200)],
)
@pytest.mark.parametrize("prefix", [at(), at("quokka")])
def test_fetch_metadata(
    world: SimpleNamespace, site: str, status: int, prefix: str
) -> None:
    response = world.client.get(
        prefix + "/api/portal/state", headers={"Sec-Fetch-Site": site}
    )
    assert response.status_code == status


def test_a_cross_site_navigation_to_a_page_is_allowed(world: SimpleNamespace) -> None:
    response = world.client.get(
        at("quokka") + "/p/api/explorer",
        headers={"Sec-Fetch-Site": "cross-site", "Origin": "http://evil.example.com"},
    )
    assert response.status_code == 200


def test_every_api_request_needs_the_client_header(world: SimpleNamespace) -> None:
    for prefix in (at(), at("quokka")):
        response = world.client.get(
            prefix + "/api/portal/state", headers={"X-WhyGraph-Client": ""}
        )
        assert response.status_code == 403
        assert response.json() == {"error": "missing x-whygraph-client header"}


# ---------------------------------------------------------------------------
# Sessions and hosts
# ---------------------------------------------------------------------------


def test_org_routes_need_a_session_on_the_org_host(world: SimpleNamespace) -> None:
    response = world.client.get(at("quokka") + "/api/projects")
    assert response.status_code == 401
    assert response.json() == LOGIN_REQUIRED
    signed_in(world.client, world.ann)
    assert world.client.get(at("quokka") + "/api/projects").status_code == 200


def test_org_routes_on_the_base_host_are_404(world: SimpleNamespace) -> None:
    signed_in(world.client, world.ann)
    for path in ("/api/projects", "/api/portal/defaults"):
        response = world.client.get(at() + path)
        assert response.status_code == 404, path
        assert response.json() == {"error": "not found"}


def test_a_session_serves_only_its_own_orgs(world: SimpleNamespace) -> None:
    signed_in(world.client, world.ann)
    state = world.client.get(at("quokka") + "/api/portal/state").json()
    assert state["org"] == {"slug": "quokka", "name": "Quokka", "role": "owner"}
    other = world.client.get(at("narwhal") + "/api/portal/state").json()
    assert other["org"] is None and other["user"]["role"] is None
    assert world.client.get(at("narwhal") + "/api/projects").status_code == 404


@pytest.mark.parametrize("path", ["/mcp/api", "/mcp", "/mcp/api/extra"])
@pytest.mark.parametrize("with_session", [False, True])
def test_mcp_does_not_exist_in_production(
    world: SimpleNamespace, path: str, with_session: bool
) -> None:
    if with_session:
        signed_in(world.client, world.ann)
    for prefix in (at("quokka"), at()):
        response = world.client.post(
            prefix + path, json=_rpc("tools/list"), headers=MCP_HEADERS
        )
        assert response.status_code == 404, (prefix, response.text)
        assert response.json() == {"error": f"no MCP endpoint {path}"}


def test_pages_and_assets_never_look_up_a_session(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = sessions.lookup

    def spy(token: str):  # noqa: ANN202
        calls.append(token)
        return real(token)

    monkeypatch.setattr(sessions, "lookup", spy)
    signed_in(world.client, world.ann)
    for path in ("/", "/p/api/explorer", "/assets/app.js", "/settings"):
        assert world.client.get(at("quokka") + path).status_code == 200, path
    assert calls == []
    assert world.client.get(at("quokka") + "/api/portal/state").status_code == 200
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Response headers
# ---------------------------------------------------------------------------


def test_security_headers_on_api_spa_and_error_responses(
    world: SimpleNamespace,
) -> None:
    client = world.client
    responses = {
        "api": client.get(at("quokka") + "/api/portal/state"),
        "401": client.get(at("quokka") + "/api/projects"),
        "403": client.get(
            at("quokka") + "/api/portal/state", headers={"Sec-Fetch-Site": "same-site"}
        ),
        "404": client.get(at() + "/mcp/api"),
        "421": client.get("http://evil.example.com:8765/"),
        "spa": client.get(at("quokka") + "/p/api/explorer"),
        "asset": client.get(at() + "/assets/app.js"),
    }
    for name, response in responses.items():
        _assert_security_headers(response)
        cache = response.headers.get("cache-control")
        if name in ("api", "401", "403"):
            assert cache == "no-store", name
        elif name in ("spa", "asset"):
            assert cache != "no-store", name


def test_hsts_only_for_an_https_base_url(
    production_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = f"https://whygraph.localhost:{PORT}"
    monkeypatch.setenv("WHYGRAPH_BASE_URL", base)
    with prod_portal(base_url=base) as client:
        api = client.get(base + "/api/portal/state")
        assert api.status_code == 200
        _assert_security_headers(api, https=True)
        page = client.get(f"https://quokka.whygraph.localhost:{PORT}/")
        _assert_security_headers(page, https=True)
        # An https page's own origin is the https one.
        refused = client.get(
            base + "/api/portal/state",
            headers={"Origin": f"http://whygraph.localhost:{PORT}"},
        )
        assert refused.status_code == 403


def test_local_mode_sends_no_production_headers(env: SimpleNamespace) -> None:
    with prod_portal(base_url=f"http://127.0.0.1:{PORT}") as client:
        response = client.get("/api/portal/state")
        assert response.status_code == 200
        assert "x-frame-options" not in response.headers
        assert "cache-control" not in response.headers


def test_no_cors_preflight_is_ever_granted(world: SimpleNamespace) -> None:
    response = world.client.options(
        at("quokka") + "/api/projects",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-whygraph-client",
        },
    )
    assert response.status_code == 403
    assert not any(h.startswith("access-control-allow") for h in response.headers)
    assert not any(m.cls is CORSMiddleware for m in world.client.app.user_middleware)


# ---------------------------------------------------------------------------
# Proxies
# ---------------------------------------------------------------------------


def test_an_untrusted_forwarded_for_is_warned_about_once(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(logging.getLogger("whygraph"), "propagate", True)
    caplog.set_level(logging.WARNING, logger="whygraph.portal.security")
    headers = {"X-Forwarded-For": "198.51.100.7"}
    world.client.get(at() + "/api/portal/state")
    assert "X-Forwarded-For" not in caplog.text
    for _ in range(3):
        world.client.get(at() + "/api/portal/state", headers=headers)
    warnings = [r for r in caplog.records if "X-Forwarded-For" in r.getMessage()]
    assert len(warnings) == 1
    assert "WHYGRAPH_TRUSTED_PROXIES" in warnings[0].getMessage()


def test_no_forwarding_warning_with_trusted_proxies(
    production_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(logging.getLogger("whygraph"), "propagate", True)
    caplog.set_level(logging.WARNING, logger="whygraph.portal.security")
    monkeypatch.setenv("WHYGRAPH_TRUSTED_PROXIES", "10.0.0.1")
    with prod_portal() as client:
        client.get(at() + "/api/portal/state", headers={"X-Forwarded-For": "1.2.3.4"})
    assert "X-Forwarded-For" not in caplog.text
