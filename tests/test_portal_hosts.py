"""Unit tests for the base URL, host classification and safe redirects."""

from __future__ import annotations

import socket

import pytest

from whygraph.portal import hosts
from whygraph.portal.hosts import BASE, BaseUrl, HostKind, classify, safe_redirect

DEV = BaseUrl.parse("http://whygraph.localhost:8765")
PROD = BaseUrl.parse("https://whygraph.example.com")


def test_parse_basic():
    assert DEV.scheme == "http"
    assert DEV.host == "whygraph.localhost"
    assert DEV.port == 8765
    assert DEV.origin == "http://whygraph.localhost:8765"
    assert DEV.netloc == "whygraph.localhost:8765"
    assert DEV.org_origin("acme") == "http://acme.whygraph.localhost:8765"
    assert DEV.org_netloc("acme") == "acme.whygraph.localhost:8765"


def test_parse_case_and_trailing_slash():
    base = BaseUrl.parse("HTTPS://WhyGraph.Example.COM/")
    assert base == PROD
    assert base.origin == "https://whygraph.example.com"


@pytest.mark.parametrize(
    "raw, netloc",
    [
        ("https://x.example:443", "x.example"),
        ("https://x.example:8443", "x.example:8443"),
        ("http://x.localhost:80", "x.localhost"),
        ("http://x.localhost:8765", "x.localhost:8765"),
    ],
)
def test_parse_ports(raw, netloc):
    base = BaseUrl.parse(raw)
    assert base.netloc == netloc
    assert base.origin.endswith(netloc)


@pytest.mark.parametrize(
    "raw, rule",
    [
        ("ftp://x.example.com", "scheme"),
        ("whygraph.example.com", "scheme"),
        ("http://whygraph.example.com", "localhost"),
        ("https://x.example.com/app", "path"),
        ("https://x.example.com/?a=1", "query"),
        ("https://x.example.com#frag", "fragment"),
        ("https://user@x.example.com", "userinfo"),
        ("https://127.0.0.1", "IP"),
        ("https://[::1]", "IP"),
        ("https://localhost", "two labels"),
        ("http://localhost:8765", "two labels"),
        (f"https://{'a' * 64}.example.com", "63"),
        ("https://x.example.com:notaport", "valid URL"),
        ("", "scheme"),
    ],
)
def test_parse_refusals(raw, rule):
    with pytest.raises(ValueError, match=rule):
        BaseUrl.parse(raw)


def test_classify_base_and_org():
    assert classify("whygraph.localhost:8765", DEV) is BASE
    assert classify("whygraph.localhost:8765", DEV).is_base
    kind = classify("acme.whygraph.localhost:8765", DEV)
    assert kind == HostKind("acme")
    assert not kind.is_base
    assert classify("acme.whygraph.example.com", PROD) == HostKind("acme")


def test_classify_uppercase_and_none():
    assert classify("ACME.WhyGraph.Localhost:8765", DEV) == HostKind("acme")
    assert classify(None, DEV) is None
    assert classify("", DEV) is None


@pytest.mark.parametrize(
    "host",
    [
        "www.whygraph.localhost:8765",
        "api.whygraph.localhost:8765",
        "mta-sts.whygraph.localhost:8765",
        "a.b.whygraph.localhost:8765",
        "acme.whygraph.localhost.evil.com:8765",
        "evilwhygraph.localhost:8765",
        "acme.evilwhygraph.localhost:8765",
        "acme.whygraph.localhost:9999",
        "acme.whygraph.localhost",
        "whygraph.localhost",
        "whygraph.localhost:9999",
        ".whygraph.localhost:8765",
        "-a.whygraph.localhost:8765",
        "a_b.whygraph.localhost:8765",
        "localhost:8765",
    ],
)
def test_classify_refuses(host):
    assert classify(host, DEV) is None


def test_classify_default_port_base():
    assert classify("acme.whygraph.example.com:443", PROD) is None
    assert classify("whygraph.example.com", PROD) is BASE


def test_classify_reserved_but_valid_label_is_an_org():
    # Reserved names only block creation; an existing org must stay reachable.
    assert classify("setup.whygraph.localhost:8765", DEV) == HostKind("setup")


@pytest.mark.parametrize(
    "target",
    [
        "http://whygraph.localhost:8765/orgs",
        "http://whygraph.localhost:8765",
        "http://acme.whygraph.localhost:8765/",
        "http://acme.whygraph.localhost:8765/p/x?y=1#z",
    ],
)
def test_safe_redirect_accepts(target):
    assert safe_redirect(target, DEV) == target


@pytest.mark.parametrize(
    "target",
    [
        None,
        "",
        "/orgs",
        "//evil.com",
        "//acme.whygraph.localhost:8765/",
        "https://acme.whygraph.localhost:8765/",
        "http://acme.whygraph.localhost:9999/",
        "http://acme.whygraph.localhost/",
        "http://user@acme.whygraph.localhost:8765/",
        "http://evil.com@acme.whygraph.localhost:8765/",
        "http://a.b.whygraph.localhost:8765/",
        "http://www.whygraph.localhost:8765/",
        "http://evil.com/",
        "http://acme.whygraph.localhost.evil.com:8765/",
        "http://whygraph.localhost:8765\\@evil.com",
        "javascript:alert(1)",
        "http://whygraph.localhost:8765/\r\nSet-Cookie: x=1",
        "http://whygraph.localhost:8765 /",
    ],
)
def test_safe_redirect_refuses(target):
    assert safe_redirect(target, DEV) is None


def test_self_check_skipped_for_localhost(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not resolve")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert hosts.self_check(DEV) == []


def test_self_check_healthy(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [("ok",)])
    assert hosts.self_check(PROD) == []


def test_self_check_names_missing_records(monkeypatch):
    def fake(name, *a, **k):
        if name == PROD.host:
            return [("ok",)]
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    problems = hosts.self_check(PROD)
    assert len(problems) == 1
    assert "*.whygraph.example.com" in problems[0]

    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(OSError)
    )
    assert len(hosts.self_check(PROD)) == 2


def test_self_check_never_raises(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(socket, "getaddrinfo", broken)
    assert isinstance(hosts.self_check(PROD), list)
