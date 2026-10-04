"""GitHub sign-in plumbing (M2d-1 step 2): config, pending logins, guard, fake.

The sign-in routes arrive with step 3; this file covers what exists before
them: the start-up configuration checks, :class:`PendingLogins`, the host
guard of :class:`GitHubOAuth`, the access-log redaction and the fake GitHub
itself.
"""

# ruff: noqa: F811 -- pytest fixtures imported from test_portal_app

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from github_fake import FakeGitHub, _s256
from test_portal_app import (  # noqa: F401 -- fixtures
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    GITHUB_FAKE_URL,
    PROD_BASE,
    env,
    github_fake,
    portal_client,
    prod_portal,
    production_env,
)
from whygraph.portal.app import PortalStartupError
from whygraph.portal.github_auth import (
    AccessLogRedactor,
    GitHubAuthConfig,
    GitHubHostError,
    GitHubOAuth,
    PendingLogins,
    load_github_config,
    parse_github_url,
)

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
