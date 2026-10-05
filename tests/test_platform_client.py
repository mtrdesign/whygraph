"""The connected portal's platform client (M2e step 5, plan section 4.7 / 4.11)."""

from __future__ import annotations

import httpx
import pytest

from platform_fake import (
    ORG,
    PLATFORM_ORIGIN,
    SLUG,
    TOKEN,
    FakePlatform,
    error_body,
    status_body,
)
from whygraph.api_v1 import EvidenceIn, RationaleIn, StatusOut, TargetIn
from whygraph.portal.platform_client import (
    DEV_PLATFORM_HTTP_ENV,
    MAX_BODY_CHARS,
    MAX_RESPONSE_BYTES,
    MAX_TITLE_CHARS,
    LocalhostTransport,
    PlatformHttp,
    PlatformRefused,
    PlatformUnreachable,
    UpdateRequired,
    api_origin_for,
    cap_strings,
    dev_platform_http_enabled,
    link_status_for,
    parse_platform_url,
)

VERIFIER = "v" * 43
REDIRECT = "http://127.0.0.1:8765/connect/callback"
TARGET = TargetIn(path="a.py", line_start=1, line_end=2)


def _client(fake: FakePlatform, *, bound: bool = True, **kw) -> PlatformHttp:
    client = PlatformHttp(
        platform_origin=fake.platform_origin,
        transport=httpx.MockTransport(fake.handle),
        **kw,
    )
    if bound:
        fake.add_token()
        client.bind(ORG, TOKEN)
    return client


# -- URL parsing ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://wg.example.com", "https://wg.example.com"),
        ("https://WG.example.com/", "https://wg.example.com"),
        ("https://wg.example.com:443", "https://wg.example.com"),
        ("https://wg.example.com:8443", "https://wg.example.com:8443"),
    ],
)
def test_parse_platform_url_https_ok(raw, expected):
    assert parse_platform_url(raw, allow_http=False) == expected


def test_platform_http_refused_without_dev_switch():
    for raw in ("http://wg.example.com", "http://localhost:5173", "http://127.0.0.1"):
        with pytest.raises(ValueError, match="https"):
            parse_platform_url(raw, allow_http=False)


def test_platform_http_loopback_ok_with_dev_switch():
    assert (
        parse_platform_url("http://whygraph.localhost:5173", allow_http=True)
        == "http://whygraph.localhost:5173"
    )
    assert parse_platform_url("http://127.0.0.1:80", allow_http=True) == (
        "http://127.0.0.1"
    )
    with pytest.raises(ValueError, match="https"):
        parse_platform_url("http://wg.example.com", allow_http=True)


@pytest.mark.parametrize(
    "raw",
    [
        "https://user:pw@wg.example.com",
        "https://wg.example.com/path",
        "https://wg.example.com?x=1",
        "https://wg.example.com#frag",
        "ftp://wg.example.com",
        "wg.example.com",
        "https://",
        "https://wg.example.com:99999",
    ],
)
def test_parse_platform_url_refused(raw):
    with pytest.raises(ValueError):
        parse_platform_url(raw, allow_http=True)


def test_dev_switch_reads_env(monkeypatch):
    monkeypatch.delenv(DEV_PLATFORM_HTTP_ENV, raising=False)
    assert not dev_platform_http_enabled()
    monkeypatch.setenv(DEV_PLATFORM_HTTP_ENV, "1")
    assert dev_platform_http_enabled()
    monkeypatch.setenv(DEV_PLATFORM_HTTP_ENV, "true")
    assert not dev_platform_http_enabled()


def test_api_origin_for():
    assert api_origin_for("https://wg.example.com", "acme") == (
        "https://acme.wg.example.com"
    )
    assert api_origin_for("http://whygraph.localhost:5173", "acme") == (
        "http://acme.whygraph.localhost:5173"
    )
    for bad in ("", "Acme", "a.b", "-x", "x/y", "a" * 80):
        with pytest.raises(ValueError):
            api_origin_for("https://wg.example.com", bad)


def test_client_refuses_http_platform_without_dev_switch(monkeypatch):
    monkeypatch.delenv(DEV_PLATFORM_HTTP_ENV, raising=False)
    with pytest.raises(ValueError):
        PlatformHttp(platform_origin="http://localhost:5173")


# -- the *.localhost transport ----------------------------------------------


def test_localhost_transport_rewrites_host():
    seen: list[httpx.Request] = []

    def inner(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    with httpx.Client(
        transport=LocalhostTransport(httpx.MockTransport(inner))
    ) as client:
        client.get("http://acme.whygraph.localhost:5173/api/v1/x")
        client.get("http://localhost/y")
        client.get("https://wg.example.com/z")
    assert seen[0].url.host == "127.0.0.1" and seen[0].url.port == 5173
    assert seen[0].headers["host"] == "acme.whygraph.localhost:5173"
    assert seen[1].url.host == "127.0.0.1" and seen[1].headers["host"] == "localhost"
    assert seen[2].url.host == "wg.example.com"
    assert seen[2].headers["host"] == "wg.example.com"


def test_client_reaches_localhost_platform_through_rewrite(monkeypatch):
    monkeypatch.setenv(DEV_PLATFORM_HTTP_ENV, "1")
    fake = FakePlatform("http://whygraph.localhost:5173")
    fake.add_token()
    client = PlatformHttp(
        platform_origin=fake.platform_origin,
        transport=httpx.MockTransport(fake.handle),
    )
    assert client.meta().api_version == 1
    client.bind(ORG, TOKEN)
    assert client.status(SLUG).slug == SLUG
    assert {r.url.host for r in fake.requests} == {"127.0.0.1"}
    assert fake.requests[-1].headers["host"] == "acme.whygraph.localhost:5173"


# -- origins, bearer, redirects, caps ---------------------------------------


def test_bearer_only_on_api_origin():
    fake = FakePlatform()
    client = _client(fake)
    client.meta()
    fake.add_code("c1", verifier=VERIFIER, redirect_uri=REDIRECT)
    client.exchange("c1", VERIFIER, REDIRECT)
    client.status(SLUG)
    meta_req, token_req, status_req = fake.requests
    assert "authorization" not in meta_req.headers
    assert "authorization" not in token_req.headers
    assert status_req.headers["authorization"] == f"Bearer {TOKEN}"
    assert all(r.headers["x-whygraph-client"] == "1" for r in fake.requests)


def test_other_origins_refused():
    fake = FakePlatform()
    client = _client(fake)
    for base in ("https://evil.example.com", "https://other.wg.example.com"):
        with pytest.raises(PlatformUnreachable, match="unexpected origin"):
            client._call("GET", base, "/x", bearer=True)
    assert fake.requests == []


def test_unbound_client_cannot_call_v1():
    fake = FakePlatform()
    client = _client(fake, bound=False)
    with pytest.raises(PlatformUnreachable):
        client.status(SLUG)
    assert fake.requests == []


def test_redirects_are_not_followed():
    fake = FakePlatform()
    fake.failure = "redirect"
    client = _client(fake)
    with pytest.raises(PlatformUnreachable, match="unavailable"):
        client.meta()
    assert len(fake.requests) == 1


def test_platform_response_size_capped():
    fake = FakePlatform()
    client = _client(fake)
    fake.failure = "oversized"
    with pytest.raises(PlatformUnreachable, match="too large"):
        client.meta()
    fake.failure = None
    fake.responses["history"] = (200, {"blob": "x" * (MAX_RESPONSE_BYTES + 1)})
    with pytest.raises(PlatformUnreachable, match="too large"):
        client.history(SLUG, "a.py")


def test_oversized_declared_length_refused():
    def handler(request):
        return httpx.Response(
            200, content=b"{}", headers={"content-length": str(MAX_RESPONSE_BYTES + 1)}
        )

    client = PlatformHttp(
        platform_origin=PLATFORM_ORIGIN, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(PlatformUnreachable):
        client.meta()


def test_strings_are_capped():
    capped = cap_strings(
        {"title": "t" * 5000, "body": "b" * 100_000, "commit_titles": ["c" * 5000]}
    )
    assert len(capped["title"]) == MAX_TITLE_CHARS
    assert len(capped["body"]) == MAX_BODY_CHARS
    assert len(capped["commit_titles"][0]) == MAX_TITLE_CHARS
    fake = FakePlatform()
    client = _client(fake)
    fake.responses["pr"] = (200, {"title": "t" * 5000})
    assert len(client.pr(SLUG, 1)["title"]) == MAX_TITLE_CHARS


# -- errors -----------------------------------------------------------------


def test_refusal_envelope_becomes_platform_refused():
    fake = FakePlatform()
    client = _client(fake)
    fake.revoke_token(TOKEN, "admin_revoked")
    with pytest.raises(PlatformRefused) as info:
        client.status(SLUG)
    assert (info.value.status, info.value.code, info.value.reason) == (
        401,
        "token_revoked",
        "admin_revoked",
    )
    fake.tokens[TOKEN]["revoked"] = None
    fake.responses["history"] = (
        429,
        error_body("generation_limited", retry_after=7),
    )
    with pytest.raises(PlatformRefused) as info:
        client.history(SLUG, "a.py")
    assert info.value.retry_after == 7


def test_5xx_timeout_and_garbage_are_unreachable():
    fake = FakePlatform()
    client = _client(fake)
    for mode in ("unreachable", "timeout", "not_json"):
        fake.failure = mode
        with pytest.raises(PlatformUnreachable):
            client.status(SLUG)
    fake.failure = None
    fake.responses["status"] = (200, {"slug": "x"})  # not a StatusOut
    with pytest.raises(PlatformUnreachable, match="shape"):
        client.status(SLUG)
    fake.responses["status"] = (403, {"nope": 1})  # no envelope
    with pytest.raises(PlatformUnreachable, match="envelope"):
        client.status(SLUG)


def test_update_required():
    fake = FakePlatform()
    fake.api_version = 2
    with pytest.raises(UpdateRequired):
        _client(fake).meta()
    fake = FakePlatform(min_client="999.0.0")
    with pytest.raises(UpdateRequired, match="999.0.0"):
        _client(fake).meta()
    assert _client(FakePlatform(min_client="0.0.1")).meta().capabilities


def test_exchange_validates_org_and_binds():
    fake = FakePlatform()
    fake.add_code("good", verifier=VERIFIER, redirect_uri=REDIRECT)
    client = _client(fake, bound=False)
    reply = client.exchange("good", VERIFIER, REDIRECT)
    assert reply.org == ORG and reply.project.slug == SLUG
    assert client.last_project is not None
    client.bind(reply.org, reply.token)
    assert client.api_origin == "https://acme.wg.example.com"
    # single use, and a wrong verifier consumes the code too
    with pytest.raises(PlatformRefused):
        client.exchange("good", VERIFIER, REDIRECT)
    fake.add_code("c2", verifier=VERIFIER, redirect_uri=REDIRECT)
    with pytest.raises(PlatformRefused):
        client.exchange("c2", "w" * 43, REDIRECT)
    with pytest.raises(PlatformRefused):
        client.exchange("c2", VERIFIER, REDIRECT)
    fake.responses["token"] = (
        200,
        {
            "token": TOKEN,
            "org": "Not Valid",
            "project": status_body(),
            "api_version": 1,
        },
    )
    with pytest.raises(PlatformUnreachable, match="invalid org"):
        client.exchange("c3", VERIFIER, REDIRECT)


def test_data_routes_round_trip():
    fake = FakePlatform()
    client = _client(fake)
    assert isinstance(client.status(SLUG), StatusOut)
    assert client.evidence(SLUG, EvidenceIn(target=TARGET)).evidence == []
    assert client.rationale(SLUG, RationaleIn(target=TARGET)).purpose == "p"
    assert client.history(SLUG, "a.py", 5, False)["commits"] == []
    assert client.commit(SLUG, "a" * 40)["id"] == "a" * 40
    assert client.pr(SLUG, 3)["id"] == "3"
    assert client.issue(SLUG, 4)["id"] == "4"
    assert client.overview(SLUG)["name"] == "Demo"
    history = next(r for r in fake.requests if r.url.path.endswith("/history"))
    assert dict(history.url.params) == {
        "path": "a.py",
        "limit": "5",
        "include_renames": "false",
    }
    client.revoke(SLUG)
    with pytest.raises(PlatformRefused):
        client.status(SLUG)


def test_link_token_never_exposed_in_client_errors(caplog):
    fake = FakePlatform()
    client = _client(fake)
    leaky = f"bad token {TOKEN} and ghs_abcdefghijkl"
    fake.responses["status"] = (401, error_body("invalid_token", error=leaky))
    with caplog.at_level("DEBUG"):
        with pytest.raises(PlatformRefused) as info:
            client.status(SLUG)
        fake.responses["history"] = (200, {"x": 1})
        fake.failure = "timeout"
        with pytest.raises(PlatformUnreachable) as info2:
            client.history(SLUG, "a.py")
    for text in (str(info.value), repr(info.value), str(info2.value), caplog.text):
        assert TOKEN not in text
        assert "abcdefghijkl" not in text
    assert "wgc_***" in str(info.value)


# -- the status mapping (plan section 4.11) ---------------------------------


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (None, ("ok", None)),
        (200, ("ok", None)),
        (StatusOut(**status_body()), ("ok", None)),
        (StatusOut(**status_body(access_lost=True)), ("access_lost", None)),
        (
            PlatformRefused(401, "token_revoked", "project_deleted"),
            ("removed", "project_deleted"),
        ),
        (
            PlatformRefused(401, "token_revoked", "org_deleted"),
            ("removed", "org_deleted"),
        ),
        *[
            (
                PlatformRefused(401, "token_revoked", reason),
                ("revoked", reason),
            )
            for reason in (
                "user_revoked",
                "admin_revoked",
                "member_removed",
                "member_left",
                "user_disabled",
                "idle",
            )
        ],
        (PlatformRefused(401, "invalid_token"), ("revoked", "invalid_token")),
        (401, ("revoked", None)),
        (PlatformUnreachable("down"), ("unreachable", None)),
        (503, ("unreachable", None)),
        (
            PlatformRefused(429, "generation_limited", None, 5),
            ("ok", "generation_limited"),
        ),
        # A data-level 404 (an unknown commit) says nothing about the link.
        (PlatformRefused(404, "not_found"), ("ok", "not_found")),
        (PlatformRefused(409, "no_llm_key"), ("ok", "no_llm_key")),
        (UpdateRequired("old"), ("update_required", None)),
        (RuntimeError("x"), ("unreachable", None)),
    ],
)
def test_link_status_for(outcome, expected):
    assert link_status_for(outcome) == expected
