"""The production identity routes (M2c plan sections 4.3, 4.7-4.9, 5.3).

Everything here drives the real routes of
:mod:`whygraph.portal.auth_routes` through a production ``TestClient``:
the bootstrap, the admin sign-ins, logout and reset, the account and org
routes, and the instance-admin page's data. End users sign in with GitHub
(M2d-1) through the fake GitHub (:func:`github_sign_in`); password accounts
other than the bootstrap admin are inserted directly (:func:`password_user`),
as no route creates one any more. Since plan 59 a password account signs in
only to the admin panel (D4), with an **admin** session on its own cookie,
and an admin session can be paired with the everyday session of the same
browser (D51). The GitHub sign-in routes themselves are
``test_portal_github_auth.py``. Startup, the pure session
helpers and the request-identity seam live in ``test_portal_identity.py``;
the guard (hosts, origins, headers) in ``test_portal_production_guard.py``.

Clients use real host names as URLs (:func:`at`), so httpx's cookie jar
scopes the ``Domain=whygraph.localhost`` session cookie like a browser.
"""

# ruff: noqa: F811 -- pytest fixtures (`env`, `production_env`) imported from test_portal_app

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterator
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlmodel import col, select

from github_fake import FakeGitHub
from test_portal_app import (  # noqa: F401 -- fixtures
    PORT,
    PROD_BASE,
    PROD_PASSWORD,
    admin_signed_in,
    at,
    claim_instance,
    env,
    github_admin,
    github_fake,
    github_sign_in,
    grant_reader,
    log_in,
    password_user,
    prod_portal,
    production_env,
    signed_in,
    uid_of_login,
)
from whygraph.portal import db as portal_db
from whygraph.portal import sessions
from whygraph.portal.hosts import BaseUrl, safe_admin_redirect
from whygraph.portal.models import (
    Membership,
    Organization,
    PasswordReset,
    User,
    UserSession,
)
from whygraph.portal.orgs import RESERVED_ORG_SLUGS
from whygraph.portal.throttle import Throttle

LOGIN_REQUIRED = {"error": "sign-in required", "code": "login_required"}
ADMIN_LOGIN_REQUIRED = {
    "error": "admin sign-in required",
    "code": "admin_login_required",
}
NOT_FOUND = {"error": "not found"}
COOKIE = sessions.COOKIE_NAME
ADMIN_COOKIE = sessions.admin_cookie_name(BaseUrl.parse(PROD_BASE))


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh(github_fake: FakeGitHub) -> Iterator[TestClient]:
    """An unclaimed production portal (its bootstrap is still pending).

    Its GitHub is the ``github_fake`` fixture's, which also knows ``ann``
    and ``bob``.
    """
    github_fake.add_user("ann")
    github_fake.add_user("bob")
    with prod_portal() as client:
        yield client


@pytest.fixture
def claimed(fresh: TestClient) -> TestClient:
    """A production portal claimed by the instance admin ``ada@example.com``."""
    claim_instance(fresh)
    fresh.cookies.clear()
    return fresh


@pytest.fixture
def world(claimed: TestClient) -> SimpleNamespace:
    """Ada (admin), Ann (owner of ``quokka``), Bob (owner of ``narwhal``), Pat.

    Ann and Bob are GitHub accounts and every org is made through the real
    routes, so the fixture is itself a sign-in / org-creation walk-through.
    Pat (``pat@example.com``) is a password account in no org, inserted
    directly - the M2c-era state the password routes still serve. The
    client is left signed out.
    """
    assert github_sign_in(claimed, "ann").status_code == 200
    assert create_org(claimed, "quokka", "Quokka").status_code == 201
    assert github_sign_in(claimed, "bob").status_code == 200
    assert create_org(claimed, "narwhal", "Narwhal").status_code == 201
    claimed.cookies.clear()
    pat = password_user("pat@example.com")
    return SimpleNamespace(
        client=claimed,
        ada=uid_of("ada@example.com"),
        ann=uid_of_login("ann"),
        bob=uid_of_login("bob"),
        pat=pat,
    )


@pytest.fixture
def audit_log(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> pytest.LogCaptureFixture:
    """``caplog`` capturing the ``whygraph.portal.audit`` records."""
    monkeypatch.setattr(logging.getLogger("whygraph"), "propagate", True)
    caplog.set_level(logging.INFO, logger="whygraph.portal.audit")
    return caplog


def create_org(client: TestClient, slug: str, name: str = "Org") -> httpx.Response:
    """``POST /api/orgs`` on the base host."""
    return client.post(at() + "/api/orgs", json={"slug": slug, "name": name})


def uid_of(email: str) -> str:
    """The stable uid of the user with ``email``."""
    with portal_db.get_session() as session:
        return session.exec(select(User.uid).where(User.email == email)).one()


def _who(email: str | None, login: str | None):  # noqa: ANN202
    """The ``WHERE`` naming a user by email or (GitHub) login."""
    assert (email is None) != (login is None)
    return User.email == email if login is None else User.github_login == login


def user_row(email: str | None = None, *, login: str | None = None) -> SimpleNamespace:
    """A snapshot of a user row, by email or by GitHub login."""
    with portal_db.get_session() as session:
        user = session.exec(select(User).where(_who(email, login))).one()
        return SimpleNamespace(
            id=user.id,
            uid=user.uid,
            display_name=user.display_name,
            password_hash=user.password_hash,
            password_changed_at=user.password_changed_at,
            is_instance_admin=user.is_instance_admin,
            github_id=user.github_id,
            github_login=user.github_login,
            avatar_url=user.avatar_url,
        )


def session_count(email: str | None = None, *, login: str | None = None) -> int:
    """How many live session rows a user has, by email or by GitHub login."""
    with portal_db.get_session() as session:
        user_id = session.exec(select(User.id).where(_who(email, login))).one()
        return len(
            session.exec(
                select(UserSession.id).where(UserSession.user_id == user_id)
            ).all()
        )


def cookie_of(response: httpx.Response) -> str:
    """The session token a response set."""
    return response.cookies[COOKIE]


def admin_cookie_of(response: httpx.Response) -> str:
    """The admin session token a response set."""
    return response.cookies[ADMIN_COOKIE]


def as_token(client: TestClient, token: str, path: str = "/api/account") -> int:
    """GET ``path`` with ``token`` as the only session cookie; return the status."""
    return client.get(at() + path, headers={"Cookie": f"{COOKIE}={token}"}).status_code


def as_admin_token(client: TestClient, token: str, path: str = "/api/admin/me") -> int:
    """GET ``path`` with ``token`` as the only admin cookie; return the status."""
    headers = {"Cookie": f"{ADMIN_COOKIE}={token}"}
    return client.get(at() + path, headers=headers).status_code


def session_row(token: str) -> SimpleNamespace | None:
    """The ``sessions`` row a raw token names (any kind), or ``None``."""
    with portal_db.get_session() as session:
        row = session.exec(
            select(UserSession).where(
                UserSession.token_hash == sessions.hash_token(token)
            )
        ).first()
        if row is None:
            return None
        return SimpleNamespace(
            id=row.id,
            user_id=row.user_id,
            kind=row.kind,
            paired_session_id=row.paired_session_id,
        )


def admin_clears(response: httpx.Response) -> list[str]:
    """The ``Set-Cookie`` headers of ``response`` that clear the admin cookie."""
    return [
        h
        for h in response.headers.get_list("set-cookie")
        if h.startswith(f"{ADMIN_COOKIE}=") and "Max-Age=0" in h
    ]


def promote(client: TestClient, uid: str) -> None:
    """Make ``uid`` an instance admin through the admin route, as Ada.

    Ada's admin session is made directly (:func:`admin_signed_in`), so no
    sign-in route runs and no other admin session of the jar is ended.
    """
    admin_signed_in(client, user_row("ada@example.com").id)
    response = client.patch(
        at() + f"/api/admin/users/{uid}", json={"is_instance_admin": True}
    )
    assert response.status_code == 200, response.text


def from_ip(client: TestClient, ip: str) -> None:
    """Make ``client``'s next requests come from ``ip``.

    ``TestClient`` fixes the peer address at construction, and the throttles
    key on it, so the transport's value is swapped in place.
    """
    client._transport.client = (ip, 1)


def signed_in_admin(client: TestClient) -> TestClient:
    """Sign the instance admin in to the admin panel on ``client``; return it."""
    assert log_in(client, "ada@example.com").status_code == 200
    return client


def reset_link(client: TestClient, uid: str) -> httpx.Response:
    """``POST /api/admin/users/{uid}/reset-link`` as the signed-in admin."""
    return client.post(at() + f"/api/admin/users/{uid}/reset-link")


def token_in(url: str) -> str:
    """The raw token from a ``<base>/reset#token=...`` link."""
    prefix, _, token = url.partition("#token=")
    assert prefix == f"{PROD_BASE}/reset" and token
    return token


def events(log: pytest.LogCaptureFixture) -> list[dict]:
    """The captured security-event records, in order."""
    return [r.audit for r in log.records if hasattr(r, "audit")]


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Bootstrap (plan section 4.3)
# ---------------------------------------------------------------------------


def test_the_bootstrap_route_reports_whether_it_is_pending(fresh: TestClient) -> None:
    assert fresh.get(at() + "/api/auth/bootstrap").json() == {"required": True}
    claim_instance(fresh)
    assert fresh.get(at() + "/api/auth/bootstrap").json() == {"required": False}


def test_the_bootstrap_makes_the_first_instance_admin(fresh: TestClient) -> None:
    secret = fresh.app.state.portal.bootstrap_secret
    response = fresh.post(
        at() + "/api/auth/bootstrap",
        json={
            "secret": secret,
            "email": "  Ada@Example.COM ",
            "display_name": " Ada ",
            "password": PROD_PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"redirect": f"{PROD_BASE}/admin"}
    assert response.headers["cache-control"] == "no-store"
    ada = user_row("ada@example.com")  # normalized, and the name trimmed
    assert ada.is_instance_admin is True and ada.display_name == "Ada"
    assert ada.password_hash and PROD_PASSWORD not in ada.password_hash
    # The response signed the browser in to the admin panel - an admin
    # session, never an everyday one (plan 59 D4) - and the secret is gone.
    assert fresh.app.state.portal.bootstrap_secret is None
    assert COOKIE not in response.cookies
    token = admin_cookie_of(response)
    assert sessions.lookup(token, "admin") is not None
    assert session_row(token).paired_session_id is None
    assert fresh.get(at() + "/api/admin/me").json()["uid"] == ada.uid
    assert fresh.get(at() + "/api/account").status_code == 401


@pytest.mark.parametrize("secret", ["nope", "", "sécret-with-non-ascii"])
def test_a_wrong_bootstrap_secret_is_403(fresh: TestClient, secret: str) -> None:
    response = fresh.post(
        at() + "/api/auth/bootstrap",
        json={
            "secret": secret,
            "email": "ada@example.com",
            "display_name": "Ada",
            "password": PROD_PASSWORD,
        },
    )
    assert response.status_code == 403
    assert response.json()["code"] == "bad_secret"
    assert fresh.app.state.portal.bootstrap_secret is not None
    with portal_db.get_session() as session:
        assert session.exec(select(User)).all() == []


def test_the_bootstrap_validates_the_account_like_register(fresh: TestClient) -> None:
    secret = fresh.app.state.portal.bootstrap_secret
    for field, value, code in (
        ("email", "not-an-email", "bad_email"),
        ("password", "short", "weak_password"),
        ("password", "passwordpassword", "common_password"),
    ):
        body = {
            "secret": secret,
            "email": "ada@example.com",
            "display_name": "Ada",
            "password": PROD_PASSWORD,
        }
        body[field] = value
        response = fresh.post(at() + "/api/auth/bootstrap", json=body)
        assert response.status_code == 422, (field, response.text)
        assert response.json()["code"] == code
    assert fresh.app.state.portal.bootstrap_secret == secret


def test_the_bootstrap_route_is_inert_once_claimed(claimed: TestClient) -> None:
    response = claimed.post(
        at() + "/api/auth/bootstrap",
        json={
            "secret": "anything",
            "email": "eve@example.com",
            "display_name": "Eve",
            "password": PROD_PASSWORD,
        },
    )
    assert response.status_code == 404 and response.json() == NOT_FOUND
    assert user_row("ada@example.com").is_instance_admin is True
    with portal_db.get_session() as session:
        assert session.exec(select(User.email)).all() == ["ada@example.com"]


def test_a_second_bootstrap_submit_makes_only_one_admin(fresh: TestClient) -> None:
    """The lock re-check: the secret survives hashing, the DB state does not."""
    state = fresh.app.state.portal
    secret = state.bootstrap_secret
    body = {
        "secret": secret,
        "email": "ada@example.com",
        "display_name": "Ada",
        "password": PROD_PASSWORD,
    }
    assert fresh.post(at() + "/api/auth/bootstrap", json=body).status_code == 200
    # A request that had already read the secret before the first one won:
    state.bootstrap_secret = secret
    second = fresh.post(
        at() + "/api/auth/bootstrap",
        json={**body, "email": "eve@example.com"},
    )
    assert second.status_code == 404 and second.json() == NOT_FOUND
    assert state.bootstrap_secret is None  # the re-check cleared it again
    with portal_db.get_session() as session:
        admins = session.exec(
            select(User.email).where(col(User.is_instance_admin).is_(True))
        ).all()
    assert admins == ["ada@example.com"]


def test_the_bootstrap_is_throttled_per_ip(fresh: TestClient) -> None:
    body = {
        "secret": "wrong",
        "email": "ada@example.com",
        "display_name": "Ada",
        "password": PROD_PASSWORD,
    }
    for _ in range(10):
        assert fresh.post(at() + "/api/auth/bootstrap", json=body).status_code == 403
    response = fresh.post(at() + "/api/auth/bootstrap", json=body)
    assert response.status_code == 429 and response.json()["code"] == "throttled"
    assert int(response.headers["Retry-After"]) > 0
    from_ip(fresh, "198.51.100.9")  # another client is unaffected
    assert fresh.post(at() + "/api/auth/bootstrap", json=body).status_code == 403


def test_login_is_refused_before_the_bootstrap(fresh: TestClient) -> None:
    response = log_in(fresh, "ann@example.com")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "bootstrap_required"
    with portal_db.get_session() as session:
        assert session.exec(select(User)).all() == []


# ---------------------------------------------------------------------------
# The admin password sign-in, logout and the redirect (plan 59 section 4.2)
# ---------------------------------------------------------------------------


def test_login_signs_in_and_starts_one_session(claimed: TestClient) -> None:
    password_user("ann@example.com", admin=True)
    assert claimed.get(at() + "/api/admin/me").status_code == 401

    response = log_in(claimed, "ANN@example.com ")
    assert response.status_code == 200, response.text
    assert response.json() == {"redirect": f"{PROD_BASE}/admin"}
    assert response.headers["cache-control"] == "no-store"
    token = admin_cookie_of(response)
    assert sessions.lookup(token, "admin") is not None
    assert COOKIE not in response.cookies  # an admin session only (D4)
    assert claimed.get(at() + "/api/admin/me").json()["display_name"] == "Ann"
    assert claimed.get(at() + "/api/account").status_code == 401


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("ann@example.com", "the wrong passphrase"),
        ("nobody@example.com", PROD_PASSWORD),
    ],
)
def test_a_wrong_password_and_an_unknown_email_answer_the_same(
    claimed: TestClient, email: str, password: str
) -> None:
    password_user("ann@example.com", admin=True)
    response = log_in(claimed, email, password)
    assert response.status_code == 401
    assert response.json() == {
        "error": "wrong email or password",
        "code": "bad_credentials",
        "surface": "admin",
    }
    assert ADMIN_COOKIE not in response.cookies and COOKIE not in response.cookies
    assert claimed.get(at() + "/api/admin/me").status_code == 401


def test_a_password_account_that_is_not_an_admin_gets_no_session(
    world: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    """D4: a password account is admin-only, so Pat (no admin) is refused."""
    client = world.client
    response = log_in(client, "pat@example.com")
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "not_an_admin"
    assert response.json()["surface"] == "admin"
    assert "set-cookie" not in response.headers
    assert session_count("pat@example.com") == 0
    (failure,) = [r for r in events(audit_log) if r["event"] == "login_failure"]
    assert (failure["uid"], failure["reason"], failure["surface"]) == (
        world.pat,
        "not_admin",
        "admin",
    )
    # And an everyday session made for it directly is refused (D4).
    with portal_db.get_session() as session:
        pat_id = session.exec(select(User.id).where(User.uid == world.pat)).one()
    signed_in(client, pat_id)
    assert client.get(at() + "/api/account").json() == LOGIN_REQUIRED


def test_the_admin_login_checks_password_then_disabled_then_admin(
    world: SimpleNamespace,
) -> None:
    """Only a correct password reveals more, and ``disabled`` comes first."""
    client = world.client
    with portal_db.get_session() as session:
        pat = session.exec(select(User).where(User.uid == world.pat)).one()
        pat.disabled_at = "2026-10-04T00:00:00+00:00"
        session.add(pat)
    wrong = log_in(client, "pat@example.com", "not pat's passphrase at all")
    assert wrong.status_code == 401 and wrong.json()["code"] == "bad_credentials"
    right = log_in(client, "pat@example.com")
    assert right.status_code == 403
    assert right.json() == {
        "error": "this account is disabled",
        "code": "account_disabled",
        "surface": "admin",
    }
    # A disabled password admin is refused the same way.
    promote(client, world.pat)
    client.cookies.clear()
    assert log_in(client, "pat@example.com").json()["code"] == "account_disabled"
    assert session_count("pat@example.com") == 0


def test_login_upgrades_an_outdated_hash(claimed: TestClient) -> None:
    password_user("ann@example.com", admin=True)
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PROD_PASSWORD)
    with portal_db.get_session() as session:
        user = session.exec(select(User).where(User.email == "ann@example.com")).one()
        user.password_hash = weak
        session.add(user)
    claimed.cookies.clear()

    assert log_in(claimed, "ann@example.com").status_code == 200
    rehashed = user_row("ann@example.com").password_hash
    assert rehashed != weak
    token = admin_cookie_of(log_in(claimed, "ann@example.com"))
    assert sessions.lookup(token, "admin") is not None
    assert user_row("ann@example.com").password_hash == rehashed  # not again


def test_safe_admin_redirect_accepts_only_the_admin_panel_on_the_base_host() -> None:
    base = BaseUrl.parse(PROD_BASE)
    fallback = f"{PROD_BASE}/admin"
    for target in (
        f"{PROD_BASE}/admin",
        f"{PROD_BASE}/admin/",
        f"{PROD_BASE}/admin/users?q=ann",
        f"{PROD_BASE}/admin/orgs/quokka#members",
    ):
        assert safe_admin_redirect(target, base) == target
    for target in (
        None,
        "",
        f"{PROD_BASE}/orgs",
        f"{PROD_BASE}/account",
        f"{PROD_BASE}/administrator",
        f"{PROD_BASE}/adminx/users",
        at("quokka") + "/admin",
        at("quokka") + "/admin/users",
        "https://evil.example/admin",
        "//evil.example/admin",
        f"http://whygraph.localhost:{PORT + 1}/admin",
        "http://whygraph.localhost.evil.com:8765/admin",
        "javascript:alert(1)",
        "/admin",
    ):
        assert safe_admin_redirect(target, base) == fallback, target


def test_the_login_redirect_only_accepts_the_admin_panel(claimed: TestClient) -> None:
    password_user("ann@example.com", admin=True)
    kept = [f"{PROD_BASE}/admin", f"{PROD_BASE}/admin/users?q=ann"]
    dropped = [
        f"{PROD_BASE}/account",
        at("quokka") + "/p/api/explorer",
        at("quokka") + "/admin",
        "https://evil.example/",
        "//evil.example/",
        f"http://whygraph.localhost:{PORT + 1}/orgs",
        "http://whygraph.localhost.evil.com:8765/",
        "http://a.b.whygraph.localhost:8765/",
        "javascript:alert(1)",
        "/orgs",
    ]
    for target in kept:
        assert log_in(claimed, "ann@example.com", next=target).json() == {
            "redirect": target
        }
    for target in dropped:
        assert log_in(claimed, "ann@example.com", next=target).json() == {
            "redirect": f"{PROD_BASE}/admin"
        }, target


def test_logout_revokes_the_session_and_clears_the_cookie(
    world: SimpleNamespace,
) -> None:
    client = world.client
    token = cookie_of(github_sign_in(client, "ann"))
    for prefix in (at("quokka"), at()):  # logout works on any host
        response = client.post(prefix + "/api/auth/logout")
        assert response.status_code == 200
        assert response.json() == {"redirect": f"{PROD_BASE}/signin"}
        cleared = [
            h
            for h in response.headers.get_list("set-cookie")
            if h.startswith(f"{COOKIE}=")
        ]
        assert len(cleared) == 2 and all("Max-Age=0" in h for h in cleared)
        # The admin cookie is cleared on the base host only (plan 59 D29).
        assert bool(admin_clears(response)) is (prefix == at())
    assert sessions.lookup(token) is None
    assert client.get(at() + "/api/account").status_code == 401


# ---------------------------------------------------------------------------
# Login throttling (plan section 4.8)
# ---------------------------------------------------------------------------


def test_five_failures_lock_the_pair_but_not_the_victim_elsewhere(
    claimed: TestClient,
) -> None:
    password_user("ann@example.com", admin=True)
    from_ip(claimed, "198.51.100.10")
    for _ in range(5):
        assert (
            log_in(claimed, "ann@example.com", "wrong wrong wrong").status_code == 401
        )
    attacker = log_in(claimed, "ann@example.com", "wrong wrong wrong")
    assert attacker.status_code == 429 and attacker.json()["code"] == "throttled"
    assert int(attacker.headers["Retry-After"]) > 0
    # The right password from the same IP is refused too - the check is first.
    assert log_in(claimed, "ann@example.com").status_code == 429
    # Ann signs in from her own address, and someone else from the attacker's.
    from_ip(claimed, "203.0.113.77")
    assert log_in(claimed, "ann@example.com").status_code == 200
    from_ip(claimed, "198.51.100.10")
    assert log_in(claimed, "ada@example.com").status_code == 200


def test_the_per_email_ceiling_holds_across_addresses(claimed: TestClient) -> None:
    claimed.app.state.portal.login_email = Throttle(3, 3600)
    password_user("ann@example.com", admin=True)
    for n in range(3):
        from_ip(claimed, f"198.51.100.{n + 20}")
        assert (
            log_in(claimed, "ann@example.com", "wrong wrong wrong").status_code == 401
        )
    from_ip(claimed, "198.51.100.90")
    assert log_in(claimed, "ann@example.com").status_code == 429
    assert log_in(claimed, "ada@example.com").status_code == 200  # per email only


def test_the_per_ip_rule_holds_across_emails(claimed: TestClient) -> None:
    claimed.app.state.portal.login_ip = Throttle(3, 900)
    password_user("ann@example.com", admin=True)
    from_ip(claimed, "198.51.100.30")
    for n in range(3):
        assert log_in(claimed, f"u{n}@example.com").status_code == 401
    assert log_in(claimed, "ann@example.com").status_code == 429
    from_ip(claimed, "198.51.100.31")
    assert log_in(claimed, "ann@example.com").status_code == 200


def test_two_addresses_in_one_ipv6_64_share_a_bucket(claimed: TestClient) -> None:
    claimed.app.state.portal.login_ip = Throttle(2, 900)
    from_ip(claimed, "2001:db8:1:1::5")
    for _ in range(2):
        assert log_in(claimed, "nobody@example.com").status_code == 401
    from_ip(claimed, "2001:db8:1:1::ffff")  # the same /64
    assert log_in(claimed, "nobody@example.com").status_code == 429
    from_ip(claimed, "2001:db8:1:2::5")  # a different /64
    assert log_in(claimed, "nobody@example.com").status_code == 401


def test_a_proxys_forwarded_address_is_the_throttle_key_only_when_trusted(
    claimed: TestClient,
) -> None:
    """Without trusted proxies every client shares the proxy's bucket.

    ``TestClient`` bypasses uvicorn, so a trusted proxy is simulated by the
    peer address uvicorn would have substituted.
    """
    claimed.app.state.portal.login_ip = Throttle(2, 900)
    from_ip(claimed, "198.51.100.50")  # the proxy, untrusted: one bucket
    headers = {"X-Forwarded-For": "203.0.113.1"}
    for _ in range(2):
        assert (
            claimed.post(
                at() + "/api/auth/admin/login",
                json={"email": "nobody@example.com", "password": PROD_PASSWORD},
                headers=headers,
            ).status_code
            == 401
        )
    behind = claimed.post(
        at() + "/api/auth/admin/login",
        json={"email": "other@example.com", "password": PROD_PASSWORD},
        headers={"X-Forwarded-For": "203.0.113.2"},
    )
    assert behind.status_code == 429  # a different client, the same bucket
    from_ip(claimed, "203.0.113.2")  # what a trusted proxy would produce
    assert log_in(claimed, "other@example.com").status_code == 401


# ---------------------------------------------------------------------------
# The account routes
# ---------------------------------------------------------------------------


def test_the_account_routes_need_a_session_and_work_on_any_host(
    world: SimpleNamespace,
) -> None:
    client = world.client
    for prefix in (at(), at("quokka")):
        response = client.get(prefix + "/api/account")
        assert response.status_code == 401 and response.json() == LOGIN_REQUIRED
    github_sign_in(client, "ann")
    for prefix in (at(), at("quokka"), at("narwhal")):
        body = client.get(prefix + "/api/account").json()
        assert body == {
            "uid": world.ann,
            "email": None,  # no email is stored for a GitHub account
            "display_name": "Ann",
            "is_instance_admin": False,
            "github_login": "ann",
            "avatar_url": user_row(login="ann").avatar_url,
            "has_password": False,
        }
    # A password account has no everyday session any more (plan 59 D4): its
    # sign-in gives only an admin session, which the account routes ignore.
    promote(client, world.pat)
    assert log_in(client, "pat@example.com").status_code == 200
    assert client.get(at() + "/api/account").json()["uid"] == world.ann
    body = client.get(at() + "/api/admin/me").json()
    assert body["uid"] == world.pat
    assert body["github_login"] is None and body["avatar_url"] is None
    assert body["has_password"] is True


def test_patch_account_changes_the_display_name(world: SimpleNamespace) -> None:
    client = world.client
    github_sign_in(client, "ann")
    response = client.patch(at() + "/api/account", json={"display_name": " Annie "})
    assert response.status_code == 200
    assert response.json()["display_name"] == "Annie"
    assert response.json()["github_login"] == "ann"
    assert response.json()["has_password"] is False
    assert client.get(at() + "/api/account").json()["display_name"] == "Annie"
    assert (
        client.patch(at() + "/api/account", json={"display_name": "  "}).status_code
        == 422
    )


def test_account_orgs_lists_real_memberships_only(world: SimpleNamespace) -> None:
    client = world.client
    github_sign_in(client, "ann")
    assert client.get(at() + "/api/account/orgs").json() == [
        {
            "slug": "quokka",
            "name": "Quokka",
            "role": "owner",
            "url": at("quokka"),
            "new": False,
        }
    ]
    github_admin(client, "dee")  # an instance admin reads, but belongs nowhere
    assert client.get(at() + "/api/account/orgs").json() == []


def test_a_password_change_rotates_this_session_and_ends_the_others(
    world: SimpleNamespace,
) -> None:
    client = world.client
    promote(client, world.pat)  # D4: only an admin keeps a password account
    link = token_in(reset_link(client, world.pat).json()["url"])
    other = admin_cookie_of(log_in(client, "pat@example.com"))
    client.cookies.clear()  # another browser: its sign-in ends nothing of `other`
    current = admin_cookie_of(log_in(client, "pat@example.com"))
    before = user_row("pat@example.com").password_changed_at

    response = client.post(
        at() + "/api/admin/me/password",
        json={"current": PROD_PASSWORD, "new": "a brand new passphrase"},
        headers={"Cookie": f"{ADMIN_COOKIE}={current}"},
    )
    assert response.status_code == 200 and response.json() == {"changed": True}
    rotated = admin_cookie_of(response)
    assert COOKIE not in response.cookies  # never the everyday cookie
    assert rotated != current
    assert sessions.lookup(rotated, "admin") is not None
    assert as_admin_token(client, rotated) == 200
    assert as_admin_token(client, current) == 401  # rotated away
    assert as_admin_token(client, other) == 401  # every other session ended
    assert session_count("pat@example.com") == 1
    assert user_row("pat@example.com").password_changed_at != before
    # The outstanding reset link stops working, and the new password is live.
    stale = client.post(
        at() + "/api/auth/reset", json={"token": link, "password": "yet another phrase"}
    )
    assert stale.status_code == 400 and stale.json()["code"] == "bad_token"
    client.cookies.clear()
    assert log_in(client, "pat@example.com", PROD_PASSWORD).status_code == 401
    assert (
        log_in(client, "pat@example.com", "a brand new passphrase").status_code == 200
    )


def test_a_wrong_current_password_is_403_and_counts_as_a_failure(
    world: SimpleNamespace,
) -> None:
    client = world.client
    promote(client, world.pat)
    client.app.state.portal.login_pair = Throttle(2, 900)
    log_in(client, "pat@example.com")
    body = {"current": "not the passphrase", "new": "a brand new passphrase"}
    for _ in range(2):
        response = client.post(at() + "/api/admin/me/password", json=body)
        assert response.status_code == 403
        assert response.json()["code"] == "bad_credentials"
    throttled = client.post(at() + "/api/admin/me/password", json=body)
    assert throttled.status_code == 429
    assert user_row("pat@example.com").password_changed_at is None


def test_a_new_password_must_meet_the_rules(world: SimpleNamespace) -> None:
    client = world.client
    promote(client, world.pat)
    log_in(client, "pat@example.com")
    for new, code in (
        ("short one", "weak_password"),
        ("passwordpassword", "common_password"),
    ):
        response = client.post(
            at() + "/api/admin/me/password",
            json={"current": PROD_PASSWORD, "new": new},
        )
        assert response.status_code == 422 and response.json()["code"] == code
    assert user_row("pat@example.com").password_changed_at is None


def test_a_github_admin_has_no_password_to_change(world: SimpleNamespace) -> None:
    client = world.client
    github_admin(client, "dee")
    assert github_sign_in(client, "dee", admin=True).status_code == 200
    response = client.post(
        at() + "/api/admin/me/password",
        json={"current": PROD_PASSWORD, "new": "a brand new passphrase"},
    )
    assert response.status_code == 409 and response.json()["code"] == "no_password"


# ---------------------------------------------------------------------------
# Reset links (plan sections 4.7, 4.8)
# ---------------------------------------------------------------------------


def test_a_reset_link_carries_its_token_in_the_fragment(
    world: SimpleNamespace,
) -> None:
    promote(world.client, world.pat)
    client = signed_in_admin(world.client)
    response = reset_link(client, world.pat)
    assert response.status_code == 200, response.text
    url = response.json()["url"]
    raw = token_in(url)
    with portal_db.get_session() as session:
        (row,) = session.exec(select(PasswordReset)).all()
        assert row.token_hash == sessions.hash_token(raw) != raw
        assert row.used_at is None and row.expires_at > row.created_at
        assert row.created_by == user_row("ada@example.com").id
    assert reset_link(client, "no-such-uid").status_code == 404


def test_a_reset_sets_the_password_signs_in_and_ends_every_session(
    world: SimpleNamespace,
) -> None:
    client = world.client
    promote(client, world.pat)
    old = admin_cookie_of(log_in(client, "pat@example.com"))
    client.cookies.clear()  # Ada's sign-in below must not be what ends `old`
    raw = token_in(reset_link(signed_in_admin(client), world.pat).json()["url"])
    client.cookies.clear()

    response = client.post(
        at() + "/api/auth/reset",
        json={"token": raw, "password": "pat's new passphrase"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"redirect": f"{PROD_BASE}/admin"}
    assert COOKIE not in response.cookies  # an admin session (plan 59 section 4.2)
    fresh_token = admin_cookie_of(response)
    assert as_admin_token(client, old) == 401  # every earlier session ended
    assert as_admin_token(client, fresh_token) == 200
    assert session_count("pat@example.com") == 1
    assert user_row("pat@example.com").password_changed_at is not None
    client.cookies.clear()
    assert log_in(client, "pat@example.com", "pat's new passphrase").status_code == 200


@pytest.mark.parametrize("why", ["used", "superseded", "expired", "unknown"])
def test_a_dead_reset_link_is_400_bad_token(world: SimpleNamespace, why: str) -> None:
    client = world.client
    promote(client, world.pat)
    raw = token_in(reset_link(signed_in_admin(client), world.pat).json()["url"])
    client.cookies.clear()
    if why == "used":
        assert (
            client.post(
                at() + "/api/auth/reset",
                json={"token": raw, "password": "pat's first new phrase"},
            ).status_code
            == 200
        )
    elif why == "superseded":
        reset_link(signed_in_admin(client), world.pat)
    elif why == "expired":
        with portal_db.get_session() as session:
            row = session.exec(select(PasswordReset)).one()
            row.expires_at = _iso(datetime.now(timezone.utc) - timedelta(seconds=1))
            session.add(row)
    elif why == "unknown":
        raw = "a token nobody issued"
    client.cookies.clear()
    response = client.post(
        at() + "/api/auth/reset",
        json={"token": raw, "password": "pat's second new phrase"},
    )
    assert response.status_code == 400
    assert response.json() == {
        "error": "this reset link is invalid or has expired",
        "code": "bad_token",
    }


def test_a_password_account_that_is_not_an_admin_gets_no_reset_link(
    world: SimpleNamespace,
) -> None:
    """D28: issuing a link for a non-admin password account is ``409``."""
    client = signed_in_admin(world.client)
    response = reset_link(client, world.pat)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "not_an_admin"
    with portal_db.get_session() as session:
        assert session.exec(select(PasswordReset)).all() == []


def test_a_reset_of_an_account_that_is_no_admin_any_more_changes_nothing(
    world: SimpleNamespace,
) -> None:
    """D28: a link issued to an admin who was demoted since is refused, unused."""
    client = world.client
    promote(client, world.pat)
    raw = token_in(reset_link(client, world.pat).json()["url"])
    demoted = client.patch(
        at() + f"/api/admin/users/{world.pat}", json={"is_instance_admin": False}
    )
    assert demoted.status_code == 200
    client.cookies.clear()
    before = user_row("pat@example.com")

    response = client.post(
        at() + "/api/auth/reset", json={"token": raw, "password": "pat's new phrase"}
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "not_an_admin"
    assert "set-cookie" not in response.headers
    after = user_row("pat@example.com")
    assert after.password_hash == before.password_hash
    assert after.password_changed_at is None
    with portal_db.get_session() as session:
        assert session.exec(select(PasswordReset.used_at)).all() == [None]


def test_the_reset_route_is_throttled_per_ip(world: SimpleNamespace) -> None:
    client = world.client
    body = {"token": "nope", "password": "a passphrase here"}
    for _ in range(10):
        assert client.post(at() + "/api/auth/reset", json=body).status_code == 400
    throttled = client.post(at() + "/api/auth/reset", json=body)
    assert throttled.status_code == 429 and throttled.json()["code"] == "throttled"


# ---------------------------------------------------------------------------
# Org creation (plan section 4.7)
# ---------------------------------------------------------------------------


def test_creating_an_org_makes_the_caller_its_owner(claimed: TestClient) -> None:
    assert github_sign_in(claimed, "ann").status_code == 200
    response = create_org(claimed, "quokka", " Quokka ")
    assert response.status_code == 201, response.text
    assert response.json() == {"slug": "quokka", "url": at("quokka")}
    with portal_db.get_session() as session:
        org = session.exec(
            select(Organization).where(Organization.slug == "quokka")
        ).one()
        assert org.name == "Quokka"
        roles = session.exec(
            select(Membership.role).where(Membership.org_id == org.id)
        ).all()
    assert roles == ["owner"]
    assert claimed.get(at() + "/api/account/orgs").json()[0]["role"] == "owner"
    # And the org host serves her straight away.
    state = claimed.get(at("quokka") + "/api/portal/state").json()
    assert state["org"] == {
        "slug": "quokka",
        "name": "Quokka",
        "role": "owner",
        "default_project_role": "contributor",
    }


@pytest.mark.parametrize("slug", ["Quokka", "-bad", "a" * 41, "", "xn--abc", "api"])
def test_a_bad_or_reserved_slug_is_422(claimed: TestClient, slug: str) -> None:
    assert github_sign_in(claimed, "ann").status_code == 200
    response = create_org(claimed, slug)
    assert response.status_code == 422, (slug, response.text)
    assert response.json()["code"] == "bad_slug"
    assert response.json()["error"]  # the rule is spelled out
    with portal_db.get_session() as session:
        assert session.exec(select(Organization)).all() == []


def test_every_reserved_slug_is_refused(claimed: TestClient) -> None:
    assert github_sign_in(claimed, "ann").status_code == 200
    for slug in sorted(RESERVED_ORG_SLUGS):
        assert create_org(claimed, slug).status_code == 422, slug
    assert "local" not in RESERVED_ORG_SLUGS  # the built-in org's slug
    assert create_org(claimed, "local").status_code == 201


def test_a_taken_slug_is_409(world: SimpleNamespace) -> None:
    client = world.client
    github_sign_in(client, "bob")
    response = create_org(client, "quokka", "Quokka Two")
    assert response.status_code == 409 and response.json()["code"] == "slug_taken"
    assert create_org(client, "", "Blank name").status_code == 422
    assert (
        client.post(at() + "/api/orgs", json={"slug": "ok-slug", "name": " "})
    ).status_code == 422


def test_org_creation_is_base_host_only(world: SimpleNamespace) -> None:
    client = world.client
    github_sign_in(client, "ann")
    response = client.post(
        at("quokka") + "/api/orgs", json={"slug": "another", "name": "Another"}
    )
    assert response.status_code == 404 and response.json() == NOT_FOUND
    assert create_org(client, "another", "Another").status_code == 201


# ---------------------------------------------------------------------------
# Instance admin (plan section 4.9)
# ---------------------------------------------------------------------------


def test_the_admin_routes_refuse_a_non_admin_and_an_org_host(
    world: SimpleNamespace,
) -> None:
    client = world.client
    paths = [
        ("GET", "/api/admin/settings"),
        ("GET", "/api/admin/update"),
        ("GET", "/api/admin/orgs"),
        ("GET", "/api/admin/users"),
        ("PATCH", f"/api/admin/users/{world.ann}"),
        ("POST", f"/api/admin/users/{world.ann}/reset-link"),
        ("GET", "/api/admin/me"),
        ("POST", "/api/admin/me/logout"),
    ]
    for method, path in paths:  # no session at all
        response = client.request(method, at() + path, json={"is_instance_admin": True})
        assert response.status_code == 401, (method, path)
        assert response.json() == ADMIN_LOGIN_REQUIRED
    # An everyday session opens no admin route (plan 59 D2), not even an
    # instance admin's: only an admin session does.
    github_sign_in(client, "ann")
    for method, path in paths:
        response = client.request(method, at() + path, json={"is_instance_admin": True})
        assert response.status_code == 401, (method, path, response.text)
        assert response.json() == ADMIN_LOGIN_REQUIRED
    github_admin(client, "dee")
    for method, path in paths:
        response = client.request(method, at() + path, json={"is_instance_admin": True})
        assert response.status_code == 401, (method, path, response.text)
        assert response.json() == ADMIN_LOGIN_REQUIRED
    signed_in_admin(client)
    for method, path in paths:  # the host check comes before the action
        response = client.request(
            method, at("quokka") + path, json={"is_instance_admin": True}
        )
        assert response.status_code == 404, (method, path)
        assert response.json() == NOT_FOUND


def test_the_admin_lists_every_user_and_org(world: SimpleNamespace) -> None:
    client = signed_in_admin(world.client)
    users = client.get(at() + "/api/admin/users").json()
    assert [(u["email"], u["github_login"]) for u in users] == [
        ("ada@example.com", None),
        (None, "ann"),
        (None, "bob"),
        ("pat@example.com", None),
    ]
    assert [u["has_password"] for u in users] == [True, False, False, True]
    assert [u["disabled"] for u in users] == [False] * 4
    assert users[0]["is_instance_admin"] is True and users[0]["org_count"] == 0
    assert users[1]["uid"] == world.ann and users[1]["org_count"] == 1
    assert all(u["created_at"] and u["display_name"] for u in users)

    orgs = client.get(at() + "/api/admin/orgs").json()
    assert [o["slug"] for o in orgs] == ["narwhal", "quokka"]
    assert orgs[1] == {
        "slug": "quokka",
        "name": "Quokka",
        "url": at("quokka"),
        "member_count": 1,
        "created_at": orgs[1]["created_at"],
    }
    settings = client.get(at() + "/api/admin/settings").json()
    assert settings["base_url"] == PROD_BASE
    assert settings["base_check"] in ([], None)  # skipped for *.localhost


def test_the_last_instance_admin_cannot_be_demoted(world: SimpleNamespace) -> None:
    client = signed_in_admin(world.client)
    response = client.patch(
        at() + f"/api/admin/users/{world.ada}", json={"is_instance_admin": False}
    )
    assert response.status_code == 409 and response.json()["code"] == "last_admin"
    assert user_row("ada@example.com").is_instance_admin is True

    promoted = client.patch(
        at() + f"/api/admin/users/{world.ann}", json={"is_instance_admin": True}
    )
    assert promoted.status_code == 200
    assert promoted.json()["is_instance_admin"] is True
    # With two admins the first can step down, and loses the page at once:
    # her admin session ends with the flag (plan 59 section 4.1).
    assert (
        client.patch(
            at() + f"/api/admin/users/{world.ada}", json={"is_instance_admin": False}
        ).status_code
        == 200
    )
    gone = client.get(at() + "/api/admin/users")
    assert gone.status_code == 401 and gone.json() == ADMIN_LOGIN_REQUIRED
    assert github_sign_in(client, "ann", admin=True).status_code == 200
    assert client.get(at() + "/api/admin/users").status_code == 200
    assert (
        client.patch(
            at() + "/api/admin/users/no-such-uid", json={"is_instance_admin": True}
        ).status_code
        == 404
    )


def test_an_unchanged_admin_flag_is_never_the_last_admin(
    world: SimpleNamespace,
) -> None:
    client = signed_in_admin(world.client)
    response = client.patch(
        at() + f"/api/admin/users/{world.ann}", json={"is_instance_admin": False}
    )
    assert response.status_code == 200 and response.json()["is_instance_admin"] is False
    assert user_row("ada@example.com").is_instance_admin is True


def test_an_instance_admin_reads_an_org_they_do_not_belong_to(
    world: SimpleNamespace,
) -> None:
    # A GitHub admin's everyday session (a password account has none, D4),
    # with a reader grant on quokka (plan 59 D3).
    client = world.client
    dee = github_admin(client, "dee")
    grant = grant_reader(client, dee, "quokka")
    state = client.get(at("quokka") + "/api/portal/state").json()
    assert state["org"] == {
        "slug": "quokka",
        "name": "Quokka",
        "role": "reader",
        "default_project_role": "contributor",
        "reader_until": grant["expires_at"],
    }
    assert client.get(at("quokka") + "/api/projects").status_code == 200
    # Demoting the admin removes the access on the very next request.
    signed_in_admin(client)  # Ada, in the admin panel of the same browser
    demoted = client.patch(
        at() + f"/api/admin/users/{dee}", json={"is_instance_admin": False}
    )
    assert demoted.status_code == 200
    assert client.get(at("quokka") + "/api/projects").status_code == 404


# ---------------------------------------------------------------------------
# The admin panel's GitHub sign-in (plan 59 section 4.2)
# ---------------------------------------------------------------------------


def user_and_session_rows() -> tuple[list[tuple], list[tuple]]:
    """Every ``users`` row (id, login, avatar) and every ``sessions`` row id."""
    with portal_db.get_session() as session:
        users = [
            (u.id, u.github_id, u.github_login, u.avatar_url, u.display_name)
            for u in session.exec(select(User).order_by(User.id)).all()
        ]
        rows = [
            (r.id, r.user_id, r.kind)
            for r in session.exec(select(UserSession).order_by(UserSession.id)).all()
        ]
    return users, rows


def test_a_github_admin_signs_in_to_the_admin_panel(
    world: SimpleNamespace,
    github_fake: FakeGitHub,
    audit_log: pytest.LogCaptureFixture,
) -> None:
    client = world.client
    dee = github_admin(client, "dee")
    everyday = client.cookies[COOKIE]
    avatar = user_row(login="dee").avatar_url
    github_fake.users["dee"].avatar_url = "https://avatars.example.test/changed"

    response = github_sign_in(
        client, "dee", admin=True, next=f"{PROD_BASE}/admin/users"
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"redirect": f"{PROD_BASE}/admin/users"}
    assert COOKIE not in response.cookies  # the everyday session is left alone
    token = admin_cookie_of(response)
    me = client.get(at() + "/api/admin/me").json()
    assert (me["uid"], me["github_login"], me["has_password"]) == (dee, "dee", False)
    assert sessions.lookup(everyday) is not None
    # Nothing but the session is written: the profile is not refreshed.
    assert user_row(login="dee").avatar_url == avatar
    # Paired with this browser's everyday session of the same user (D51).
    assert session_row(token).paired_session_id == session_row(everyday).id
    (signin,) = [r for r in events(audit_log) if r["event"] == "admin_signin"]
    assert (signin["uid"], signin["method"], signin["github_login"]) == (
        dee,
        "github",
        "dee",
    )
    # A next outside the admin panel lands on it instead.
    again = github_sign_in(client, "dee", admin=True, next=at("quokka") + "/admin")
    assert again.json() == {"redirect": f"{PROD_BASE}/admin"}


def test_a_github_admin_without_an_everyday_session_is_unpaired(
    world: SimpleNamespace,
) -> None:
    client = world.client
    github_admin(client, "dee")
    client.cookies.clear()
    token = admin_cookie_of(github_sign_in(client, "dee", admin=True))
    assert session_row(token).paired_session_id is None
    # A password admin never has an everyday session to pair with.
    ada = admin_cookie_of(log_in(client, "ada@example.com"))
    assert session_row(ada).paired_session_id is None


def test_an_unknown_github_account_gets_no_account_and_no_session(
    world: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    """P3: an admin sign-in never creates an account or releases a login."""
    client = world.client
    with portal_db.get_session() as session:  # a stale holder of the login
        session.add(User(display_name="Old Dee", github_id=9999, github_login="dee"))
    before = user_and_session_rows()

    response = github_sign_in(client, "dee", admin=True)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "not_an_admin"
    assert response.json()["surface"] == "admin"
    assert ADMIN_COOKIE not in response.cookies and COOKIE not in response.cookies
    assert user_and_session_rows() == before  # no row, no release, no session
    (refused,) = [r for r in events(audit_log) if r["event"] == "admin_signin_refused"]
    assert (refused["reason"], refused["github_login"]) == ("unknown_user", "dee")
    assert not [r for r in events(audit_log) if r["event"] == "github_signin"]


@pytest.mark.parametrize("why", ["not_admin", "disabled"])
def test_a_github_account_that_may_not_administer_is_refused(
    world: SimpleNamespace, audit_log: pytest.LogCaptureFixture, why: str
) -> None:
    client = world.client
    if why == "not_admin":
        login = "ann"  # an org owner, not an instance admin
    else:
        login = "dee"
        github_admin(client, "dee")
        with portal_db.get_session() as session:
            user = session.exec(select(User).where(User.github_login == "dee")).one()
            user.disabled_at = "2026-10-04T00:00:00+00:00"
            session.add(user)
    client.cookies.clear()
    before = user_and_session_rows()

    response = github_sign_in(client, login, admin=True)
    assert response.status_code == 403, response.text
    assert response.json() == {
        "error": "this account is not an instance admin",
        "code": "not_an_admin",
        "surface": "admin",
    }
    assert ADMIN_COOKIE not in response.cookies
    assert user_and_session_rows() == before
    (refused,) = [r for r in events(audit_log) if r["event"] == "admin_signin_refused"]
    assert (refused["reason"], refused["github_login"]) == (why, login)


def test_an_admin_sign_in_without_2fa_is_refused_for_the_admin_page(
    world: SimpleNamespace,
    github_fake: FakeGitHub,
    audit_log: pytest.LogCaptureFixture,
) -> None:
    client = world.client
    response = github_sign_in(client, "nofa", admin=True)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "github_2fa_required"
    assert response.json()["surface"] == "admin"
    (refused,) = [r for r in events(audit_log) if r["event"] == "github_signin_refused"]
    assert (refused["reason"], refused["surface"]) == ("2fa_required", "admin")
    # The everyday sign-in's refusal carries no surface.
    everyday = github_sign_in(client, "nofa")
    assert "surface" not in everyday.json()
    last = [r for r in events(audit_log) if r["event"] == "github_signin_refused"][-1]
    assert "surface" not in last


def test_an_admin_sign_in_ends_the_admin_session_the_cookie_names(
    world: SimpleNamespace,
) -> None:
    """Fixation (rule 1): the admin cookie is read, the everyday one left alone."""
    client = world.client
    github_admin(client, "dee")
    everyday = client.cookies[COOKIE]
    first = admin_cookie_of(github_sign_in(client, "dee", admin=True))
    second = admin_cookie_of(github_sign_in(client, "dee", admin=True))
    assert first != second
    assert session_row(first) is None
    assert sessions.lookup(second, "admin") is not None
    assert sessions.lookup(everyday) is not None
    # The password sign-in does the same, whoever the cookie's session was.
    ada = admin_cookie_of(log_in(client, "ada@example.com"))
    assert session_row(second) is None and session_row(ada) is not None
    assert sessions.lookup(everyday) is not None


# ---------------------------------------------------------------------------
# The admin panel's own account (plan 59 section 4.2)
# ---------------------------------------------------------------------------


def test_admin_me_describes_the_admin_and_this_session(world: SimpleNamespace) -> None:
    client = signed_in_admin(world.client)
    body = client.get(at() + "/api/admin/me").json()
    session = body.pop("session")
    assert body == {
        "uid": world.ada,
        "display_name": "Ada",
        "github_login": None,
        "avatar_url": None,
        "has_password": True,
        "reader_grants": [],
    }
    started = datetime.fromisoformat(session["created_at"])
    ends = datetime.fromisoformat(session["expires_at"])
    assert ends - started == sessions.ADMIN_ABSOLUTE


def test_admin_logout_ends_only_the_admin_session(
    world: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    client = world.client
    dee = github_admin(client, "dee")
    everyday = client.cookies[COOKIE]
    token = admin_cookie_of(github_sign_in(client, "dee", admin=True))

    response = client.post(at() + "/api/admin/me/logout")
    assert response.status_code == 200, response.text
    assert response.json() == {"redirect": f"{PROD_BASE}/admin/login"}
    assert admin_clears(response)
    assert session_row(token) is None
    assert sessions.lookup(everyday) is not None  # D29: left alone
    assert client.get(at() + "/api/account").json()["uid"] == dee
    assert client.get(at() + "/api/admin/me").json() == ADMIN_LOGIN_REQUIRED
    (signout,) = [r for r in events(audit_log) if r["event"] == "admin_signout"]
    assert signout["uid"] == dee


def test_the_replaced_password_routes_are_gone(world: SimpleNamespace) -> None:
    client = world.client
    github_sign_in(client, "ann")
    gone = client.post(
        at() + "/api/auth/login",
        json={"email": "ada@example.com", "password": PROD_PASSWORD},
    )
    assert gone.status_code == 404
    assert gone.json() == {"error": "no API route /api/auth/login"}
    gone = client.post(
        at() + "/api/account/password",
        json={"current": PROD_PASSWORD, "new": "a brand new passphrase"},
    )
    assert gone.status_code == 404
    assert gone.json() == {"error": "no API route /api/account/password"}


# ---------------------------------------------------------------------------
# Sign-out and the admin session of the same browser (plan 59 D29, D51)
# ---------------------------------------------------------------------------


def test_a_base_host_sign_out_ends_an_unpaired_admin_session(
    world: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    client = world.client
    ada = admin_cookie_of(log_in(client, "ada@example.com"))  # unpaired
    # From an org host the admin cookie is not a browser's to send, and the
    # rows it names are not touched there.
    from_org = client.post(
        at("quokka") + "/api/auth/logout", headers={"Cookie": f"{ADMIN_COOKIE}={ada}"}
    )
    assert from_org.status_code == 200 and not admin_clears(from_org)
    assert sessions.lookup(ada, "admin") is not None
    # On the base host, signed out of the everyday portal or not, it ends.
    response = client.post(at() + "/api/auth/logout")
    assert response.status_code == 200
    assert admin_clears(response)
    assert session_row(ada) is None
    (signout,) = [r for r in events(audit_log) if r["event"] == "admin_signout"]
    assert (signout["uid"], signout["reason"]) == (world.ada, "signout")


@pytest.mark.parametrize("host", [None, "quokka"])
def test_an_everyday_sign_out_ends_the_paired_admin_session_only(
    world: SimpleNamespace, host: str | None
) -> None:
    client = world.client
    github_admin(client, "dee")
    first_day = client.cookies[COOKIE]
    first_admin = admin_cookie_of(github_sign_in(client, "dee", admin=True))
    client.cookies.clear()  # the same admin in a second browser
    github_sign_in(client, "dee")
    second_day = client.cookies[COOKIE]
    second_admin = admin_cookie_of(github_sign_in(client, "dee", admin=True))
    assert session_row(second_admin).paired_session_id == session_row(second_day).id

    # The first browser signs out; on an org host only its everyday cookie
    # arrives (the admin cookie is host-only).
    response = client.post(
        at(host) + "/api/auth/logout", headers={"Cookie": f"{COOKIE}={first_day}"}
    )
    assert response.status_code == 200
    assert session_row(first_day) is None
    assert session_row(first_admin) is None
    assert sessions.lookup(second_day) is not None
    assert sessions.lookup(second_admin, "admin") is not None


def test_another_users_sign_in_ends_the_admin_session_of_the_browser(
    world: SimpleNamespace,
) -> None:
    client = world.client
    github_admin(client, "dee")
    token = admin_cookie_of(github_sign_in(client, "dee", admin=True))
    response = github_sign_in(client, "ann")
    assert response.status_code == 200
    assert session_row(token) is None
    assert admin_clears(response)
    # An unpaired one (a password admin's) ends the same way.
    ada = admin_cookie_of(log_in(client, "ada@example.com"))
    response = github_sign_in(client, "bob")
    assert session_row(ada) is None and admin_clears(response)


def test_a_re_sign_in_of_the_same_user_moves_the_pairing(
    world: SimpleNamespace,
) -> None:
    client = world.client
    github_admin(client, "dee")
    token = admin_cookie_of(github_sign_in(client, "dee", admin=True))
    response = github_sign_in(client, "dee")
    assert response.status_code == 200 and not admin_clears(response)
    renewed = cookie_of(response)
    assert session_row(token).paired_session_id == session_row(renewed).id
    assert sessions.lookup(token, "admin") is not None
    # Duplicate everyday cookies leave the callback no principal, so only the
    # admin cookie's own row tells it what to re-pair with the new session.
    start = client.post(at() + "/api/auth/github/start", json={})
    authorize = client.app.state.portal.github._request(
        "GET", start.json()["authorize_url"] + "&" + urlencode({"login": "dee"})
    )
    query = parse_qs(urlsplit(authorize.headers["location"]).query)
    oauth = client.cookies["whygraph_oauth"]
    jar = (
        f"whygraph_oauth={oauth}; {COOKIE}={renewed}; {COOKIE}=tossed-in; "
        f"{ADMIN_COOKIE}={token}"
    )
    again = client.post(
        at() + "/api/auth/github/callback",
        json={"code": query["code"][0], "state": query["state"][0]},
        headers={"Cookie": jar},
    )
    assert again.status_code == 200, again.text
    assert session_row(renewed) is not None  # no principal: nothing revoked
    newest = cookie_of(again)
    assert session_row(token).paired_session_id == session_row(newest).id


def test_a_sweep_of_the_everyday_row_does_not_end_the_admin_session(
    world: SimpleNamespace,
) -> None:
    client = world.client
    github_admin(client, "dee")
    everyday = client.cookies[COOKIE]
    token = admin_cookie_of(github_sign_in(client, "dee", admin=True))
    past = _iso(datetime.now(timezone.utc) - timedelta(seconds=1))
    with portal_db.get_session() as session:
        row = session.get(UserSession, session_row(everyday).id)
        row.expires_at = past
        session.add(row)
    with portal_db.get_session() as session:  # any sign-in sweeps
        sessions.create_session(session, user_row("ada@example.com").id, "pytest")
    assert session_row(everyday) is None
    assert session_row(token).paired_session_id is None  # SET NULL, not CASCADE
    assert sessions.lookup(token, "admin") is not None


# ---------------------------------------------------------------------------
# The security event log (plan sections 0.2, 4.8)
# ---------------------------------------------------------------------------


def test_every_security_event_is_logged_once_with_its_fields(
    github_fake: FakeGitHub, audit_log: pytest.LogCaptureFixture
) -> None:
    secrets_used = []
    with prod_portal() as client:
        secrets_used.append(client.app.state.portal.bootstrap_secret)
        claim_instance(client)
        ada = uid_of("ada@example.com")
        ann = password_user("ann@example.com")  # inserted: no event
        github_sign_in(client, "ben")
        ben = uid_of_login("ben")
        with portal_db.get_session() as session:  # inserted: no event
            # Cy's GitHub id in the fake: adding resolves the login on GitHub.
            session.add(User(display_name="Cy", github_id=1002, github_login="cy"))
        cy = uid_of_login("cy")
        create_org(client, "quokka", "Quokka")
        members = at("quokka") + "/api/org/members"
        client.post(members, json={"github_login": "nobody", "role": "member"})
        client.post(members, json={"github_login": "cy", "role": "member"})
        client.patch(f"{members}/{cy}", json={"role": "admin"})
        client.delete(f"{members}/{cy}")
        client.post(members, json={"github_login": "cy", "role": "owner"})
        client.delete(at("quokka") + "/api/org/membership")  # ben leaves
        github_sign_in(client, "nofa")  # refused: no 2FA
        client.post(at() + "/api/auth/logout")
        log_in(client, "ann@example.com", "the wrong passphrase")
        log_in(client, "ann@example.com")  # refused: not an admin (D4)
        log_in(client, "ada@example.com")
        client.patch(at() + f"/api/admin/users/{ann}", json={"is_instance_admin": True})
        log_in(client, "ann@example.com")
        client.post(
            at() + "/api/admin/me/password",
            json={"current": PROD_PASSWORD, "new": "ann's second passphrase"},
        )
        client.post(at() + "/api/admin/me/logout")
        log_in(client, "ada@example.com")
        link = reset_link(client, ann).json()["url"]
        raw = token_in(link)
        client.patch(at() + f"/api/admin/users/{cy}", json={"disabled": True})
        client.patch(at() + f"/api/admin/users/{cy}", json={"disabled": False})
        client.cookies.clear()
        assert (
            client.post(
                at() + "/api/auth/reset",
                json={"token": raw, "password": "ann's third passphrase"},
            ).status_code
            == 200
        )
        dee = github_admin(client, "dee")  # ends Ann's admin session (D29)
        grant_reader(client, dee, "quokka")  # "Open as reader" (plan 59 D3)
        client.get(at("quokka") + "/api/projects")  # dee reads as `reader`
        github_sign_in(client, "dee", admin=True)
        client.patch(
            at() + f"/api/admin/users/{ann}", json={"is_instance_admin": False}
        )
        github_sign_in(client, "cy", admin=True)  # refused: not an admin
        # Ben renames on GitHub and a newcomer claims "ben"; GitHub's revoke
        # fails this once.
        github_fake.rename_user("ben", "benjamin")
        newcomer = github_fake.add_user("ben")
        github_fake.force("revoke", status=500)
        assert github_sign_in(client, "ben").status_code == 200
        ben2 = uid_of_login("ben")
        secrets_used.extend(
            [PROD_PASSWORD, raw, "ann's second passphrase", "ann's third passphrase"]
        )
        secrets_used.extend([*github_fake.codes, *github_fake.tokens])

    records = events(audit_log)
    assert [r["event"] for r in records] == [
        "bootstrap_claimed",
        "github_signin",
        "org_created",
        "member_add_refused",
        "member_added",
        "member_role_changed",
        "member_removed",
        "member_added",
        "member_left",
        "github_signin_refused",
        "logout",
        "login_failure",
        "login_failure",
        "admin_signin",
        "admin_granted",
        "admin_signin",
        "password_changed",
        "admin_signout",
        "admin_signin",
        "reset_link_issued",
        "user_disabled",
        "user_enabled",
        "reset_link_used",
        "github_signin",
        "reader_grant_created",
        "reader_request",
        "admin_signin",
        "admin_revoked",
        "admin_signin_refused",
        "github_token_revoke_failed",
        "github_login_released",
        "github_signin",
    ]
    by_event = {r["event"]: r for r in records}
    assert by_event["bootstrap_claimed"]["uid"] == ada
    first, by_dee, second = (r for r in records if r["event"] == "github_signin")
    assert (first["uid"], first["github_login"], first["new_user"]) == (
        ben,
        "ben",
        True,
    )
    assert (by_dee["uid"], by_dee["new_user"]) == (dee, True)
    assert (second["uid"], second["new_user"]) == (ben2, True) and ben2 != ben
    assert newcomer.id != github_fake.users["benjamin"].id
    assert by_event["github_signin_refused"]["reason"] == "2fa_required"
    assert by_event["github_signin_refused"]["github_login"] == "nofa"
    assert by_event["github_login_released"]["target"] == ben
    assert by_event["github_login_released"]["github_login"] == "ben"
    assert by_event["org_created"]["org"] == "quokka"
    assert by_event["org_created"]["uid"] == ben
    assert by_event["logout"]["uid"] == ben
    failures = [r for r in records if r["event"] == "login_failure"]
    assert all(r["email"] == "ann...@example.com" for r in failures)
    assert all(r["surface"] == "admin" for r in failures)
    assert [r.get("reason") for r in failures] == [None, "not_admin"]
    signins = [r for r in records if r["event"] == "admin_signin"]
    assert [(r["uid"], r["method"]) for r in signins] == [
        (ada, "password"),
        (ann, "password"),
        (ada, "password"),
        (dee, "github"),
    ]
    assert signins[-1]["github_login"] == "dee"
    assert by_event["admin_signout"]["uid"] == ann
    assert by_event["admin_signin_refused"]["reason"] == "not_admin"
    assert by_event["admin_signin_refused"]["github_login"] == "cy"
    assert by_event["password_changed"]["uid"] == ann
    assert by_event["reset_link_issued"]["uid"] == ada
    assert by_event["reset_link_issued"]["target"] == ann
    assert by_event["reset_link_used"]["uid"] == ann
    assert (by_event["admin_granted"]["uid"], by_event["admin_granted"]["target"]) == (
        ada,
        ann,
    )
    assert (by_event["admin_revoked"]["uid"], by_event["admin_revoked"]["target"]) == (
        dee,
        ann,
    )
    assert by_event["member_add_refused"]["reason"] == "no_such_github_user"
    assert by_event["member_add_refused"]["github_login"] == "nobody"
    added = [r for r in records if r["event"] == "member_added"]
    assert [(r["uid"], r["target"], r["role"]) for r in added] == [
        (ben, cy, "member"),
        (ben, cy, "owner"),
    ]
    assert all(r["org"] == "quokka" and r["github_login"] == "cy" for r in added)
    changed = by_event["member_role_changed"]
    assert (changed["target"], changed["role"], changed["previous"]) == (
        cy,
        "admin",
        "member",
    )
    assert by_event["member_removed"]["target"] == cy
    assert (by_event["member_left"]["uid"], by_event["member_left"]["role"]) == (
        ben,
        "owner",
    )
    for event in ("user_disabled", "user_enabled"):
        assert (by_event[event]["uid"], by_event[event]["target"]) == (ada, cy)
    granted = by_event["reader_grant_created"]
    assert (granted["uid"], granted["org"]) == (dee, "quokka")
    assert by_event["reader_request"]["org"] == "quokka"
    assert by_event["reader_request"]["method"] == "GET"
    assert all(r["ip"] == "203.0.113.5" for r in records)
    assert by_event["reader_request"]["host"] == f"quokka.whygraph.localhost:{PORT}"
    assert by_event["admin_signin"]["host"] == f"whygraph.localhost:{PORT}"

    # No password, session token, bootstrap secret, reset token, GitHub code
    # or GitHub token is ever logged.
    text = "\n".join(
        f"{r.getMessage()} {r.audit!r}"
        for r in audit_log.records
        if hasattr(r, "audit")
    )
    for value in secrets_used:
        assert value and value not in text
    assert "ann@example.com" not in text  # only the truncated form
    with portal_db.get_session() as session:
        for token_hash in session.exec(select(sessions.UserSession.token_hash)).all():
            assert token_hash not in text
