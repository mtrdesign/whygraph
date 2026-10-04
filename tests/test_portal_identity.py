"""Production startup, sessions and request identity (M2c plan section 5.3).

Production clients use real host names as URLs (``http://whygraph.localhost``
and ``http://<org>.whygraph.localhost``), so httpx's cookie jar sends the
``Domain=whygraph.localhost`` session cookie to every org host. Sessions are
made directly with :func:`whygraph.portal.sessions.create_session` until the
sign-in routes exist (``signed_in``).
"""

# ruff: noqa: F811 -- pytest fixtures (`env`, `production_env`) imported from test_portal_app

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from sqlmodel import select
from starlette.responses import Response

from test_portal_app import (  # noqa: F401 -- fixtures
    PORT,
    PROD_BASE,
    at,
    env,
    prod_portal,
    production_env,
    signed_in,
)
from conftest import HeaderIdentity
from whygraph.portal import db as portal_db
from whygraph.portal import sessions
from whygraph.portal.app import PortalStartupError
from whygraph.portal.deps import LocalIdentity, SessionIdentity
from whygraph.portal.hosts import BaseUrl
from whygraph.portal.models import Organization, Setting, User, UserSession
from whygraph.portal.orgs import add_member, create_org

SECRET_RE = re.compile(r"Bootstrap secret: ([A-Za-z0-9_-]{24})")
LOGIN_REQUIRED = {"error": "sign-in required", "code": "login_required"}
DEV = BaseUrl.parse(PROD_BASE)
HTTPS = BaseUrl.parse("https://whygraph.example.com")


@pytest.fixture
def portal_logs(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> pytest.LogCaptureFixture:
    """``caplog`` for the portal loggers (a CLI test may stop propagation)."""
    monkeypatch.setattr(logging.getLogger("whygraph"), "propagate", True)
    caplog.set_level(logging.INFO, logger="whygraph.portal")
    return caplog


def _settings_row() -> SimpleNamespace | None:
    with portal_db.get_session() as session:
        row = session.get(Setting, 1)
        if row is None:
            return None
        return SimpleNamespace(mode=row.mode, builtin_org_id=row.builtin_org_id)


def _user(email: str, *, admin: bool = False) -> int:
    with portal_db.get_session() as session:
        user = User(display_name=email.split("@")[0].title(), email=email)
        user.is_instance_admin = admin
        session.add(user)
        session.flush()
        assert user.id is not None
        return user.id


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _session_row(token: str) -> UserSession:
    with portal_db.get_session() as session:
        row = session.exec(
            select(UserSession).where(
                UserSession.token_hash == sessions.hash_token(token)
            )
        ).one()
        session.expunge(row)
        return row


def _age(token: str, **fields: timedelta) -> None:
    """Move a session's timestamps ``fields`` into the past."""
    now = datetime.now(timezone.utc)
    with portal_db.get_session() as session:
        row = session.exec(
            select(UserSession).where(
                UserSession.token_hash == sessions.hash_token(token)
            )
        ).one()
        for name, delta in fields.items():
            setattr(row, name, _iso(now - delta))
        session.add(row)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("var", "value", "match"),
    [
        ("WHYGRAPH_BASE_URL", None, "needs WHYGRAPH_BASE_URL"),
        ("WHYGRAPH_BASE_URL", "http://whygraph.example.com", "only allowed"),
        ("WHYGRAPH_BASE_URL", "https://whygraph.example.com/app", "path"),
        ("WHYGRAPH_SHARED_FOLDERS", "/srv/repos", "must be empty"),
        ("WHYGRAPH_TRUSTED_PROXIES", "10.0.0.1, bogus", "'bogus'"),
    ],
)
def test_production_refuses_a_bad_environment_and_writes_nothing(
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
    assert _settings_row() is None  # a refused first start leaves no trace
    with portal_db.get_session() as session:
        assert session.exec(select(Organization)).all() == []


def test_a_stored_production_portal_validates_without_the_mode_variable(
    production_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with prod_portal():
        pass
    assert _settings_row().mode == "production"
    monkeypatch.delenv("WHYGRAPH_MODE")
    monkeypatch.delenv("WHYGRAPH_BASE_URL")
    with pytest.raises(PortalStartupError, match="needs WHYGRAPH_BASE_URL"):
        with prod_portal():
            pass
    monkeypatch.setenv("WHYGRAPH_BASE_URL", PROD_BASE)
    monkeypatch.setenv("WHYGRAPH_SHARED_FOLDERS", "/srv/repos")
    with pytest.raises(PortalStartupError, match="must be empty"):
        with prod_portal():
            pass
    monkeypatch.delenv("WHYGRAPH_SHARED_FOLDERS")
    with prod_portal() as client:
        assert client.app.state.portal.mode == "production"


def test_the_mode_cannot_change_either_way(
    production_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with prod_portal():
        pass
    monkeypatch.setenv("WHYGRAPH_MODE", "local")
    with pytest.raises(PortalStartupError, match="cannot be changed"):
        with prod_portal():
            pass


def test_a_local_portal_cannot_become_production(
    production_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHYGRAPH_MODE", "local")
    with prod_portal():
        pass
    monkeypatch.setenv("WHYGRAPH_MODE", "production")
    # The mismatch wins even though the production environment is broken.
    monkeypatch.setenv("WHYGRAPH_SHARED_FOLDERS", "/srv/repos")
    with pytest.raises(PortalStartupError, match="cannot be changed"):
        with prod_portal():
            pass


def test_production_starts_without_a_builtin_org_or_local_services(
    production_env: SimpleNamespace,
) -> None:
    with prod_portal() as client:
        state = client.app.state.portal
        assert state.mode == "production"
        assert state.base_url == BaseUrl.parse(PROD_BASE)
        assert isinstance(state.identity, SessionIdentity)
        assert state.builtin_org_id is None and state.builtin_org_slug is None
        assert state.origins.base == state.base_url
        assert state.origins.base_url == PROD_BASE
        assert state.shared_folders == ()
        assert state.session_manager is None  # no MCP in production
        assert state.port_change is None  # no port reconcile
    setting = _settings_row()
    assert setting.mode == "production" and setting.builtin_org_id is None
    with portal_db.get_session() as session:
        assert session.exec(select(Organization)).all() == []


def test_the_base_url_self_check_result_is_kept(
    production_env: SimpleNamespace,
) -> None:
    with prod_portal() as client:
        state = client.app.state.portal
        deadline = time.monotonic() + 5
        while state.base_check is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert state.base_check == []  # skipped for *.localhost


def test_an_injected_identity_is_kept_in_production(
    production_env: SimpleNamespace,
) -> None:
    identity = HeaderIdentity()
    with prod_portal(identity=identity) as client:
        assert client.app.state.portal.identity is identity


def test_local_mode_keeps_the_local_identity(env: SimpleNamespace) -> None:
    with prod_portal(base_url=f"http://127.0.0.1:{PORT}") as client:
        state = client.app.state.portal
        assert isinstance(state.identity, LocalIdentity)
        assert state.base_url is None and state.bootstrap_secret is None


def test_dev_origins_are_ignored_in_production_with_a_warning(
    production_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    portal_logs: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("WHYGRAPH_DEV_ORIGINS", "http://localhost:5173")
    with prod_portal() as client:
        assert client.app.state.portal.origins.origins == frozenset()
        response = client.get("/api/portal/state", headers={"host": "localhost:5173"})
        assert response.status_code == 421
    assert "WHYGRAPH_DEV_ORIGINS is ignored in production mode" in portal_logs.text


def test_the_bootstrap_secret_is_logged_as_two_short_records(
    production_env: SimpleNamespace, portal_logs: pytest.LogCaptureFixture
) -> None:
    with prod_portal() as client:
        secret = client.app.state.portal.bootstrap_secret
        state = client.get("/api/portal/state").json()
    records = [
        r.getMessage()
        for r in portal_logs.records
        if r.name == "whygraph.portal.app" and r.levelno == logging.WARNING
    ]
    assert records == [
        f"First-time setup: open {PROD_BASE}/setup and enter the bootstrap secret "
        "below.",
        f"Bootstrap secret: {secret}",
    ]
    match = SECRET_RE.fullmatch(records[1])
    assert match is not None and match.group(1) == secret
    assert state["bootstrap_required"] is True
    assert state["setup_complete"] is False


def test_each_start_makes_a_new_secret_until_an_admin_exists(
    production_env: SimpleNamespace, portal_logs: pytest.LogCaptureFixture
) -> None:
    with prod_portal() as client:
        first = client.app.state.portal.bootstrap_secret
    with prod_portal() as client:
        second = client.app.state.portal.bootstrap_secret
    assert first and second and first != second
    assert SECRET_RE.findall(portal_logs.text) == [first, second]

    _user("ada@example.com", admin=True)
    portal_logs.clear()
    with prod_portal() as client:
        assert client.app.state.portal.bootstrap_secret is None
        state = client.get("/api/portal/state").json()
    assert "Bootstrap secret" not in portal_logs.text
    assert state["bootstrap_required"] is False
    assert state["setup_complete"] is True


def test_a_degraded_production_portal_shows_its_error(
    production_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(portal_db, "ensure_initialized", _boom)
    with prod_portal() as client:
        state = client.app.state.portal
        assert state.degraded is not None and state.session_manager is None
        for prefix in (at(), at("quokka")):
            response = client.get(prefix + "/api/portal/state")
            assert response.status_code == 200  # not 421
            assert "disk on fire" in response.json()["error"]
            assert response.headers["x-frame-options"] == "DENY"
        assert client.get(at("quokka") + "/api/projects").status_code == 503
        assert client.get(at() + "/mcp/api").status_code == 404


def test_a_degraded_portal_without_a_usable_base_url_serves_local_hosts(
    production_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        portal_db, "ensure_initialized", lambda: (_ for _ in ()).throw(RuntimeError)
    )
    monkeypatch.setenv("WHYGRAPH_BASE_URL", "not a url")
    with prod_portal() as client:
        assert client.get(at() + "/api/portal/state").status_code == 421
        local = client.get(f"http://127.0.0.1:{PORT}/api/portal/state")
        assert local.status_code == 200 and "error" in local.json()


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def test_the_cli_refuses_bad_trusted_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    from whygraph.cli.commands.portal import portal_cmd

    monkeypatch.setenv("WHYGRAPH_TRUSTED_PROXIES", "10.0.0.0/8,*")
    result = CliRunner().invoke(portal_cmd, [])
    assert result.exit_code == 2
    assert "WHYGRAPH_TRUSTED_PROXIES entry '*'" in result.output


def test_the_cli_exits_2_on_a_refused_start(
    production_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    from whygraph.cli.commands.portal import portal_cmd
    from whygraph.portal import app as app_mod

    seen: dict = {}

    class FakeServer:
        def __init__(self, config: uvicorn.Config, app) -> None:  # noqa: ANN001
            seen["forwarded"] = config.forwarded_allow_ips
            self.app = app

        def run(self) -> None:
            self.app.state.portal.startup_error = PortalStartupError("no base URL")

    monkeypatch.setattr(app_mod, "PortalServer", FakeServer)
    monkeypatch.setenv("WHYGRAPH_TRUSTED_PROXIES", "172.18.0.1/16")
    result = CliRunner().invoke(portal_cmd, [])
    assert result.exit_code == 2, result.output
    assert "error: no base URL" in result.output
    assert f"WhyGraph portal → {PROD_BASE}" in result.output
    assert seen["forwarded"] == "172.18.0.0/16"

    monkeypatch.delenv("WHYGRAPH_TRUSTED_PROXIES")
    monkeypatch.setenv("WHYGRAPH_MODE", "local")
    result = CliRunner().invoke(portal_cmd, [])
    assert seen["forwarded"] == ""  # trust no proxy, not uvicorn's 127.0.0.1
    assert f"WhyGraph portal → http://127.0.0.1:{PORT}" in result.output


def test_the_cli_refusal_message_no_longer_mentions_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.cli.commands.portal import portal_cmd

    monkeypatch.delenv("WHYGRAPH_IN_IMAGE", raising=False)
    result = CliRunner().invoke(portal_cmd, ["--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert "refusing to bind 0.0.0.0 outside the WhyGraph image" in result.output
    assert "login" not in result.output


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def test_create_session_stores_only_the_tokens_hash(
    production_env: SimpleNamespace,
) -> None:
    portal_db.ensure_initialized()
    uid = _user("ann@example.com")
    with portal_db.get_session() as session:
        token = sessions.create_session(session, uid, "x" * 500)
    assert len(token) >= 43
    row = _session_row(token)
    assert row.token_hash == sessions.hash_token(token) != token
    assert len(row.token_hash) == 64
    assert row.user_agent == "x" * 200
    assert row.expires_at > row.created_at == row.last_seen_at
    found = sessions.lookup(token)
    assert found is not None and found.user_id == uid
    assert found.email == "ann@example.com" and found.is_instance_admin is False
    assert sessions.lookup(token + "x") is None
    assert sessions.lookup("") is None


def test_expired_and_idle_sessions_are_refused_and_swept(
    production_env: SimpleNamespace,
) -> None:
    portal_db.ensure_initialized()
    uid = _user("ann@example.com")
    with portal_db.get_session() as session:
        expired = sessions.create_session(session, uid, None)
        idle = sessions.create_session(session, uid, None)
        live = sessions.create_session(session, uid, None)
    now = datetime.now(timezone.utc)
    with portal_db.get_session() as session:
        for token, field, value in (
            (expired, "expires_at", now - timedelta(seconds=1)),
            (idle, "last_seen_at", now - sessions.IDLE - timedelta(seconds=1)),
        ):
            row = session.exec(
                select(UserSession).where(
                    UserSession.token_hash == sessions.hash_token(token)
                )
            ).one()
            setattr(row, field, _iso(value))
            session.add(row)
    assert sessions.lookup(expired) is None
    assert sessions.lookup(idle) is None
    assert sessions.lookup(live) is not None
    with portal_db.get_session() as session:  # the next sign-in sweeps both
        sessions.create_session(session, uid, None)
    with portal_db.get_session() as session:
        hashes = set(session.exec(select(UserSession.token_hash)).all())
    assert sessions.hash_token(expired) not in hashes
    assert sessions.hash_token(idle) not in hashes
    assert sessions.hash_token(live) in hashes


def test_touch_writes_at_most_every_five_minutes(
    production_env: SimpleNamespace,
) -> None:
    portal_db.ensure_initialized()
    uid = _user("ann@example.com")
    with portal_db.get_session() as session:
        token = sessions.create_session(session, uid, None)
    row = _session_row(token)
    assert sessions.touch(row.id) is False  # just created
    _age(token, last_seen_at=timedelta(minutes=4))
    assert sessions.touch(row.id) is False
    _age(token, last_seen_at=timedelta(minutes=6))
    before = _session_row(token).last_seen_at
    assert sessions.touch(row.id) is True
    assert _session_row(token).last_seen_at > before
    assert sessions.touch(row.id) is False


def test_a_request_touches_a_stale_session_only(
    production_env: SimpleNamespace,
) -> None:
    with prod_portal() as client:
        uid = _user("ann@example.com")
        token = signed_in(client, uid)
        _age(token, last_seen_at=timedelta(minutes=4))
        stamp = _session_row(token).last_seen_at
        assert client.get("/api/portal/state").json()["user"] is not None
        assert _session_row(token).last_seen_at == stamp  # fresh enough
        _age(token, last_seen_at=timedelta(minutes=6))
        stamp = _session_row(token).last_seen_at
        assert client.get("/api/portal/state").json()["user"] is not None
        assert _session_row(token).last_seen_at > stamp


def test_revoke_rotate_and_revoke_user(production_env: SimpleNamespace) -> None:
    portal_db.ensure_initialized()
    ann, bob = _user("ann@example.com"), _user("bob@example.com")
    with portal_db.get_session() as session:
        a1, a2, a3 = (sessions.create_session(session, ann, None) for _ in range(3))
        b1 = sessions.create_session(session, bob, None)
    sessions.revoke(_session_row(a1).id)
    assert sessions.lookup(a1) is None and sessions.lookup(a2) is not None
    with portal_db.get_session() as session:
        a4 = sessions.rotate(session, _session_row(a2).id, ann, None)
    assert sessions.lookup(a2) is None and sessions.lookup(a4) is not None
    sessions.revoke_user(ann, except_id=_session_row(a4).id)
    assert sessions.lookup(a3) is None and sessions.lookup(a4) is not None
    assert sessions.lookup(b1) is not None  # another user's sessions stay
    with portal_db.get_session() as session:
        sessions.revoke_user(ann, db=session)
    assert sessions.lookup(a4) is None and sessions.lookup(b1) is not None


@pytest.mark.parametrize(("base", "secure"), [(DEV, False), (HTTPS, True)])
def test_the_session_cookie_attributes(base: BaseUrl, secure: bool) -> None:
    response = Response()
    sessions.set_cookie(response, "tok", base)
    (header,) = response.headers.getlist("set-cookie")
    parts = {p.strip().split("=")[0].lower(): p.strip() for p in header.split(";")}
    assert header.startswith("whygraph_session=tok;")
    assert parts["domain"] == f"Domain={base.host}"  # no port
    assert ":" not in parts["domain"]
    assert parts["path"] == "Path=/"
    assert parts["max-age"] == f"Max-Age={30 * 24 * 3600}"
    assert "httponly" in parts
    assert parts["samesite"].lower() == "samesite=lax"
    assert ("secure" in parts) is secure


def test_clearing_the_cookie_covers_the_domain_and_host_only_forms() -> None:
    response = Response()
    sessions.clear_cookie(response, DEV)
    headers = response.headers.getlist("set-cookie")
    assert len(headers) == 2
    assert all("Max-Age=0" in h and h.startswith("whygraph_session=") for h in headers)
    assert sum(f"Domain={DEV.host}" in h for h in headers) == 1
    assert [h.encode() for h in headers] == [
        v for _, v in sessions.clearing_headers(DEV)
    ]


def test_session_cookies_keeps_duplicates() -> None:
    assert sessions.session_cookies([]) == []
    assert sessions.session_cookies(["a=1; whygraph_session=x"]) == ["x"]
    assert sessions.session_cookies(
        ["whygraph_session=x; other=2", 'whygraph_session="y"']
    ) == ["x", "y"]
    assert sessions.session_cookies(["whygraph_sessionx=1; xwhygraph_session=2"]) == []


# ---------------------------------------------------------------------------
# Request identity
# ---------------------------------------------------------------------------


def _quokka_world() -> SimpleNamespace:
    """Org ``quokka`` owned by ann; bob belongs to no org."""
    with portal_db.get_session() as session:
        org = create_org(session, slug="quokka", name="Quokka")
        ann = User(display_name="Ann", email="ann@example.com")
        bob = User(display_name="Bob", email="bob@example.com")
        session.add_all([ann, bob])
        session.flush()
        add_member(session, org_id=org.id, user_id=ann.id, role="owner")
        return SimpleNamespace(ann=ann.id, bob=bob.id, ann_uid=ann.uid)


def test_a_members_session_works_on_its_org_host(
    production_env: SimpleNamespace,
) -> None:
    with prod_portal() as client:
        world = _quokka_world()
        signed_in(client, world.ann)
        state = client.get(at("quokka") + "/api/portal/state")
        assert state.status_code == 200, state.text
        body = state.json()
        assert body["mode"] == "production"
        assert body["host_kind"] == "org"
        assert body["base_url"] == PROD_BASE
        assert body["bootstrap_required"] is True  # no instance admin yet
        assert body["org"] == {"slug": "quokka", "name": "Quokka", "role": "owner"}
        assert body["user"] == {
            "uid": world.ann_uid,
            "display_name": "Ann",
            "role": "owner",
            "email": "ann@example.com",
            "is_instance_admin": False,
            "github_login": None,
            "avatar_url": None,
            "has_password": False,  # this row was inserted with no hash
        }
        projects = client.get(at("quokka") + "/api/projects")
        assert projects.status_code == 200 and projects.json() == {"projects": []}
        base = client.get(at() + "/api/portal/state").json()
        assert base["host_kind"] == "base" and base["org"] is None
        assert base["user"]["role"] is None


def test_without_a_session_org_routes_are_401(production_env: SimpleNamespace) -> None:
    with prod_portal() as client:
        _quokka_world()
        for prefix in (at("quokka"), at()):
            response = client.get(prefix + "/api/projects")
            assert response.status_code == 401
            assert response.json() == LOGIN_REQUIRED
            unknown = client.get(prefix + "/api/no/such/route")
            assert unknown.status_code == 401  # the /api 404 also needs a user
        state = client.get(at("quokka") + "/api/portal/state").json()
        assert state["user"] is None and state["org"] is None
        assert state["setup_complete"] is False


def test_org_routes_on_the_base_host_are_404_with_a_session(
    production_env: SimpleNamespace,
) -> None:
    with prod_portal() as client:
        world = _quokka_world()
        signed_in(client, world.ann)
        response = client.get(at() + "/api/projects")
        assert response.status_code == 404
        assert response.json() == {"error": "not found"}


def test_a_non_member_and_an_unknown_org_are_404(
    production_env: SimpleNamespace,
) -> None:
    with prod_portal() as client:
        world = _quokka_world()
        signed_in(client, world.bob)
        for slug in ("quokka", "narwhal"):
            response = client.get(at(slug) + "/api/projects")
            assert response.status_code == 404
            assert response.json() == {"error": "not found"}


def test_an_expired_session_is_signed_out(production_env: SimpleNamespace) -> None:
    with prod_portal() as client:
        world = _quokka_world()
        token = signed_in(client, world.ann)
        _age(token, expires_at=timedelta(seconds=1))
        response = client.get(at("quokka") + "/api/projects")
        assert response.status_code == 401 and response.json() == LOGIN_REQUIRED


def test_two_session_cookies_mean_signed_out_and_both_are_cleared(
    production_env: SimpleNamespace,
) -> None:
    with prod_portal() as client:
        world = _quokka_world()
        with portal_db.get_session() as session:
            token = sessions.create_session(session, world.ann, None)
        # A valid session plus one tossed in from a sibling host.
        cookie = f"whygraph_session={token}; whygraph_session=tossed"
        for path in ("/api/projects", "/mcp/api"):
            response = client.get(at("quokka") + path, headers={"Cookie": cookie})
            if path == "/api/projects":
                assert response.status_code == 401
                assert response.json() == LOGIN_REQUIRED
            else:
                assert response.status_code == 404
            set_cookies = response.headers.get_list("set-cookie")
            assert len(set_cookies) == 2, set_cookies
            assert all("Max-Age=0" in h for h in set_cookies)
            assert sum("Domain=whygraph.localhost" in h for h in set_cookies) == 1
            assert response.headers["cache-control"] == "no-store"
        # One cookie alone signs in, and nothing is cleared.
        single = client.get(
            at("quokka") + "/api/projects",
            headers={"Cookie": f"whygraph_session={token}"},
        )
        assert single.status_code == 200
        assert "set-cookie" not in single.headers


def test_a_session_cookie_never_reaches_a_lookalike_host(tmp_path: Path) -> None:
    """httpx's jar (what the tests rely on) scopes the cookie like a browser."""
    import httpx

    jar = httpx.Cookies()
    jar.set("whygraph_session", "tok", domain="whygraph.localhost")
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("cookie")
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handler), cookies=jar) as http:
        for host in (
            "whygraph.localhost",
            "quokka.whygraph.localhost",
            "evil.example.com",
            "acme.whygraph.localhost.evil.com",
        ):
            http.get(f"http://{host}:8765/")
    assert seen["whygraph.localhost"] == "whygraph_session=tok"
    assert seen["quokka.whygraph.localhost"] == "whygraph_session=tok"
    assert seen["evil.example.com"] is None
    assert seen["acme.whygraph.localhost.evil.com"] is None
