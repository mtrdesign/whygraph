"""Managing a linked project locally: refusals, removal, status (M2e step 9).

A project linked to a WhyGraph platform (plan section 4.11) is configured,
browsed and scanned **there**; this portal holds the checkout, its CodeGraph
index, the agent wiring and the git hooks. So:

* every management route the platform owns answers the one ``403
  managed_on_platform`` with the three deep links - including the Explorer and
  Chat mounts, which refuse in their mount dependency, before the initialized
  gate that would ask for a database a linked project deliberately lacks;
* ``[scan].hooks`` is the single key a ``PUT .../config`` may still set;
* **Remove from this machine** gives up this machine's connection token on the
  platform first (best effort, reported as ``token_revoked``) and leaves the
  platform project alone;
* every project summary carries a ``link`` block whose ``status`` is the
  *recorded* one - refreshed by the lifespan task and by every call an agent
  makes, never by a listing that would then wait on a platform.

The platform is ``tests/platform_fake.py`` behind an ``httpx.MockTransport``,
and the link flow is ``tests/test_portal_link.py``'s, so nothing here touches
the network.
"""

# ruff: noqa: F811 -- pytest fixtures imported from the sibling test modules

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from platform_fake import ORG, SLUG, TOKEN, FakePlatform, status_body
from test_portal_app import (  # noqa: F401 -- `env` is a fixture
    env,
    init_project,
    make_repo,
)
from test_portal_link import (  # noqa: F401 -- `fake` / `portal` are fixtures
    _transport,
    allow,
    callback,
    connect,
    fake,
    link,
    link_row,
    linked,
    mirror,
    pending_of,
    portal,
    project_id,
)
from test_portal_runner import scanner  # noqa: F401 -- a fixture
from whygraph.api_v1 import StatusOut
from whygraph.core.remote import RemoteError
from whygraph.portal import db as portal_db
from whygraph.portal.linked import (
    REFRESH_AFTER_SEC,
    due_for_refresh,
    refresh_links,
    save_status,
)
from whygraph.portal.models import PlatformLink, Project
from whygraph.portal.secrets import decrypt

API_ORIGIN = f"https://{ORG}.wg.example.com"
"""The fake platform's org host, where a linked project is managed."""

HOME = f"{API_ORIGIN}/p/{SLUG}"
"""Its project home - what ``link.manage_url`` points at."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def linked_project(
    client: TestClient, fake: FakePlatform, env: SimpleNamespace, **over: Any
) -> Path:
    """Link a fresh checkout to the fake's ``demo`` project and Initialize it."""
    root = mirror(env, "demo-checkout")
    response = link(client, linked(client, fake), root)
    assert response.status_code == 201, response.text
    assert init_project(client, SLUG, **over)["initialized"] is True
    return root


def transport_of(client: TestClient):
    """The platform transport the portal under test is using."""
    return client.app.state.portal.platform_transport


def age_status(slug: str, *, when: str) -> None:
    """Backdate (or set) a link's ``status_at``, as a stale status would be."""
    with portal_db.get_session() as session:
        row = session.get(PlatformLink, project_id(slug))
        assert row is not None
        row.status_at = when
        session.add(row)


def summary(client: TestClient, slug: str = SLUG) -> dict:
    """The project's row in ``GET /api/projects``."""
    body = client.get("/api/projects")
    assert body.status_code == 200, body.text
    rows = {p["slug"]: p for p in body.json()["projects"]}
    return rows[slug]


# ---------------------------------------------------------------------------
# The refusal table (plan section 4.11)
# ---------------------------------------------------------------------------

MANAGED = "managed_on_platform"

REFUSED: list[tuple[str, str, dict | None]] = [
    # The name follows the platform's.
    ("PATCH", f"/api/projects/{SLUG}", {"name": "Renamed"}),
    # Config: everything but [scan].hooks, and never a secret.
    (
        "PUT",
        f"/api/projects/{SLUG}/config",
        {"config": {"analyze": {"max_workers": 4}}},
    ),
    (
        "PUT",
        f"/api/projects/{SLUG}/config",
        {"config": {"scan": {"forge": "github", "hooks": True}}},
    ),
    (
        "PUT",
        f"/api/projects/{SLUG}/config",
        {"secrets": {"llm": {"anthropic": "sk-nope"}}},
    ),
    # The LLM work happens on the platform, so there is nothing to estimate.
    ("GET", f"/api/projects/{SLUG}/scan-estimate", None),
    # A scan is allowed, but never an analyzing one.
    ("POST", f"/api/projects/{SLUG}/scans", {"analyze": True}),
    ("POST", f"/api/projects/{SLUG}/scans", {"trigger": "describe"}),
    # The data router: the Explorer opens on the platform.
    ("GET", f"/api/projects/{SLUG}/search", None),
    ("GET", f"/api/projects/{SLUG}/tree", None),
    ("GET", f"/api/projects/{SLUG}/graph/overview", None),
    ("GET", f"/api/projects/{SLUG}/node", None),
    ("GET", f"/api/projects/{SLUG}/node/evidence", None),
    ("POST", f"/api/projects/{SLUG}/node/rationale", {}),
    ("GET", f"/api/projects/{SLUG}/history", None),
    ("GET", f"/api/projects/{SLUG}/commit/abc", None),
    ("GET", f"/api/projects/{SLUG}/pr/1", None),
    ("GET", f"/api/projects/{SLUG}/issue/1", None),
    # The chat router: Chat opens on the platform too.
    ("GET", f"/api/projects/{SLUG}/chat/providers", None),
    ("GET", f"/api/projects/{SLUG}/chat/sessions", None),
    ("POST", f"/api/projects/{SLUG}/chat/sessions", {}),
    ("GET", f"/api/projects/{SLUG}/chat/sessions/1", None),
    ("DELETE", f"/api/projects/{SLUG}/chat/sessions/1", None),
    ("POST", f"/api/projects/{SLUG}/chat/sessions/1/messages", {"text": "hi"}),
    # Its keys are the platform's to test (M2f-3 section 4.13)
    ("POST", f"/api/projects/{SLUG}/keys/anthropic/test", None),
    ("POST", f"/api/projects/{SLUG}/github-token/test", None),
]
"""Every route of plan section 4.11's table that a linked project refuses."""


@pytest.mark.parametrize(("method", "path", "body"), REFUSED)
def test_linked_management_routes_refuse(
    portal: TestClient,
    fake: FakePlatform,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
    method: str,
    path: str,
    body: dict | None,
) -> None:
    """Each refused route answers ``403 managed_on_platform`` with the deep links."""
    linked_project(portal, fake, env)
    response = portal.request(method, path, json=body)
    assert response.status_code == 403, (method, path, response.text)
    answer = response.json()
    assert answer["code"] == MANAGED, (method, path)
    assert answer["manage_url"] == HOME
    assert answer["explorer_url"] == f"{HOME}/explorer"
    assert answer["chat_url"] == f"{HOME}/chat"
    # Nothing was done: the name, the layer and the queue are untouched.
    assert summary(portal)["name"] == "Demo"
    with portal_db.get_session() as session:
        assert session.exec(select(Project.name)).all() == ["Demo"]


ALLOWED: list[tuple[str, str, dict | None]] = [
    ("GET", f"/api/projects/{SLUG}", None),
    ("GET", f"/api/projects/{SLUG}/config", None),
    ("GET", f"/api/projects/{SLUG}/scans", None),
    ("PUT", f"/api/projects/{SLUG}/config", {"config": {"scan": {"hooks": True}}}),
    ("POST", f"/api/projects/{SLUG}/scans", {}),
    ("POST", f"/api/projects/{SLUG}/scans", {"trigger": "manual"}),
    ("POST", f"/api/projects/{SLUG}/scans", {"trigger": "hook"}),
]
"""The other half of the table: what a linked project still answers itself."""


@pytest.mark.parametrize(("method", "path", "body"), ALLOWED)
def test_linked_management_routes_allowed(
    portal: TestClient,
    fake: FakePlatform,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
    method: str,
    path: str,
    body: dict | None,
) -> None:
    """The checkout's own business - reads, the hooks toggle, a scan - still works."""
    linked_project(portal, fake, env)
    response = portal.request(method, path, json=body)
    assert response.status_code in (200, 202), (method, path, response.text)


def test_linked_init_cancel_and_remove_are_allowed(
    portal: TestClient,
    fake: FakePlatform,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
) -> None:
    """Initialize, a scan cancel and the removal are this machine's to run."""
    linked_project(portal, fake, env)
    again = portal.post(f"/api/projects/{SLUG}/init", json={"agents": []})
    assert again.status_code == 200, again.text
    # A cancel of an unknown run is a 404, not the platform's refusal.
    cancel = portal.post(f"/api/projects/{SLUG}/scans/9999/cancel")
    assert cancel.status_code == 404, cancel.text
    removed = portal.delete(f"/api/projects/{SLUG}")
    assert removed.status_code == 200, removed.text


def test_linked_config_get_is_read_only_and_secret_free(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    """``GET .../config`` serves the hooks layer and no secrets block at all."""
    linked_project(portal, fake, env)
    body = portal.get(f"/api/projects/{SLUG}/config")
    assert body.status_code == 200, body.text
    answer = body.json()
    assert answer["secrets"] is None
    # M2f-3 section 0.3 #42: read-only, managed on the platform, no key fields.
    assert answer["read_only"] is True
    assert answer["managed_on_platform"] is True
    assert answer["can_test_keys"] is False
    for key in ("effective_keys", "inherited", "github", "key_last_used"):
        assert key not in answer, key
    # The writable hooks key answers the same secret-free view (BUG-21).
    put = portal.put(
        f"/api/projects/{SLUG}/config", json={"config": {"scan": {"hooks": True}}}
    )
    assert put.status_code == 200, put.text
    assert put.json()["secrets"] is None
    assert put.json()["managed_on_platform"] is True
    # The control: a local project's config GET does carry one.
    local = make_repo(env.shared, "plain", remote=None)
    added = portal.post("/api/projects", json={"source": "local", "path": str(local)})
    assert added.status_code == 201, added.text
    other = portal.get("/api/projects/plain/config").json()
    assert other["secrets"]["github_token"] == {"set": False, "hint": None}


def test_linked_put_config_names_the_refused_keys(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    """The refusal says which keys the platform owns, so the UI can explain."""
    linked_project(portal, fake, env)
    refused = portal.put(
        f"/api/projects/{SLUG}/config",
        json={"config": {"analyze": {"max_workers": 4}, "scan": {"hooks": False}}},
    )
    assert refused.status_code == 403, refused.text
    # A whole table the platform owns is named as the table (`filter_layer`).
    assert refused.json()["keys"] == ["analyze"]
    sibling = portal.put(
        f"/api/projects/{SLUG}/config",
        json={"config": {"scan": {"forge": "github"}}},
    )
    assert sibling.status_code == 403, sibling.text
    assert sibling.json()["keys"] == ["scan.forge"]
    # A secret inside `config` is still the ordinary 422, value never echoed.
    secret = portal.put(
        f"/api/projects/{SLUG}/config",
        json={"config": {"llm": {"anthropic": {"api_key": "sk-leak"}}}},
    )
    assert secret.status_code == 422, secret.text
    assert "sk-leak" not in secret.text


# ---------------------------------------------------------------------------
# Remove from this machine
# ---------------------------------------------------------------------------


def test_remove_from_machine_revokes_token(
    portal: TestClient,
    fake: FakePlatform,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
) -> None:
    """The platform is told to drop this machine's token before anything local."""
    root = linked_project(portal, fake, env)
    assert fake.tokens[TOKEN]["revoked"] is None
    before = len(fake.requests)

    response = portal.delete(f"/api/projects/{SLUG}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_revoked"] is True
    assert body["token_revoke_result"] == "revoked"
    assert body["warnings"] == []
    assert body["checkout_deleted"] is False

    revoke = fake.requests[before]
    assert revoke.method == "DELETE"
    assert revoke.url.path == f"/api/v1/projects/{SLUG}/token"
    assert fake.tokens[TOKEN]["revoked"] == "removed_locally"

    # The project and its link row are gone; the checkout is not.
    with portal_db.get_session() as session:
        assert session.exec(select(Project.id)).all() == []
        assert session.exec(select(PlatformLink.project_id)).all() == []
    assert root.is_dir()
    assert not (root / ".whygraph" / "portal.json").exists()
    assert portal.get(f"/api/projects/{SLUG}").status_code == 404


def test_remove_from_machine_when_unreachable(
    portal: TestClient,
    fake: FakePlatform,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
) -> None:
    """An unreachable platform does not block the removal; the answer says so."""
    linked_project(portal, fake, env)
    fake.failure = "unreachable"

    response = portal.delete(f"/api/projects/{SLUG}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_revoked"] is False
    assert body["token_revoke_result"] == "unreachable"
    assert len(body["warnings"]) == 1
    warning = body["warnings"][0]
    assert fake.platform_origin in warning
    assert "Connected portals" in warning
    assert TOKEN not in warning

    # Removed here all the same, and the platform still holds the token.
    assert fake.tokens[TOKEN]["revoked"] is None
    with portal_db.get_session() as session:
        assert session.exec(select(Project.id)).all() == []


def test_remove_from_machine_with_an_unreadable_token(
    portal: TestClient,
    fake: FakePlatform,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
) -> None:
    """A token this portal can no longer decrypt is reported, never guessed at."""
    linked_project(portal, fake, env)
    with portal_db.get_session() as session:
        row = session.get(PlatformLink, project_id(SLUG))
        assert row is not None
        row.token_ciphertext = "not-a-fernet-token"
        session.add(row)
    before = len(fake.requests)

    body = portal.delete(f"/api/projects/{SLUG}").json()
    assert body["token_revoked"] is False
    assert body["token_revoke_result"] == "unreachable"
    assert "no longer read the token" in body["warnings"][0]
    assert len(fake.requests) == before  # nothing was sent with a guess


def test_remove_from_machine_when_the_token_was_already_revoked(
    portal: TestClient,
    fake: FakePlatform,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
) -> None:
    """A platform answering ``401 token_revoked`` is ``already_revoked`` (BUG-7).

    The token works nowhere any more, so ``token_revoked`` stays true and no
    "revoke it on the platform" warning is added.
    """
    linked_project(portal, fake, env)
    fake.revoke_token(TOKEN, "admin_revoked")

    body = portal.delete(f"/api/projects/{SLUG}").json()
    assert body["token_revoked"] is True
    assert body["token_revoke_result"] == "already_revoked"
    assert body["warnings"] == []
    with portal_db.get_session() as session:
        assert session.exec(select(Project.id)).all() == []


# ---------------------------------------------------------------------------
# Reconnect (plan section 4.11's "reconnect or remove")
# ---------------------------------------------------------------------------


def test_reconnect_replaces_a_revoked_link(
    portal: TestClient,
    fake: FakePlatform,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
) -> None:
    """The same checkout, platform and project: the link row takes a new token."""
    root = linked_project(portal, fake, env)
    fake.revoke_token(TOKEN, "admin_revoked")
    refresh_links(transport=transport_of(portal))
    assert (link_row(SLUG).status, link_row(SLUG).status_reason) == (
        "revoked",
        "admin_revoked",
    )
    was = project_id(SLUG)

    fake.projects[SLUG] = status_body(project_role="viewer")  # lowered meanwhile
    fresh = "wgc_" + "R" * 43
    started = connect(portal, fake)
    allow(fake, started, code="second", token=fresh)
    reply = callback(portal, started, code="second")
    assert reply.status_code == 200, reply.text
    again = link(portal, reply.json()["link_id"], root)
    assert again.status_code == 201, again.text

    # The same project row, a new token, the status cleared.
    assert project_id(SLUG) == was
    row = link_row(SLUG)
    assert decrypt(row.token_ciphertext) == fresh
    assert row.token_hint == "…" + fresh[-4:]
    assert (row.status, row.status_reason) == ("ok", None)
    assert row.last_platform_head == status_body()["last_scanned_head"]
    assert row.remote_project_role == "viewer"
    with portal_db.get_session() as session:
        assert len(session.exec(select(Project.id)).all()) == 1
    # The token it superseded stays unusable (the admin had already revoked it).
    assert fake.tokens[TOKEN]["revoked"] == "admin_revoked"
    # And the project works again, without an Initialize.
    assert summary(portal)["link"]["status"] == "ok"

    # Reconnecting again gives up the token it supersedes, which is still live.
    third = "wgc_" + "T" * 43
    started = connect(portal, fake)
    allow(fake, started, code="third", token=third)
    reply = callback(portal, started, code="third")
    assert link(portal, reply.json()["link_id"], root).status_code == 201
    assert fake.tokens[fresh]["revoked"] == "removed_locally"
    assert decrypt(link_row(SLUG).token_ciphertext) == third


def test_reconnect_refuses_a_different_link_target(
    portal: TestClient,
    fake: FakePlatform,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
) -> None:
    """Only the very same link is replaced; every other collision stays ``409``."""
    root = linked_project(portal, fake, env)
    kept = decrypt(link_row(SLUG).token_ciphertext)

    # Another checkout of the same platform project: the slug is taken.
    other = mirror(env, "demo-elsewhere")
    started = connect(portal, fake)
    allow(fake, started, code="c2", token="wgc_" + "X" * 43)
    second = callback(portal, started, code="c2")
    refused = link(portal, second.json()["link_id"], other)
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "slug_taken"

    # The same checkout and slug, but the project's clone URL changed: not
    # unambiguously the same link, so the link row is left alone.
    fake.projects[SLUG] = status_body(
        clone_url="https://github.com/acme/renamed.git",
        last_scanned_head=None,
    )
    started = connect(portal, fake)
    allow(fake, started, code="c3", token="wgc_" + "Y" * 43)
    third = callback(portal, started, code="c3")
    mismatch = link(portal, third.json()["link_id"], root)
    assert mismatch.status_code in (409, 422), mismatch.text
    assert mismatch.json()["code"] in ("slug_taken", "duplicate", "origin_mismatch")
    assert decrypt(link_row(SLUG).token_ciphertext) == kept


def test_pending_offers_the_checkout_a_reconnect_replaces(
    portal: TestClient,
    fake: FakePlatform,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
) -> None:
    """The picker names the already-linked checkout, and the add route takes it.

    The POST is driven with the ``reconnect.path`` the picker was given, so
    the offering side and the accepting side cannot drift apart.
    """
    root = linked_project(portal, fake, env)
    fake.revoke_token(TOKEN, "admin_revoked")
    refresh_links(transport=transport_of(portal))
    was = project_id(SLUG)

    fresh = "wgc_" + "R" * 43
    started = connect(portal, fake)
    allow(fake, started, code="second", token=fresh)
    link_id = callback(portal, started, code="second").json()["link_id"]
    body = pending_of(portal, link_id)
    assert body["reconnect"] == {"path": str(root), "slug": SLUG}
    # Not a dead end any more, and the checkout is offered exactly once.
    assert body["slug_taken"] is False
    assert str(root) not in [c["path"] for c in body["candidates"]]
    assert str(root) not in [r["path"] for r in body["other_repos"]]

    again = link(portal, link_id, Path(body["reconnect"]["path"]))
    assert again.status_code == 201, again.text
    assert project_id(SLUG) == was
    assert decrypt(link_row(SLUG).token_ciphertext) == fresh


def test_pending_reports_no_reconnect_for_another_link_target(
    portal: TestClient,
    fake: FakePlatform,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
) -> None:
    """Same slug, another platform / org / project / clone URL: not a reconnect.

    Each of these is the ``409 slug_taken`` it was before, so the picker must
    not offer the checkout: the rule is the add route's
    (:func:`whygraph.portal.platform_routes.reconnect_target`).
    """
    root = linked_project(portal, fake, env)
    second = FakePlatform("https://wg2.example.com")  # also serves `demo`
    portal.app.state.portal.platform_transport = _transport(fake, second)

    def pending_for(platform: FakePlatform, code: str, **over: Any) -> dict:
        started = connect(portal, platform)
        allow(platform, started, code=code, token="wgc_" + code[0].upper() * 43, **over)
        reply = callback(portal, started, code=code)
        assert reply.status_code == 200, reply.text
        return pending_of(portal, reply.json()["link_id"])

    # Another platform serving a project of the same slug and clone URL.
    other_platform = pending_for(second, "xplatform")
    assert other_platform["reconnect"] is None
    assert other_platform["slug_taken"] is True

    # The same platform and project, but another org there.
    other_org = pending_for(fake, "yorg", org="other")
    assert other_org["reconnect"] is None
    assert other_org["slug_taken"] is True

    # The same link, but the platform project's clone URL changed.
    fake.projects[SLUG] = status_body(
        clone_url="https://github.com/acme/renamed.git", last_scanned_head=None
    )
    renamed = pending_for(fake, "zclone")
    assert renamed["reconnect"] is None
    assert renamed["slug_taken"] is True
    fake.projects[SLUG] = status_body()

    # A link row whose remote_slug is no longer the reply's project.
    with portal_db.get_session() as session:
        row = session.get(PlatformLink, project_id(SLUG))
        assert row is not None
        row.remote_slug = "elsewhere"
        session.add(row)
    moved = pending_for(fake, "wslug")
    assert moved["reconnect"] is None
    assert moved["slug_taken"] is True

    # None of that touched the link, and the checkout stays unofferable.
    assert decrypt(link_row(SLUG).token_ciphertext) == TOKEN
    for body in (other_platform, other_org, renamed, moved):
        paths = [c["path"] for c in body["candidates"]]
        paths += [r["path"] for r in body["other_repos"]]
        assert str(root) not in paths


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_summary_carries_the_link_block(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    """``_summary.link`` is exactly the ten fields the SPA reads."""
    linked_project(portal, fake, env)
    block = summary(portal)["link"]
    assert block == {
        "platform_origin": fake.platform_origin,
        "org": ORG,
        "remote_slug": SLUG,
        "status": "ok",
        "status_reason": None,
        "last_platform_head": status_body()["last_scanned_head"],
        "project_role": "contributor",
        "explorer_url": f"{HOME}/explorer",
        "chat_url": f"{HOME}/chat",
        "manage_url": HOME,
    }
    # The details route carries the same block, and a local project none.
    assert portal.get(f"/api/projects/{SLUG}").json()["link"] == block
    local = make_repo(env.shared, "plain", remote=None)
    portal.post("/api/projects", json={"source": "local", "path": str(local)})
    assert summary(portal, "plain")["link"] is None


MAPPING: list[tuple[dict, tuple[str, str | None]]] = [
    # A 2xx answer: linked, unless the platform lost access to the repo.
    ({}, ("ok", None)),
    ({"project": status_body(access_lost=True)}, ("access_lost", None)),
    # 401 token_revoked, the reasons that mean the project is gone.
    ({"revoked": "project_deleted"}, ("removed", "project_deleted")),
    ({"revoked": "org_deleted"}, ("removed", "org_deleted")),
    # 401 token_revoked, the reasons that mean "reconnect or remove".
    ({"revoked": "user_revoked"}, ("revoked", "user_revoked")),
    ({"revoked": "admin_revoked"}, ("revoked", "admin_revoked")),
    ({"revoked": "member_removed"}, ("revoked", "member_removed")),
    ({"revoked": "member_left"}, ("revoked", "member_left")),
    ({"revoked": "user_disabled"}, ("revoked", "user_disabled")),
    ({"revoked": "idle"}, ("revoked", "idle")),
    ({"revoked": "project_access_removed"}, ("revoked", "project_access_removed")),
    # 401 invalid_token: the platform does not know this token at all.
    ({"forget": True}, ("revoked", "invalid_token")),
    # A connection error, a timeout or a 5xx.
    ({"failure": "unreachable"}, ("unreachable", None)),
    ({"failure": "timeout"}, ("unreachable", None)),
    # An incompatible `meta`: this WhyGraph cannot speak to that platform.
    ({"api_version": 2}, ("update_required", None)),
]
"""Every row of plan section 4.11's platform-answer -> local-status table."""


@pytest.mark.parametrize(("answer", "expected"), MAPPING)
def test_link_status_mapping(
    portal: TestClient,
    fake: FakePlatform,
    env: SimpleNamespace,
    answer: dict,
    expected: tuple[str, str | None],
) -> None:
    """A probe maps the platform's answer to the link's status, and stores it."""
    linked_project(portal, fake, env)
    if "project" in answer:
        fake.projects[SLUG] = answer["project"]
    if "revoked" in answer:
        fake.revoke_token(TOKEN, answer["revoked"])
    if answer.get("forget"):
        fake.tokens.pop(TOKEN)
    if "failure" in answer:
        fake.failure = answer["failure"]
    if "api_version" in answer:
        fake.api_version = answer["api_version"]

    assert refresh_links(transport=transport_of(portal)) == 1
    row = link_row(SLUG)
    assert (row.status, row.status_reason) == expected
    assert summary(portal)["link"]["status"] == expected[0]


def test_every_platform_call_updates_the_link_status(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    """An agent's own call records what it saw, so no status loop is needed.

    The project's context carries the
    :class:`~whygraph.portal.linked.LinkedProject` the MCP tool bodies use,
    bound to the local ``projects.id`` - so its answer, or its refusal,
    lands in the link row without anything else polling the platform.
    """
    linked_project(portal, fake, env)
    state = portal.app.state.portal
    remote = state.contexts.get(project_id(SLUG)).remote
    assert remote is not None and remote.project_id == project_id(SLUG)

    fake.projects[SLUG] = status_body(access_lost=True)
    remote.overview()
    assert link_row(SLUG).status == "access_lost"

    fake.revoke_token(TOKEN, "member_left")
    with pytest.raises(Exception, match="revoked"):
        remote.overview()
    row = link_row(SLUG)
    assert (row.status, row.status_reason) == ("revoked", "member_left")
    assert summary(portal)["link"]["status"] == "revoked"


def test_link_status_removed_after_platform_delete(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    """A project deleted on the platform reads ``removed``, not ``revoked``."""
    linked_project(portal, fake, env)
    fake.revoke_token(TOKEN, "project_deleted")
    refresh_links(transport=transport_of(portal))

    block = summary(portal)["link"]
    assert (block["status"], block["status_reason"]) == ("removed", "project_deleted")
    # The card's action is "remove from this machine", and it still works.
    assert portal.delete(f"/api/projects/{SLUG}").status_code == 200


def test_link_status_follows_a_rename_on_the_platform(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    """A renamed platform project renames the local one; the row is written once."""
    linked_project(portal, fake, env)
    fake.projects[SLUG] = status_body(name="Demo Renamed", last_scanned_head="b" * 40)
    refresh_links(transport=transport_of(portal))

    row = link_row(SLUG)
    assert row.remote_name == "Demo Renamed"
    assert row.last_platform_head == "b" * 40
    assert summary(portal)["name"] == "Demo Renamed"
    # Nothing changed, so a second probe writes nothing.
    assert save_status(project_id(SLUG), "ok", None) is False


def test_link_records_the_project_role_and_follows_it(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    """The caller's role on the platform project is kept, and written when it moves."""
    linked_project(portal, fake, env)
    assert link_row(SLUG).remote_project_role == "contributor"  # from the exchange
    fake.projects[SLUG] = status_body(project_role="viewer")
    refresh_links(transport=transport_of(portal))
    assert link_row(SLUG).remote_project_role == "viewer"
    assert summary(portal)["link"]["project_role"] == "viewer"
    same = StatusOut.model_validate(status_body(project_role="viewer"))
    assert save_status(project_id(SLUG), "ok", None, same) is False
    raised = StatusOut.model_validate(status_body(project_role="admin"))
    assert save_status(project_id(SLUG), "ok", None, raised) is True
    assert link_row(SLUG).remote_project_role == "admin"


def test_lost_project_access_says_ask_an_admin_not_reconnect(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    """``project_access_removed``: reconnecting cannot help, a project admin can."""
    linked_project(portal, fake, env)
    remote = portal.app.state.portal.contexts.get(project_id(SLUG)).remote
    fake.revoke_token(TOKEN, "project_access_removed")
    with pytest.raises(RemoteError) as caught:
        remote.overview()
    message = str(caught.value)
    assert "ask a project admin" in message and "reconnect" not in message
    assert (caught.value.status, caught.value.reason) == (
        "revoked",
        "project_access_removed",
    )
    row = link_row(SLUG)
    assert (row.status, row.status_reason) == ("revoked", "project_access_removed")
    # Any other revocation still says "reconnect".
    fake.revoke_token(TOKEN, "admin_revoked")
    with pytest.raises(RemoteError, match="reconnect"):
        remote.overview()


def test_status_refresh_never_blocks_listing(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    """A stale link is queued for the refresh task; the listing serves the cache."""
    linked_project(portal, fake, env)
    fake.failure = "timeout"  # any probe would hang, so none may run inline
    state = portal.app.state.portal
    state.link_refresh.take()  # start from an empty queue
    seen = len(fake.requests)

    # A fresh status is not refreshed at all.
    assert summary(portal)["link"]["status"] == "ok"
    assert state.link_refresh.pending() == []

    age_status(SLUG, when="2020-01-01T00:00:00+00:00")
    block = summary(portal)["link"]
    assert block["status"] == "ok"  # the recorded status, not a probe
    assert len(fake.requests) == seen  # the listing talked to no platform
    assert state.link_refresh.pending() == [project_id(SLUG)]
    assert state.link_refresh.take() == [project_id(SLUG)]
    assert state.link_refresh.take() == []


def test_due_for_refresh_scopes_and_ages(
    portal: TestClient, fake: FakePlatform, env: SimpleNamespace
) -> None:
    """Only stale links, only the asked-for org, and an unparsable date counts."""
    linked_project(portal, fake, env)
    pid = project_id(SLUG)
    with portal_db.get_session() as session:
        org_id = session.exec(select(Project.org_id)).one()
        assert due_for_refresh(session, org_id) == []
        assert due_for_refresh(session, org_id, after_sec=0) == [pid]
        assert due_for_refresh(session, org_id + 1) == []
    age_status(SLUG, when="not a timestamp")
    with portal_db.get_session() as session:
        assert due_for_refresh(session) == [pid]
    assert REFRESH_AFTER_SEC == 300


# ---------------------------------------------------------------------------
# No shared data on this machine
# ---------------------------------------------------------------------------


def test_no_platform_content_in_portal_tables(
    portal: TestClient,
    fake: FakePlatform,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
) -> None:
    """The portal DB holds the link's bookkeeping and none of the platform's data.

    Plan decision 0.1 #11: a linked project has no local WhyGraph database,
    and the portal's own tables must not become one either. After a status
    refresh that carried a whole ``StatusOut``, the only platform-derived
    values anywhere are the ones the link row is documented to hold.
    """
    root = linked_project(portal, fake, env)
    fake.projects[SLUG] = status_body(
        name="Demo", last_scanned_head="c" * 40, last_scan_at="2026-10-05T11:00:00Z"
    )
    refresh_links(transport=transport_of(portal))
    assert summary(portal)["link"]["last_platform_head"] == "c" * 40

    row = link_row(SLUG)
    allowed = {
        row.platform_origin,
        row.api_origin,
        row.org_slug,
        row.remote_slug,
        row.remote_name,
        row.clone_url,
        row.default_branch,
        row.last_platform_head,
        row.remote_project_role,
    }
    # Whatever the platform said, only those values are stored.
    assert row.last_platform_head == "c" * 40
    assert "2026-10-05T11:00:00Z" not in allowed

    with portal_db.get_session() as session:
        tables = sorted(
            name
            for name in session.connection()
            .exec_driver_sql(
                "select table_name from information_schema.tables "
                "where table_schema = 'public'"
            )
            .scalars()
            if not name.startswith("alembic")
        )
        haystack = ""
        for name in tables:
            rows = session.connection().exec_driver_sql(f'select * from "{name}"')
            haystack += "\n".join(str(tuple(r)) for r in rows)
    # The platform's own timestamps and role never land in any table...
    for leaked in ("2026-10-05T11:00:00Z", "member", TOKEN):
        assert leaked not in haystack, leaked
    # ...and the project still has no WhyGraph database of its own.
    assert not (root / ".whygraph" / "whygraph.db").exists()
    with portal_db.get_session() as session:
        assert session.exec(select(PlatformLink.last_platform_head)).all() == ["c" * 40]
