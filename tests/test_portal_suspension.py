"""Org suspension (plan 59 section 4.5, D6, D15, D33, D35, D36).

A suspended org keeps its data readable and refuses everything else with
``403 org_suspended``. The request gates are checked from the route
inventory itself: every non-``GET`` org-scoped route of
:data:`~test_portal_app.ROUTE_ACTIONS` is called on a suspended org with
the body its isolation sweep already sends
(:data:`~test_portal_tenancy.ROUTE_REQUESTS`), so a new route joins the
sweep without an edit here. The gates the inventory cannot see (plan
section 3, P6) each have an explicit case: ``/api/v1`` and its one
exemption, the connect flow, invitation redemption at sign-in, the
import's own org check, the runner gate, the reconcile and webhook
selectors, queued runs at start and at dispatch, the LLM binding and the
write-free ``permissions``.

Three worlds are reused: :func:`~test_portal_hosts_isolation.prod_world`
(two orgs over real sessions, each with a scanned GitHub project),
:func:`~test_portal_connect.cn` (connection tokens) and
:func:`~test_portal_github_import.world` (a real GitHub App import).
"""

# ruff: noqa: F811 -- pytest fixtures are imported from other test modules

from __future__ import annotations

import json
import threading
from functools import partial
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import update
from sqlmodel import col, select

from test_portal_app import (  # noqa: F401 -- fixtures
    admin_signed_in,
    at,
    env,
    github_sign_in,
    grant_reader,
    production_env,
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
    self_revoke,
    token_rows,
    validate,
)
from test_portal_github_import import (  # noqa: F401 -- fixtures
    API_REPO,
    World,
    app_env,
    connected,
    github_app_key,
    github_git_server,
    import_repo,
    project_row,
    world,
)
from test_portal_hosts_isolation import (  # noqa: F401 -- `prod_world` is a fixture
    GITHUB_IDS,
    PROD_API_ROUTES,
    PRODUCTION_REFUSALS,
    ProdWorld,
    _call,
    _wait_idle,
    prod_world,
)
from test_portal_identity_routes import audit_log, events  # noqa: F401 -- fixture
from test_portal_tenancy import ROUTE_REQUESTS, TERMINAL, _ok, wait_for
from whygraph.portal import auth_routes, sessions, webhook
from whygraph.portal import db as portal_db
from whygraph.portal import routes as routes_mod
from whygraph.portal import runner as runner_mod
from whygraph.portal import v1_routes
from whygraph.portal.authz import Action
from whygraph.portal.deps import ApiError, bind_project, load_org_access
from whygraph.portal.models import (
    Invitation,
    Membership,
    Organization,
    ScanRun,
    User,
)
from whygraph.portal.paths import project_db_path
from whygraph.portal.runner import OrgSuspended
from whygraph.mcp.rationale import GenerationNotPermitted
from whygraph.serve.errors import whygraph_error_handler

REASON = "Unpaid invoice: talk to billing"
SUSPENDED_PERMISSIONS = ["project.read", "project.access", "project.usage"]
"""An owner's ``permissions`` while suspended: the reads, in declaration order."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def as_admin(
    client, admin_id: int, method: str, path: str, body: dict | None = None
) -> httpx.Response:  # noqa: ANN001
    """Call an ``/api/admin`` route with a fresh admin session of ``admin_id``.

    The admin cookie is dropped afterwards, so the client's everyday
    session (if any) is all that is left, as in :func:`grant_reader`.
    """
    base = client.app.state.portal.base_url
    admin_signed_in(client, admin_id)
    try:
        return client.request(method, at() + path, json=body)
    finally:
        client.cookies.delete(sessions.admin_cookie_name(base), domain=base.host)


def suspend(client, admin_id: int, org: str, reason: str = REASON) -> httpx.Response:  # noqa: ANN001
    return as_admin(
        client, admin_id, "POST", f"/api/admin/orgs/{org}/suspend", {"reason": reason}
    )


def unsuspend(client, admin_id: int, org: str) -> httpx.Response:  # noqa: ANN001
    return as_admin(client, admin_id, "POST", f"/api/admin/orgs/{org}/unsuspend")


def refused(response: httpx.Response, where: object = None) -> None:
    """Assert the suspension's ``403 org_suspended``."""
    assert response.status_code == 403, (where, response.text)
    assert response.json()["code"] == "org_suspended", (where, response.text)


def set_org(slug: str, **values: object) -> None:
    """Write ``organizations`` columns directly (a concurrent change, not a route)."""
    with portal_db.get_session() as session:
        session.exec(
            update(Organization).where(col(Organization.slug) == slug).values(**values)
        )


def org_row(slug: str) -> Organization:
    with portal_db.get_session() as session:
        row = session.exec(select(Organization).where(Organization.slug == slug)).one()
        session.expunge(row)
        return row


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


def ada(w: ProdWorld) -> int:
    """``prod_world``'s instance admin (a GitHub account in no org)."""
    return w.ids["ada"]


def password_admin_id() -> int:
    """The password admin who claimed a ``cn`` / ``world`` instance."""
    with portal_db.get_session() as session:
        return session.exec(
            select(User.id).where(User.email == "ada@example.com")
        ).one()


# ---------------------------------------------------------------------------
# The admin routes
# ---------------------------------------------------------------------------


def test_suspend_and_unsuspend_answer_and_audit_once(
    prod_world: ProdWorld, audit_log: pytest.LogCaptureFixture
) -> None:
    w = prod_world
    body = _ok(suspend(w.client, ada(w), "quokka"))
    row = org_row("quokka")
    assert body == {
        "slug": "quokka",
        "suspended": {"at": row.suspended_at, "reason": REASON},
    }
    assert (row.suspended_reason, row.suspended_by) == (REASON, w.ids["ada"])
    # Idempotent: a second suspend changes nothing, not even the reason.
    assert _ok(suspend(w.client, ada(w), "quokka", "Another reason")) == body
    assert org_row("quokka").suspended_reason == REASON
    assert org_row("narwhal").suspended_at is None
    # Management stays open on a suspended org (D33): a reader grant.
    assert grant_reader(w.client, w.uids["ada"], "quokka")["expires_at"]

    assert _ok(unsuspend(w.client, ada(w), "quokka")) == {
        "slug": "quokka",
        "suspended": None,
    }
    assert _ok(unsuspend(w.client, ada(w), "quokka")) == {
        "slug": "quokka",
        "suspended": None,
    }
    row = org_row("quokka")
    assert (row.suspended_at, row.suspended_reason, row.suspended_by) == (
        None,
        None,
        None,
    )
    recorded = [
        e
        for e in events(audit_log)
        if e["event"] in ("org_suspended", "org_unsuspended")
    ]
    assert [(e["event"], e["org_id"], e["org"]) for e in recorded] == [
        ("org_suspended", w.quokka.org_id, "quokka"),
        ("org_unsuspended", w.quokka.org_id, "quokka"),
    ]
    assert all(e["uid"] == w.uids["ada"] for e in recorded)


def test_suspend_refuses_what_it_cannot_suspend(prod_world: ProdWorld) -> None:
    w = prod_world
    for reason in ("", "x" * 501):
        assert suspend(w.client, ada(w), "quokka", reason).status_code == 422
    assert suspend(w.client, ada(w), "nosuchorg").status_code == 404
    assert unsuspend(w.client, ada(w), "nosuchorg").status_code == 404
    assert _ok(suspend(w.client, ada(w), "quokka", "x" * 500))
    set_org("narwhal", deleted_at="2026-11-18T00:00:00+00:00")
    for response in (
        suspend(w.client, ada(w), "narwhal"),
        unsuspend(w.client, ada(w), "narwhal"),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "org_deleted"
    assert org_row("narwhal").suspended_at is None
    # Only an admin session reaches them.
    w.sign_in("ann")
    response = w.client.post(
        at() + "/api/admin/orgs/quokka/unsuspend", json={"reason": REASON}
    )
    assert response.status_code == 401, response.text


def test_suspend_runs_under_the_orgs_reservation(
    prod_world: ProdWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queued runs dropped, the running one ended, describes stopped (D35)."""
    w = prod_world
    runner = w.client.app.state.portal.runner
    pid = w.quokka.project_id
    stopped: list = []
    monkeypatch.setattr(auth_routes, "stop_describes", stopped.append)
    started = len(w.scanner.calls())
    w.scanner.hold.touch()
    w.sign_in("ann")
    scans = at("quokka") + "/api/projects/api/scans"
    first = _ok(w.client.post(scans, json={}), 202)["run_id"]
    wait_for(lambda: len(w.scanner.calls()) == started + 1)
    job = wait_for(lambda: runner._running.get(pid))
    second = _ok(w.client.post(scans, json={}), 202)["run_id"]
    assert second != first and pid in runner._pending

    _ok(suspend(w.client, ada(w), "quokka"))

    assert job.cancelled and job.done.is_set() and not job.alive()
    assert pid not in runner._pending
    statuses = {r.id: r.status for r in runs_of(pid)}
    assert statuses[second] == "cancelled"
    wait_for(lambda: {r.id: r.status for r in runs_of(pid)}[first] in TERMINAL)
    assert {r.id: r.status for r in runs_of(pid)}[first] == "cancelled"
    assert stopped == [project_db_path(w.env.data, "quokka", "api")]
    with runner._claims:
        assert not runner._deleting_orgs and not runner._removing
    w.scanner.hold.unlink()
    # The other org's runs are untouched.
    w.sign_in("bob")
    _ok(w.client.post(at("narwhal") + "/api/projects/api/scans", json={}), 202)
    _wait_idle(w, w.narwhal)


def test_suspend_is_busy_while_the_org_is_held_and_changes_nothing(
    prod_world: ProdWorld,
) -> None:
    w = prod_world
    runner = w.client.app.state.portal.runner
    org_id = w.quokka.org_id
    with runner._claims:
        runner._deleting_orgs[org_id] = "deleted"
    try:
        response = suspend(w.client, ada(w), "quokka")
    finally:
        with runner._claims:
            runner._deleting_orgs.pop(org_id)
    assert response.status_code == 409, response.text
    assert response.json() == {
        "error": "the organization is already being deleted",
        "code": "busy",
    }
    assert org_row("quokka").suspended_at is None
    # While suspending, others are told so, not that it is being deleted.
    _ok(suspend(w.client, ada(w), "quokka"))
    with runner._claims:
        runner._deleting_orgs[org_id] = "suspended"
    try:
        with pytest.raises(runner_mod.ProjectBusy, match="being suspended"):
            with runner._claim_org(org_id):
                pass
        # "Already suspended" is checked before the reservation: 200, not 409.
        assert suspend(w.client, ada(w), "quokka").status_code == 200
    finally:
        with runner._claims:
            runner._deleting_orgs.pop(org_id)


# ---------------------------------------------------------------------------
# The request gates, from the route inventory
# ---------------------------------------------------------------------------


def test_every_org_write_route_is_refused_on_a_suspended_org(
    prod_world: ProdWorld,
) -> None:
    """Every non-GET org route gives ``403 org_suspended``; reads still answer."""
    w = prod_world
    _ok(suspend(w.client, ada(w), "quokka"))
    w.sign_in("ann")
    writes = [(m, p) for (m, p) in PROD_API_ROUTES if m != "GET"]
    reads = [(m, p) for (m, p) in PROD_API_ROUTES if m == "GET"]
    assert len(writes) > 30 and len(reads) > 30
    # D15 and D35: leaving and dismissing the welcome banner are writes too.
    assert {
        ("DELETE", "/api/org/membership"),
        ("DELETE", "/api/org/welcome"),
        ("POST", "/api/projects/{slug}/chat/sessions/{session_id}/messages"),
        ("POST", "/api/projects"),
    } <= set(writes)
    for method, path in writes:
        spec = ROUTE_REQUESTS[(method, path)]
        refused(_call(w, w.quokka, method, path, spec), (method, path))
    for method, path in reads:
        spec = ROUTE_REQUESTS[(method, path)]
        response = _call(w, w.quokka, method, path, spec)
        where = (method, path, response.text)
        if (method, path) in PRODUCTION_REFUSALS:
            assert response.status_code == PRODUCTION_REFUSALS[(method, path)][0]
            continue
        assert response.status_code == (spec.prod_status or spec.status), where
    # Another org is untouched, and unsuspending reopens every gate.
    w.sign_in("bob")
    renamed = {"name": "Narwhal renamed"}
    _ok(w.client.patch(at("narwhal") + "/api/projects/api", json=renamed))
    _ok(unsuspend(w.client, ada(w), "quokka"))
    w.sign_in("ann")
    _ok(w.client.patch(at("quokka") + "/api/projects/api", json={"name": "Renamed"}))
    _ok(w.client.post(at("quokka") + "/api/projects/api/scans", json={}), 202)
    _wait_idle(w, w.quokka)


def test_members_read_a_suspended_org_with_no_write_permissions(
    prod_world: ProdWorld,
) -> None:
    w = prod_world
    q = at("quokka")
    w.sign_in("ann")
    full = _ok(w.client.get(q + "/api/projects/api"))["permissions"]
    assert set(SUSPENDED_PERMISSIONS) < set(full)
    at_ = _ok(suspend(w.client, ada(w), "quokka"))["suspended"]["at"]

    state = _ok(w.client.get(q + "/api/portal/state"))
    assert state["org"]["suspended"] == {"at": at_, "reason": REASON}
    details = _ok(w.client.get(q + "/api/projects/api"))
    assert details["permissions"] == SUSPENDED_PERMISSIONS
    assert (details["llm_block"], details["llm_block_scope"]) == ("org_suspended", None)
    (listed,) = _ok(w.client.get(q + "/api/projects"))["projects"]
    assert listed["permissions"] == SUSPENDED_PERMISSIONS
    assert listed["llm_block"] == "org_suspended"
    config = _ok(w.client.get(q + "/api/projects/api/config"))
    assert (config["read_only"], config["can_test_keys"]) == (True, False)
    defaults = _ok(w.client.get(q + "/api/portal/defaults"))
    assert (defaults["read_only"], defaults["can_test_keys"]) == (True, False)
    # The other org: no suspension, every permission.
    w.sign_in("bob")
    state = _ok(w.client.get(at("narwhal") + "/api/portal/state"))
    assert state["org"]["suspended"] is None
    assert _ok(w.client.get(at("narwhal") + "/api/projects/api"))["permissions"] == full

    _ok(unsuspend(w.client, ada(w), "quokka"))
    w.sign_in("ann")
    assert _ok(w.client.get(q + "/api/portal/state"))["org"]["suspended"] is None
    details = _ok(w.client.get(q + "/api/projects/api"))
    assert (details["permissions"], details["llm_block"]) == (full, None)
    config = _ok(w.client.get(q + "/api/projects/api/config"))
    assert (config["read_only"], config["can_test_keys"]) == (False, True)


def test_a_suspended_orgs_project_is_bound_with_the_llm_off(
    prod_world: ProdWorld,
) -> None:
    """``llm_allowed`` is what every spending surface reads, so it is off."""
    w = prod_world
    state = w.client.app.state.portal
    _ok(suspend(w.client, ada(w), "quokka"))
    access = load_org_access(w.ids["ann"], "quokka")
    assert access is not None and access.suspended
    bound = w.client.portal.call(
        bind_project, state, access, "api", Action.PROJECT_READ
    )
    assert bound.org_suspended is True
    assert bound.ctx.llm_allowed is False
    assert bound.ctx.llm_block == "org_suspended"
    _ok(unsuspend(w.client, ada(w), "quokka"))
    access = load_org_access(w.ids["ann"], "quokka")
    assert access is not None and not access.suspended
    bound = w.client.portal.call(
        bind_project, state, access, "api", Action.PROJECT_READ
    )
    assert (bound.org_suspended, bound.ctx.llm_allowed) == (False, True)


def test_a_generation_refused_for_suspension_is_a_403_on_every_surface() -> None:
    """Defence in depth behind the gates: the refusal's code and status."""
    exc = GenerationNotPermitted(reason="org_suspended")
    assert exc.code == "org_suspended"
    response = whygraph_error_handler(None, exc)  # type: ignore[arg-type]
    assert response.status_code == 403
    assert json.loads(response.body)["code"] == "org_suspended"
    with pytest.raises(ApiError) as raised:
        with v1_routes._mapped_errors():
            raise exc
    assert (raised.value.status, raised.value.code) == (403, "org_suspended")
    # The viewer's refusal is unchanged.
    assert GenerationNotPermitted().code == "generation_not_permitted"


# ---------------------------------------------------------------------------
# /api/v1 and the connect flow (base host)
# ---------------------------------------------------------------------------


def test_v1_refuses_a_suspended_org_but_a_machine_may_give_up_its_token(
    cn: SimpleNamespace,
) -> None:
    n = cn
    keep = issue(n, "ben", "api")
    drop = issue(n, "ben", "api", name="desk")
    url = at("acme") + "/api/v1/projects/api"
    n.client.cookies.clear()
    _ok(n.client.get(url, headers=bearer(keep)))

    _ok(suspend(n.client, password_admin_id(), "acme"))
    n.client.cookies.clear()
    refused(n.client.get(url, headers=bearer(keep)))
    refused(
        n.client.post(
            url + "/rationale",
            headers=bearer(keep),
            json={"file": "README.md", "line_start": 1, "line_end": 1},
        )
    )
    # The token stays valid (a 401 would make the local portal drop the link).
    assert revoked_reason(keep) is None
    # D35: the one call a suspended org answers.
    assert self_revoke(n, drop).status_code == 204
    assert revoked_reason(drop) == "removed_locally"
    # Another org's token is untouched.
    lib = issue(n, "dee", "lib")
    _ok(n.client.get(at("bravo") + "/api/v1/projects/lib", headers=bearer(lib)))

    _ok(unsuspend(n.client, password_admin_id(), "acme"))
    n.client.cookies.clear()
    _ok(n.client.get(url, headers=bearer(keep)))


def test_connect_mints_nothing_for_a_suspended_org(cn: SimpleNamespace) -> None:
    n = cn
    consented = code_for(n)  # before the suspension
    _ok(suspend(n.client, password_admin_id(), "acme"))
    be(n, "ben")
    # The reads still answer.
    _ok(validate(n, org="acme", project="api"))
    listed = _ok(n.client.get(at() + "/api/connect/projects"))
    assert {(p["org"], p["slug"]) for p in listed} >= {("acme", "api")}
    # A code is a write; so is the token.
    refused(authorize(n))
    tokens = len(token_rows())
    refused(exchange(n, consented))
    assert len(token_rows()) == tokens

    _ok(unsuspend(n.client, password_admin_id(), "acme"))
    assert exchange(n, code_for(n)).status_code == 200
    assert len(token_rows()) == tokens + 1


# ---------------------------------------------------------------------------
# Invitations (D36) and the import's own check
# ---------------------------------------------------------------------------


def test_an_invitation_of_a_suspended_org_waits(prod_world: ProdWorld) -> None:
    w = prod_world
    w.sign_in("ann")
    invited = w.client.post(
        at("quokka") + "/api/org/members",
        json={"github_login": "quokka-newcomer", "role": "member"},
    )
    assert _ok(invited, 201)["pending"] is True
    _ok(suspend(w.client, ada(w), "quokka"))

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

    _ok(unsuspend(w.client, ada(w), "quokka"))
    w.sign_out()
    assert github_sign_in(w.client, "quokka-newcomer").status_code == 200
    assert membership() is not None and open_invitations() == 0


def test_an_import_racing_a_suspension_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The import passed ``current_org`` and is about to insert when the org is suspended."""
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
    suspended = suspend(w.client, password_admin_id(), "acme")
    release.set()
    importer.join(30)

    _ok(suspended)
    refused(answers[0])
    assert project_row("acme", "api") is None
    assert not (w.repos / "acme" / "api").exists()


# ---------------------------------------------------------------------------
# The runner (P6): its gate, the background selectors, queued runs
# ---------------------------------------------------------------------------


def test_every_run_request_raises_org_suspended(prod_world: ProdWorld) -> None:
    w = prod_world
    runner = w.client.app.state.portal.runner
    pid = w.quokka.project_id
    before = len(runs_of(pid))
    _ok(suspend(w.client, ada(w), "quokka"))
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
    for request in requests:
        with pytest.raises(OrgSuspended):
            w.client.portal.call(request)
    assert len(runs_of(pid)) == before
    assert (OrgSuspended, "org_suspended") in routes_mod.SCAN_REFUSAL_CODES


def test_a_reconcile_pass_skips_a_suspended_orgs_project_and_goes_on(
    prod_world: ProdWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is minted or probed for it, and the next org's project is synced."""
    w = prod_world
    runner = w.client.app.state.portal.runner
    first, second = w.narwhal.project_id, w.quokka.project_id
    assert first < second  # the suspended one comes first in the pass
    _ok(suspend(w.client, ada(w), "narwhal"))
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
    _wait_idle(w, w.quokka)


def test_a_suspension_after_the_reconcile_selection_does_not_end_the_pass(
    prod_world: ProdWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``OrgSuspended`` is in the pass's ``except`` tuple: the next project is synced."""
    w = prod_world
    runner = w.client.app.state.portal.runner
    first, second = w.narwhal.project_id, w.quokka.project_id
    real = runner_mod._reconcile_targets

    def targets():  # noqa: ANN202
        found = real()
        set_org("narwhal", suspended_at="2026-11-18T00:00:00+00:00")
        return found

    monkeypatch.setattr(runner_mod, "_reconcile_targets", targets)
    monkeypatch.setattr(runner, "_remote_head", lambda target: "0" * 40)
    narwhal_runs = len(runs_of(first))

    w.client.portal.call(runner.reconcile)

    assert len(runs_of(first)) == narwhal_runs
    assert [r.trigger for r in runs_of(second)][-1] == "reconcile"
    _wait_idle(w, w.quokka)


def test_a_webhook_fan_out_skips_a_suspended_orgs_project_and_goes_on(
    prod_world: ProdWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = prod_world
    state = w.client.app.state.portal
    first, second = w.narwhal.project_id, w.quokka.project_id
    _ok(suspend(w.client, ada(w), "narwhal"))
    assert webhook._push_targets(GITHUB_IDS["narwhal"][1]) == []
    assert webhook._push_targets(GITHUB_IDS["quokka"][1]) == [second]
    # A suspension that lands after the selection is the runner's refusal.
    monkeypatch.setattr(webhook, "_push_targets", lambda repo_id: [first, second])
    narwhal_runs = len(runs_of(first))

    w.client.portal.call(webhook._queue, state, 0, "push")

    assert len(runs_of(first)) == narwhal_runs
    assert [r.trigger for r in runs_of(second)][-1] == "push"
    _wait_idle(w, w.quokka)


def test_queued_runs_of_a_suspended_org_are_cancelled_at_start(
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
        held, kept = (row.id for row in rows)
    set_org("quokka", suspended_at="2026-11-18T00:00:00+00:00")

    specs = runner_mod._recover_queued(production=True)

    assert [spec.run_id for spec in specs] == [kept]
    with portal_db.get_session() as session:
        assert session.get(ScanRun, held).status == "cancelled"
        assert session.get(ScanRun, kept).status == "queued"
        session.get(ScanRun, kept).status = "cancelled"  # nothing will run it


def test_a_queued_run_dispatched_after_a_suspension_is_refused(
    prod_world: ProdWorld,
) -> None:
    """A run queued before the suspension (written straight to the row) never starts."""
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
    set_org("quokka", suspended_at="2026-11-18T00:00:00+00:00")

    w.scanner.hold.unlink()

    def ended(run_id: int) -> ScanRun | None:
        row = next(r for r in runs_of(pid) if r.id == run_id)
        return row if row.status in TERMINAL else None

    assert wait_for(lambda: ended(first)).status == "ok"
    row = wait_for(lambda: ended(second))
    assert row.status == "failed"
    assert json.loads(row.summary)["error"] == runner_mod.SUSPENDED_MESSAGE
    assert len(w.scanner.calls()) == started + 1  # the second never ran
