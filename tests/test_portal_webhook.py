"""The GitHub App's webhook, ``POST /github/webhook`` (M2d-2 plan sections 4.7, 5.1).

The world of ``test_portal_github_import`` (a production portal whose GitHub
App talks to :class:`~github_fake.FakeGitHub`, git over dumb HTTP on a real
socket) with ``acme/api`` imported and scanned once, and the runner's fake
scan child. Deliveries are built and signed by the fake
(:meth:`~github_fake.FakeGitHub.webhook_delivery`) and posted to the base
host through the portal's own ``TestClient``; their background work has
finished when the post returns.

Covered: signature refusals (missing, wrong, truncated, prefix-only, SHA-1
only) before anything is parsed; the body cap by header and by stream; the
org host, local mode and degraded refusals; replayed delivery ids; pushes
to the default branch, another branch, a tag and a deletion; the fan-out
across two orgs and its filters, and the isolation of one repository imported
into two orgs; the installation, installation_repositories
and repository events.
"""

# ruff: noqa: F811 -- pytest fixtures imported from other test modules

from __future__ import annotations

import hmac
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from github_fake import FakeControl
from test_portal_app import (  # noqa: F401
    GITHUB_WEBHOOK_SECRET,
    at,
    client,
    env,
    github_sign_in,
)
from test_portal_github_import import (  # noqa: F401 -- fixtures
    API_REPO,
    INSTALLATION,
    World,
    app_env,
    connected,
    github_app_key,
    github_git_server,
    import_repo,
    production_env,
    project_row,
    world,
)
from test_portal_identity_routes import (  # noqa: F401 -- `audit_log` is a fixture
    audit_log,
    create_org,
    events,
)
from test_portal_runner import TERMINAL, scanner, wait_for  # noqa: F401
from whygraph.portal import db as portal_db
from whygraph.portal import webhook as webhook_mod
from whygraph.portal.models import Project, ScanRun
from whygraph.portal.runner import REASON_NO_ACCESS, _mark_access_lost

HOOK = at() + webhook_mod.WEBHOOK_PATH
NEW_INSTALLATION = 77


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def deliver(
    w: World,
    event: str,
    payload: Any,
    *,
    url: str = HOOK,
    delivery: str | None = None,
    secret: str | None = None,
    headers: dict[str, str | None] | None = None,
    body: bytes | None = None,
) -> httpx.Response:
    """Sign and post a delivery; ``headers`` overrides (``None`` drops one)."""
    signed, raw = w.fake.webhook_delivery(
        event, payload, secret=secret, delivery=delivery
    )
    for name, value in (headers or {}).items():
        signed.pop(name, None)
        if value is not None:
            signed[name] = value
    return w.client.post(url, content=raw if body is None else body, headers=signed)


def push(
    ref: str = "refs/heads/main",
    *,
    deleted: bool = False,
    repo_id: int = API_REPO,
    default_branch: str = "main",
) -> dict:
    return {
        "ref": ref,
        "deleted": deleted,
        "created": False,
        "repository": {
            "id": repo_id,
            "full_name": "acme/api",
            "default_branch": default_branch,
        },
        "installation": {"id": INSTALLATION},
    }


def org_runs(w: World, org: str = "acme") -> list[dict]:
    response = w.client.get(at(org) + "/api/projects/api/scans")
    assert response.status_code == 200, response.text
    return response.json()["runs"]


def wait_idle(w: World, org: str = "acme") -> list[dict]:
    return wait_for(
        lambda: (
            (rs := org_runs(w, org)) and all(r["status"] in TERMINAL for r in rs) and rs
        )
    )


def scanned(w: World, org: str = "acme") -> None:
    """Run the project's first scan to completion."""
    response = w.client.post(at(org) + "/api/projects/api/scans")
    assert response.status_code == 202, response.text
    (run,) = wait_idle(w, org)
    assert run["status"] == "ok", run


@pytest.fixture
def hooked(world: World, scanner: SimpleNamespace) -> World:
    """``acme/api`` imported into ``acme`` and scanned once."""
    connected(world)
    assert import_repo(world, API_REPO).status_code == 201
    scanned(world)
    return world


def row(org: str = "acme") -> Project:
    found = project_row(org, "api")
    assert found is not None
    return found


def all_runs() -> list[ScanRun]:
    with portal_db.get_session() as session:
        rows = session.exec(select(ScanRun).order_by(ScanRun.id)).all()
        for r in rows:
            session.expunge(r)
        return list(rows)


@pytest.fixture
def requests_seen(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> Iterator[list[tuple[int, str]]]:
    """Every ``request_sync`` the webhook makes: ``(project_id, trigger)``."""
    runner = world.state.runner
    real = runner.request_sync
    seen: list[tuple[int, str]] = []

    async def spy(project_id: int, **kwargs: Any) -> int:
        seen.append((project_id, kwargs.get("trigger", "sync")))
        return await real(project_id, **kwargs)

    monkeypatch.setattr(runner, "request_sync", spy)
    yield seen


# ---------------------------------------------------------------------------
# Verification (plan section 4.7)
# ---------------------------------------------------------------------------


def test_a_good_signature_is_accepted(world: World) -> None:
    response = deliver(world, "ping", {"zen": "Keep it logically awesome."})
    assert response.status_code == 202, response.text
    assert response.json() == {"status": "ignored"}


NOT_JSON = b'{"ref": "refs/heads/main", not json'
"""A body that would answer ``400`` if it were parsed before the signature check."""


def _sign(
    body: bytes, secret: str = GITHUB_WEBHOOK_SECRET, algo: str = "sha256"
) -> str:
    return f"{algo}=" + hmac.new(secret.encode(), body, algo).hexdigest()


@pytest.mark.parametrize(
    "case",
    ["missing", "wrong_secret", "truncated", "prefix_only", "half", "sha1_only"],
)
def test_a_bad_signature_is_401_before_anything_is_parsed(
    world: World,
    audit_log: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    w = world
    parsed: list[Any] = []
    monkeypatch.setattr(
        webhook_mod, "handle_event", lambda *a: parsed.append(a)
    )  # never reached
    good = _sign(NOT_JSON)
    signature = {
        "missing": None,
        "wrong_secret": _sign(NOT_JSON, "another-secret-of-at-least-32-characters"),
        "truncated": good[:-1],
        "prefix_only": "sha256=",
        "half": good[: len("sha256=") + 32],
        "sha1_only": None,
    }[case]
    headers = {"X-GitHub-Event": "push", "X-GitHub-Delivery": f"bad-{case}"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    if case == "sha1_only":  # the legacy SHA-1 header alone is never enough
        headers["X-Hub-Signature"] = _sign(NOT_JSON, algo="sha1")
    response = w.client.post(HOOK, content=NOT_JSON, headers=headers)
    assert response.status_code == 401, response.text
    assert parsed == [] and all_runs() == []
    (rejected,) = [e for e in events(audit_log) if e["event"] == "webhook_rejected"]
    assert (rejected["github_event"], rejected["delivery"]) == ("push", f"bad-{case}")
    assert rejected["reason"] in ("missing_signature", "bad_signature")
    # The same body, signed right, gets past the check (and only then is parsed).
    headers["X-Hub-Signature-256"] = good
    response = w.client.post(HOOK, content=NOT_JSON, headers=headers)
    assert response.status_code == 400, response.text


def test_the_body_cap_by_header_and_by_stream(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    monkeypatch.setattr(webhook_mod, "MAX_BODY_BYTES", 2048)
    big = {"padding": "x" * 4096, **push()}
    by_header = deliver(w, "push", big)
    assert by_header.status_code == 413, by_header.text

    headers, raw = w.fake.webhook_delivery("push", big)

    def chunks() -> Iterator[bytes]:  # no Content-Length: a chunked body
        for start in range(0, len(raw), 512):
            yield raw[start : start + 512]

    by_stream = w.client.post(HOOK, content=chunks(), headers=headers)
    assert "content-length" not in {k.lower() for k in by_stream.request.headers}
    assert by_stream.status_code == 413, by_stream.text

    small = deliver(w, "ping", {"zen": "x"})  # under the cap: accepted
    assert small.status_code == 202, small.text


def test_an_org_host_is_404(hooked: World) -> None:
    w = hooked
    w.server.commit("acme/api")
    response = deliver(w, "push", push(), url=at("acme") + webhook_mod.WEBHOOK_PATH)
    assert response.status_code == 404, response.text
    assert len(org_runs(w)) == 1


def test_a_degraded_portal_is_503(world: World) -> None:
    w = world
    w.state.degraded = "pretend the database is gone"
    try:
        response = deliver(w, "ping", {})
    finally:
        w.state.degraded = None
    assert response.status_code == 503, response.text


def test_local_mode_is_404(client: TestClient) -> None:
    response = client.post(
        webhook_mod.WEBHOOK_PATH,
        content=b"{}",
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": "sha256=" + "0" * 64},
    )
    assert response.status_code == 404, response.text
    assert response.json() == {"error": "not found"}


def test_without_the_github_app_it_is_503(world: World) -> None:
    w = world
    app, w.state.github_app = w.state.github_app, None
    try:
        response = deliver(w, "ping", {})
    finally:
        w.state.github_app = app
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "github_app_not_configured"


def test_a_replayed_delivery_is_queued_once(
    hooked: World, requests_seen: list[tuple[int, str]]
) -> None:
    w = hooked
    w.server.commit("acme/api")
    first = deliver(w, "push", push(), delivery="delivery-1")
    assert (first.status_code, first.json()) == (202, {"status": "queued"})
    runs = wait_idle(w)
    assert len(runs) == 2 and runs[0]["trigger"] == "push"

    again = deliver(w, "push", push(), delivery="delivery-1")
    assert (again.status_code, again.json()) == (202, {"status": "duplicate"})
    assert requests_seen == [(row().id, "push")]
    assert len(org_runs(w)) == 2

    other = deliver(w, "push", push(), delivery="delivery-2")
    assert other.json() == {"status": "queued"}
    assert len(requests_seen) == 2


def test_the_delivery_ids_are_bounded() -> None:
    ids = webhook_mod.DeliveryIds(max_entries=3)
    assert [ids.add(d) for d in ("a", "b", "a", "c", "d")] == [
        True,
        True,
        False,
        True,
        True,
    ]
    assert len(ids) == 3
    assert ids.add("a") is True  # the oldest was dropped


# ---------------------------------------------------------------------------
# Pushes
# ---------------------------------------------------------------------------


def test_a_push_to_the_default_branch_syncs_and_scans(hooked: World) -> None:
    w = hooked
    pushed = w.server.commit("acme/api", message="Pushed")
    response = deliver(w, "push", push())
    assert response.status_code == 202, response.text
    run = wait_idle(w)[0]
    assert (run["kind"], run["trigger"], run["status"]) == ("sync", "push", "ok")
    assert run["summary"]["moved"] is True
    assert row().last_scanned_head == pushed


@pytest.mark.parametrize(
    "payload",
    [
        push("refs/heads/feature"),
        push("refs/tags/v1.0.0"),
        push(deleted=True),
        push("refs/heads/main", default_branch="trunk"),
        push(repo_id=API_REPO + 100),
    ],
    ids=["other-branch", "tag", "deletion", "not-the-default", "unknown-repo"],
)
def test_other_pushes_are_ignored(
    hooked: World, requests_seen: list[tuple[int, str]], payload: dict
) -> None:
    w = hooked
    response = deliver(w, "push", payload)
    assert response.status_code == 202, response.text
    assert requests_seen == []
    assert len(org_runs(w)) == 1


def test_a_push_fans_out_across_orgs_and_skips_what_it_must(
    hooked: World, requests_seen: list[tuple[int, str]]
) -> None:
    w = hooked
    assert create_org(w.client, "bravo", "Bravo").status_code == 201
    assert import_repo(w, API_REPO, org="bravo").status_code == 201

    # bravo's copy was never scanned: left to its first, explicit scan.
    w.server.commit("acme/api")
    deliver(w, "push", push())
    assert requests_seen == [(row().id, "push")]
    wait_idle(w)

    scanned(w, "bravo")
    requests_seen.clear()
    pushed = w.server.commit("acme/api")
    deliver(w, "push", push())
    assert sorted(requests_seen) == sorted(
        [(row().id, "push"), (row("bravo").id, "push")]
    )
    for org in ("acme", "bravo"):
        assert wait_idle(w, org)[0]["trigger"] == "push"
        assert row(org).last_scanned_head == pushed

    # An access-lost project and an uninitialized one are skipped.
    _mark_access_lost(row("bravo").id, REASON_NO_ACCESS)
    with portal_db.get_session() as session:
        acme = session.get(Project, row().id)
        acme.initialized_at = None
        session.add(acme)
    requests_seen.clear()
    deliver(w, "push", push())
    assert requests_seen == []


def test_the_same_repo_in_two_orgs_stays_apart_after_a_push(
    hooked: World, requests_seen: list[tuple[int, str]]
) -> None:
    """One repository imported into two orgs: one push, two runs, no crossing.

    The push fans out to both copies, but a member of ``bravo`` alone sees
    ``bravo``'s copy and runs only: ``acme``'s host is the binding's plain
    ``404``, ``acme``'s run ids are not found under ``bravo``'s ``api``, and
    ``acme``'s access state does not show in ``bravo``.
    """
    w = hooked
    assert create_org(w.client, "bravo", "Bravo").status_code == 201
    assert import_repo(w, API_REPO, org="bravo").status_code == 201
    scanned(w, "bravo")
    assert github_sign_in(w.client, "cy").status_code == 200
    assert github_sign_in(w.client, "ben").status_code == 200
    added = w.client.post(
        at("bravo") + "/api/org/members", json={"github_login": "cy", "role": "member"}
    )
    assert added.status_code == 201, added.text

    pushed = w.server.commit("acme/api")
    assert deliver(w, "push", push()).json() == {"status": "queued"}
    assert sorted(requests_seen) == sorted(
        [(row().id, "push"), (row("bravo").id, "push")]
    )
    ids = {}
    for org in ("acme", "bravo"):
        runs = wait_idle(w, org)
        assert len(runs) == 2 and runs[0]["trigger"] == "push", (org, runs)
        assert row(org).last_scanned_head == pushed
        ids[org] = [r["id"] for r in runs]
    assert not set(ids["acme"]) & set(ids["bravo"])
    _mark_access_lost(row().id, REASON_NO_ACCESS)

    assert github_sign_in(w.client, "cy").status_code == 200
    acme_run = ids["acme"][0]
    for path in (
        "/api/projects",
        "/api/projects/api",
        "/api/projects/api/scans",
        f"/api/projects/api/scans/{acme_run}/log",
    ):
        refused = w.client.get(at("acme") + path)
        assert (refused.status_code, refused.json()) == (404, {"error": "not found"})
    listed = w.client.get(at("bravo") + "/api/projects").json()["projects"]
    assert [
        (p["slug"], p["github_full_name"], p["access_lost"], p["root"]) for p in listed
    ] == [("api", "acme/api", False, None)]
    assert [r["id"] for r in org_runs(w, "bravo")] == ids["bravo"]
    for method, suffix in (("GET", "/log"), ("GET", "/events"), ("POST", "/cancel")):
        other = w.client.request(
            method, at("bravo") + f"/api/projects/api/scans/{acme_run}{suffix}"
        )
        assert other.status_code == 404, (suffix, other.text)
        assert other.json() == {"error": f"run {acme_run} not found"}, suffix


# ---------------------------------------------------------------------------
# installation and installation_repositories
# ---------------------------------------------------------------------------


def installation(action: str, ident: int = INSTALLATION, **extra: Any) -> dict:
    return {"action": action, "installation": {"id": ident}, **extra}


def test_uninstalling_marks_access_lost_and_a_reinstall_clears_it(
    hooked: World, audit_log: pytest.LogCaptureFixture
) -> None:
    w = hooked
    deliver(w, "installation", installation("deleted"))
    assert (row().access_lost_at is not None, row().access_lost_reason) == (
        True,
        "no_access",
    )
    refused = w.client.post(at("acme") + "/api/projects/api/scans")
    assert refused.json()["code"] == "github_access_lost"

    # A re-install gets a new id and lists the repositories it covers.
    deliver(
        w,
        "installation",
        installation(
            "created", NEW_INSTALLATION, repositories=[{"id": API_REPO, "name": "api"}]
        ),
    )
    assert row().access_lost_at is None
    assert row().github_installation_id == NEW_INSTALLATION
    assert [e["event"] for e in events(audit_log)].count("project_access_restored") == 1
    assert row("acme").slug == "api"  # never removed


def test_suspend_and_unsuspend(hooked: World) -> None:
    w = hooked
    deliver(w, "installation", installation("suspend"))
    assert row().access_lost_reason == "no_access"
    deliver(w, "installation", installation("unsuspend"))
    assert row().access_lost_at is None
    # Another installation's events touch nothing.
    deliver(w, "installation", installation("deleted", ident=INSTALLATION + 1))
    assert row().access_lost_at is None


def test_repositories_removed_and_added(hooked: World) -> None:
    w = hooked
    deliver(
        w,
        "installation_repositories",
        installation("removed", repositories_removed=[{"id": API_REPO + 99}]),
    )
    assert row().access_lost_at is None  # not this repository
    deliver(
        w,
        "installation_repositories",
        installation("removed", repositories_removed=[{"id": API_REPO}]),
    )
    assert row().access_lost_reason == "no_access"
    deliver(
        w,
        "installation_repositories",
        installation("added", NEW_INSTALLATION, repositories_added=[{"id": API_REPO}]),
    )
    assert row().access_lost_at is None
    assert row().github_installation_id == NEW_INSTALLATION


def test_the_fakes_control_routes_send_what_the_portal_acts_on(hooked: World) -> None:
    """The out-of-process fake's ``/_fake/*`` routes (e2e, smoke, the dev loop)."""
    w = hooked
    control = FakeControl(w.fake, HOOK, client=w.client)
    assert control.handle("GET", "/_fake/push", b"")[0] == 405
    assert control.handle("POST", "/_fake/nope", b"{}")[0] == 404
    assert control.handle("POST", "/_fake/push", b'{"repo": "acme/none"}')[0] == 404

    status, body = control.handle("POST", "/_fake/push", b'{"repo": "acme/api"}')
    assert (status, body["webhook_status"]) == (200, 202), body
    run = wait_idle(w)[0]
    assert (run["kind"], run["trigger"], run["status"]) == ("sync", "push", "ok")
    assert row().last_scanned_head == body["sha"]

    status, body = control.handle("POST", "/_fake/remove-repo", b'{"repo": "acme/api"}')
    assert (status, body["webhook_status"]) == (200, 202), body
    assert row().access_lost_reason == "no_access"
    assert w.fake.repos[API_REPO].installation is None

    payload = b'{"installation": %d}' % INSTALLATION
    status, body = control.handle("POST", "/_fake/uninstall", payload)
    assert (status, body["webhook_status"]) == (200, 202), body
    assert INSTALLATION not in w.fake.installations
    assert control.handle("POST", "/_fake/uninstall", payload)[0] == 404


# ---------------------------------------------------------------------------
# repository
# ---------------------------------------------------------------------------


def repository(action: str, full_name: str = "acme/api", **extra: Any) -> dict:
    return {
        "action": action,
        "repository": {
            "id": API_REPO,
            "full_name": full_name,
            "default_branch": "main",
        },
        **extra,
    }


def test_a_rename_follows_the_remote_url(hooked: World) -> None:
    w = hooked
    deliver(w, "repository", repository("renamed", "acme/renamed"))
    assert row().remote_url == f"{w.server.url}/acme/renamed"


def test_a_transfer_follows_the_installation_or_loses_access(hooked: World) -> None:
    w = hooked
    w.fake.add_installation(NEW_INSTALLATION, "neworg", account_type="Organization")
    repo = w.fake.repos[API_REPO]
    repo.full_name, repo.installation = "neworg/api", NEW_INSTALLATION
    deliver(w, "repository", repository("transferred", "neworg/api"))
    assert row().github_installation_id == NEW_INSTALLATION
    assert row().remote_url == f"{w.server.url}/neworg/api"
    assert row().access_lost_at is None
    shown = w.client.get(at("acme") + "/api/projects/api").json()
    assert (shown["github_full_name"], shown["installation_account"]) == (
        "neworg/api",
        "neworg",
    )
    # The sync now mints through the new installation.
    response = w.client.post(at("acme") + "/api/projects/api/scans")
    assert response.status_code == 202, response.text
    assert wait_idle(w)[0]["status"] == "ok"

    # Transferred where the app is not installed: access lost.
    repo.full_name, repo.installation = "elsewhere/api", None
    deliver(w, "repository", repository("transferred", "elsewhere/api"))
    assert row().access_lost_reason == "no_access"
    assert row().github_installation_id == NEW_INSTALLATION


def test_a_new_default_branch_is_recorded(hooked: World) -> None:
    w = hooked
    payload = repository("edited", changes={"default_branch": {"from": "main"}})
    payload["repository"]["default_branch"] = "trunk"
    deliver(w, "repository", payload)
    assert row().default_branch == "trunk"
    # An edit of something else leaves it.
    payload = repository("edited", changes={"description": {"from": ""}})
    payload["repository"]["default_branch"] = "other"
    deliver(w, "repository", payload)
    assert row().default_branch == "trunk"


def test_a_deleted_repository_is_access_lost(
    hooked: World, audit_log: pytest.LogCaptureFixture
) -> None:
    w = hooked
    deliver(w, "repository", repository("deleted"))
    assert row().access_lost_reason == "repo_deleted"
    refused = w.client.post(at("acme") + "/api/projects/api/scans")
    assert refused.status_code == 409
    assert refused.json()["code"] == "github_access_lost"
    assert "deleted on GitHub" in refused.json()["error"]
    (lost,) = [e for e in events(audit_log) if e["event"] == "project_access_lost"]
    assert lost["reason"] == "repo_deleted"
    # The history stays.
    assert row().last_scanned_head is not None

    # The reconcile's mint is refused, and the reason stays "deleted".
    gone = w.fake.repos.pop(API_REPO)
    w.client.portal.call(w.state.runner.reconcile)
    assert row().access_lost_reason == "repo_deleted"
    # Restored on GitHub: the next successful mint clears it.
    w.fake.repos[API_REPO] = gone
    w.client.portal.call(w.state.runner.reconcile)
    assert row().access_lost_at is None


def test_other_events_are_ignored(hooked: World) -> None:
    w = hooked
    for event in ("github_app_authorization", "ping", "star"):
        response = deliver(w, event, {"action": "revoked"})
        assert (response.status_code, response.json()) == (202, {"status": "ignored"})
    response = deliver(w, "repository", repository("archived"))
    assert response.status_code == 202
    assert row().access_lost_at is None
