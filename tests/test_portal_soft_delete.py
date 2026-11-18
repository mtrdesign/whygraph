"""Soft delete, restore and purge of orgs and projects (plan 59 sections 4.3.2, 4.3.3).

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

Worlds reused: :func:`~test_portal_hosts_isolation.prod_world`,
:func:`~test_portal_connect.cn` and :func:`~test_portal_github_import.world`.
"""

# ruff: noqa: F811 -- pytest fixtures are imported from other test modules

from __future__ import annotations

import threading
from decimal import Decimal
from functools import partial
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import update
from sqlmodel import col, select

from test_portal_app import (  # noqa: F401 -- fixtures
    add_local,
    admin_request,
    at,
    client,
    env,
    github_sign_in,
    history_db,
    make_repo,
    production_env,
    ready,
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
from test_portal_tenancy import ROUTE_REQUESTS, TERMINAL, _ok, _url, wait_for
from whygraph.portal import db as portal_db
from whygraph.portal import org_routes
from whygraph.portal import routes as routes_mod
from whygraph.portal import runner as runner_mod
from whygraph.portal import webhook
from whygraph.portal.deps import load_org_access, project_role_for
from whygraph.portal.models import (
    Invitation,
    Membership,
    Organization,
    Project,
    ProjectGrant,
    RetiredOrgSlug,
    ScanRun,
    User,
)
from whygraph.portal.paths import project_db_path
from whygraph.portal.runner import ProjectGone

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
