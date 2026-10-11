"""Keys and config (M2f-3 plan sections 4.13, 6.1 #3 / #6, 6.2 #10 / #16).

The free key probe (:mod:`whygraph.portal.key_test`) through an
``httpx.MockTransport`` per provider shape; the key-test throttle's pairs;
the config / defaults payloads per role (tails and ``key_last_used`` only for
configurers, ``effective_keys``, ``inherited``, ``github``); the three test
routes (action gates, ``key_missing`` / ``bad_provider`` / ``not_testable`` /
``not_github``, the GitHub token test's ``404`` in production, the throttle,
the ``key_tested`` audit event, no ledger row); and a project's connections
with ``include=revoked``. Nothing here reaches the network: the conftest's
``key_test_network`` answers every probe.
"""

# ruff: noqa: F811 -- pytest fixtures (`env`, `team`, ...) are imported

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Iterator

import httpx
import pytest
from sqlmodel import func, select

from test_portal_app import (  # noqa: F401 -- fixtures
    at,
    env,
    github_fake,
    make_repo,
    portal_client,
    production_env,
)
from test_portal_hosts_isolation import _insert_project
from test_portal_identity_routes import audit_log, events  # noqa: F401 -- fixture
from test_portal_members import be, team, with_roles  # noqa: F401 -- fixture
from whygraph.portal import db as portal_db
from whygraph.portal.key_test import (
    BODY_CAP,
    PROBES,
    KeyTestResult,
    probe_llm_key,
    probe_url,
)
from whygraph.portal.models import (
    ConnectionToken,
    Project,
    ProjectGrant,
    ScanRun,
    UsageEvent,
    User,
)
from whygraph.portal.secrets import LLM_API_KEY, put_secret
from whygraph.portal.throttle import Throttle
from whygraph.services.github import GitHubError, RepoAccessError

NOW = datetime.now(timezone.utc)
ORG_ANTHROPIC = "sk-ant-org-key-9f3c"
ORG_OPENAI = "sk-org-openai-7a7a"
PROJECT_OPENAI = "sk-proj-openai-a1b2"
PROJECT_DEEPSEEK = "sk-deepseek-proj-4d4d"
GITHUB_TOKEN = "ghp_projecttoken123456"
REMOTE = "https://github.com/acme/widget.git"


def _stamp(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


def _transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# The probe (section 6.1 #6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "url", "header", "value"),
    [
        ("anthropic", "https://api.anthropic.com/v1/models", "x-api-key", "k-1"),
        ("openai", "https://api.openai.com/v1/models", "authorization", "Bearer k-1"),
        ("deepseek", "https://api.deepseek.com/models", "authorization", "Bearer k-1"),
        (
            "openrouter",
            "https://openrouter.ai/api/v1/key",
            "authorization",
            "Bearer k-1",
        ),
    ],
)
def test_each_provider_is_probed_at_its_documented_endpoint(
    provider: str, url: str, header: str, value: str
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "secret-model-list"}]})

    found = probe_llm_key(provider, "k-1", transport=_transport(handler))
    assert found == KeyTestResult(ok=True, result="ok")
    (request,) = seen
    assert (request.method, str(request.url)) == ("GET", url)
    assert request.headers[header] == value
    if provider == "anthropic":
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers


def test_openrouter_is_never_probed_on_its_keyless_models_listing() -> None:
    assert probe_url("openrouter").endswith("/api/v1/key")
    assert set(PROBES) == {"anthropic", "openai", "deepseek", "openrouter"}


@pytest.mark.parametrize(
    ("status", "result"),
    [
        (200, "ok"),
        (204, "ok"),
        (401, "rejected"),
        (403, "rejected"),
        (429, "rate_limited"),
        (500, "unreachable"),
        (503, "unreachable"),
        (404, "unexpected"),
        (400, "unexpected"),
    ],
)
def test_statuses_map_to_result_words(status: int, result: str) -> None:
    found = probe_llm_key(
        "openai",
        "k",
        transport=_transport(lambda r: httpx.Response(status, text="provider says")),
    )
    assert found == KeyTestResult(ok=result == "ok", result=result)


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow"),
    ],
)
def test_network_failures_are_unreachable(exc: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    found = probe_llm_key("anthropic", "k", transport=_transport(handler))
    assert found.result == "unreachable" and found.ok is False


def test_a_redirect_is_not_followed() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    found = probe_llm_key("openai", "k", transport=_transport(handler))
    assert found.result == "unexpected"
    assert seen == ["https://api.openai.com/v1/models"]


def test_a_custom_base_url_is_probed_and_the_body_never_returned() -> None:
    seen: list[str] = []
    body = b"internal secret page " * (BODY_CAP // 10)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=body)

    found = probe_llm_key(
        "openai", "k", "http://gateway.internal:8080/v1/", transport=_transport(handler)
    )
    assert seen == ["http://gateway.internal:8080/v1/models"]
    # Only a word comes back: nothing of the body, nothing of the key.
    assert found == KeyTestResult(ok=True, result="ok")
    assert set(vars(found)) == {"ok", "result"}


def test_an_unusable_base_url_is_not_a_crash() -> None:
    found = probe_llm_key("openai", "k", "ftp://gateway.example/v1")
    assert found.result in ("unreachable", "unexpected")


# ---------------------------------------------------------------------------
# The throttle's pairs (section 6.1 #3)
# ---------------------------------------------------------------------------


def test_the_key_test_pairs_count_both_or_neither() -> None:
    now = [0.0]
    throttle = Throttle(60, 3600, clock=lambda: now[0])

    def hit(user: int) -> int | None:
        return throttle.hit_all([(("u", 1, user), 10), (("o", 1), 60)])

    for _ in range(10):
        assert hit(1) is None
    assert hit(1) is not None  # the user's 11th in the hour
    # The refused call counted on neither key.
    assert throttle.remaining(("o", 1), limit=60) == 50
    assert throttle.remaining(("u", 1, 1), limit=10) == 0
    for user in range(2, 7):  # five more users, ten each: the org's 60
        for _ in range(10):
            assert hit(user) is None
    retry = hit(7)
    assert retry is not None and 0 < retry <= 3600
    assert throttle.remaining(("u", 1, 7), limit=10) == 10  # not counted
    now[0] += 3601
    assert hit(7) is None


# ---------------------------------------------------------------------------
# Local mode: the payload and the routes (section 6.2 #10)
# ---------------------------------------------------------------------------


@pytest.fixture
def local(env: SimpleNamespace) -> Iterator[SimpleNamespace]:
    """A local portal with ``demo`` (a GitHub origin) and keys at both layers.

    Portal defaults: Anthropic and OpenAI keys and a GitHub token. ``demo``:
    its own OpenAI key... and its own OpenAI endpoint, which takes it off
    the org key; its own DeepSeek key and GitHub token.
    """
    root = make_repo(env.shared, "demo", remote=REMOTE)
    with portal_client() as client:
        assert (
            client.post("/api/portal/setup", json={"display_name": "Tess"}).status_code
            == 201
        )
        added = client.post(
            "/api/projects", json={"source": "local", "path": str(root)}
        )
        assert added.status_code == 201, added.text
        defaults = client.put(
            "/api/portal/defaults",
            json={
                "secrets": {
                    "llm": {"anthropic": ORG_ANTHROPIC, "openai": ORG_OPENAI},
                    "github_token": "ghp_orgtoken99999999",
                }
            },
        )
        assert defaults.status_code == 200, defaults.text
        project = client.put(
            "/api/projects/demo/config",
            json={
                "config": {"llm": {"openai": {"base_url": "http://gw.example/v1"}}},
                "secrets": {
                    "llm": {"deepseek": PROJECT_DEEPSEEK},
                    "github_token": GITHUB_TOKEN,
                },
            },
        )
        assert project.status_code == 200, project.text
        with portal_db.get_session() as session:
            row = session.exec(select(Project).where(Project.slug == "demo")).one()
            project_id, org_id = row.id, row.org_id
            user_id = session.exec(select(User.id)).one()
        yield SimpleNamespace(
            client=client,
            root=root,
            state=client.app.state.portal,
            project_id=project_id,
            org_id=org_id,
            user_id=user_id,
        )


def _usage(
    w: SimpleNamespace, provider: str, scope: str, at: str, *, project_id=None
) -> None:  # noqa: ANN001
    with portal_db.get_session() as session:
        session.add(
            UsageEvent(
                org_id=w.org_id,
                project_id=project_id,
                project_slug="demo",
                project_name="demo",
                actor_kind="member",
                user_id=w.user_id,
                actor_label="Tess",
                source="chat",
                task="chat",
                provider=provider,
                model_requested="m",
                key_scope=scope,
                cost_source="estimated",
                input_tokens=1,
                output_tokens=1,
                cost_usd=Decimal("0.01"),
                created_at=at,
            )
        )


def _scan(project_id: int, trigger: str, status: str, finished: str) -> None:
    with portal_db.get_session() as session:
        session.add(
            ScanRun(
                project_id=project_id,
                trigger=trigger,
                analyze=False,
                status=status,
                queued_at=finished,
                started_at=finished,
                finished_at=finished,
            )
        )


def _ledger_rows() -> int:
    with portal_db.get_session() as session:
        return session.exec(select(func.count()).select_from(UsageEvent)).one()


def test_the_local_config_payload_says_where_each_key_comes_from(
    local: SimpleNamespace,
) -> None:
    w = local
    # Ledger rows: the org's Anthropic key used in another project later than
    # here; the project's DeepSeek key; an org-scope OpenAI row, which does
    # not count for a project off the org key.
    _usage(w, "anthropic", "org", _stamp(3), project_id=w.project_id)
    _usage(w, "anthropic", "org", _stamp(1), project_id=None)
    _usage(w, "deepseek", "project", _stamp(2), project_id=w.project_id)
    _usage(w, "openai", "org", _stamp(1), project_id=None)
    # GitHub: the newest ok fetching run counts, not a hook run or a failure.
    _scan(w.project_id, "manual", "ok", _stamp(4))
    _scan(w.project_id, "hook", "ok", _stamp(1))
    _scan(w.project_id, "manual", "failed", _stamp(0.5))

    body = w.client.get("/api/projects/demo/config").json()
    assert body["read_only"] is False
    assert body["managed_on_platform"] is False
    assert body["can_test_keys"] is True
    assert body["effective_keys"] == {
        "anthropic": "org",
        "openai": "none",  # its own endpoint: the org key never follows (BUG-11)
        "deepseek": "project",
        "openrouter": "none",
    }
    assert body["inherited"] == {
        "anthropic": {"set": True},
        "openai": {"set": False},
        "deepseek": {"set": False},
        "openrouter": {"set": False},
    }
    assert body["github"] == {"remote": "acme/widget", "token": "project"}
    # The project's own tails, never an org key's.
    assert body["secrets"]["llm"]["deepseek"] == {"set": True, "hint": "…4d4d"}
    assert body["secrets"]["llm"]["anthropic"] == {"set": False, "hint": None}
    assert body["secrets"]["github_token"] == {"set": True, "hint": "…3456"}
    text = str(body)
    assert ORG_ANTHROPIC[-4:] not in text and ORG_OPENAI[-4:] not in text
    assert body["key_last_used"] == {
        "llm": {
            "anthropic": _stamp(1),
            "openai": None,
            "deepseek": _stamp(2),
            "openrouter": None,
        },
        "github_token": _stamp(4),
    }

    # The defaults payload: the local user is the owner.
    defaults = w.client.get("/api/portal/defaults").json()
    assert (defaults["read_only"], defaults["can_test_keys"]) == (False, True)
    assert defaults["secrets"]["llm"]["anthropic"]["hint"] == "…9f3c"
    assert defaults["key_last_used"]["llm"]["anthropic"] == _stamp(1)
    assert defaults["key_last_used"]["llm"]["openai"] == _stamp(1)
    assert defaults["key_last_used"]["llm"]["deepseek"] is None
    # The org token is used by no project (demo has its own).
    assert defaults["key_last_used"]["github_token"] is None


def test_the_put_answer_carries_the_new_key_scopes(local: SimpleNamespace) -> None:
    w = local
    body = w.client.put(
        "/api/projects/demo/config", json={"secrets": {"llm": {"deepseek": None}}}
    ).json()
    assert body["effective_keys"]["deepseek"] == "none"
    assert body["secrets"]["llm"]["deepseek"] == {"set": False, "hint": None}
    assert body["can_test_keys"] is True


def test_the_project_key_test_uses_the_key_in_effect(
    local: SimpleNamespace, key_test_network: Any
) -> None:
    w = local
    before = _ledger_rows()
    org = w.client.post("/api/projects/demo/keys/anthropic/test")
    assert org.status_code == 200, org.text
    body = org.json()
    assert (body["ok"], body["result"], body["scope_tested"]) == (True, "ok", "org")
    datetime.fromisoformat(body["checked_at"])
    own = w.client.post("/api/projects/demo/keys/deepseek/test").json()
    assert own["scope_tested"] == "project"
    sent = [
        (r.url.host, r.headers.get("x-api-key") or r.headers["authorization"])
        for r in key_test_network.requests
    ]
    assert sent == [
        ("api.anthropic.com", ORG_ANTHROPIC),
        ("api.deepseek.com", f"Bearer {PROJECT_DEEPSEEK}"),
    ]
    # A provider's refusal is a result word, not an error.
    key_test_network.respond = lambda r: httpx.Response(401, json={"error": "bad key"})
    rejected = w.client.post("/api/projects/demo/keys/deepseek/test")
    assert rejected.status_code == 200
    assert rejected.json()["result"] == "rejected"
    assert "bad key" not in rejected.text
    # Off the org key (own endpoint) and never set: nothing to test.
    for provider in ("openai", "openrouter"):
        missing = w.client.post(f"/api/projects/demo/keys/{provider}/test")
        assert missing.status_code == 409, missing.text
        assert missing.json()["code"] == "key_missing"
    assert w.client.post("/api/projects/demo/keys/ollama/test").json()["code"] == (
        "not_testable"
    )
    unknown = w.client.post("/api/projects/demo/keys/nosuch/test")
    assert (unknown.status_code, unknown.json()["code"]) == (422, "bad_provider")
    assert len(key_test_network.requests) == 3
    assert _ledger_rows() == before  # not ledgered


def test_an_environment_key_is_tested_as_such(
    local: SimpleNamespace,
    key_test_network: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-from-env")
    local.state.contexts.invalidate()
    body = local.client.post("/api/projects/demo/keys/openrouter/test").json()
    assert body["scope_tested"] == "environment"
    assert key_test_network.requests[-1].headers["authorization"] == (
        "Bearer sk-or-v1-from-env"
    )


def test_the_defaults_key_test(local: SimpleNamespace, key_test_network: Any) -> None:
    w = local
    ok = w.client.post("/api/portal/defaults/keys/openai/test")
    assert ok.status_code == 200, ok.text
    assert ok.json()["scope_tested"] == "org"
    assert (
        key_test_network.requests[-1].headers["authorization"] == f"Bearer {ORG_OPENAI}"
    )
    assert str(key_test_network.requests[-1].url) == "https://api.openai.com/v1/models"
    missing = w.client.post("/api/portal/defaults/keys/deepseek/test")
    assert (missing.status_code, missing.json()["code"]) == (409, "key_missing")
    assert w.client.post("/api/portal/defaults/keys/ollama/test").status_code == 422


@pytest.mark.parametrize(
    ("raised", "result"),
    [
        (None, "ok"),
        (RepoAccessError("bad_token", "GitHub rejected the token"), "rejected"),
        (RepoAccessError("no_access", "no"), "no_repo_access"),
        (RepoAccessError("not_found", "no"), "no_repo_access"),
        (GitHubError("timed out"), "unreachable"),
    ],
)
def test_the_github_token_test_maps_the_repo_probe(
    local: SimpleNamespace,
    key_test_network: Any,
    raised: Exception | None,
    result: str,
) -> None:
    def repo(owner: str, name: str, token: str) -> None:
        if raised is not None:
            raise raised

    key_test_network.repo = repo
    response = local.client.post("/api/projects/demo/github-token/test")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["result"], body["scope_tested"]) == (result, "project")
    assert body["ok"] is (result == "ok")
    assert key_test_network.repo_calls == [("acme", "widget", GITHUB_TOKEN)]


def test_the_github_token_test_needs_a_github_remote_and_a_token(
    local: SimpleNamespace, env: SimpleNamespace, key_test_network: Any
) -> None:
    w = local
    # The project's own token removed: the org default takes over.
    w.client.put("/api/projects/demo/config", json={"secrets": {"github_token": None}})
    inherited = w.client.post("/api/projects/demo/github-token/test").json()
    assert inherited["scope_tested"] == "org"
    w.client.put("/api/portal/defaults", json={"secrets": {"github_token": None}})
    missing = w.client.post("/api/projects/demo/github-token/test")
    assert (missing.status_code, missing.json()["code"]) == (409, "key_missing")
    other = make_repo(env.shared, "elsewhere", remote="https://gitlab.com/acme/x.git")
    assert (
        w.client.post(
            "/api/projects", json={"source": "local", "path": str(other)}
        ).status_code
        == 201
    )
    refused = w.client.post("/api/projects/elsewhere/github-token/test")
    assert (refused.status_code, refused.json()["code"]) == (422, "not_github")
    assert w.client.get("/api/projects/elsewhere/config").json()["github"] == {
        "remote": None,
        "token": "none",
    }
    assert len(key_test_network.repo_calls) == 1


def test_key_tests_are_throttled_per_user(
    local: SimpleNamespace, key_test_network: Any
) -> None:
    w = local
    for _ in range(10):
        assert (
            w.client.post("/api/projects/demo/keys/anthropic/test").status_code == 200
        )
    refused = w.client.post("/api/portal/defaults/keys/openai/test")
    assert refused.status_code == 429, refused.text
    assert refused.json()["code"] == "throttled"
    assert int(refused.headers["Retry-After"]) > 0
    assert len(key_test_network.requests) == 10
    # A refusal before the probe (no key) is not counted and not throttled.
    assert w.client.post("/api/projects/demo/keys/openrouter/test").status_code == 409


# ---------------------------------------------------------------------------
# Production: roles (section 6.2 #10, Q9)
# ---------------------------------------------------------------------------


@pytest.fixture
def acme(team: SimpleNamespace, production_env: SimpleNamespace) -> SimpleNamespace:
    """``acme`` with ``api``: Ben owner, Cy org admin, Dee member, Eve a viewer."""
    t = team
    root = make_repo(production_env.tmp, "api")
    t.project_id = _insert_project(t.org_id, "api", "Acme API", root, t.ids["ben"])
    with_roles(t, cy="admin", dee="member", eve="member")
    with portal_db.get_session() as session:
        session.add(
            ProjectGrant(
                org_id=t.org_id,
                project_id=t.project_id,
                user_id=t.ids["eve"],
                role="viewer",
                granted_by=t.ids["ben"],
            )
        )
        put_secret(
            session,
            kind=LLM_API_KEY,
            value=ORG_ANTHROPIC,
            provider="anthropic",
            org_id=t.org_id,
        )
        put_secret(
            session,
            kind=LLM_API_KEY,
            value=PROJECT_OPENAI,
            provider="openai",
            project_id=t.project_id,
            org_id=t.org_id,
        )
    t.client.app.state.portal.contexts.invalidate()
    return t


def _get(t: SimpleNamespace, path: str) -> dict:
    response = t.client.get(at("acme") + path)
    assert response.status_code == 200, response.text
    return response.json()


def test_tails_and_last_used_are_for_configurers_only(acme: SimpleNamespace) -> None:
    t = acme
    for name, configurer in (
        ("ben", True),
        ("cy", True),
        ("dee", False),
        ("eve", False),
    ):
        be(t, name)
        body = _get(t, "/api/projects/api/config")
        assert body["read_only"] is not configurer, name
        assert body["can_test_keys"] is configurer, name
        assert ("key_last_used" in body) is configurer, name
        openai = body["secrets"]["llm"]["openai"]
        assert openai == {"set": True, "hint": "…a1b2" if configurer else None}, name
        assert body["effective_keys"]["anthropic"] == "org", name
        assert body["inherited"]["anthropic"] == {"set": True}, name
        # The org key's tail never appears on project settings.
        assert ORG_ANTHROPIC[-4:] not in str(body), name
        assert body["github"] == {"remote": None, "token": "none"}, name
    # The defaults: only the owner holds org.configure.
    for name, owner in (("ben", True), ("cy", False), ("dee", False)):
        be(t, name)
        defaults = _get(t, "/api/portal/defaults")
        assert defaults["read_only"] is not owner, name
        assert defaults["can_test_keys"] is owner, name
        assert ("key_last_used" in defaults) is owner, name
        assert defaults["secrets"]["llm"]["anthropic"] == {
            "set": True,
            "hint": "…9f3c" if owner else None,
        }, name


def test_the_key_test_routes_in_production(
    acme: SimpleNamespace,
    key_test_network: Any,
    audit_log: pytest.LogCaptureFixture,
) -> None:
    t = acme
    before = _ledger_rows()
    # Members and viewers are refused both; the org admin the org key's.
    for name in ("dee", "eve"):
        be(t, name)
        for path in (
            "/api/projects/api/keys/openai/test",
            "/api/portal/defaults/keys/anthropic/test",
        ):
            response = t.client.post(at("acme") + path)
            assert response.status_code == 403, (name, path, response.text)
            assert response.json()["code"] == "forbidden"
    be(t, "cy")
    refused = t.client.post(at("acme") + "/api/portal/defaults/keys/anthropic/test")
    assert refused.status_code == 403
    assert key_test_network.requests == []
    audit_log.clear()
    project = t.client.post(at("acme") + "/api/projects/api/keys/openai/test")
    assert project.status_code == 200, project.text
    assert project.json()["scope_tested"] == "project"
    be(t, "ben")
    org = t.client.post(at("acme") + "/api/portal/defaults/keys/anthropic/test")
    assert org.status_code == 200, org.text
    tested = [e for e in events(audit_log) if e["event"] == "key_tested"]
    assert [(e["scope"], e["provider"], e["result"]) for e in tested] == [
        ("project", "openai", "ok"),
        ("org", "anthropic", "ok"),
    ]
    assert tested[0]["project"] == "api" and tested[0]["org"] == "acme"
    for key in (ORG_ANTHROPIC, PROJECT_OPENAI):
        assert key not in str(tested) and key[-4:] not in str(tested)
    # The GitHub token test is local mode's: production answers 404.
    github = t.client.post(at("acme") + "/api/projects/api/github-token/test")
    assert (github.status_code, github.json()) == (404, {"error": "not found"})
    assert key_test_network.repo_calls == []
    assert _ledger_rows() == before


# ---------------------------------------------------------------------------
# A project's connections with include=revoked (section 6.2 #16)
# ---------------------------------------------------------------------------


def test_revoked_connections_are_listed_for_thirty_days(acme: SimpleNamespace) -> None:
    t = acme

    def token(name: str, client: str, revoked_days: float | None) -> None:
        with portal_db.get_session() as session:
            session.add(
                ConnectionToken(
                    user_id=t.ids[name],
                    org_id=t.org_id,
                    project_id=t.project_id,
                    token_hash=f"hash-{client}",
                    client_name=client,
                    created_at=_stamp(60),
                    revoked_at=None if revoked_days is None else _stamp(revoked_days),
                    revoked_reason=None if revoked_days is None else "admin_revoked",
                )
            )

    token("ben", "live-laptop", None)
    token("dee", "recent", 5)
    token("eve", "ancient", 40)
    be(t, "ben")
    live = _get(t, "/api/projects/api/connections")
    assert [c["client_name"] for c in live] == ["live-laptop"]
    assert "revoked_at" not in live[0]
    rows = _get(t, "/api/projects/api/connections?include=revoked")
    by_name = {c["client_name"]: c for c in rows}
    assert set(by_name) == {"live-laptop", "recent"}
    assert by_name["recent"]["revoked_reason"] == "admin_revoked"
    assert by_name["recent"]["revoked_at"] == _stamp(5)
    assert by_name["live-laptop"]["revoked_at"] is None
    bad = t.client.get(at("acme") + "/api/projects/api/connections?include=all")
    assert (bad.status_code, bad.json()["code"]) == (422, "bad_filter")


def test_production_removal_never_names_the_server_path(
    acme: SimpleNamespace,
) -> None:
    """``confirm_name`` names no clone path in production (MODE-1)."""
    t = acme
    be(t, "ben")
    refused = t.client.request("DELETE", at("acme") + "/api/projects/api", json={})
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["code"] == "confirm_name"
    assert "folder" not in body
    with portal_db.get_session() as session:
        root = session.get(Project, t.project_id).root
    assert root not in refused.text
