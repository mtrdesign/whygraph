"""GitHub sign-in (M2d-1 plan sections 4.3, 4.4, 4.9, 5.2).

The start-up configuration checks, :class:`PendingLogins`, the host guard of
:class:`GitHubOAuth`, the access-log redaction and the fake GitHub itself;
then the two sign-in routes driven end to end against the fake (happy path,
``next``, state binding, PKCE, scope, GitHub failures, 2FA, disabled
accounts, concurrency, token hygiene, renames) and what became of M2c's
password routes.
"""

# ruff: noqa: F811 -- pytest fixtures imported from test_portal_app

from __future__ import annotations

import base64
import logging
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import col, select

from github_fake import FakeGitHub, _s256
from test_portal_app import (  # noqa: F401 -- fixtures
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    GITHUB_FAKE_URL,
    PROD_BASE,
    PROD_PASSWORD,
    at,
    claim_instance,
    env,
    github_fake,
    github_sign_in,
    log_in,
    password_user,
    portal_client,
    prod_portal,
    production_env,
    uid_of_login,
)
from test_portal_identity_routes import audit_log, events  # noqa: F401 -- fixture
from whygraph.portal import auth_routes, sessions
from whygraph.portal import db as portal_db
from whygraph.portal.app import PortalStartupError
from whygraph.portal.github_auth import (
    AccessLogRedactor,
    GitHubAuthConfig,
    GitHubHostError,
    GitHubNoSuchUser,
    GitHubOAuth,
    GitHubRateLimited,
    GitHubUnavailable,
    PendingLogins,
    load_github_config,
    parse_github_url,
    pkce_challenge,
)
from whygraph.portal.hosts import BaseUrl
from whygraph.portal.models import User, UserSession
from whygraph.portal.throttle import Throttle

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "var, value, match",
    [
        (
            "WHYGRAPH_GITHUB_OAUTH_CLIENT_ID",
            None,
            "needs WHYGRAPH_GITHUB_OAUTH_CLIENT_ID",
        ),
        (
            "WHYGRAPH_GITHUB_OAUTH_CLIENT_ID",
            "  ",
            "needs WHYGRAPH_GITHUB_OAUTH_CLIENT_ID",
        ),
        (
            "WHYGRAPH_GITHUB_OAUTH_CLIENT_SECRET_FILE",
            None,
            "needs WHYGRAPH_GITHUB_OAUTH_CLIENT_SECRET_FILE",
        ),
        (
            "WHYGRAPH_GITHUB_OAUTH_CLIENT_SECRET_FILE",
            "",
            "needs WHYGRAPH_GITHUB_OAUTH_CLIENT_SECRET_FILE",
        ),
        (
            "WHYGRAPH_GITHUB_OAUTH_CLIENT_SECRET_FILE",
            "/nonexistent/secret",
            "cannot read WHYGRAPH_GITHUB_OAUTH_CLIENT_SECRET_FILE",
        ),
        ("WHYGRAPH_GITHUB_URL", "ftp://github.com", "WHYGRAPH_GITHUB_URL"),
        ("WHYGRAPH_GITHUB_API_URL", "http://api.github.com", "WHYGRAPH_GITHUB_API_URL"),
    ],
)
def test_production_refuses_a_bad_github_environment(
    production_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    var: str,
    value: str | None,
    match: str,
) -> None:
    if value is None:
        monkeypatch.delenv(var, raising=False)
    else:
        monkeypatch.setenv(var, value)
    with pytest.raises(PortalStartupError, match=match):
        with prod_portal():
            pass


def test_an_empty_secret_file_is_refused(
    production_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = production_env.tmp / "empty-secret"
    empty.write_text("\n  \n")
    monkeypatch.setenv("WHYGRAPH_GITHUB_OAUTH_CLIENT_SECRET_FILE", str(empty))
    with pytest.raises(PortalStartupError, match="is empty"):
        with prod_portal():
            pass


def test_the_github_check_runs_after_the_mode_check(
    production_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with prod_portal():
        pass
    # A stored production portal restarted in local mode, without any GitHub
    # variable: the mode-change message wins.
    monkeypatch.setenv("WHYGRAPH_MODE", "local")
    monkeypatch.delenv("WHYGRAPH_GITHUB_OAUTH_CLIENT_ID")
    with pytest.raises(PortalStartupError, match="cannot be changed"):
        with portal_client():
            pass


def test_local_mode_needs_no_github_and_holds_none(env: SimpleNamespace) -> None:
    with portal_client() as client:
        assert client.app.state.portal.github is None


def test_production_builds_the_client_and_closes_it(
    production_env: SimpleNamespace,
) -> None:
    with prod_portal() as client:
        github = client.app.state.portal.github
        assert isinstance(github, GitHubOAuth)
        assert github.config.client_id == GITHUB_CLIENT_ID
        assert github.config.client_secret == GITHUB_CLIENT_SECRET
        assert github.config.web_url == GITHUB_FAKE_URL
        assert github.config.api_url == GITHUB_FAKE_URL + "/api/v3"
        assert not github._client.is_closed
    assert github._client.is_closed


def test_defaults_point_at_github_com(tmp_path: Path) -> None:
    secret = tmp_path / "s"
    secret.write_text("shh\n")
    config = load_github_config(
        {
            "WHYGRAPH_GITHUB_OAUTH_CLIENT_ID": "id",
            "WHYGRAPH_GITHUB_OAUTH_CLIENT_SECRET_FILE": str(secret),
        }
    )
    assert config == GitHubAuthConfig(
        "id", "shh", "https://github.com", "https://api.github.com"
    )


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://github.com", "https://github.com"),
        ("https://github.com/", "https://github.com"),
        ("https://ghe.example.com/api/v3/", "https://ghe.example.com/api/v3"),
        ("https://10.0.0.5:8443", "https://10.0.0.5:8443"),
        ("http://127.0.0.1:9", "http://127.0.0.1:9"),
        ("http://127.0.0.1:9/api/v3", "http://127.0.0.1:9/api/v3"),
        ("http://localhost:1234", "http://localhost:1234"),
        ("http://whygraph.localhost", "http://whygraph.localhost"),
        ("http://[::1]:80", "http://[::1]:80"),
    ],
)
def test_parse_github_url_accepts(raw: str, expected: str) -> None:
    assert parse_github_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "github.com",
        "ftp://github.com",
        "http://github.com",
        "http://10.0.0.5",
        "http://notlocalhost",
        "https://",
        "https://github.com?x=1",
        "https://github.com/?",
        "https://github.com#frag",
        "https://user@github.com",
        "https://user:pw@github.com",
        "https://github.com:notaport",
    ],
)
def test_parse_github_url_refuses(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_github_url(raw, name="X")


# ---------------------------------------------------------------------------
# PendingLogins
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_pending_pop_is_single_use() -> None:
    pending = PendingLogins(clock=Clock())
    pending.put("s", "verifier", "http://x/next")
    entry = pending.pop("s")
    assert entry is not None
    assert (entry.verifier, entry.next) == ("verifier", "http://x/next")
    assert pending.pop("s") is None
    assert pending.pop("never-started") is None


def test_pending_entries_expire_after_ten_minutes() -> None:
    clock = Clock()
    pending = PendingLogins(clock=clock)
    pending.put("s", "v", None)
    clock.now += 599
    assert pending.pop("s") is not None
    pending.put("t", "v", None)
    clock.now += 600
    assert pending.pop("t") is None
    assert len(pending) == 0


def test_pending_cap_evicts_the_oldest() -> None:
    pending = PendingLogins(clock=Clock(), max_entries=3)
    for state in ("a", "b", "c", "d"):
        pending.put(state, "v", None)
    assert len(pending) == 3
    assert pending.pop("a") is None
    assert all(pending.pop(s) is not None for s in ("b", "c", "d"))


def test_pending_default_cap_is_ten_thousand() -> None:
    pending = PendingLogins()
    for i in range(10_005):
        pending.put(f"s{i}", "v", None)
    assert len(pending) == 10_000
    assert pending.pop("s0") is None
    assert pending.pop("s10004") is not None


# ---------------------------------------------------------------------------
# The host guard
# ---------------------------------------------------------------------------


def _oauth(handler=None) -> GitHubOAuth:  # noqa: ANN001
    config = GitHubAuthConfig(
        "id", "secret", "https://github.com", "https://api.github.com"
    )
    seen = handler or (lambda request: httpx.Response(204))
    return GitHubOAuth(config, transport=httpx.MockTransport(seen))


def test_github_oauth_sends_to_the_configured_hosts_only() -> None:
    github = _oauth()
    try:
        assert (
            github._request("GET", "https://github.com/login/oauth/x").status_code
            == 204
        )
        assert github._request("GET", "https://api.github.com/user").status_code == 204
        for url in (
            "https://evil.example/user",
            "http://github.com/login",  # right host, wrong scheme
            "https://github.com:8443/login",  # right host, wrong port
            "https://api.github.com.evil.example/user",
        ):
            with pytest.raises(GitHubHostError):
                github._request("GET", url)
    finally:
        github.close()
    assert github._client.is_closed


# ---------------------------------------------------------------------------
# The access-log filter
# ---------------------------------------------------------------------------


def _record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", path, "1.1", 200),
        None,
    )


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/auth/github?code=abc&state=xyz", "/auth/github?<redacted>"),
        ("/auth/github?error=access_denied", "/auth/github?<redacted>"),
        ("/auth/github", "/auth/github"),
        # The GitHub App's callback page (M2d-2 section 4.4).
        ("/auth/github-app?code=abc&state=xyz&iss=x", "/auth/github-app?<redacted>"),
        (
            "/auth/github-app?code=abc&state=xyz&installation_id=7&setup_action=install",
            "/auth/github-app?<redacted>",
        ),
        ("/auth/github-app", "/auth/github-app"),
        ("/signin?next=/orgs", "/signin?next=/orgs"),
        ("/api/auth/github/callback", "/api/auth/github/callback"),
    ],
)
def test_access_log_filter_redacts_the_callback_query(path: str, expected: str) -> None:
    record = _record(path)
    assert AccessLogRedactor().filter(record) is True
    message = record.getMessage()
    assert f'"GET {expected} HTTP/1.1"' in message
    assert "code=abc" not in message and "state=xyz" not in message


def test_access_log_filter_installed_on_the_logger_applies() -> None:
    class Collect(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.lines: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.lines.append(record.getMessage())

    logger = logging.getLogger("uvicorn.access")
    flt, handler = AccessLogRedactor(), Collect()
    logger.addFilter(flt)
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "1.2.3.4:5",
            "GET",
            "/auth/github?code=secretcode&state=s",
            "1.1",
            200,
        )
    finally:
        logger.setLevel(old_level)
        logger.removeHandler(handler)
        logger.removeFilter(flt)
    assert len(handler.lines) == 1
    assert "secretcode" not in handler.lines[0]
    assert "/auth/github?<redacted>" in handler.lines[0]


# ---------------------------------------------------------------------------
# The fake GitHub itself
# ---------------------------------------------------------------------------

REDIRECT = f"{PROD_BASE}/auth/github"
WEB = "http://127.0.0.1:9"


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub("cid", "csecret", REDIRECT)


def _client(fake: FakeGitHub) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(fake.handle))


def _authorize(
    client: httpx.Client,
    login: str,
    verifier: str,
    *,
    scope: str = "read:user",
    state: str = "st",
) -> str:
    response = client.get(
        f"{WEB}/login/oauth/authorize",
        params={
            "client_id": "cid",
            "redirect_uri": REDIRECT,
            "state": state,
            "code_challenge": _s256(verifier),
            "code_challenge_method": "S256",
            "scope": scope,
            "login": login,
        },
    )
    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == REDIRECT
    query = parse_qs(location.query)
    assert query["state"] == [state]
    return query["code"][0]


def _exchange(client: httpx.Client, code: str, verifier: str, **over: str) -> dict:
    data = {
        "client_id": "cid",
        "client_secret": "csecret",
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": verifier,
    } | over
    response = client.post(
        f"{WEB}/login/oauth/access_token",
        json=data,
        headers={"accept": "application/json"},
    )
    assert response.status_code == 200  # GitHub answers failures with 200 too
    return response.json()


def test_fake_authorize_page_lists_every_user(fake: FakeGitHub) -> None:
    with _client(fake) as client:
        page = client.get(
            f"{WEB}/login/oauth/authorize",
            params={"client_id": "cid", "redirect_uri": REDIRECT, "state": "s"},
        )
    assert page.status_code == 200
    for login in ("ben", "cy", "dee", "nofa"):
        assert f"Continue as {login}" in page.text


def test_fake_authorize_refuses_a_wrong_client_or_redirect(fake: FakeGitHub) -> None:
    with _client(fake) as client:
        bad_client = client.get(
            f"{WEB}/login/oauth/authorize",
            params={"client_id": "other", "redirect_uri": REDIRECT, "login": "ben"},
        )
        bad_redirect = client.get(
            f"{WEB}/login/oauth/authorize",
            params={
                "client_id": "cid",
                "redirect_uri": "http://evil/x",
                "login": "ben",
            },
        )
    assert bad_client.status_code == 404
    assert bad_redirect.status_code == 422


def test_fake_round_trip_and_revoke(fake: FakeGitHub) -> None:
    with _client(fake) as client:
        code = _authorize(client, "ben", "v" * 43)
        token = _exchange(client, code, "v" * 43)
        assert token["access_token"].startswith("gho_")
        assert token["token_type"] == "bearer"
        assert token["scope"] == "read:user"
        auth = {"authorization": f"Bearer {token['access_token']}"}
        user = client.get(f"{WEB}/api/v3/user", headers=auth).json()
        assert (user["id"], user["login"]) == (fake.users["ben"].id, "ben")
        assert user["two_factor_authentication"] is True
        assert {"name", "avatar_url"} <= set(user)

        revoke = client.request(
            "DELETE",
            f"{WEB}/api/v3/applications/cid/token",
            auth=("cid", "csecret"),
            json={"access_token": token["access_token"]},
        )
        assert revoke.status_code == 204
        assert client.get(f"{WEB}/api/v3/user", headers=auth).status_code == 401
    assert fake.is_revoked(token["access_token"])


def test_fake_revoke_needs_the_client_credentials(fake: FakeGitHub) -> None:
    with _client(fake) as client:
        code = _authorize(client, "cy", "v" * 43)
        token = _exchange(client, code, "v" * 43)["access_token"]
        response = client.request(
            "DELETE",
            f"{WEB}/api/v3/applications/cid/token",
            auth=("cid", "wrong"),
            json={"access_token": token},
        )
    assert response.status_code == 401
    assert not fake.is_revoked(token)


def test_fake_refuses_a_wrong_pkce_verifier_with_an_error_field(
    fake: FakeGitHub,
) -> None:
    with _client(fake) as client:
        code = _authorize(client, "ben", "v" * 43)
        body = _exchange(client, code, "w" * 43)
    assert body["error"] == "bad_verification_code"
    assert "access_token" not in body


def test_fake_codes_are_single_use(fake: FakeGitHub) -> None:
    with _client(fake) as client:
        code = _authorize(client, "ben", "v" * 43)
        assert "access_token" in _exchange(client, code, "v" * 43)
        assert _exchange(client, code, "v" * 43)["error"] == "bad_verification_code"


def test_fake_codes_expire() -> None:
    clock = Clock()
    fake = FakeGitHub("cid", "csecret", REDIRECT, now=clock)
    with _client(fake) as client:
        code = _authorize(client, "ben", "v" * 43)
        clock.now += 601
        assert _exchange(client, code, "v" * 43)["error"] == "bad_verification_code"


def test_fake_checks_client_secret_and_redirect(fake: FakeGitHub) -> None:
    with _client(fake) as client:
        code = _authorize(client, "ben", "v" * 43)
        assert (
            _exchange(client, code, "v" * 43, client_secret="nope")["error"]
            == "incorrect_client_credentials"
        )
        code = _authorize(client, "ben", "v" * 43)
        assert (
            _exchange(client, code, "v" * 43, redirect_uri="http://x/y")["error"]
            == "redirect_uri_mismatch"
        )


def test_fake_accepts_a_form_body(fake: FakeGitHub) -> None:
    with _client(fake) as client:
        code = _authorize(client, "ben", "v" * 43)
        response = client.post(
            f"{WEB}/login/oauth/access_token",
            data={
                "client_id": "cid",
                "client_secret": "csecret",
                "code": code,
                "redirect_uri": REDIRECT,
                "code_verifier": "v" * 43,
            },
        )
    assert "access_token" in response.json()


def test_fake_reports_2fa_only_for_a_read_user_token(fake: FakeGitHub) -> None:
    with _client(fake) as client:
        tokens = {}
        for login, scope in (("ben", "read:user"), ("ben", ""), ("nofa", "read:user")):
            code = _authorize(client, login, "v" * 43, scope=scope)
            tokens[(login, scope)] = _exchange(client, code, "v" * 43)["access_token"]

        def user(key: tuple[str, str]) -> dict:
            return client.get(
                f"{WEB}/api/v3/user",
                headers={"authorization": f"Bearer {tokens[key]}"},
            ).json()

        assert user(("ben", "read:user"))["two_factor_authentication"] is True
        assert "two_factor_authentication" not in user(("ben", ""))
        assert user(("nofa", "read:user"))["two_factor_authentication"] is False


def test_fake_can_grant_a_custom_scope(fake: FakeGitHub) -> None:
    fake.granted_scope = "read:user,repo"
    with _client(fake) as client:
        code = _authorize(client, "ben", "v" * 43)
        assert _exchange(client, code, "v" * 43)["scope"] == "read:user,repo"


def test_fake_rename_keeps_the_id_and_users_can_be_added(fake: FakeGitHub) -> None:
    ident = fake.users["ben"].id
    fake.rename_user("ben", "benjamin")
    assert fake.users["benjamin"].id == ident and "ben" not in fake.users
    new = fake.add_user("eve")
    assert new.id not in {u.id for login, u in fake.users.items() if login != "eve"}
    with _client(fake) as client:
        assert _authorize(client, "eve", "v" * 43)


def test_fake_can_force_a_failure(fake: FakeGitHub) -> None:
    fake.force("access_token", status=503)
    fake.force("user", exc=httpx.ReadTimeout("slow"))
    with _client(fake) as client:
        code = _authorize(client, "ben", "v" * 43)
        first = client.post(f"{WEB}/login/oauth/access_token", json={})
        assert first.status_code == 503
        token = _exchange(client, code, "v" * 43)["access_token"]  # forced once only
        with pytest.raises(httpx.ReadTimeout):
            client.get(
                f"{WEB}/api/v3/user", headers={"authorization": f"Bearer {token}"}
            )
        assert (
            client.get(
                f"{WEB}/api/v3/user", headers={"authorization": f"Bearer {token}"}
            ).status_code
            == 200
        )


def test_the_github_fake_fixture_serves_the_running_portal(
    github_fake: FakeGitHub,
) -> None:
    with prod_portal() as client:
        github = client.app.state.portal.github
        code = github_fake.issue_code("ben")
        response = github._request(
            "POST",
            f"{github.config.web_url}/login/oauth/access_token",
            json={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": f"{PROD_BASE}/auth/github",
            },
        )
        assert "access_token" in response.json()
        with pytest.raises(GitHubHostError):
            github._request("GET", "https://github.com/login/oauth/authorize")


def test_pkce_challenge_is_unpadded_base64url_sha256() -> None:
    challenge = pkce_challenge("a-verifier")
    assert challenge == _s256("a-verifier")
    assert len(challenge) == 43 and "=" not in challenge
    assert "+" not in challenge and "/" not in challenge


# ---------------------------------------------------------------------------
# Sign-in routes (plan section 4.4): helpers and fixtures
# ---------------------------------------------------------------------------

OAUTH = auth_routes.OAUTH_COOKIE
SESSION = sessions.COOKIE_NAME
START = at() + "/api/auth/github/start"
CALLBACK = at() + "/api/auth/github/callback"


@pytest.fixture
def portal(github_fake: FakeGitHub) -> Iterator[TestClient]:
    """A claimed production portal on the fake GitHub; the client is signed out."""
    with prod_portal() as client:
        claim_instance(client)
        client.cookies.clear()
        yield client


def begin(
    client: TestClient, login: str, *, next: str | None = None
) -> tuple[str, str]:
    """Start a sign-in and pass the fake's authorize step; return (code, state).

    ``client`` keeps the ``whygraph_oauth`` cookie the start route set.
    """
    start = client.post(START, json={"next": next})
    assert start.status_code == 200, start.text
    authorize = client.app.state.portal.github._request(
        "GET", start.json()["authorize_url"] + "&" + urlencode({"login": login})
    )
    assert authorize.status_code == 302, authorize.text
    query = parse_qs(urlsplit(authorize.headers["location"]).query)
    assert query["state"] == [client.cookies[OAUTH]]
    return query["code"][0], query["state"][0]


def callback(
    client: TestClient, code: str, state: str, *, cookie: str | None = None
) -> httpx.Response:
    """``POST`` the callback; ``cookie`` replaces the jar's cookies when given."""
    headers = {"Cookie": cookie} if cookie is not None else None
    return client.post(CALLBACK, json={"code": code, "state": state}, headers=headers)


def clears_oauth_cookie(response: httpx.Response) -> bool:
    """Whether ``response`` clears the ``whygraph_oauth`` cookie (host-only)."""
    return any(
        h.startswith(f'{OAUTH}="";') or h.startswith(f"{OAUTH}=;")
        if "Max-Age=0" in h and "Path=/api/auth/github" in h and "Domain" not in h
        else False
        for h in response.headers.get_list("set-cookie")
    )


def github_users() -> list[SimpleNamespace]:
    """Every GitHub account row, oldest first."""
    with portal_db.get_session() as session:
        rows = session.exec(
            select(User).where(col(User.github_id).is_not(None)).order_by(User.id)
        ).all()
        return [
            SimpleNamespace(
                id=u.id,
                uid=u.uid,
                github_id=u.github_id,
                github_login=u.github_login,
                avatar_url=u.avatar_url,
                display_name=u.display_name,
                email=u.email,
                password_hash=u.password_hash,
                disabled_at=u.disabled_at,
            )
            for u in rows
        ]


def assert_refused(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    assert clears_oauth_cookie(response), response.headers.get_list("set-cookie")
    assert SESSION not in response.cookies


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_the_start_route_binds_state_pkce_and_the_cookie(
    portal: TestClient, github_fake: FakeGitHub
) -> None:
    response = portal.post(START, json={})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    url = urlsplit(response.json()["authorize_url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == (
        f"{GITHUB_FAKE_URL}/login/oauth/authorize"
    )
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert set(query) == {
        "client_id",
        "redirect_uri",
        "state",
        "code_challenge",
        "code_challenge_method",
        "scope",
    }
    assert query["client_id"] == GITHUB_CLIENT_ID
    assert query["redirect_uri"] == f"{PROD_BASE}/auth/github"
    assert query["scope"] == "read:user"
    assert query["code_challenge_method"] == "S256"
    (cookie,) = response.headers.get_list("set-cookie")
    assert cookie.startswith(f"{OAUTH}={query['state']};")
    for part in ("HttpOnly", "Max-Age=600", "Path=/api/auth/github", "SameSite=lax"):
        assert part in cookie, cookie
    assert "Domain" not in cookie and "Secure" not in cookie  # host-only, http
    # The verifier stays on the server: only its challenge left.
    pending = portal.app.state.portal.github.pending.pop(query["state"])
    assert pending is not None and pending.next is None
    assert pkce_challenge(pending.verifier) == query["code_challenge"]
    assert pending.verifier not in response.text
    assert len(query["state"]) >= 40 and len(pending.verifier) >= 80
    assert github_fake.requests == []  # starting talks to nobody


def test_the_oauth_cookie_is_secure_on_https() -> None:
    args = auth_routes._oauth_cookie_args(BaseUrl.parse("https://whygraph.example"))
    assert args["secure"] is True and args["path"] == "/api/auth/github"
    clearing = auth_routes._oauth_clearing_header(
        BaseUrl.parse("https://whygraph.example")
    )
    assert "Secure" in clearing and "Max-Age=0" in clearing
    assert "Domain" not in clearing


def test_a_first_sign_in_creates_the_account_and_a_session(
    portal: TestClient, github_fake: FakeGitHub
) -> None:
    response = github_sign_in(portal, "ben")
    assert response.status_code == 200, response.text
    assert response.json() == {"redirect": f"{PROD_BASE}/orgs"}
    assert response.headers["cache-control"] == "no-store"
    assert clears_oauth_cookie(response)
    (ben,) = github_users()
    fake_ben = github_fake.users["ben"]
    assert (ben.github_id, ben.github_login) == (fake_ben.id, "ben")
    assert ben.avatar_url == fake_ben.avatar_url
    assert ben.display_name == "Ben"  # GitHub's name
    assert ben.email is None and ben.password_hash is None  # nothing else kept
    assert sessions.lookup(response.cookies[SESSION]) is not None
    assert portal.get(at() + "/api/account").json() == {
        "uid": ben.uid,
        "email": None,
        "display_name": "Ben",
        "is_instance_admin": False,
        "github_login": "ben",
        "avatar_url": fake_ben.avatar_url,
        "has_password": False,
    }
    user = portal.get(at() + "/api/portal/state").json()["user"]
    assert (user["github_login"], user["avatar_url"], user["has_password"]) == (
        "ben",
        fake_ben.avatar_url,
        False,
    )
    # GitHub's token was revoked straight after reading the profile.
    (token,) = github_fake.tokens_for("ben")
    assert github_fake.is_revoked(token)
    assert portal.get(at() + "/api/account/orgs").json() == []


def test_a_nameless_github_account_is_named_by_its_login(
    portal: TestClient, github_fake: FakeGitHub
) -> None:
    github_fake.add_user("quiet").name = None
    assert github_sign_in(portal, "quiet").status_code == 200
    assert github_users()[0].display_name == "quiet"


def test_the_callback_lands_on_a_safe_next_only(portal: TestClient) -> None:
    kept = [f"{PROD_BASE}/account", at("quokka") + "/p/api/explorer"]
    dropped = ["https://evil.example/", "//evil.example/", "/orgs", "javascript:x"]
    for target in kept:
        response = github_sign_in(portal, "ben", next=target)
        assert response.json() == {"redirect": target}
    for target in dropped:
        response = github_sign_in(portal, "ben", next=target)
        assert response.json() == {"redirect": f"{PROD_BASE}/orgs"}, target


def test_a_second_sign_in_finds_the_account_by_id_and_keeps_its_name(
    portal: TestClient, github_fake: FakeGitHub
) -> None:
    assert github_sign_in(portal, "ben").status_code == 200
    (first,) = github_users()
    assert (
        portal.patch(at() + "/api/account", json={"display_name": "Benedict"})
    ).status_code == 200
    github_fake.users["ben"].avatar_url = "https://avatars.example.test/new"
    github_fake.users["ben"].name = "Ben on GitHub"
    portal.cookies.clear()

    assert github_sign_in(portal, "ben").status_code == 200
    (again,) = github_users()
    assert again.uid == first.uid and again.id == first.id
    assert again.avatar_url == "https://avatars.example.test/new"  # refreshed
    assert again.display_name == "Benedict"  # the user's own edit is kept


def test_a_sign_in_over_an_existing_session_revokes_it(portal: TestClient) -> None:
    ben_token = github_sign_in(portal, "ben").cookies[SESSION]
    cy_token = github_sign_in(portal, "cy").cookies[SESSION]
    assert sessions.lookup(ben_token) is None  # the browser's old session ended
    assert sessions.lookup(cy_token).uid == uid_of_login("cy")
    # A password session carried into a GitHub sign-in ends too.
    ada_token = log_in(portal, "ada@example.com").cookies[SESSION]
    assert github_sign_in(portal, "ben").status_code == 200
    assert sessions.lookup(ada_token) is None
    assert sessions.lookup(cy_token) is not None  # another browser's: untouched


# ---------------------------------------------------------------------------
# The state binding (plan section 0.2 #5)
# ---------------------------------------------------------------------------


def test_a_missing_cookie_is_oauth_state(portal: TestClient) -> None:
    code, state = begin(portal, "ben")
    portal.cookies.clear()
    assert_refused(callback(portal, code, state), 400, "oauth_state")
    assert github_users() == []


def test_a_mismatched_cookie_is_oauth_state_and_spends_nothing(
    portal: TestClient,
) -> None:
    """Login CSRF: the attacker's code + state, the victim's browser cookie."""
    code, state = begin(portal, "ben")
    refused = callback(portal, code, state, cookie=f"{OAUTH}=someone-elses-state")
    assert_refused(refused, 400, "oauth_state")
    assert github_users() == []
    # The pending sign-in was not spent by the refusal.
    assert callback(portal, code, state, cookie=f"{OAUTH}={state}").status_code == 200


def test_two_oauth_cookies_are_oauth_state(portal: TestClient) -> None:
    code, state = begin(portal, "ben")
    tossed = f"{OAUTH}={state}; {OAUTH}={state}"
    assert_refused(callback(portal, code, state, cookie=tossed), 400, "oauth_state")


def test_an_unknown_state_is_oauth_state(portal: TestClient) -> None:
    code, _ = begin(portal, "ben")
    forged = "a-state-nobody-started"
    response = callback(portal, code, forged, cookie=f"{OAUTH}={forged}")
    assert_refused(response, 400, "oauth_state")


def test_an_expired_state_is_oauth_state(portal: TestClient) -> None:
    clock = Clock()
    portal.app.state.portal.github.pending = PendingLogins(clock=clock)
    code, state = begin(portal, "ben")
    clock.now += 601
    assert_refused(callback(portal, code, state), 400, "oauth_state")
    assert github_users() == []


def test_a_replayed_state_is_oauth_state(portal: TestClient) -> None:
    code, state = begin(portal, "ben")
    assert callback(portal, code, state).status_code == 200
    portal.cookies.clear()
    replay = callback(portal, code, state, cookie=f"{OAUTH}={state}")
    assert_refused(replay, 400, "oauth_state")


def test_the_pending_cap_evicts_the_oldest_sign_in(portal: TestClient) -> None:
    portal.app.state.portal.github.pending = PendingLogins(max_entries=2)
    first = begin(portal, "ben")
    begin(portal, "cy")
    third = begin(portal, "dee")
    code, state = first
    assert_refused(
        callback(portal, code, state, cookie=f"{OAUTH}={state}"), 400, "oauth_state"
    )
    code, state = third
    assert callback(portal, code, state, cookie=f"{OAUTH}={state}").status_code == 200


# ---------------------------------------------------------------------------
# PKCE, scope and GitHub's failures
# ---------------------------------------------------------------------------


def test_a_wrong_pkce_verifier_is_github_auth_failed(portal: TestClient) -> None:
    code, state = begin(portal, "ben")
    pending = portal.app.state.portal.github.pending
    entry = pending.pop(state)
    pending.put(state, entry.verifier[::-1], entry.next)  # not the challenged one
    assert_refused(callback(portal, code, state), 400, "github_auth_failed")
    assert github_users() == []


@pytest.mark.parametrize("scope", ["read:user,repo", "", "user", "repo read:user"])
def test_any_scope_but_read_user_is_github_auth_failed(
    portal: TestClient, github_fake: FakeGitHub, scope: str
) -> None:
    github_fake.granted_scope = scope
    assert_refused(github_sign_in(portal, "ben"), 400, "github_auth_failed")
    assert github_users() == []
    (token,) = github_fake.tokens_for("ben")
    assert github_fake.is_revoked(token)
    assert github_fake.calls("GET", "/api/v3/user") == []  # refused before /user


def test_a_github_error_field_is_github_auth_failed(
    portal: TestClient, github_fake: FakeGitHub
) -> None:
    github_fake.client_secret = "rotated"  # GitHub answers 200 + error
    response = github_sign_in(portal, "ben")
    assert_refused(response, 400, "github_auth_failed")
    assert github_fake.tokens == {}


@pytest.mark.parametrize(
    ("route", "failure"),
    [
        ("access_token", {"status": 500}),
        ("access_token", {"status": 503}),
        ("access_token", {"exc": httpx.ReadTimeout("slow")}),
        ("access_token", {"exc": httpx.ConnectError("down")}),
        ("user", {"status": 502}),
        ("user", {"exc": httpx.ReadTimeout("slow")}),
    ],
)
def test_github_being_down_is_github_unavailable(
    portal: TestClient, github_fake: FakeGitHub, route: str, failure: dict
) -> None:
    github_fake.force(route, **failure)
    assert_refused(github_sign_in(portal, "ben"), 502, "github_unavailable")
    assert github_users() == []
    # A token obtained before /user failed is still revoked.
    assert all(github_fake.is_revoked(t) for t in github_fake.tokens)


def test_a_failed_user_lookup_is_github_auth_failed(
    portal: TestClient, github_fake: FakeGitHub
) -> None:
    github_fake.force("user", status=401)
    assert_refused(github_sign_in(portal, "ben"), 400, "github_auth_failed")
    assert all(github_fake.is_revoked(t) for t in github_fake.tokens)


def test_a_failed_revoke_is_audited_never_an_error(
    portal: TestClient,
    github_fake: FakeGitHub,
    audit_log: pytest.LogCaptureFixture,
) -> None:
    github_fake.force("revoke", exc=httpx.ConnectError("down"))
    assert github_sign_in(portal, "ben").status_code == 200
    github_fake.force("revoke", status=500)
    assert github_sign_in(portal, "ben").status_code == 200
    names = [r["event"] for r in events(audit_log)]
    assert names.count("github_token_revoke_failed") == 2
    assert names.count("github_signin") == 2


# ---------------------------------------------------------------------------
# Refusals: 2FA and disabled accounts
# ---------------------------------------------------------------------------


def test_an_account_without_2fa_is_refused_and_leaves_no_row(
    portal: TestClient, github_fake: FakeGitHub
) -> None:
    response = github_sign_in(portal, "nofa")
    assert_refused(response, 403, "github_2fa_required")
    assert response.json()["fix_url"] == f"{GITHUB_FAKE_URL}/settings/security"
    assert github_users() == []
    (token,) = github_fake.tokens_for("nofa")
    assert github_fake.is_revoked(token)


def test_a_missing_2fa_field_is_refused_the_same_way(
    portal: TestClient, github_fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = github_fake._user

    def without_2fa(request: httpx.Request) -> httpx.Response:
        body = real(request).json()
        body.pop("two_factor_authentication", None)
        return httpx.Response(200, json=body)

    monkeypatch.setattr(github_fake, "_user", without_2fa)
    assert_refused(github_sign_in(portal, "ben"), 403, "github_2fa_required")
    assert github_users() == []


def test_a_disabled_account_is_refused_after_github_authenticated(
    portal: TestClient, github_fake: FakeGitHub
) -> None:
    token = github_sign_in(portal, "ben").cookies[SESSION]
    with portal_db.get_session() as session:
        user = session.exec(select(User).where(User.github_login == "ben")).one()
        user.disabled_at = "2026-10-04T00:00:00+00:00"
        session.add(user)
    # The surviving session resolves to signed out at once.
    assert sessions.lookup(token) is None
    assert portal.get(at() + "/api/account").status_code == 401
    github_fake.users["ben"].avatar_url = "https://avatars.example.test/changed"
    portal.cookies.clear()

    response = github_sign_in(portal, "ben")
    assert_refused(response, 403, "account_disabled")
    (ben,) = github_users()
    assert ben.avatar_url != "https://avatars.example.test/changed"  # no write
    with portal_db.get_session() as session:  # no new session was started
        assert (
            len(
                session.exec(
                    select(UserSession.id).where(UserSession.user_id == ben.id)
                ).all()
            )
            == 1
        )
    assert all(github_fake.is_revoked(t) for t in github_fake.tokens_for("ben"))


# ---------------------------------------------------------------------------
# Concurrency, renames
# ---------------------------------------------------------------------------


def test_two_concurrent_first_sign_ins_make_one_account(
    portal: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A double click: both callbacks reach the upsert together."""
    first = begin(portal, "ben")
    second = begin(portal, "ben")
    portal.cookies.clear()
    barrier = threading.Barrier(2, timeout=10)
    github = portal.app.state.portal.github
    real = github.identify

    def identify(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        user = real(*args, **kwargs)
        barrier.wait()
        return user

    monkeypatch.setattr(github, "identify", identify)
    with ThreadPoolExecutor(2) as pool:
        responses = list(
            pool.map(
                lambda pair: callback(portal, *pair, cookie=f"{OAUTH}={pair[1]}"),
                [first, second],
            )
        )
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    (ben,) = github_users()
    for response in responses:
        assert sessions.lookup(response.cookies[SESSION]).uid == ben.uid


def test_a_renamed_github_user_keeps_their_account(
    portal: TestClient, github_fake: FakeGitHub
) -> None:
    assert github_sign_in(portal, "ben").status_code == 200
    (before,) = github_users()
    github_fake.rename_user("ben", "benjamin")
    assert github_sign_in(portal, "benjamin").status_code == 200
    (after,) = github_users()
    assert after.uid == before.uid and after.github_login == "benjamin"


def test_a_freed_username_reclaimed_by_a_new_id_is_released_from_the_old_row(
    portal: TestClient,
    github_fake: FakeGitHub,
    audit_log: pytest.LogCaptureFixture,
) -> None:
    assert github_sign_in(portal, "ben").status_code == 200
    old_uid = uid_of_login("ben")
    # Ben renames on GitHub but does not sign in; someone else takes the
    # name, in another case.
    github_fake.rename_user("ben", "benjamin")
    newcomer = github_fake.add_user("Ben")
    assert github_sign_in(portal, "Ben").status_code == 200
    old, new = github_users()
    assert old.uid == old_uid and old.github_login is None  # released
    assert (new.github_id, new.github_login) == (newcomer.id, "Ben")
    released = [r for r in events(audit_log) if r["event"] == "github_login_released"]
    assert [r["target"] for r in released] == [old_uid]
    # The old account is findable again once its owner signs in.
    assert github_sign_in(portal, "benjamin").status_code == 200
    old, new = github_users()
    assert old.github_login == "benjamin" and new.github_login == "Ben"


# ---------------------------------------------------------------------------
# Token hygiene (plan section 0.2 #3, acceptance criterion 3)
# ---------------------------------------------------------------------------


def test_no_code_token_state_or_verifier_reaches_a_log_a_response_or_a_row(
    portal: TestClient,
    github_fake: FakeGitHub,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logging.getLogger("whygraph"), "propagate", True)
    caplog.set_level(logging.DEBUG)
    bodies = []
    # Success, 2FA refusal, scope refusal, error field, unavailable, disabled.
    bodies.append(github_sign_in(portal, "ben"))
    bodies.append(github_sign_in(portal, "nofa"))
    github_fake.granted_scope = "read:user,repo"
    bodies.append(github_sign_in(portal, "cy"))
    github_fake.granted_scope = None
    github_fake.force("access_token", exc=httpx.ReadTimeout("slow"))
    bodies.append(github_sign_in(portal, "cy"))
    github_fake.force("user", status=500)
    bodies.append(github_sign_in(portal, "cy"))
    with portal_db.get_session() as session:
        ben = session.exec(select(User).where(User.github_login == "ben")).one()
        ben.disabled_at = "2026-10-04T00:00:00+00:00"
        session.add(ben)
    bodies.append(github_sign_in(portal, "ben"))
    code, state = begin(portal, "dee")
    bodies.append(callback(portal, code, state, cookie=f"{OAUTH}=wrong"))
    assert [r.status_code for r in bodies] == [200, 403, 400, 502, 502, 403, 400]

    # Every token GitHub handed out was revoked.
    assert github_fake.tokens and all(t.revoked for t in github_fake.tokens.values())
    secrets_seen = [*github_fake.codes, *github_fake.tokens]
    for request in github_fake.calls("GET", "/login/oauth/authorize"):
        secrets_seen.append(request.url.params["state"])
    for request in github_fake.calls("POST", "/login/oauth/access_token"):
        secrets_seen.append(parse_qs(request.content.decode())["code_verifier"][0])
    assert github_fake.codes and github_fake.tokens and len(secrets_seen) > 10

    whygraph_records = [r for r in caplog.records if r.name.startswith("whygraph")]
    assert any(r.name == "whygraph.portal.audit" for r in whygraph_records)
    logged = "\n".join(
        f"{r.getMessage()} {getattr(r, 'audit', '')!r} {r.exc_text or ''}"
        for r in whygraph_records
    )
    answered = "\n".join(
        r.text + " " + " ".join(f"{k}={v}" for k, v in r.headers.items())
        for r in bodies
    )
    with portal_db.get_session() as session:
        rows = repr(session.exec(select(User)).all()) + repr(
            session.exec(select(UserSession)).all()
        )
    for value in secrets_seen:
        assert value not in logged
        assert value not in answered
        assert value not in rows


# ---------------------------------------------------------------------------
# Throttling, the bootstrap, and M2c's routes (plan section 4.4's table)
# ---------------------------------------------------------------------------


def test_start_and_callback_share_one_ip_throttle(portal: TestClient) -> None:
    portal.app.state.portal.github_ip = Throttle(3, 900)
    code, state = begin(portal, "ben")  # 1
    assert callback(portal, code, state).status_code == 200  # 2
    assert portal.post(START, json={}).status_code == 200  # 3
    throttled = portal.post(START, json={})
    assert throttled.status_code == 429 and throttled.json()["code"] == "throttled"
    late = callback(portal, "a-code", portal.cookies[OAUTH])
    assert_refused(late, 429, "throttled")
    assert int(late.headers["Retry-After"]) > 0


def test_both_github_routes_need_the_bootstrap_first(
    github_fake: FakeGitHub,
) -> None:
    with prod_portal() as client:
        start = client.post(START, json={})
        assert start.status_code == 409
        assert start.json()["code"] == "bootstrap_required"
        assert "set-cookie" not in start.headers
        response = callback(client, "a-code", "a-state", cookie=f"{OAUTH}=a-state")
        assert_refused(response, 409, "bootstrap_required")
    assert github_fake.requests == []


def test_the_github_routes_are_base_host_only(portal: TestClient) -> None:
    for path in ("/api/auth/github/start", "/api/auth/github/callback"):
        response = portal.post(at("quokka") + path, json={"code": "c", "state": "s"})
        assert response.status_code == 404, path


def test_register_is_gone_and_the_password_admin_still_signs_in(
    portal: TestClient,
) -> None:
    assert log_in(portal, "ada@example.com").status_code == 200
    gone = portal.post(
        at() + "/api/auth/register",
        json={
            "email": "eve@example.com",
            "display_name": "Eve",
            "password": PROD_PASSWORD,
        },
    )
    assert gone.status_code == 404, gone.text
    with portal_db.get_session() as session:
        assert session.exec(select(User.email)).all() == ["ada@example.com"]


def test_a_github_account_has_no_password_to_use_change_or_reset(
    portal: TestClient,
) -> None:
    assert github_sign_in(portal, "ben").status_code == 200
    ben = uid_of_login("ben")
    for email in ("ben", "ben@users.noreply.github.com", ""):
        refused = portal.post(
            at() + "/api/auth/login", json={"email": email, "password": PROD_PASSWORD}
        )
        assert refused.status_code == 401, email
        assert refused.json()["code"] == "bad_credentials"
    change = portal.post(
        at() + "/api/account/password",
        json={"current": PROD_PASSWORD, "new": "a brand new passphrase"},
    )
    assert change.status_code == 409 and change.json()["code"] == "no_password"
    assert log_in(portal, "ada@example.com").status_code == 200
    link = portal.post(at() + f"/api/admin/users/{ben}/reset-link")
    assert link.status_code == 409 and link.json()["code"] == "no_password"
    # A password account still gets its link.
    pat = password_user("pat@example.com")
    assert portal.post(at() + f"/api/admin/users/{pat}/reset-link").status_code == 200


# ---------------------------------------------------------------------------
# Looking a login up (M2f-1 plan section 4.8)
# ---------------------------------------------------------------------------


def _profile(**over: object) -> dict:
    return {
        "id": 42,
        "login": "Gus",
        "name": "Gus G",
        "avatar_url": "https://a.example/42",
        "type": "User",
    } | over


def _lookup(*answers: httpx.Response | Exception, budget=None):  # noqa: ANN001, ANN202
    """``user_by_login("gus")`` against canned answers; ``(result, requests)``."""
    seen: list[httpx.Request] = []
    queue = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    github = _oauth(handler)
    try:
        return github.user_by_login("gus", anonymous_budget=budget), seen
    finally:
        github.close()


def test_user_by_login_sends_the_client_credentials() -> None:
    user, seen = _lookup(httpx.Response(200, json=_profile()))
    assert (user.id, user.login, user.name, user.two_factor) == (
        42,
        "Gus",
        "Gus G",
        False,
    )
    (request,) = seen
    assert str(request.url) == "https://api.github.com/users/gus"
    assert request.headers["authorization"] == "Basic " + base64.b64encode(
        b"id:secret"
    ).decode("ascii")


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(404, json={"message": "Not Found"}),
        httpx.Response(200, json=_profile(type="Organization")),
        httpx.Response(200, json=_profile(type="Bot")),
    ],
)
def test_user_by_login_no_such_user(answer: httpx.Response) -> None:
    with pytest.raises(GitHubNoSuchUser):
        _lookup(answer)


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500),
        httpx.Response(502),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("down"),
        httpx.Response(403, json={"message": "Forbidden"}),  # not a rate limit
        httpx.Response(200, json={"id": "42", "login": "gus", "type": "User"}),
        httpx.Response(200, text="not json"),
    ],
)
def test_user_by_login_unavailable(answer: httpx.Response | Exception) -> None:
    with pytest.raises(GitHubUnavailable):
        _lookup(answer)


def test_user_by_login_rate_limits() -> None:
    reset = str(int(time.time()) + 120)
    with pytest.raises(GitHubRateLimited) as primary:
        _lookup(
            httpx.Response(
                403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset},
            )
        )
    assert 100 <= primary.value.retry_after <= 120
    with pytest.raises(GitHubRateLimited) as secondary:
        _lookup(httpx.Response(429, headers={"retry-after": "30"}))
    assert secondary.value.retry_after == 30


def test_refused_credentials_retry_anonymously_within_the_budget() -> None:
    refused = httpx.Response(401, json={"message": "Bad credentials"})
    budget = Throttle(1, 3600)
    user, seen = _lookup(
        refused,
        httpx.Response(200, json=_profile()),
        budget=lambda: budget.hit("anonymous"),
    )
    assert user.id == 42
    assert [("authorization" in r.headers) for r in seen] == [True, False]
    # The budget is spent: no anonymous retry.
    with pytest.raises(GitHubRateLimited) as spent:
        _lookup(refused, budget=lambda: budget.hit("anonymous"))
    assert spent.value.retry_after > 0
    # No budget at all: GitHub is unavailable to us.
    with pytest.raises(GitHubUnavailable):
        _lookup(refused)


def test_the_fake_answers_user_lookups_and_records_the_credentials(
    fake: FakeGitHub,
) -> None:
    fake.add_user("acme-corp", account_type="Organization")
    good = "Basic " + base64.b64encode(b"cid:csecret").decode("ascii")
    bad = "Basic " + base64.b64encode(b"cid:wrong").decode("ascii")
    with _client(fake) as client:
        found = client.get(f"{WEB}/api/v3/users/BEN", headers={"authorization": good})
        assert found.status_code == 200
        assert {k: found.json()[k] for k in ("id", "login", "type")} == {
            "id": 1001,
            "login": "ben",
            "type": "User",
        }
        assert client.get(f"{WEB}/api/v3/users/cy").status_code == 200
        org = client.get(f"{WEB}/api/v3/users/acme-corp").json()
        assert org["type"] == "Organization"
        assert client.get(f"{WEB}/api/v3/users/nobody").status_code == 404
        refused = client.get(f"{WEB}/api/v3/users/ben", headers={"authorization": bad})
        assert refused.status_code == 401
    assert fake.user_lookups == [
        ("BEN", True),
        ("cy", False),
        ("acme-corp", False),
        ("nobody", False),
        ("ben", False),
    ]
