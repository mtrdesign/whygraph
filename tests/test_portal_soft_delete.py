"""Soft delete, restore, purge and erase (plan 59 sections 4.3.1-4.3.3).

In production every delete of an org or a project hides it and keeps its
rows and files (D12); only a platform administrator restores it or deletes
it permanently (D14). The request gates are checked from the route
inventory itself: every org-scoped route of
:data:`~test_portal_app.ROUTE_ACTIONS` on a soft-deleted org's host, and
every ``{slug}`` route for a soft-deleted project, answers ``404`` (with
the body its isolation sweep already sends, so a ``422`` cannot mask it).
The places off the request path (P13) each have an explicit case: the
account and connect lists, the org lists that join ``projects``
themselves, invitations, the import's own org check, the runner gate, the
background selectors and queued runs. Then the routes: the owner's and the
org admin's soft delete, the admin's delete, restore and purge (D34), the
re-import refusal (P14, D23) and the import picker's flag (D47).

The ``test_user_*`` cases cover users (section 4.3.1): the soft delete's
snapshot, refusals and lock order (D49), the routes that refuse a deleted
account, sign-in, Restore, Erase in every store (D38, D54), and
``[Deleted User]`` on every org-facing site of the label table (D37).

Worlds reused: :func:`~test_portal_hosts_isolation.prod_world`,
:func:`~test_portal_connect.cn`, :func:`~test_portal_github_import.world`
and :func:`~test_portal_members.team`.
"""

# ruff: noqa: F811 -- pytest fixtures are imported from other test modules

from __future__ import annotations

import json
import threading
import time
from decimal import Decimal
from functools import partial
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import update
from sqlmodel import col, select

from test_portal_app import (  # noqa: F401 -- fixtures
    PROD_PASSWORD,
    add_local,
    admin_request,
    admin_signed_in,
    at,
    client,
    env,
    github_fake,
    github_sign_in,
    grant_reader,
    history_db,
    make_repo,
    password_user,
    production_env,
    ready,
    signed_in,
)
from test_portal_connect import (  # noqa: F401 -- `cn` is a fixture
    authorize,
    be,
    bearer,
    cn,
    code_for,
    exchange,
    issue,
    revoked_reason,
    validate,
)
from test_portal_github_import import (  # noqa: F401 -- fixtures
    API_REPO,
    API,
    INSTALLATION,
    World,
    app_env,
    clone_dirs,
    connected,
    github_app_key,
    github_git_server,
    import_repo,
    imported_ok,
    project_row,
    wait_import,
    world,
)
from test_portal_hosts_isolation import (  # noqa: F401 -- `prod_world` is a fixture
    GITHUB_IDS,
    NOT_FOUND,
    PROD_API_ROUTES,
    ProdWorld,
    _call,
    _wait_idle,
    prod_world,
)
from test_portal_identity_routes import (  # noqa: F401 -- fixture
    audit_log,
    create_org,
    events,
)
from test_portal_hosts_isolation import _insert_project
from test_portal_members import be as be_in_team
from test_portal_members import team  # noqa: F401 -- fixture
from test_portal_tenancy import ROUTE_REQUESTS, TERMINAL, _ok, _url, wait_for
from whygraph.core.context import use_project
from whygraph.db import get_session as project_session
from whygraph.db.models import ChatMessage, ChatSession, ImportedChat, ProjectImport
from whygraph.portal import audit as audit_mod
from whygraph.portal import audit_store, auth_routes, connections, notes_routes
from whygraph.portal import db as portal_db
from whygraph.portal import org_routes
from whygraph.portal import sessions as sessions_mod
from whygraph.portal import transfer_export
from whygraph.portal import routes as routes_mod
from whygraph.portal import runner as runner_mod
from whygraph.portal import webhook
from whygraph.portal.deps import load_org_access, project_role_for
from whygraph.portal.models import (
    AuditEvent,
    ConnectionToken,
    Invitation,
    Membership,
    Organization,
    Project,
    ProjectGrant,
    ReaderGrant,
    RetiredOrgSlug,
    ScanRun,
    UsageEvent,
    User,
    UserSession,
)
from whygraph.portal.paths import project_db_path
from whygraph.portal.runner import ProjectGone
from whygraph.portal.usage import actor_label

SOFT_RESULT_KEYS = {
    "removed",
    "hooks",
    "agent_files",
    "checkout_deleted",
    "history_deleted",
    "warnings",
    "soft",
}
"""The ``DeleteProjectResult`` shape a soft delete answers (plan section 4.3.3)."""

DELETED_AT = "2026-11-18T00:00:00+00:00"
MIRROR_REPO = API + 50
"""A second repository on installation 7, imported under the name ``api``."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def owner_delete_org(client, org: str) -> httpx.Response:  # noqa: ANN001
    """The owner's ``DELETE /api/org`` on ``org``'s host (the caller's session)."""
    return client.request("DELETE", at(org) + "/api/org", json={"confirm_slug": org})


def remove_project(client, org: str, name: str, slug: str = "api") -> httpx.Response:  # noqa: ANN001
    """The org admin's ``DELETE /api/projects/{slug}``, typing ``name``."""
    return client.request(
        "DELETE", at(org) + f"/api/projects/{slug}", json={"confirm_name": name}
    )


def admin_delete_org(client, org: str, confirm: str | None = None) -> httpx.Response:  # noqa: ANN001
    body = {"confirm_slug": org if confirm is None else confirm}
    return admin_request(client, "DELETE", f"/api/admin/orgs/{org}", body)


def restore_org(client, org: str) -> httpx.Response:  # noqa: ANN001
    return admin_request(client, "POST", f"/api/admin/orgs/{org}/restore")


def purge_org(client, org: str, confirm: str | None = None) -> httpx.Response:  # noqa: ANN001
    body = {"confirm_slug": org if confirm is None else confirm}
    return admin_request(client, "POST", f"/api/admin/orgs/{org}/purge", body)


def admin_delete_project(
    client, org: str, name: str, slug: str = "api"
) -> httpx.Response:  # noqa: ANN001
    path = f"/api/admin/projects/{org}/{slug}"
    return admin_request(client, "DELETE", path, {"confirm_name": name})


def restore_project(client, org: str, slug: str = "api") -> httpx.Response:  # noqa: ANN001
    return admin_request(client, "POST", f"/api/admin/projects/{org}/{slug}/restore")


def purge_project(client, org: str, name: str, slug: str = "api") -> httpx.Response:  # noqa: ANN001
    path = f"/api/admin/projects/{org}/{slug}/purge"
    return admin_request(client, "POST", path, {"confirm_name": name})


def code_of(
    response: httpx.Response, status: int, code: str, where: object = None
) -> None:
    """Assert ``status`` with ``code``."""
    assert response.status_code == status, (where, response.text)
    assert response.json().get("code") == code, (where, response.text)


def org_row(slug: str) -> Organization | None:
    with portal_db.get_session() as session:
        row = session.exec(
            select(Organization).where(Organization.slug == slug)
        ).first()
        if row is not None:
            session.expunge(row)
        return row


def project_by_id(project_id: int) -> Project | None:
    with portal_db.get_session() as session:
        row = session.get(Project, project_id)
        if row is not None:
            session.expunge(row)
        return row


def set_org(slug: str, **values: object) -> None:
    with portal_db.get_session() as session:
        session.exec(
            update(Organization).where(col(Organization.slug) == slug).values(**values)
        )


def set_project(project_id: int, **values: object) -> None:
    with portal_db.get_session() as session:
        session.exec(
            update(Project).where(col(Project.id) == project_id).values(**values)
        )


def runs_of(project_id: int) -> list[ScanRun]:
    with portal_db.get_session() as session:
        rows = session.exec(
            select(ScanRun)
            .where(ScanRun.project_id == project_id)
            .order_by(col(ScanRun.id))
        ).all()
        for row in rows:
            session.expunge(row)
        return rows


def released(runner) -> bool:  # noqa: ANN001
    with runner._claims:
        return not runner._deleting_orgs and not runner._removing


# ---------------------------------------------------------------------------
# The request gates, from the route inventory
# ---------------------------------------------------------------------------


def test_every_org_route_is_404_on_a_soft_deleted_org(prod_world: ProdWorld) -> None:
    """Members and a reader alike get the unknown org's ``404`` on every route."""
    w = prod_world
    w.read_as_reader("quokka")  # a live grant from before the delete
    w.sign_in("ann")
    assert _ok(owner_delete_org(w.client, "quokka")) == {
        "deleted": "quokka",
        "projects": 1,
    }
    assert len(PROD_API_ROUTES) > 60
    w.sign_in("ann")
    for method, path in PROD_API_ROUTES:
        spec = ROUTE_REQUESTS[(method, path)]
        response = _call(w, w.quokka, method, path, spec)
        where = (method, path, response.text)
        assert response.status_code == 404, where
        assert response.json() == NOT_FOUND, where
    # The reader's grant no longer opens it either (bodies left out: some
    # build fixtures on disk, and the org check comes first).
    w.sign_in("ada")
    for method, path in PROD_API_ROUTES:
        response = w.client.request(method, at("quokka") + _url(path, w.quokka))
        where = ("reader", method, path, response.text)
        assert (response.status_code, response.json()) == (404, NOT_FOUND), where
    # The other org is untouched.
    w.sign_in("bob")
    assert _ok(w.client.get(at("narwhal") + "/api/projects"))["projects"]
    assert load_org_access(w.ids["bob"], "quokka") is None
    assert load_org_access(w.ids["bob"], "narwhal") is not None


def test_every_project_route_is_404_for_a_soft_deleted_project(
    prod_world: ProdWorld,
) -> None:
    w = prod_world
    w.sign_in("ann")
    body = _ok(remove_project(w.client, "quokka", w.quokka.name))
    assert body == {
        "removed": "api",
        "hooks": None,
        "agent_files": [],
        "checkout_deleted": False,
        "history_deleted": False,
        "warnings": [],
        "soft": True,
    }
    slug_routes = [(m, p) for (m, p) in PROD_API_ROUTES if "{slug}" in p]
    assert len(slug_routes) > 40
    for who in ("ann", "bob"):
        w.sign_in(who)
        for method, path in slug_routes:
            spec = ROUTE_REQUESTS[(method, path)]
            response = _call(w, w.quokka, method, path, spec)
            assert response.status_code == 404, (who, method, path, response.text)
    # The org itself still answers, without the project.
    w.sign_in("ann")
    assert _ok(w.client.get(at("quokka") + "/api/projects")) == {"projects": []}
    # Nothing was removed: the row, the history and the root stay.
    row = project_by_id(w.quokka.project_id)
    assert row is not None and row.deleted_at is not None
    assert row.deleted_by == w.ids["ann"]
    assert project_db_path(w.env.data, "quokka", "api").is_file()
    assert (w.quokka.root / ".git").is_dir()
    # The other org's project is untouched.
    w.sign_in("bob")
    assert _ok(w.client.get(at("narwhal") + "/api/projects/api"))["slug"] == "api"


# ---------------------------------------------------------------------------
# Explicit cases off the request path (P13)
# ---------------------------------------------------------------------------


def test_the_account_lists_omit_a_soft_deleted_org(prod_world: ProdWorld) -> None:
    w = prod_world
    w.sign_in("bob")  # owner of narwhal, member of quokka
    orgs = [o["slug"] for o in _ok(w.client.get(at() + "/api/account/orgs"))]
    assert sorted(orgs) == ["narwhal", "quokka"]
    _ok(admin_delete_org(w.client, "quokka"))
    w.sign_in("bob")
    orgs = [o["slug"] for o in _ok(w.client.get(at() + "/api/account/orgs"))]
    assert orgs == ["narwhal"]
    usage = _ok(w.client.get(at() + "/api/account/usage"))
    assert [o["slug"] for o in usage["orgs"]] == ["narwhal"]
    # The org row, its memberships and its slug are kept.
    row = org_row("quokka")
    assert row is not None and row.deleted_at is not None
    with portal_db.get_session() as session:
        kept = session.exec(
            select(Membership).where(Membership.org_id == w.quokka.org_id)
        ).all()
    assert len(kept) == 2
    w.sign_in("ann")
    code_of(create_org(w.client, "quokka", "Again"), 409, "slug_taken")


def test_the_org_lists_omit_a_soft_deleted_project(
    prod_world: ProdWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Projects, budgets, grants, invitations, onboarding, the banner, the usage filter."""
    w = prod_world
    q = at("quokka")
    state = w.client.app.state.portal
    w.sign_in("ann")
    _ok(
        w.client.put(
            q + f"/api/projects/api/access/{w.uids['bob']}", json={"role": "viewer"}
        )
    )
    _ok(
        w.client.put(
            q + "/api/projects/api/budget", json={"monthly_usd": 1, "hard_stop": False}
        )
    )
    invited = w.client.post(
        q + "/api/org/members",
        json={
            "github_login": "quokka-newcomer",
            "role": "member",
            "grants": [{"project": "api", "role": "viewer"}],
        },
    )
    assert _ok(invited, 201)["pending"] is True
    monkeypatch.setattr(state.spend, "spent", lambda *a, **k: Decimal("5"))

    def lists() -> dict:
        members = _ok(w.client.get(q + "/api/org/members"))
        bob = next(m for m in members if m["uid"] == w.uids["bob"])
        (invitation,) = _ok(w.client.get(q + "/api/org/invitations"))
        onboarding = _ok(w.client.get(q + "/api/onboarding"))["items"]
        usage = _ok(w.client.get(q + "/api/usage", params={"project": "api"}))
        return {
            "projects": [
                p["slug"] for p in _ok(w.client.get(q + "/api/projects"))["projects"]
            ],
            "budgets": [
                p["slug"] for p in _ok(w.client.get(q + "/api/budgets"))["projects"]
            ],
            "grants": [g["project"] for g in bob["grants"]],
            "invitation": [g["project"] for g in invitation["grants"]],
            "onboarding": next(i for i in onboarding if i["id"] == "project")["done"],
            "banner": [
                p["slug"]
                for p in _ok(w.client.get(q + "/api/portal/state"))["usage"][
                    "projects_over"
                ]
            ],
            "usage": usage["totals"]["calls"],
        }

    before = lists()
    assert before == {
        "projects": ["api"],
        "budgets": ["api"],
        "grants": ["api"],
        "invitation": ["api"],
        "onboarding": True,
        "banner": ["api"],
        "usage": before["usage"],
    }
    assert before["usage"] > 0
    _ok(remove_project(w.client, "quokka", w.quokka.name))
    assert lists() == {
        "projects": [],
        "budgets": [],
        "grants": [],
        "invitation": [],
        "onboarding": False,
        "banner": [],
        "usage": 0,
    }
    # A hidden project cannot be granted.
    refused = w.client.post(
        q + "/api/org/members",
        json={
            "github_login": "quokka-newcomer",
            "role": "member",
            "grants": [{"project": "api", "role": "viewer"}],
        },
    )
    code_of(refused, 404, "no_such_project")
    # The scan-event stream's re-check sees no access either.
    access = load_org_access(w.ids["bob"], "quokka")
    assert access is not None
    with portal_db.get_session() as session:
        assert project_role_for(session, access, w.quokka.project_id) is None
    # An invitation redeemed now adds the membership but no grant for it.
    w.sign_out()
    assert github_sign_in(w.client, "quokka-newcomer").status_code == 200
    with portal_db.get_session() as session:
        newcomer = session.exec(
            select(User.id).where(User.github_login == "quokka-newcomer")
        ).one()
        assert session.get(Membership, (w.quokka.org_id, newcomer)) is not None
        assert (
            session.exec(
                select(ProjectGrant).where(ProjectGrant.user_id == newcomer)
            ).all()
            == []
        )
    # Usage history keeps its rows: the org-wide view still counts them.
    w.sign_in("ann")
    assert _ok(w.client.get(q + "/api/usage"))["totals"]["calls"] >= before["usage"]


def test_an_invitation_to_a_soft_deleted_org_is_not_redeemed(
    prod_world: ProdWorld,
) -> None:
    w = prod_world
    w.sign_in("ann")
    invited = w.client.post(
        at("quokka") + "/api/org/members",
        json={"github_login": "quokka-newcomer", "role": "member"},
    )
    assert _ok(invited, 201)["pending"] is True
    _ok(admin_delete_org(w.client, "quokka"))

    def membership() -> Membership | None:
        with portal_db.get_session() as session:
            return session.exec(
                select(Membership)
                .join(User, col(User.id) == col(Membership.user_id))
                .where(
                    Membership.org_id == w.quokka.org_id,
                    User.github_login == "quokka-newcomer",
                )
            ).first()

    def open_invitations() -> int:
        with portal_db.get_session() as session:
            return len(
                session.exec(
                    select(Invitation).where(
                        Invitation.org_id == w.quokka.org_id,
                        col(Invitation.redeemed_at).is_(None),
                    )
                ).all()
            )

    w.sign_out()
    assert github_sign_in(w.client, "quokka-newcomer").status_code == 200
    assert membership() is None and open_invitations() == 1
    _ok(restore_org(w.client, "quokka"))
    w.sign_out()
    assert github_sign_in(w.client, "quokka-newcomer").status_code == 200
    assert membership() is not None and open_invitations() == 0


def test_connect_lists_and_mints_nothing_for_a_soft_deleted_project(
    cn: SimpleNamespace,
) -> None:
    n = cn
    token = issue(n, "ben", "api")
    consented = code_for(n)  # acme/api, before the delete
    url = at("acme") + "/api/v1/projects/api"
    n.client.cookies.clear()
    _ok(n.client.get(url, headers=bearer(token)))

    be(n, "ben")
    _ok(remove_project(n.client, "acme", "Api project"))
    assert revoked_reason(token) == "project_deleted"
    n.client.cookies.clear()
    assert n.client.get(url, headers=bearer(token)).status_code == 401
    be(n, "ben")
    listed = _ok(n.client.get(at() + "/api/connect/projects"))
    assert [(p["org"], p["slug"]) for p in listed] == [("acme", "web")]
    assert validate(n, org="acme", project="api").json()["field"] == "project"
    assert authorize(n).status_code == 404
    tokens = _token_count()
    assert exchange(n, consented).status_code == 400  # invalid_grant
    assert _token_count() == tokens

    # A Restore brings the project back, not its token.
    _ok(restore_project(n.client, "acme"))
    assert revoked_reason(token) == "project_deleted"
    n.client.cookies.clear()
    assert n.client.get(url, headers=bearer(token)).status_code == 401
    assert exchange(n, code_for(n)).status_code == 200


def test_connect_lists_and_mints_nothing_for_a_soft_deleted_org(
    cn: SimpleNamespace,
) -> None:
    n = cn
    token = issue(n, "ben", "api")
    consented = code_for(n)
    _ok(admin_delete_org(n.client, "acme"))
    assert revoked_reason(token) == "org_deleted"
    be(n, "ben")
    assert _ok(n.client.get(at() + "/api/connect/projects")) == []
    response = validate(n, org="acme")
    assert (response.status_code, response.json()["field"]) == (422, "org")
    assert _ok(validate(n))["orgs"] == []
    assert authorize(n).status_code == 404
    assert exchange(n, consented).status_code == 400
    # Dee's org is untouched.
    lib = issue(n, "dee", "lib")
    n.client.cookies.clear()
    _ok(n.client.get(at("bravo") + "/api/v1/projects/lib", headers=bearer(lib)))
    _ok(restore_org(n.client, "acme"))
    assert revoked_reason(token) == "org_deleted"  # tokens stay revoked


def _token_count() -> int:
    from whygraph.portal.models import ConnectionToken

    with portal_db.get_session() as session:
        return len(session.exec(select(ConnectionToken.id)).all())


def test_an_import_racing_a_soft_delete_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The import passed ``current_org`` and is about to insert when the org is deleted."""
    w = world
    connected(w)
    entered, release = threading.Event(), threading.Event()
    real = routes_mod._insert_import_row

    def paused_insert(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        entered.set()
        release.wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr(routes_mod, "_insert_import_row", paused_insert)
    answers: list[httpx.Response] = []
    importer = threading.Thread(target=lambda: answers.append(import_repo(w, API_REPO)))
    importer.start()
    assert entered.wait(30)
    deleted = owner_delete_org(w.client, "acme")
    release.set()
    importer.join(30)

    assert _ok(deleted) == {"deleted": "acme", "projects": 0}
    assert (answers[0].status_code, answers[0].json()) == (404, NOT_FOUND)
    assert project_row("acme", "api") is None
    assert not (w.repos / "acme" / "api").exists()
    assert org_row("acme").deleted_at is not None  # soft: the row stays


# ---------------------------------------------------------------------------
# The runner (P13): its gate, the background selectors, queued runs
# ---------------------------------------------------------------------------


def test_every_run_request_raises_project_gone(prod_world: ProdWorld) -> None:
    """``project_deleted`` for a deleted project, ``org_deleted`` when its org is."""
    w = prod_world
    runner = w.client.app.state.portal.runner
    pid = w.quokka.project_id
    before = len(runs_of(pid))
    project = SimpleNamespace(id=pid, source="github")
    requests = (
        partial(runner.request_sync, pid),
        partial(runner.request_sync, pid, trigger="reconcile"),
        partial(runner.request_import, pid),
        partial(
            runner.request_scan,
            project,
            trigger=None,
            analyze=False,
            principal=None,
            may_spend=False,
        ),
    )
    set_project(pid, deleted_at=DELETED_AT)
    for request in requests:
        with pytest.raises(ProjectGone) as raised:
            w.client.portal.call(request)
        assert raised.value.code == "project_deleted"
    # A deleted org wins over its deleted project, and over its suspension.
    set_org("quokka", deleted_at=DELETED_AT, suspended_at=DELETED_AT)
    for request in requests:
        with pytest.raises(ProjectGone) as raised:
            w.client.portal.call(request)
        assert raised.value.code == "org_deleted"
    assert len(runs_of(pid)) == before
    assert (ProjectGone, "project_deleted") in routes_mod.SCAN_REFUSAL_CODES


def test_a_reconcile_pass_skips_hidden_projects_and_goes_on(
    prod_world: ProdWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is minted or probed for them; a delete after the selection ends nothing."""
    w = prod_world
    runner = w.client.app.state.portal.runner
    first, second = w.narwhal.project_id, w.quokka.project_id
    assert first < second  # the hidden one comes first in the pass
    w.sign_in("bob")
    _ok(remove_project(w.client, "narwhal", w.narwhal.name))
    probed: list[int] = []
    checked: list[int] = []

    def remote_head(target):  # noqa: ANN001, ANN202 -- what mints a token
        probed.append(target.id)
        return "0" * 40  # the remote "moved"

    monkeypatch.setattr(runner, "_remote_head", remote_head)
    monkeypatch.setattr(runner, "check_access", checked.append)
    narwhal_runs = len(runs_of(first))

    w.client.portal.call(runner.reconcile)

    assert probed == [second] and checked == []
    assert [r.trigger for r in runs_of(second)][-1] == "reconcile"
    assert len(runs_of(first)) == narwhal_runs
    assert project_by_id(first).deleted_at is not None  # hidden, not gone
    _wait_idle(w, w.quokka)
    # An org deleted after the selection: ProjectGone is in the except tuple.
    set_project(first, deleted_at=None)
    real = runner_mod._reconcile_targets

    def targets():  # noqa: ANN202
        found = real()
        set_org("narwhal", deleted_at=DELETED_AT)
        return found

    monkeypatch.setattr(runner_mod, "_reconcile_targets", targets)
    w.client.portal.call(runner.reconcile)
    assert len(runs_of(first)) == narwhal_runs
    assert [r.trigger for r in runs_of(second)][-2:] == ["reconcile", "reconcile"]
    _wait_idle(w, w.quokka)


def test_a_webhook_fan_out_skips_hidden_projects_and_goes_on(
    prod_world: ProdWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = prod_world
    state = w.client.app.state.portal
    first, second = w.narwhal.project_id, w.quokka.project_id
    set_project(first, deleted_at=DELETED_AT)
    assert webhook._push_targets(GITHUB_IDS["narwhal"][1]) == []
    set_project(first, deleted_at=None)
    set_org("narwhal", deleted_at=DELETED_AT)
    assert webhook._push_targets(GITHUB_IDS["narwhal"][1]) == []
    assert webhook._push_targets(GITHUB_IDS["quokka"][1]) == [second]
    # A delete that lands after the selection is the runner's refusal.
    monkeypatch.setattr(webhook, "_push_targets", lambda repo_id: [first, second])
    narwhal_runs = len(runs_of(first))

    w.client.portal.call(webhook._queue, state, 0, "push")

    assert len(runs_of(first)) == narwhal_runs
    assert [r.trigger for r in runs_of(second)][-1] == "push"
    _wait_idle(w, w.quokka)


def test_the_start_up_selectors_skip_hidden_projects(prod_world: ProdWorld) -> None:
    w = prod_world
    ids = lambda: {p["id"] for p in runner_mod._initialized_projects()}  # noqa: E731
    both = {w.quokka.project_id, w.narwhal.project_id}
    assert ids() >= both
    set_project(w.quokka.project_id, deleted_at=DELETED_AT)
    set_org("narwhal", deleted_at=DELETED_AT)
    assert not ids() & both
    assert not {t.id for t in runner_mod._reconcile_targets()} & both


def test_queued_runs_of_hidden_projects_are_cancelled_at_start(
    prod_world: ProdWorld,
) -> None:
    w = prod_world
    with portal_db.get_session() as session:
        rows = [
            ScanRun(project_id=pid, kind="sync", trigger="sync", status="queued")
            for pid in (w.quokka.project_id, w.narwhal.project_id)
        ]
        session.add_all(rows)
        session.flush()
        hidden, kept = (row.id for row in rows)
    set_project(w.quokka.project_id, deleted_at=DELETED_AT)

    specs = runner_mod._recover_queued(production=True)

    assert [spec.run_id for spec in specs] == [kept]
    with portal_db.get_session() as session:
        assert session.get(ScanRun, hidden).status == "cancelled"
        session.get(ScanRun, kept).status = "queued"
    # A deleted org's queued row too.
    set_project(w.quokka.project_id, deleted_at=None)
    set_org("narwhal", deleted_at=DELETED_AT)
    assert runner_mod._recover_queued(production=True) == []
    with portal_db.get_session() as session:
        assert session.get(ScanRun, kept).status == "cancelled"


def test_a_queued_run_dispatched_after_a_soft_delete_is_refused(
    prod_world: ProdWorld,
) -> None:
    """A run queued before the delete (written straight to the row) never starts."""
    w = prod_world
    runner = w.client.app.state.portal.runner
    pid = w.quokka.project_id
    started = len(w.scanner.calls())
    w.scanner.hold.touch()
    w.sign_in("ann")
    scans = at("quokka") + "/api/projects/api/scans"
    first = _ok(w.client.post(scans, json={}), 202)["run_id"]
    wait_for(lambda: len(w.scanner.calls()) == started + 1)
    second = _ok(w.client.post(scans, json={}), 202)["run_id"]
    assert pid in runner._pending
    set_project(pid, deleted_at=DELETED_AT)

    w.scanner.hold.unlink()

    def ended(run_id: int) -> ScanRun | None:
        row = next(r for r in runs_of(pid) if r.id == run_id)
        return row if row.status in TERMINAL else None

    assert wait_for(lambda: ended(first)).status == "ok"
    row = wait_for(lambda: ended(second))
    assert row.status == "failed"
    assert runner_mod.GONE_MESSAGE in (row.summary or "")
    assert len(w.scanner.calls()) == started + 1  # the second never ran


# ---------------------------------------------------------------------------
# The owner's and the org admin's deletes: soft in production
# ---------------------------------------------------------------------------


def test_the_owners_org_delete_is_soft_and_keeps_every_file(
    world: World, audit_log: pytest.LogCaptureFixture
) -> None:
    w = world
    connected(w)
    imported_ok(w)
    clone = w.repos / "acme" / "api"
    history = history_db("api", "acme")
    assert clone.is_dir() and history.is_file()

    response = owner_delete_org(w.client, "acme")

    assert _ok(response) == {"deleted": "acme", "projects": 1}
    row = org_row("acme")
    assert row is not None and row.deleted_at is not None and row.deleted_by
    assert clone.is_dir() and history.is_file()
    assert project_row("acme", "api") is not None
    assert w.client.get(at("acme") + "/api/projects").status_code == 404
    # The slug stays taken; nothing is retired yet.
    code_of(create_org(w.client, "acme", "Again"), 409, "slug_taken")
    with portal_db.get_session() as session:
        assert session.get(RetiredOrgSlug, "acme") is None
    (event,) = [e for e in events(audit_log) if e["event"] == "org_deleted"]
    assert (event["org"], event["projects"], event["soft"]) == ("acme", 1, True)
    assert "via" not in event
    assert released(w.state.runner)


def test_the_org_admins_project_delete_is_soft_in_production(
    world: World, audit_log: pytest.LogCaptureFixture
) -> None:
    w = world
    connected(w)
    imported_ok(w)
    clone, history = w.repos / "acme" / "api", history_db("api", "acme")
    runs = sorted((w.env.data / "runs").glob("*.jsonl"))
    assert runs
    # The typed name is still required; the history choice is ignored.
    refused = w.client.request(
        "DELETE", at("acme") + "/api/projects/api", json={"delete_history": True}
    )
    code_of(refused, 409, "confirm_name")
    response = w.client.request(
        "DELETE",
        at("acme") + "/api/projects/api",
        json={"confirm_name": "api", "delete_history": True},
    )
    body = _ok(response)
    assert set(body) == SOFT_RESULT_KEYS and body["soft"] is True
    assert (body["removed"], body["hooks"], body["agent_files"]) == ("api", None, [])
    assert (body["checkout_deleted"], body["history_deleted"]) == (False, False)
    assert clone.is_dir() and history.is_file()
    assert all(p.exists() for p in runs)
    row = project_row("acme", "api")
    assert row is not None and row.deleted_at is not None
    (event,) = [e for e in events(audit_log) if e["event"] == "project_removed"]
    assert (event["soft"], event["checkout_deleted"], event["history_deleted"]) == (
        True,
        False,
        False,
    )


def test_local_mode_removal_is_unchanged(ready, env: SimpleNamespace) -> None:  # noqa: ANN001
    add_local(ready, make_repo(env.shared, "demo"))
    removed = ready.request("DELETE", "/api/projects/demo", json={})
    body = _ok(removed)
    assert "soft" not in body and body["history_deleted"] is False
    with portal_db.get_session() as session:
        assert session.exec(select(Project).where(Project.slug == "demo")).all() == []


# ---------------------------------------------------------------------------
# The admin routes (D34): delete, restore, purge
# ---------------------------------------------------------------------------


def test_restore_brings_an_org_back_and_tokens_stay_revoked(
    cn: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    n = cn
    token = issue(n, "ben", "api")
    be(n, "ben")
    _ok(remove_project(n.client, "acme", "Web project", slug="web"))
    code_of(restore_org(n.client, "acme"), 409, "not_deleted")
    code_of(admin_delete_org(n.client, "acme", confirm="wrong"), 409, "confirm_slug")
    assert _ok(admin_delete_org(n.client, "acme")) == {"deleted": "acme", "projects": 1}
    # Writes on a deleted org answer org_deleted (D34); its own Restore works.
    code_of(admin_delete_org(n.client, "acme"), 409, "org_deleted")
    code_of(
        admin_request(
            n.client, "POST", "/api/admin/orgs/acme/suspend", {"reason": "x"}
        ),
        409,
        "org_deleted",
    )
    assert admin_request(n.client, "GET", "/api/admin/orgs").status_code == 200

    assert _ok(restore_org(n.client, "acme")) == {"restored": "acme"}

    assert org_row("acme").deleted_at is None and org_row("acme").deleted_by is None
    be(n, "ben")
    listed = _ok(n.client.get(at("acme") + "/api/projects"))["projects"]
    # A project deleted on its own stays deleted.
    assert [p["slug"] for p in listed] == ["api"]
    assert revoked_reason(token) == "org_deleted"
    names = [e["event"] for e in events(audit_log)]
    assert names.count("org_deleted") == 1 and names.count("org_restored") == 1
    (deleted,) = [e for e in events(audit_log) if e["event"] == "org_deleted"]
    assert (deleted["soft"], deleted["via"]) == (True, "instance_admin")
    code_of(restore_org(n.client, "acme"), 409, "not_deleted")
    assert restore_org(n.client, "nosuch").status_code == 404
    assert released(n.state.runner)


def test_purging_an_org_runs_the_hard_path(
    world: World, audit_log: pytest.LogCaptureFixture
) -> None:
    w = world
    connected(w)
    imported_ok(w)
    code_of(purge_org(w.client, "acme"), 409, "not_deleted")
    _ok(owner_delete_org(w.client, "acme"))
    code_of(purge_org(w.client, "acme", confirm="Acme"), 409, "confirm_slug")
    assert org_row("acme") is not None

    assert _ok(purge_org(w.client, "acme")) == {"purged": "acme", "projects": 1}

    assert org_row("acme") is None and project_row("acme", "api") is None
    assert not (w.repos / "acme").exists()
    assert not (w.env.data / "projects" / "acme").exists()
    with portal_db.get_session() as session:
        assert session.get(RetiredOrgSlug, "acme") is not None
    code_of(create_org(w.client, "acme", "Again"), 409, "slug_taken")
    (event,) = [e for e in events(audit_log) if e["event"] == "org_purged"]
    assert (event["org"], event["projects"]) == ("acme", 1)
    assert purge_org(w.client, "acme").status_code == 404
    assert released(w.state.runner)


def test_an_org_purge_cannot_race_a_restore(
    prod_world: ProdWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = prod_world
    runner = w.client.app.state.portal.runner
    org_id = w.quokka.org_id
    _ok(admin_delete_org(w.client, "quokka"))
    # A Restore holds the org: the purge is busy, and nothing is deleted.
    with runner._claims:
        runner._deleting_orgs[org_id] = "restored"
    try:
        response = purge_org(w.client, "quokka")
    finally:
        with runner._claims:
            runner._deleting_orgs.pop(org_id)
    assert response.json() == {
        "error": "the organization is already being restored",
        "code": "busy",
    }
    # A Restore that commits after the purge's first look: the check under
    # the purge's row lock refuses.
    real = org_routes._delete_rows

    def restored_first(org: int, slug: str):  # noqa: ANN202
        set_org(slug, deleted_at=None)
        return real(org, slug)

    monkeypatch.setattr(org_routes, "_delete_rows", restored_first)
    code_of(purge_org(w.client, "quokka"), 409, "not_deleted")
    assert org_row("quokka") is not None
    assert project_by_id(w.quokka.project_id) is not None
    assert released(runner)
    # And a purge holding the org makes a Restore busy.
    set_org("quokka", deleted_at=DELETED_AT)
    with runner._claims:
        runner._deleting_orgs[org_id] = "purged"
    try:
        code_of(restore_org(w.client, "quokka"), 409, "busy")
    finally:
        with runner._claims:
            runner._deleting_orgs.pop(org_id)
    assert org_row("quokka").deleted_at == DELETED_AT


def test_restore_and_purge_a_project(
    world: World, audit_log: pytest.LogCaptureFixture
) -> None:
    w = world
    connected(w)
    imported_ok(w)
    clone, history = w.repos / "acme" / "api", history_db("api", "acme")
    code_of(restore_project(w.client, "acme"), 409, "not_deleted")
    code_of(purge_project(w.client, "acme", "api"), 409, "not_deleted")
    assert _ok(admin_delete_project(w.client, "acme", "api"))["soft"] is True
    code_of(admin_delete_project(w.client, "acme", "api"), 409, "project_deleted")
    assert w.client.get(at("acme") + "/api/projects/api").status_code == 404

    assert _ok(restore_project(w.client, "acme")) == {"restored": "api"}
    assert _ok(w.client.get(at("acme") + "/api/projects/api"))["slug"] == "api"
    code_of(restore_project(w.client, "acme"), 409, "not_deleted")

    _ok(
        w.client.request(
            "DELETE", at("acme") + "/api/projects/api", json={"confirm_name": "api"}
        )
    )
    code_of(purge_project(w.client, "acme", "API"), 409, "confirm_name")
    body = _ok(purge_project(w.client, "acme", "api"))
    assert body["removed"] == "api" and "soft" not in body
    assert (body["checkout_deleted"], body["history_deleted"]) == (True, True)
    assert project_row("acme", "api") is None
    assert not clone.exists() and not history.parent.exists()
    assert purge_project(w.client, "acme", "api").status_code == 404
    names = [e["event"] for e in events(audit_log)]
    assert names.count("project_restored") == 1 and names.count("project_purged") == 1
    removed = [e for e in events(audit_log) if e["event"] == "project_removed"]
    assert [e.get("via") for e in removed] == ["instance_admin", None]
    assert all(e["soft"] is True for e in removed)
    # Purged, the repository can be imported again (a new project).
    again = imported_ok(w)
    assert again.json()["project"]["slug"] == "api"
    assert released(w.state.runner)


def test_a_project_purge_cannot_race_a_restore(
    prod_world: ProdWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = prod_world
    runner = w.client.app.state.portal.runner
    pid = w.quokka.project_id
    w.sign_in("ann")
    _ok(remove_project(w.client, "quokka", w.quokka.name))
    # A Restore holding the project's claim makes the purge busy.
    with runner.reserve_removal(pid):
        code_of(purge_project(w.client, "quokka", w.quokka.name), 409, "busy")
        code_of(restore_project(w.client, "quokka"), 409, "busy")
    # Restored after the purge's first look: the check inside the claim refuses.
    from whygraph.portal import auth_routes

    real = auth_routes._bound

    def restored_first(state, project):  # noqa: ANN001, ANN202
        set_project(pid, deleted_at=None)
        return real(state, project)

    monkeypatch.setattr(auth_routes, "_bound", restored_first)
    code_of(purge_project(w.client, "quokka", w.quokka.name), 409, "not_deleted")
    assert project_by_id(pid) is not None
    assert project_db_path(w.env.data, "quokka", "api").is_file()
    assert released(runner)


def test_a_project_of_a_deleted_org_is_purged_but_not_restored(
    prod_world: ProdWorld,
) -> None:
    """D34: a project inside a soft-deleted org can be purged on its own."""
    w = prod_world
    w.sign_in("ann")
    _ok(remove_project(w.client, "quokka", w.quokka.name))
    _ok(admin_delete_org(w.client, "quokka"))
    code_of(restore_project(w.client, "quokka"), 409, "org_deleted")
    code_of(admin_delete_project(w.client, "quokka", w.quokka.name), 409, "org_deleted")
    # Live in a deleted org, too.
    set_project(w.quokka.project_id, deleted_at=None)
    body = _ok(purge_project(w.client, "quokka", w.quokka.name))
    assert body["history_deleted"] is True
    assert project_by_id(w.quokka.project_id) is None
    assert not project_db_path(w.env.data, "quokka", "api").exists()
    # The org stays deleted and can be restored, without the project.
    _ok(restore_org(w.client, "quokka"))
    w.sign_in("ann")
    assert _ok(w.client.get(at("quokka") + "/api/projects")) == {"projects": []}


def test_the_admin_routes_need_an_admin_session(prod_world: ProdWorld) -> None:
    w = prod_world
    w.sign_in("ann")
    for method, path in (
        ("DELETE", "/api/admin/orgs/quokka"),
        ("POST", "/api/admin/orgs/quokka/restore"),
        ("POST", "/api/admin/orgs/quokka/purge"),
        ("DELETE", "/api/admin/projects/quokka/api"),
        ("POST", "/api/admin/projects/quokka/api/restore"),
        ("POST", "/api/admin/projects/quokka/api/purge"),
    ):
        response = w.client.request(method, at() + path, json={})
        assert response.status_code == 401, (method, path, response.text)
    for response in (
        admin_delete_org(w.client, "nosuch"),
        purge_org(w.client, "nosuch"),
        admin_delete_project(w.client, "quokka", "x", slug="nosuch"),
        restore_project(w.client, "nosuch"),
        purge_project(w.client, "quokka", "x", slug="nosuch"),
    ):
        assert response.status_code == 404, response.text
    assert org_row("quokka").deleted_at is None


# ---------------------------------------------------------------------------
# Re-import (P14, D23) and the import picker (D47)
# ---------------------------------------------------------------------------


def test_a_soft_deleted_repository_is_refused_and_marked_in_the_picker(
    world: World,
) -> None:
    w = world
    connected(w)
    imported_ok(w)
    url = at("acme") + f"/api/github/installations/{INSTALLATION}/repos"
    flags = lambda: {  # noqa: E731
        r["id"]: (r["imported"], r["deleted"]) for r in _ok(w.client.get(url))["repos"]
    }
    assert flags()[API_REPO] == (True, False)
    _ok(
        w.client.request(
            "DELETE", at("acme") + "/api/projects/api", json={"confirm_name": "api"}
        )
    )
    assert flags()[API_REPO] == (True, True)
    assert all(not deleted for rid, (_, deleted) in flags().items() if rid != API_REPO)

    code_of(import_repo(w, API_REPO), 409, "project_deleted")


def test_an_import_racing_into_a_soft_deleted_row_answers_project_deleted(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``IntegrityError`` branch: the pre-check missed the repository's row."""
    w = world
    connected(w)
    imported_ok(w)
    pid = project_row("acme", "api").id
    _ok(
        w.client.request(
            "DELETE", at("acme") + "/api/projects/api", json={"confirm_name": "api"}
        )
    )
    set_project(pid, github_repo_id=None)  # the pre-check finds nothing
    real = w.state.import_org.hit

    def hit(org_id: int):  # noqa: ANN202 -- runs between the pre-check and the insert
        set_project(pid, github_repo_id=API_REPO)
        return real(org_id)

    monkeypatch.setattr(w.state.import_org, "hit", hit)
    code_of(import_repo(w, API_REPO), 409, "project_deleted")
    with portal_db.get_session() as session:
        rows = session.exec(
            select(Project.slug).where(Project.github_repo_id == API_REPO)
        ).all()
    assert rows == ["api"]
    # A live row answers duplicate, as before.
    set_project(pid, deleted_at=None)
    code_of(import_repo(w, API_REPO), 409, "duplicate")


def test_another_repository_deriving_the_same_slug_is_imported(world: World) -> None:
    w = world
    connected(w)
    imported_ok(w)
    _ok(
        w.client.request(
            "DELETE", at("acme") + "/api/projects/api", json={"confirm_name": "api"}
        )
    )
    w.server.add_repo(
        MIRROR_REPO,
        "acme/api-mirror",
        INSTALLATION,
        readers={"ben": {"pull": True}},
        files={"README.md": "mirror\n"},
    )
    other = w.client.post(
        at("acme") + "/api/projects",
        json={
            "source": "github",
            "installation_id": INSTALLATION,
            "repo_id": MIRROR_REPO,
            "name": "api",
        },
    )
    assert wait_import(w, other)["status"] == "ok"
    assert other.json()["project"]["slug"] == "api-2"
    assert project_row("acme", "api").deleted_at is not None


# ---------------------------------------------------------------------------
# Users: soft delete, restore, erase (plan 59 section 4.3.1)
# ---------------------------------------------------------------------------


def delete_user(
    client, uid: str, confirm: str, *, as_id: int | None = None
) -> httpx.Response:  # noqa: ANN001
    return admin_request(
        client, "DELETE", f"/api/admin/users/{uid}", {"confirm": confirm}, user_id=as_id
    )


def restore_user(client, uid: str) -> httpx.Response:  # noqa: ANN001
    return admin_request(client, "POST", f"/api/admin/users/{uid}/restore")


def erase_user(client, uid: str, confirm: str) -> httpx.Response:  # noqa: ANN001
    return admin_request(
        client, "POST", f"/api/admin/users/{uid}/erase", {"confirm": confirm}
    )


def user_row(user_id: int) -> User:
    with portal_db.get_session() as session:
        row = session.get(User, user_id)
        session.expunge(row)
        return row


def set_user(user_id: int, **values: object) -> None:
    with portal_db.get_session() as session:
        session.exec(update(User).where(col(User.id) == user_id).values(**values))


def memberships_of(user_id: int) -> dict[int, tuple[str, str, bool]]:
    """``{org_id: (role, created_at, welcome_pending)}`` of a user."""
    with portal_db.get_session() as session:
        return {
            m.org_id: (m.role, m.created_at, m.welcome_pending)
            for m in session.exec(
                select(Membership).where(Membership.user_id == user_id)
            ).all()
        }


def sessions_of(user_id: int) -> list[str]:
    """The ``kind`` of every session row of a user."""
    with portal_db.get_session() as session:
        return list(
            session.exec(
                select(UserSession.kind).where(UserSession.user_id == user_id)
            ).all()
        )


def add_member(t: SimpleNamespace, owner: str, org: str, login: str, role: str) -> None:
    """``owner`` adds ``login`` to ``org`` through the Members route."""
    be_in_team(t, owner)
    response = t.client.post(
        at(org) + "/api/org/members", json={"github_login": login, "role": role}
    )
    assert response.status_code == 201, response.text


def new_org(t: SimpleNamespace, owner: str, slug: str) -> int:
    be_in_team(t, owner)
    assert create_org(t.client, slug, slug.title()).status_code == 201
    return org_row(slug).id  # type: ignore[union-attr]


def admin_cookie(client, token: str) -> str:  # noqa: ANN001
    base = client.app.state.portal.base_url
    return f"{sessions_mod.admin_cookie_name(base)}={token}"


def _admin_token(user_id: int) -> str:
    with portal_db.get_session() as session:
        return sessions_mod.create_session(session, user_id, "pytest", kind="admin")


def _everyday_token(user_id: int) -> str:
    with portal_db.get_session() as session:
        return sessions_mod.create_session(session, user_id, "pytest")


def _concurrently(calls: list) -> list[httpx.Response]:  # noqa: ANN001
    """Run every ``call`` (a no-argument function) at once; their responses."""
    barrier = threading.Barrier(len(calls))
    results: list[httpx.Response | None] = [None] * len(calls)

    def run(index: int) -> None:
        barrier.wait()
        results[index] = calls[index]()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(calls))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(r is not None for r in results)
    return results  # type: ignore[return-value]


def _while_deleting(user_id: int, call) -> httpx.Response:  # noqa: ANN001
    """Run ``call`` while a soft delete of ``user_id`` holds its row, then commit.

    The test's own transaction takes the user row ``FOR NO KEY UPDATE`` and
    sets ``deleted_at`` the way the soft delete does; ``call`` starts in a
    thread (its principal resolves, the row is still live to it) and must
    wait at its ``FOR SHARE`` read until the transaction commits.
    """
    result: list[httpx.Response] = []
    thread = threading.Thread(target=lambda: result.append(call()))
    with portal_db.get_session() as session:
        session.exec(
            select(User.id).where(User.id == user_id).with_for_update(key_share=True)
        ).one()
        session.exec(
            update(User).where(col(User.id) == user_id).values(deleted_at=DELETED_AT)
        )
        thread.start()
        thread.join(timeout=1.0)
        assert thread.is_alive(), "the request did not wait for the user row"
    thread.join(timeout=30)
    (response,) = result
    return response


def test_user_soft_delete_takes_the_snapshot_and_ends_everything(
    team: SimpleNamespace, tmp_path, audit_log: pytest.LogCaptureFixture
) -> None:  # noqa: ANN001
    t = team
    cy, cy_uid = t.ids["cy"], t.uids["cy"]
    add_member(t, "ben", "acme", "cy", "admin")
    bravo = new_org(t, "dee", "bravo")
    add_member(t, "dee", "bravo", "cy", "member")
    new_org(t, "eve", "charlie")
    before = memberships_of(cy)
    assert set(before) == {t.org_id, bravo}
    # Cy is also a GitHub instance admin, with a reader grant on charlie.
    set_user(cy, is_instance_admin=True)
    grant_reader(t.client, cy_uid, "charlie")
    project_id = _insert_project(t.org_id, "api", "Api", tmp_path / "api", t.ids["ben"])
    with portal_db.get_session() as session:
        token = connections.issue(
            session,
            user=session.get(User, cy),
            project=session.get(Project, project_id),
            client_name="cy-laptop",
        )
    _everyday_token(cy)
    _admin_token(cy)
    t.client.cookies.clear()

    response = delete_user(t.client, cy_uid, "cy")
    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": cy_uid, "orgs": ["acme", "bravo"]}
    row = user_row(cy)
    assert row.deleted_at is not None and row.deleted_by == t.ids["ada"]
    assert row.is_instance_admin is False
    # The snapshot is exactly what the membership rows held.
    assert json.loads(row.deleted_memberships) == [
        {
            "org_id": org_id,
            "role": role,
            "created_at": created_at,
            "welcome_pending": welcome,
        }
        for org_id, (role, created_at, welcome) in sorted(before.items())
    ]
    assert memberships_of(cy) == {}
    assert sessions_of(cy) == []
    with portal_db.get_session() as session:
        reason = session.exec(
            select(ConnectionToken.revoked_reason).where(
                ConnectionToken.token_hash == connections.hash_token(token)
            )
        ).one()
        grants = session.exec(
            select(ReaderGrant.revoked_at).where(ReaderGrant.user_id == cy)
        ).all()
    assert reason == "user_deleted"
    assert grants and all(revoked_at is not None for revoked_at in grants)
    # A session made afterwards resolves to nobody: lookup refuses the row.
    signed_in(t.client, cy)
    assert t.client.get(at() + "/api/account").status_code == 401
    t.client.cookies.clear()
    admin_signed_in(t.client, cy)
    assert t.client.get(at() + "/api/admin/me").status_code == 401
    t.client.cookies.clear()

    seen = events(audit_log)
    (deleted,) = [e for e in seen if e["event"] == "user_deleted"]
    assert deleted["uid"] == t.uids["ada"] and deleted["target"] == cy_uid
    removed = [e for e in seen if e["event"] == "member_removed"]
    assert {(e["org"], e["role"]) for e in removed} == {
        ("acme", "admin"),
        ("bravo", "member"),
    }
    for event in removed:
        assert event["target"] == cy_uid and event["via"] == "instance_admin"
        assert event["reason"] == "user_deleted" and event["org_id"] is not None
    (revoked,) = [e for e in seen if e["event"] == "reader_grant_revoked"]
    assert revoked["org"] == "charlie" and revoked["reason"] == "user_deleted"


def test_user_soft_delete_refuses_self_confirm_and_the_last_owner(
    team: SimpleNamespace,
) -> None:
    t = team
    code_of(delete_user(t.client, t.uids["ada"], "ada@example.com"), 409, "self_delete")
    code_of(delete_user(t.client, t.uids["cy"], "nope"), 409, "confirm_user")
    code_of(delete_user(t.client, t.uids["cy"], ""), 409, "confirm_user")
    assert user_row(t.ids["cy"]).deleted_at is None
    # A password account is named by its email.
    code_of(delete_user(t.client, t.uids["pat"], "pat"), 409, "confirm_user")
    assert delete_user(t.client, t.uids["pat"], "pat@example.com").status_code == 200
    code_of(
        delete_user(t.client, t.uids["pat"], "pat@example.com"), 409, "user_deleted"
    )
    # Ben is acme's only owner: nothing changes.
    response = delete_user(t.client, t.uids["ben"], "ben")
    code_of(response, 409, "last_owner")
    assert response.json()["orgs"] == [{"slug": "acme", "deleted": False}]
    assert user_row(t.ids["ben"]).deleted_at is None
    assert memberships_of(t.ids["ben"])[t.org_id][0] == "owner"
    # D49: the sole owner of a soft-deleted org counts too.
    new_org(t, "eve", "charlie")
    assert owner_delete_org(t.client, "charlie").status_code == 200
    response = delete_user(t.client, t.uids["eve"], "eve")
    code_of(response, 409, "last_owner")
    assert response.json()["orgs"] == [{"slug": "charlie", "deleted": True}]
    assert user_row(t.ids["eve"]).deleted_at is None
    # With a second owner, Ben can go.
    add_member(t, "ben", "acme", "finn", "owner")
    assert delete_user(t.client, t.uids["ben"], "ben").status_code == 200


def test_user_a_deleted_admin_is_not_an_enabled_admin(team: SimpleNamespace) -> None:
    t = team
    # A row that kept its flag next to deleted_at must not count, or the
    # real last admin could demote themselves (P9).
    set_user(t.ids["eve"], is_instance_admin=True, deleted_at=DELETED_AT)
    response = admin_request(
        t.client,
        "PATCH",
        f"/api/admin/users/{t.uids['ada']}",
        {"is_instance_admin": False},
    )
    code_of(response, 409, "last_admin")
    # Nor does it sign in to the admin panel.
    t.client.cookies.clear()
    admin_signed_in(t.client, t.ids["eve"])
    assert t.client.get(at() + "/api/admin/me").status_code == 401


def test_user_two_admins_deleting_each_other_leave_one(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = team
    set_user(t.ids["finn"], is_instance_admin=True)
    ada, finn = _admin_token(t.ids["ada"]), _admin_token(t.ids["finn"])
    lock_user = auth_routes._lock_user

    def slow(db, uid):  # noqa: ANN001, ANN202
        user = lock_user(db, uid)
        time.sleep(0.4)
        return user

    monkeypatch.setattr(auth_routes, "_lock_user", slow)
    t.client.cookies.clear()

    def call(token: str, uid: str, confirm: str):  # noqa: ANN202
        return lambda: t.client.request(
            "DELETE",
            at() + f"/api/admin/users/{uid}",
            json={"confirm": confirm},
            headers={"Cookie": admin_cookie(t.client, token)},
        )

    results = _concurrently(
        [
            call(ada, t.uids["finn"], "finn"),
            call(finn, t.uids["ada"], "ada@example.com"),
        ]
    )
    assert sorted(r.status_code for r in results) == [200, 409], [
        r.text for r in results
    ]
    (refused,) = (r for r in results if r.status_code == 409)
    assert refused.json()["code"] == "last_admin"
    alive = [u for u in ("ada", "finn") if user_row(t.ids[u]).deleted_at is None]
    assert len(alive) == 1 and user_row(t.ids[alive[0]]).is_instance_admin


def test_user_an_admin_patch_racing_a_soft_delete_does_not_deadlock(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = team
    set_user(t.ids["finn"], is_instance_admin=True)
    add_member(t, "ben", "acme", "cy", "member")
    ada, finn = _admin_token(t.ids["ada"]), _admin_token(t.ids["finn"])
    lock_owners = auth_routes._lock_owners

    def slow(db, org_id):  # noqa: ANN001, ANN202
        owners = lock_owners(db, org_id)
        time.sleep(0.4)
        return owners

    monkeypatch.setattr(auth_routes, "_lock_owners", slow)
    t.client.cookies.clear()
    path = at() + f"/api/admin/users/{t.uids['cy']}"
    results = _concurrently(
        [
            lambda: t.client.request(
                "DELETE",
                path,
                json={"confirm": "cy"},
                headers={"Cookie": admin_cookie(t.client, ada)},
            ),
            lambda: t.client.request(
                "PATCH",
                path,
                json={"disabled": True},
                headers={"Cookie": admin_cookie(t.client, finn)},
            ),
            # A role change locks the owners first, then its target.
            lambda: t.client.request(
                "PATCH",
                at("acme") + f"/api/org/members/{t.uids['cy']}",
                json={"role": "admin"},
                headers={
                    "Cookie": f"{sessions_mod.COOKIE_NAME}="
                    + _everyday_token(t.ids["ben"])
                },
            ),
        ]
    )
    deleted, patched, role = results
    assert deleted.status_code == 200, deleted.text
    # Each other write either finished first or saw the deleted account.
    assert (patched.status_code, patched.json().get("code")) in (
        (200, None),
        (409, "user_deleted"),
    ), patched.text
    assert (role.status_code, role.json().get("code")) in (
        (200, None),
        (404, "not_member"),
    ), role.text
    assert user_row(t.ids["cy"]).deleted_at is not None
    assert memberships_of(t.ids["cy"]) == {}


def test_user_a_deleted_user_cannot_come_back_through_another_route(
    team: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    t = team
    add_member(t, "ben", "acme", "cy", "member")
    assert delete_user(t.client, t.uids["cy"], "cy").status_code == 200
    be_in_team(t, "ben")
    readd = t.client.post(
        at("acme") + "/api/org/members", json={"github_login": "cy", "role": "member"}
    )
    code_of(readd, 409, "user_deleted")
    refused = [e for e in events(audit_log) if e["event"] == "member_add_refused"]
    assert refused[-1]["reason"] == "user_deleted"
    role = t.client.patch(
        at("acme") + f"/api/org/members/{t.uids['cy']}", json={"role": "admin"}
    )
    code_of(role, 404, "not_member")
    transfer = t.client.post(
        at("acme") + "/api/org/transfer",
        json={"user_uid": t.uids["cy"], "confirm_slug": "acme"},
    )
    code_of(transfer, 404, "not_member")
    t.client.cookies.clear()
    promote = admin_request(
        t.client,
        "PATCH",
        f"/api/admin/users/{t.uids['cy']}",
        {"is_instance_admin": True},
    )
    code_of(promote, 409, "user_deleted")
    # A deleted password admin gets no reset link.
    pat = t.ids["pat"]
    set_user(pat, is_instance_admin=True)
    assert delete_user(t.client, t.uids["pat"], "pat@example.com").status_code == 200
    reset = admin_request(
        t.client, "POST", f"/api/admin/users/{t.uids['pat']}/reset-link"
    )
    code_of(reset, 409, "user_deleted")
    assert memberships_of(t.ids["cy"]) == {}


def test_user_member_add_and_org_create_wait_for_a_running_soft_delete(
    team: SimpleNamespace,
) -> None:
    t = team
    eve = _everyday_token(t.ids["eve"])
    created = _while_deleting(
        t.ids["eve"],
        lambda: t.client.post(
            at() + "/api/orgs",
            json={"slug": "late", "name": "Late"},
            headers={"Cookie": f"{sessions_mod.COOKIE_NAME}={eve}"},
        ),
    )
    code_of(created, 409, "user_deleted")
    assert org_row("late") is None
    ben = _everyday_token(t.ids["ben"])
    added = _while_deleting(
        t.ids["finn"],
        lambda: t.client.post(
            at("acme") + "/api/org/members",
            json={"github_login": "finn", "role": "member"},
            headers={"Cookie": f"{sessions_mod.COOKIE_NAME}={ben}"},
        ),
    )
    code_of(added, 409, "user_deleted")
    assert memberships_of(t.ids["finn"]) == {}


def test_user_sign_in_of_a_deleted_account_is_refused_and_writes_nothing(
    team: SimpleNamespace, github_fake, audit_log: pytest.LogCaptureFixture
) -> None:  # noqa: ANN001
    t = team
    set_user(t.ids["finn"], is_instance_admin=True)
    for login in ("cy", "finn"):
        assert delete_user(t.client, t.uids[login], login).status_code == 200
    before = user_row(t.ids["cy"])
    github_fake.users["cy"].avatar_url = "https://avatars.example.test/new"
    t.client.cookies.clear()
    response = github_sign_in(t.client, "cy")
    code_of(response, 403, "account_deleted")
    after = user_row(t.ids["cy"])
    assert (after.avatar_url, after.github_login) == (
        before.avatar_url,
        before.github_login,
    )
    assert sessions_of(t.ids["cy"]) == []
    refused = [e for e in events(audit_log) if e["event"] == "github_signin_refused"]
    assert refused[-1]["reason"] == "deleted"
    # The admin panel's GitHub sign-in: the one not_an_admin, reason deleted.
    admin = github_sign_in(t.client, "finn", admin=True)
    code_of(admin, 403, "not_an_admin")
    (event,) = [e for e in events(audit_log) if e["event"] == "admin_signin_refused"]
    assert event["reason"] == "deleted"
    assert sessions_of(t.ids["finn"]) == []
    # A deleted password admin: account_deleted only after a correct password.
    password_user("root2@example.com", admin=True)
    with portal_db.get_session() as session:
        root2 = session.exec(
            select(User).where(User.email == "root2@example.com")
        ).one()
        root2_id, root2_uid = root2.id, root2.uid
    assert delete_user(t.client, root2_uid, "root2@example.com").status_code == 200
    wrong = t.client.post(
        at() + "/api/auth/admin/login",
        json={"email": "root2@example.com", "password": "not the password at all"},
    )
    code_of(wrong, 401, "bad_credentials")
    login = t.client.post(
        at() + "/api/auth/admin/login",
        json={"email": "root2@example.com", "password": PROD_PASSWORD},
    )
    code_of(login, 403, "account_deleted")
    assert sessions_of(root2_id) == []


def test_user_restore_brings_memberships_back_but_not_the_admin_flag(
    team: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    t = team
    cy, cy_uid = t.ids["cy"], t.uids["cy"]
    add_member(t, "ben", "acme", "cy", "admin")
    bravo = new_org(t, "dee", "bravo")
    add_member(t, "dee", "bravo", "cy", "member")
    charlie = new_org(t, "eve", "charlie")
    add_member(t, "eve", "charlie", "cy", "member")
    set_user(cy, is_instance_admin=True)
    before = memberships_of(cy)
    assert delete_user(t.client, cy_uid, "cy").status_code == 200
    # bravo is soft-deleted (its member comes back, D49); charlie is purged.
    be_in_team(t, "dee")
    assert owner_delete_org(t.client, "bravo").status_code == 200
    be_in_team(t, "eve")
    assert owner_delete_org(t.client, "charlie").status_code == 200
    t.client.cookies.clear()
    assert purge_org(t.client, "charlie").status_code == 200

    response = restore_user(t.client, cy_uid)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "restored": [
            {"org": "acme", "role": "admin", "deleted": False},
            {"org": "bravo", "role": "member", "deleted": True},
        ],
        "skipped": [charlie],
    }
    assert memberships_of(cy) == {k: v for k, v in before.items() if k != charlie}
    assert set(memberships_of(cy)) == {t.org_id, bravo}
    row = user_row(cy)
    assert row.deleted_at is None and row.deleted_by is None
    assert row.deleted_memberships is None
    assert row.is_instance_admin is False  # not granted again
    signed_in(t.client, cy)
    assert t.client.get(at() + "/api/account").status_code == 200
    t.client.cookies.clear()
    seen = events(audit_log)
    (restored,) = [e for e in seen if e["event"] == "user_restored"]
    assert restored["target"] == cy_uid
    added = [
        e
        for e in seen
        if e["event"] == "member_added" and e.get("reason") == "user_restored"
    ]
    assert {e["org"] for e in added} == {"acme", "bravo"}
    assert all(e["via"] == "instance_admin" for e in added)
    code_of(restore_user(t.client, cy_uid), 409, "not_deleted")
    set_user(cy, deleted_at=DELETED_AT, erased_at=DELETED_AT)
    code_of(restore_user(t.client, cy_uid), 409, "erased")


def test_user_erase_frees_the_github_id(team: SimpleNamespace) -> None:
    t = team
    code_of(erase_user(t.client, t.uids["cy"], "cy"), 409, "not_deleted")
    assert delete_user(t.client, t.uids["cy"], "cy").status_code == 200
    response = erase_user(t.client, t.uids["cy"], "cy")
    assert response.status_code == 200, response.text
    assert response.json() == {"erased": t.uids["cy"]}
    code_of(erase_user(t.client, t.uids["cy"], t.uids["cy"]), 409, "erased")
    t.client.cookies.clear()
    # The same GitHub account signs up again as a new account.
    assert github_sign_in(t.client, "cy").status_code == 200
    with portal_db.get_session() as session:
        rows = session.exec(
            select(User.uid, User.display_name, User.github_login).where(
                col(User.uid) == t.uids["cy"]
            )
        ).all()
        fresh = session.exec(select(User.uid).where(User.github_login == "cy")).one()
    assert rows == [(t.uids["cy"], "[Deleted User]", None)]
    assert fresh != t.uids["cy"]


# -- Erase in every store (the `cn` world: its projects have databases) ------


def _chat(n: SimpleNamespace, project: str, owner_uid: str, *, imported: bool) -> int:
    """A chat session with a message in ``project``'s database; its id."""
    ctx = n.state.contexts.get(n.projects[project])
    now = "2026-11-18T00:00:00+00:00"
    with use_project(ctx), project_session() as session:
        chat = ChatSession(
            title="t",
            provider="p",
            model="m",
            created_at=now,
            updated_at=now,
            owner_uid=owner_uid,
        )
        session.add(chat)
        session.flush()
        session.add(
            ChatMessage(session_id=chat.id, role="user", content="hi", created_at=now)
        )
        if imported:
            bundle = ProjectImport(
                bundle_sha256="x",
                imported_by=owner_uid,
                imported_at=now,
                source_mode="local",
                source_label="l",
                whygraph_version="2",
                counts="{}",
            )
            session.add(bundle)
            session.flush()
            session.add(
                ImportedChat(
                    origin_key=f"k{chat.id}", session_id=chat.id, import_id=bundle.id
                )
            )
        return chat.id


def _chat_ids(n: SimpleNamespace, project: str) -> set[int]:
    ctx = n.state.contexts.get(n.projects[project])
    with use_project(ctx), project_session() as session:
        return set(session.exec(select(ChatSession.id)).all())


def _audit(**fields) -> int:  # noqa: ANN003
    with portal_db.get_session() as session:
        row = AuditEvent(created_at=DELETED_AT, **fields)
        session.add(row)
        session.flush()
        return row.id


def _audit_row(event_id: int) -> AuditEvent:
    with portal_db.get_session() as session:
        row = session.get(AuditEvent, event_id)
        session.expunge(row)
        return row


def test_user_erase_wipes_every_store(cn: SimpleNamespace) -> None:
    n = cn
    cy, cy_uid, eve = n.ids["cy"], n.uids["cy"], n.ids["eve"]
    ben = n.ids["ben"]
    acme, bravo = n.orgs["acme"], n.orgs["bravo"]
    # Audit rows that name Cy, and ones that must stay.
    as_actor = _audit(
        event="login_success",
        actor_id=cy,
        actor_label="@cy",
        ip="1.1.1.1",
        fields={"github_login": "cy", "email": "cy@example.com", "reason": "x"},
    )
    as_target = _audit(
        event="member_added",
        actor_id=ben,
        actor_label="@ben",
        ip="2.2.2.2",
        target=cy_uid,
        org_id=acme,
        fields={"github_login": "cy", "role": "member"},
    )
    refused = _audit(
        event="github_signin_refused",
        ip="3.3.3.3",
        fields={"github_login": "CY", "reason": "deleted"},
    )
    by_id = _audit(
        event="invitation_created",
        actor_id=ben,
        actor_label="@ben",
        ip="2.2.2.3",
        org_id=acme,
        fields={"github_id": 5101, "role": "member"},
    )
    # Cy once held "oldcy"; it was released to Eve, who used it since.
    old_named = _audit(
        event="member_add_refused",
        ip="4.4.4.4",
        fields={"github_login": "oldcy", "client_name": "box"},
    )
    release = _audit(
        event="github_login_released",
        ip="5.5.5.5",
        target=cy_uid,
        fields={"github_login": "oldcy"},
    )
    takers = [
        _audit(
            event="github_signin",
            actor_id=eve,
            actor_label="@oldcy",
            ip="6.6.6.6",
            fields={"github_login": "oldcy"},
        ),
        _audit(
            event="github_signin_refused",
            ip="7.7.7.7",
            fields={"github_login": "oldcy", "reason": "2fa_required"},
        ),
    ]
    other = _audit(
        event="login_success",
        actor_id=ben,
        actor_label="@ben",
        ip="8.8.8.8",
        fields={"github_login": "ben", "email": "ben@example.com"},
    )
    with portal_db.get_session() as session:
        session.add(
            UsageEvent(
                org_id=acme,
                project_id=n.projects["api"],
                project_slug="api",
                project_name="Api",
                actor_kind="member",
                user_id=cy,
                actor_label="Cy (@cy)",
                source="agent",
                task="chat",
                client_name="cy-laptop",
                provider="anthropic",
                model_requested="m",
                key_scope="org",
                cost_source="estimated",
            )
        )
        for org_id, github_id, redeemed_by in (
            (bravo, 5101, None),
            (acme, 99999, cy),
            (bravo, 5102, None),  # Dee's: stays
        ):
            session.add(
                Invitation(
                    uid=f"inv-{org_id}-{github_id}",
                    org_id=org_id,
                    github_id=github_id,
                    github_login=f"x{github_id}",
                    role="member",
                    invited_by=ben,
                    created_at=DELETED_AT,
                    expires_at="2099-01-01T00:00:00+00:00",
                    redeemed_by=redeemed_by,
                )
            )
        session.add(
            ReaderGrant(
                user_id=cy, org_id=bravo, expires_at="2099-01-01T00:00:00+00:00"
            )
        )
    issue(n, "cy", "api", "cy-laptop")
    issue(n, "ben", "api", "ben-laptop")
    cy_chats = {
        _chat(n, "api", cy_uid, imported=False),
        _chat(n, "lib", cy_uid, imported=True),  # bravo: an org Cy never joined
    }
    ben_chat = _chat(n, "api", n.uids["ben"], imported=False)
    # A project without Initialize, and one whose database file is gone.
    root = n.state.data_dir / "never"
    with portal_db.get_session() as session:
        session.add(
            Project(
                org_id=acme,
                slug="never",
                name="Never",
                source="github",
                root=str(root),
                created_by=ben,
            )
        )
        session.add(
            Project(
                org_id=acme,
                slug="gone",
                name="Gone",
                source="github",
                root=str(root) + "-gone",
                created_by=ben,
                initialized_at="2026-10-03T00:00:00+00:00",
            )
        )
    data = n.state.data_dir
    assert delete_user(n.client, cy_uid, "cy").status_code == 200
    # A row still queued in the writer is written before the rewrite.
    writer = audit_mod.current_writer()
    assert writer is not None
    writer.submit(
        created_at=DELETED_AT,
        event="logout",
        uid=cy_uid,
        ip="9.9.9.9",
        fields={"github_login": "cy"},
    )

    response = erase_user(n.client, cy_uid, "cy")
    assert response.status_code == 200, response.text

    row = user_row(cy)
    assert row.display_name == "[Deleted User]" and row.erased_at is not None
    assert (row.email, row.github_id, row.github_login, row.avatar_url) == (
        None,
        None,
        None,
        None,
    )
    assert row.password_hash is None and row.deleted_memberships is None
    assert row.uid == cy_uid and row.deleted_at is not None
    actor = _audit_row(as_actor)
    assert (actor.actor_label, actor.ip) == ("[Deleted User]", None)
    assert actor.fields == {"reason": "x"}
    target = _audit_row(as_target)
    assert target.ip == "2.2.2.2" and target.fields == {"role": "member"}
    assert _audit_row(refused).ip is None
    assert _audit_row(refused).fields == {"reason": "deleted"}
    assert _audit_row(by_id).fields == {"role": "member"}
    assert _audit_row(by_id).ip == "2.2.2.3"
    assert (_audit_row(old_named).ip, _audit_row(old_named).fields) == (None, {})
    # The release names Cy as its target (stripped) but carries Eve's address.
    assert (_audit_row(release).ip, _audit_row(release).fields) == ("5.5.5.5", {})
    for taker, ip in zip(takers, ("6.6.6.6", "7.7.7.7"), strict=True):
        kept = _audit_row(taker)
        assert kept.ip == ip and kept.fields["github_login"] == "oldcy"
    assert _audit_row(takers[0]).actor_label == "@oldcy"
    untouched = _audit_row(other)
    assert untouched.ip == "8.8.8.8" and untouched.fields["email"] == "ben@example.com"
    with portal_db.get_session() as session:
        queued = session.exec(
            select(AuditEvent).where(
                AuditEvent.event == "logout", AuditEvent.created_at == DELETED_AT
            )
        ).one()
        assert (queued.actor_label, queued.ip, queued.fields) == (
            "[Deleted User]",
            None,
            {},
        )
        usage = session.exec(select(UsageEvent).where(UsageEvent.user_id == cy)).one()
        assert (usage.actor_label, usage.client_name) == ("[Deleted User]", None)
        left = {i.uid for i in session.exec(select(Invitation)).all()}
        assert left == {f"inv-{bravo}-5102"}
        tokens = session.exec(select(ConnectionToken.user_id)).all()
        assert cy not in tokens and ben in tokens
        assert (
            session.exec(select(ReaderGrant).where(ReaderGrant.user_id == cy)).all()
            == []
        )
        assert "cy-laptop" not in str(
            session.exec(select(UsageEvent.client_name)).all()
        )
    assert _chat_ids(n, "api") == {ben_chat}
    assert not cy_chats & (_chat_ids(n, "api") | _chat_ids(n, "lib"))
    # No database was created for a project without one.
    assert not project_db_path(data, "acme", "never").exists()
    assert not project_db_path(data, "acme", "gone").exists()


def test_user_erase_is_all_or_nothing(
    cn: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = cn
    cy, cy_uid = n.ids["cy"], n.uids["cy"]
    code_of(erase_user(n.client, cy_uid, "cy"), 409, "not_deleted")
    assert delete_user(n.client, cy_uid, "cy").status_code == 200
    code_of(erase_user(n.client, cy_uid, "nope"), 409, "confirm_user")
    chat = _chat(n, "api", cy_uid, imported=False)

    def intact() -> None:
        row = user_row(cy)
        assert row.erased_at is None and row.github_login == "cy"
        assert chat in _chat_ids(n, "api")

    # A writer that cannot flush: 503 busy, before anything changed.
    with monkeypatch.context() as patched:
        patched.setattr(n.state.usage_writer, "flush", lambda *a, **k: False)
        code_of(erase_user(n.client, cy_uid, "cy"), 503, "busy")
    intact()
    # A project database that cannot be opened stops it (D54).
    lib = project_db_path(n.state.data_dir, "bravo", "lib")
    moved = lib.with_name("whygraph.real.db")
    lib.rename(moved)
    lib.symlink_to(moved)
    try:
        response = erase_user(n.client, cy_uid, "cy")
        code_of(response, 409, "erase_blocked")
        assert response.json()["projects"] == ["bravo/lib"]
        intact()
    finally:
        lib.unlink()
        moved.rename(lib)
    # A Restore that commits while the chats are deleted: the portal step
    # re-checks under its row lock and refuses.
    erase_chats = auth_routes._erase_chats

    def restored_meanwhile(projects, uid):  # noqa: ANN001, ANN202
        erase_chats(projects, uid)
        set_user(cy, deleted_at=None)

    monkeypatch.setattr(auth_routes, "_erase_chats", restored_meanwhile)
    code_of(erase_user(n.client, cy_uid, "cy"), 409, "not_deleted")
    row = user_row(cy)
    assert row.erased_at is None and row.github_login == "cy"


def test_user_org_pages_show_deleted_user_and_admin_pages_the_real_identity(
    cn: SimpleNamespace,
) -> None:
    n = cn
    cy, cy_uid, ben = n.ids["cy"], n.uids["cy"], n.ids["ben"]
    acme = n.orgs["acme"]
    _audit(
        event="login_success",
        actor_id=cy,
        actor_label="@cy",
        ip="1.1.1.1",
        org_id=acme,
        org_slug="acme",
        fields={"github_login": "cy", "email": "cy@example.com"},
    )
    _audit(
        event="member_added",
        actor_id=ben,
        actor_label="@ben",
        org_id=acme,
        org_slug="acme",
        target=cy_uid,
        fields={"github_login": "cy"},
    )
    by_id = _audit(
        event="invitation_created",
        actor_id=ben,
        actor_label="@ben",
        org_id=acme,
        org_slug="acme",
        fields={"github_id": 5101, "github_login": "cy"},
    )
    with portal_db.get_session() as session:
        session.add(
            UsageEvent(
                org_id=acme,
                project_id=n.projects["api"],
                project_slug="api",
                project_name="Api",
                actor_kind="member",
                user_id=cy,
                actor_label="Cy (@cy)",
                source="chat",
                task="chat",
                provider="anthropic",
                model_requested="m",
                key_scope="org",
                cost_source="estimated",
            )
        )
        session.add(
            ScanRun(
                project_id=n.projects["api"],
                trigger="manual",
                requested_by=cy,
                status="ok",
                queued_at=DELETED_AT,
            )
        )
        session.add(
            Invitation(
                uid="inv-by-cy",
                org_id=acme,
                github_id=777,
                github_login="newbie",
                role="member",
                invited_by=cy,
                created_at=DELETED_AT,
                expires_at="2099-01-01T00:00:00+00:00",
            )
        )
    issue(n, "cy", "api", "cy-laptop")
    assert delete_user(n.client, cy_uid, "cy").status_code == 200
    be(n, "ben")
    acme_at = at("acme")

    audit_page = _ok(n.client.get(acme_at + "/api/org/audit"))["events"]
    mine = [
        e
        for e in audit_page
        if (e["actor"] or {}).get("uid") == cy_uid
        or e["target"] == cy_uid
        or e["id"] == by_id
    ]
    assert len(mine) >= 3
    for event in mine:
        if (event["actor"] or {}).get("uid") == cy_uid:
            assert event["actor"]["label"] == "[Deleted User]"
        if event["target"] == cy_uid:
            assert event["target_label"] == "[Deleted User]"
        for key in ("github_login", "email"):
            if key in event["fields"]:
                assert event["fields"][key] == "[Deleted User]"
    assert "@cy" not in str(mine) and "cy@example.com" not in str(mine)
    # The actor filter no longer finds the deleted user by login.
    found = _ok(n.client.get(acme_at + "/api/org/audit", params={"actor": "@cy"}))
    assert found["events"] == []
    audit_csv = n.client.get(acme_at + "/api/org/audit.csv")
    assert audit_csv.status_code == 200
    assert "[Deleted User]" in audit_csv.text
    assert "@cy" not in audit_csv.text and "cy@example.com" not in audit_csv.text

    calls = _ok(n.client.get(acme_at + "/api/usage/calls"))["items"]
    (call,) = [c for c in calls if c["user_uid"] == cy_uid]
    assert call["actor_label"] == "[Deleted User]"
    usage_csv = n.client.get(acme_at + "/api/usage.csv")
    assert usage_csv.status_code == 200
    assert "[Deleted User]" in usage_csv.text and "@cy" not in usage_csv.text
    groups = _ok(n.client.get(acme_at + "/api/usage", params={"group": "member"}))
    (group,) = [g for g in groups["groups"] if g["key"] == cy_uid]
    assert group["label"] == "[Deleted User]"

    tokens = _ok(
        n.client.get(acme_at + "/api/projects/api/connections?include=revoked")
    )
    (token,) = [r for r in tokens if r["client_name"] == "cy-laptop"]
    assert (token["user_name"], token["user_login"]) == ("[Deleted User]", None)
    runs = _ok(n.client.get(acme_at + "/api/projects/api/scans"))
    runs = runs["runs"] if isinstance(runs, dict) else runs
    (run,) = [r for r in runs if r["requested_by"] is not None]
    assert run["requested_by"]["label"] == "[Deleted User]"
    invitations = _ok(n.client.get(acme_at + "/api/org/invitations"))
    (invitation,) = [i for i in invitations if i["uid"] == "inv-by-cy"]
    assert invitation["invited_by"]["display_name"] == "[Deleted User]"
    assert invitation["invited_by"]["github_login"] is None
    # Notes and transfer read the same names.
    assert notes_routes._names({cy_uid}) == {cy_uid: "[Deleted User]"}
    assert transfer_export._user_names({cy_uid}) == {cy_uid: "[Deleted User]"}
    assert actor_label(user_row(cy), production=True) == "[Deleted User]"
    n.client.cookies.clear()

    # The admin panel shows the real identity until Erase.
    detail = _ok(admin_request(n.client, "GET", f"/api/admin/users/{cy_uid}"))
    assert detail["display_name"] == "Cy" and detail["github_login"] == "cy"
    labels = {(e["actor"] or {}).get("label") for e in detail["events"]}
    assert "@cy" in labels
    assert any(e["fields"].get("github_login") == "cy" for e in detail["events"])
    raw, _next = audit_store.query_events(
        audit_store.make_filter(org_id=acme), mask_deleted=False
    )
    assert any((e["actor"] or {}).get("label") == "@cy" for e in raw)
