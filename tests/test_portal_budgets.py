"""Monthly budgets, the hard stop and the threshold alerts (M2f-2 sections 4.7, 4.8).

Unit tests for the :class:`~whygraph.portal.budgets.BudgetBook` (covering
budgets, the blocked scope's order, thresholds firing once, re-evaluation,
raising never re-arming); Postgres tests for the budget routes (both modes,
validation codes, the admin self / owner rule), the hard stop on every
spending surface (Explorer Generate, the backfill, chat before and during a
turn, full scans requested, queued and running, ``/api/v1``), the alerts'
rows and audit events, membership removal dropping an override, and the
``403 budget_exceeded`` contract with its linked-portal side.

Spend is put into the :class:`~whygraph.portal.usage_store.SpendBook`
directly where a test is about the budget, not the call that spent it.
"""

# ruff: noqa: F811 -- pytest fixtures are imported by name

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from test_portal_app import (  # noqa: F401 -- fixtures
    at,
    env,
    portal_client,
    production_env,
    signed_in,
)
from test_portal_identity_routes import audit_log, events  # noqa: F401
from test_portal_runner import (  # noqa: F401 -- `portal`, `scanner` are fixtures
    GOOD_USAGE,
    _scan_rows,
    cancel,
    first_scan,
    local_project,
    portal,
    run_by_id,
    scan,
    scanner,
    wait_for,
    wait_run,
)
from test_portal_usage import (  # noqa: F401 -- `local`, `stub_llm` are fixtures
    _flushed,
    _key,
    _TwoRounds,
    local,
    stub_llm,
)
from test_portal_v1 import (  # noqa: F401 -- `v1`, `generator` are fixtures
    ORG,
    SLUG,
    V1World,
    _Generator,
    call,
    generator,
    target_body,
    v1,
)
from whygraph.api_v1 import PLATFORM_BUDGET_MESSAGE, ErrorOut
from whygraph.portal import audit as audit_mod
from whygraph.portal import db as portal_db
from whygraph.portal.budgets import BudgetBook, BudgetLimit, OrgBudgets
from whygraph.portal.models import AuditEvent, BudgetAlert, User
from whygraph.portal.prices import Price
from whygraph.portal.usage_store import SpendBook, UsageRow, current_month
from whygraph.serve import chat as serve_chat

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
STAMP = NOW.isoformat(timespec="seconds")


class _Clock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


def _row(
    *,
    org_id: int = 1,
    project_id: int | None = 10,
    user_id: int | None = 100,
    cost: str | None = "1",
    created_at: str = STAMP,
    cost_source: str = "estimated",
) -> UsageRow:
    return UsageRow(
        org_id=org_id,
        project_id=project_id,
        project_slug="demo",
        project_name="Demo",
        actor_kind="system" if user_id is None else "member",
        user_id=user_id,
        actor_label="Tess",
        source="explorer",
        task="analyze",
        provider="anthropic",
        model_requested="claude-opus-4-7",
        key_scope="org",
        cost_source=cost_source,
        created_at=created_at,
        cost_usd=None if cost is None else Decimal(cost),
    )


def _limit(id_: int, scope: str, amount: str, hard: bool = True, **ids: int):
    return BudgetLimit(
        id=id_, scope=scope, monthly_usd=Decimal(amount), hard_stop=hard, **ids
    )


def _book() -> tuple[BudgetBook, SpendBook, list]:
    spend = SpendBook(clock=_Clock(NOW))
    deferred: list = []
    book = BudgetBook(spend, defer=deferred.append)
    spend.on_add = book.on_add
    return book, spend, deferred


def _crossings(deferred: list) -> list[tuple[int, int | None, int]]:
    """``(budget_id, user_id, threshold)`` of every deferred alert, in order."""
    found = []
    for job in deferred:
        alert = job.args[0]
        found.append((alert.budget.id, alert.user_id, alert.threshold))
    return found


# ---------------------------------------------------------------------------
# The budget map (unit, section 6.1 #8)
# ---------------------------------------------------------------------------


def test_covering_budgets_and_the_blocked_scope_order() -> None:
    book, spend, _ = _book()
    book.set_org(
        1,
        OrgBudgets(
            org_slug="acme",
            org=_limit(1, "org", "10"),
            member_default=_limit(2, "member_default", "2"),
            members={101: _limit(3, "member", "3", hard=False, user_id=101)},
            projects={10: _limit(4, "project", "5", project_id=10)},
        ),
    )
    state = book.budget_state(1, 10, 100)
    assert [c.scope for c in book.covering(1, 10, 100)] == ["member", "project", "org"]
    assert state.limits["member"].id == 2  # the member default
    assert book.covering(1, 10, 101)[0].budget.id == 3  # the override wins
    assert state.blocked_scope is None
    assert [c.scope for c in book.covering(1, 11, None)] == ["org"]  # system actor

    spend.add(_row(user_id=100, cost="2"))
    state = book.budget_state(1, 10, 100)
    assert state.spent == {
        "member": Decimal(2),
        "project": Decimal(2),
        "org": Decimal(2),
    }
    assert state.blocked_scope == "member"
    assert book.blocked_scope(1, 10, 102) is None  # another member: not covered

    spend.add(_row(user_id=None, cost="3"))  # the project's 5 are spent
    assert book.blocked_scope(1, 10, 102) == "project"
    assert book.blocked_scope(1, 10, 100) == "member"  # member first
    assert book.blocked_scope(1, 11, None) is None

    spend.add(_row(project_id=11, user_id=101, cost="5"))  # the org's 10 too
    # 101's soft override is over (5 of 3) but never blocks; the org does.
    assert book.blocked_scope(1, 11, 101) == "org"
    assert book.blocked_scope(1, 11, None) == "org"
    assert book.blocked_scope(2, 10, 100) is None  # another org


def test_unpriced_calls_never_count_toward_a_budget() -> None:
    book, spend, deferred = _book()
    book.set_org(1, OrgBudgets(org_slug="acme", org=_limit(1, "org", "1")))
    spend.add(_row(cost=None, cost_source="unpriced"))
    spend.add(_row(cost="5", cost_source="unpriced"))
    assert book.blocked_scope(1, 10, 100) is None
    assert deferred == []


def test_soft_budgets_only_alert() -> None:
    book, spend, deferred = _book()
    book.set_org(1, OrgBudgets(org_slug="acme", org=_limit(1, "org", "1", hard=False)))
    spend.add(_row(cost="3"))
    assert book.blocked_scope(1, 10, 100) is None
    assert _crossings(deferred) == [(1, None, 50), (1, None, 75), (1, None, 100)]


def test_thresholds_fire_once_per_budget_and_per_member() -> None:
    book, spend, deferred = _book()
    book.set_org(
        1,
        OrgBudgets(
            org_slug="acme",
            org=_limit(1, "org", "10"),
            member_default=_limit(2, "member_default", "2"),
        ),
    )
    spend.add(_row(user_id=100, cost="1"))  # 100: 50% of the default
    spend.add(_row(user_id=101, cost="1"))  # 101 crosses on their own
    spend.add(_row(user_id=None, cost="3"))  # org 5 of 10: 50%
    spend.add(_row(user_id=None, cost="0.1"))  # nothing new
    spend.add(_row(user_id=None, cost="2.5"))  # org 7.6: 75%
    spend.add(_row(user_id=100, cost="3"))  # 100 at 4 of 2; org 10.6
    assert _crossings(deferred) == [
        (2, 100, 50),
        (2, 101, 50),
        (1, None, 50),
        (1, None, 75),
        (1, None, 100),
        (2, 100, 75),
        (2, 100, 100),
    ]
    alert = deferred[0].args[0]
    assert (alert.org_slug, alert.month, alert.spent) == ("acme", "2026-10", 1)
    assert book.fired(2, 100, "2026-10") == {50, 75, 100}


def test_a_put_re_evaluates_and_raising_never_re_arms(monkeypatch) -> None:
    book, spend, deferred = _book()
    spend.add(_row(cost="6"))  # no budget yet: nothing fires
    assert deferred == []

    book.set_org(1, OrgBudgets(org_slug="acme", org=_limit(1, "org", "10")))
    book.evaluate_org(1)  # lowering (or creating) can cross with no new spend
    assert _crossings(deferred) == [(1, None, 50)]

    book.set_org(1, OrgBudgets(org_slug="acme", org=_limit(1, "org", "100")))
    book.evaluate_org(1)
    book.set_org(1, OrgBudgets(org_slug="acme", org=_limit(1, "org", "10")))
    book.evaluate_org(1)
    assert len(deferred) == 1  # raised and lowered again: 50 stays fired

    book.set_org(1, OrgBudgets(org_slug="acme", org=_limit(1, "org", "7")))
    book.evaluate_org(1)
    assert _crossings(deferred)[1:] == [(1, None, 75)]

    # Deleted and re-created: a new id, which may fire again.
    book.set_org(1, OrgBudgets(org_slug="acme", org=_limit(9, "org", "10")))
    book.evaluate_org(1)
    assert _crossings(deferred)[2:] == [(9, None, 50)]
    assert book.fired(1, None, "2026-10") == set()  # the gone budget's are dropped


def test_the_member_default_is_evaluated_per_member() -> None:
    book, spend, deferred = _book()
    spend.add(_row(user_id=100, cost="1"))
    spend.add(_row(user_id=101, cost="0.1"))
    spend.add(_row(user_id=102, cost="2"))
    book.set_org(
        1,
        OrgBudgets(
            org_slug="acme",
            member_default=_limit(2, "member_default", "2"),
            members={102: _limit(3, "member", "4", user_id=102)},
        ),
    )
    book.evaluate_org(1)
    assert sorted(_crossings(deferred)) == [(2, 100, 50), (3, None, 50)]


def test_seeded_alerts_never_fire_again() -> None:
    book, spend, deferred = _book()
    book.seed_fired([(1, None, "2026-10", 50), (1, None, "2026-10", 75)])
    book.set_org(1, OrgBudgets(org_slug="acme", org=_limit(1, "org", "1")))
    spend.add(_row(cost="0.8"))
    assert deferred == []
    spend.add(_row(cost="0.2"))
    assert _crossings(deferred) == [(1, None, 100)]


# ---------------------------------------------------------------------------
# Routes, local mode (section 6.2 #7)
# ---------------------------------------------------------------------------


def _spend(state: Any, w: SimpleNamespace, cost: str, *, user: bool = False) -> None:
    """Count (and ledger) ``cost`` against ``w``'s project, now."""
    row = _row(
        org_id=w.org_id,
        project_id=w.project_id,
        user_id=w.user_id if user else None,
        cost=cost,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    state.spend.add(row)
    state.usage_writer.submit(row)


def _alerts() -> list[tuple[int, int]]:
    with portal_db.get_session() as session:
        rows = session.exec(select(BudgetAlert).order_by(BudgetAlert.id)).all()
        return [(r.budget_id, r.threshold) for r in rows]


def test_budget_routes_set_validate_and_remove(
    local: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    client = local.client
    empty = client.get("/api/budgets").json()
    month = current_month()
    assert empty == {
        "month": month,
        "resets_at": empty["resets_at"],
        "org": None,
        "member_default": None,
        "members": [],
        "projects": [],
        "alerts": [],
        "unpriced_calls": 0,
    }
    assert empty["resets_at"] > f"{month}-28"

    for amount in (0, -1, 1_000_001, 0.001):
        refused = client.put("/api/budgets/org", json={"monthly_usd": amount})
        assert refused.status_code == 422, (amount, refused.text)
        assert refused.json()["code"] == "invalid_amount"

    put = client.put("/api/budgets/org", json={"monthly_usd": 10, "hard_stop": True})
    assert put.status_code == 200, put.text
    assert put.json() == {
        "monthly_usd": 10.0,
        "hard_stop": True,
        "spent_usd": 0.0,
        "pct": 0.0,
    }

    above = client.put("/api/projects/demo/budget", json={"monthly_usd": 20})
    assert above.status_code == 422, above.text
    assert above.json()["code"] == "budget_above_org"
    project = client.put("/api/projects/demo/budget", json={"monthly_usd": 5})
    assert project.status_code == 200, project.text
    assert (project.json()["slug"], project.json()["monthly_usd"]) == ("demo", 5.0)

    below = client.put("/api/budgets/org", json={"monthly_usd": 4})
    assert below.status_code == 409, below.text
    assert below.json()["code"] == "budget_below_children"
    assert below.json()["children"] == [
        {"scope": "project", "monthly_usd": 5.0, "slug": "demo", "name": "demo"}
    ]

    _spend(local.state, local, "2.5")
    listed = client.get("/api/budgets").json()
    assert listed["org"] == {
        "monthly_usd": 10.0,
        "hard_stop": True,
        "spent_usd": 2.5,
        "pct": 25.0,
    }
    assert listed["projects"] == [
        {
            "monthly_usd": 5.0,
            "hard_stop": False,
            "spent_usd": 2.5,
            "pct": 50.0,
            "slug": "demo",
            "name": "demo",
        }
    ]
    assert local.state.usage_writer.flush()
    listed = client.get("/api/budgets").json()
    assert [(a["scope"], a["label"], a["threshold"]) for a in listed["alerts"]] == [
        ("project", "demo", 50)
    ]

    # Local mode has no member budgets.
    for path in ("/api/budgets/member-default", "/api/budgets/members/someone"):
        assert client.put(path, json={"monthly_usd": 1}).status_code == 404

    for _ in range(2):  # idempotent
        assert client.delete("/api/projects/demo/budget").status_code == 204
    assert client.delete("/api/budgets/org").status_code == 204
    after = client.get("/api/budgets").json()
    assert (after["org"], after["projects"]) == (None, [])
    assert local.state.budgets.for_org(local.org_id) is None

    recorded = [(e["event"], e.get("scope")) for e in events(audit_log)]
    assert recorded.count(("budget_set", "org")) == 1
    assert recorded.count(("budget_set", "project")) == 1
    assert recorded.count(("budget_removed", "project")) == 1  # once, not twice
    assert recorded.count(("budget_removed", "org")) == 1
    (crossed,) = [
        e for e in events(audit_log) if e["event"] == "budget_threshold_crossed"
    ]
    assert (crossed["scope"], crossed["project"], crossed["threshold"]) == (
        "project",
        "demo",
        50,
    )
    assert (crossed["spent_usd"], crossed["budget_usd"]) == ("2.50", "5.00")


def test_the_unpriced_calls_of_the_month_are_counted(local: SimpleNamespace) -> None:
    row = _row(
        org_id=local.org_id,
        project_id=local.project_id,
        user_id=None,
        cost=None,
        cost_source="unpriced",
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    local.state.usage_writer.submit(row)
    assert local.state.usage_writer.flush()
    assert local.client.get("/api/budgets").json()["unpriced_calls"] == 1


def test_alerts_fire_once_across_a_restart_and_on_a_lowering_put(
    env: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    def spend(state: Any, org_id: int, cost: str) -> None:
        _spend(state, SimpleNamespace(org_id=org_id, project_id=None), cost)
        assert state.usage_writer.flush()

    with portal_client() as client:
        assert client.post(
            "/api/portal/setup", json={"display_name": "Tess"}
        ).is_success
        state = client.app.state.portal
        org_id = state.builtin_org_id
        put = client.put("/api/budgets/org", json={"monthly_usd": 1, "hard_stop": True})
        assert put.status_code == 200, put.text
        spend(state, org_id, "0.6")
        ((budget_id, _),) = _alerts()
        assert _alerts() == [(budget_id, 50)]

        # Lowering re-evaluates with no new spend; raising never re-arms.
        for amount in (0.7, 5, 0.7, 1):
            put = client.put(
                "/api/budgets/org", json={"monthly_usd": amount, "hard_stop": True}
            )
            assert put.status_code == 200, put.text
        assert state.usage_writer.flush()
        assert _alerts() == [(budget_id, 50), (budget_id, 75)]

    with portal_client() as client:
        state = client.app.state.portal
        assert state.budgets.fired(budget_id, None, current_month()) == {50, 75}
        spend(state, org_id, "0.2")  # 0.8 of 1: past 75 again, never re-recorded
        assert _alerts() == [(budget_id, 50), (budget_id, 75)]
        assert state.budgets.blocked_scope(org_id, None, None) is None
        spend(state, org_id, "0.2")
        assert _alerts() == [(budget_id, 50), (budget_id, 75), (budget_id, 100)]
        assert state.budgets.blocked_scope(org_id, None, None) == "org"
    recorded = [
        (e["event"], e["threshold"])
        for e in events(audit_log)
        if e["event"].startswith("budget_") and "threshold" in e
    ]
    assert recorded == [
        ("budget_threshold_crossed", 50),
        ("budget_threshold_crossed", 75),
        ("budget_threshold_crossed", 100),
        ("budget_hard_stop_engaged", 100),
    ]


# ---------------------------------------------------------------------------
# The hard stop on every surface (section 6.2 #5)
# ---------------------------------------------------------------------------


def _block_project(local: SimpleNamespace, amount: str = "1") -> None:
    _spend(local.state, local, amount)
    put = local.client.put(
        "/api/projects/demo/budget", json={"monthly_usd": amount, "hard_stop": True}
    )
    assert put.status_code == 200, put.text


def test_a_hard_stop_blocks_generate_backfill_and_chat_until_raised(
    local: SimpleNamespace,
) -> None:
    _key(local, "anthropic", project=False)
    client = local.client
    _block_project(local)
    budget_body = {"code": "budget_exceeded", "scope": "project"}

    generate = client.post(
        "/api/projects/demo/node/rationale", params={"qualified_name": "sample.fn"}
    )
    assert generate.status_code == 403, generate.text
    assert {k: generate.json()[k] for k in budget_body} == budget_body
    assert "monthly LLM budget of this project" in generate.json()["error"]

    # A read still works and backfills nothing (llm_allowed is off).
    evidence = client.get(
        "/api/projects/demo/node/evidence", params={"qualified_name": "sample.fn"}
    )
    assert evidence.status_code == 200, evidence.text
    assert [r.subject for r in _flushed(local.state)] == [None]  # _spend's alone

    session = client.post("/api/projects/demo/chat/sessions", json={}).json()
    sent = client.post(
        f"/api/projects/demo/chat/sessions/{session['id']}/messages",
        json={"content": "why?"},
    )
    assert sent.status_code == 403, sent.text
    assert {k: sent.json()[k] for k in budget_body} == budget_body
    transcript = client.get(f"/api/projects/demo/chat/sessions/{session['id']}")
    assert transcript.json()["messages"] == []  # refused before anything was written

    # Raising the budget lifts the stop on the next request.
    raised = client.put(
        "/api/projects/demo/budget", json={"monthly_usd": 2, "hard_stop": True}
    )
    assert raised.status_code == 200, raised.text
    again = client.post(
        "/api/projects/demo/node/rationale", params={"qualified_name": "sample.fn"}
    )
    assert again.status_code == 200, again.text


def test_a_chat_turn_stops_before_the_round_after_the_budget_is_spent(
    local: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _key(local, "openai", project=True)
    # 40 input tokens at $1000 / Mtok: round one costs $0.04.
    local.state.prices.set_org(
        local.org_id,
        {
            ("openai", "gpt-4o"): (
                Price(input=Decimal("1000"), output=Decimal("0")),
                "t",
            )
        },
    )
    client = local.client
    put = client.put(
        "/api/projects/demo/budget", json={"monthly_usd": 0.03, "hard_stop": True}
    )
    assert put.status_code == 200, put.text
    rounds = _TwoRounds()
    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: rounds)
    session = client.post(
        "/api/projects/demo/chat/sessions",
        json={"provider": "openai", "model": "gpt-4o"},
    ).json()
    response = client.post(
        f"/api/projects/demo/chat/sessions/{session['id']}/messages",
        json={"content": "why?"},
    )
    assert response.status_code == 200, response.text
    frames = [
        json.loads(block.removeprefix("data: "))
        for block in response.text.split("\n\n")
        if block.strip()
    ]
    assert [f["type"] for f in frames][-2:] == ["budget_exceeded", "done"]
    assert frames[-2]["scope"] == "project"
    assert rounds.calls == 1  # round two was never sent
    assert len(_flushed(local.state)) == 1
    messages = client.get(f"/api/projects/demo/chat/sessions/{session['id']}").json()[
        "messages"
    ]
    assert (messages[-1]["content"], messages[-1]["error"]) == (
        serve_chat.BUDGET_STOP_MESSAGE,
        "budget_exceeded",
    )
    # And the next message is refused up front.
    again = client.post(
        f"/api/projects/demo/chat/sessions/{session['id']}/messages",
        json={"content": "and?"},
    )
    assert again.status_code == 403, again.text


# ---------------------------------------------------------------------------
# Scans (sections 4.7 item 3, 9.2 D6)
# ---------------------------------------------------------------------------


def _ids(client: TestClient) -> SimpleNamespace:
    from whygraph.portal.models import Project

    with portal_db.get_session() as session:
        project = session.exec(select(Project).where(Project.slug == "demo")).one()
        user = session.exec(select(User)).one()
        ids = (project.org_id, project.id, user.id)
    return SimpleNamespace(
        state=client.app.state.portal, org_id=ids[0], project_id=ids[1], user_id=ids[2]
    )


def _block_scans(client: TestClient, w: SimpleNamespace, amount: str = "1") -> None:
    _spend(w.state, w, amount)
    put = client.put(
        "/api/projects/demo/budget", json={"monthly_usd": amount, "hard_stop": True}
    )
    assert put.status_code == 200, put.text


def test_a_blocked_full_scan_is_refused_while_quick_scans_and_cancel_work(
    portal: TestClient, env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    local_project(portal, env, "demo")
    first_scan(portal, "demo")
    w = _ids(portal)
    _block_scans(portal, w)

    for body in ({"trigger": "manual"}, {"trigger": "describe"}, None):
        refused = portal.post("/api/projects/demo/scans", json=body)
        assert refused.status_code == 403, (body, refused.text)
        assert (refused.json()["code"], refused.json()["scope"]) == (
            "budget_exceeded",
            "project",
        )
    quick = wait_run(portal, "demo", scan(portal, "demo", analyze=False))
    assert quick["status"] == "ok"

    scanner.hold.touch()
    running = scan(portal, "demo", analyze=False)
    wait_for(lambda: run_by_id(portal, "demo", running)["status"] == "running")
    assert cancel(portal, "demo", running).status_code == 202  # never gated
    scanner.hold.unlink()
    assert wait_run(portal, "demo", running)["status"] == "cancelled"

    raised = portal.put("/api/projects/demo/budget", json={"monthly_usd": 2})
    assert raised.status_code == 200, raised.text
    assert wait_run(portal, "demo", scan(portal, "demo", trigger="manual"))["analyze"]


def test_a_queued_full_run_blocked_at_dispatch_scans_structure_only(
    portal: TestClient, env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    local_project(portal, env, "demo")
    first_scan(portal, "demo")
    w = _ids(portal)
    scanner.hold.touch()
    running = scan(portal, "demo", trigger="hook")
    wait_for(lambda: run_by_id(portal, "demo", running)["status"] == "running")
    pending = scan(portal, "demo", trigger="manual")  # queued while not blocked
    scanner.configure(usage=json.dumps([GOOD_USAGE]))
    _block_scans(portal, w)
    scanner.hold.unlink()

    run = wait_run(portal, "demo", pending)
    assert run["status"] == "ok"
    assert run["summary"].get("analyze_skipped") == "budget", run
    assert run["summary"]["usage"]["calls"] == 0
    assert "--skip-analyze" in scanner.calls()[-1]["argv"]
    assert _scan_rows(w.state, pending) == []  # a structure-only run's events drop


def test_a_running_full_scan_is_cancelled_once_its_spend_exhausts_the_budget(
    portal: TestClient, env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    local_project(portal, env, "demo")
    first_scan(portal, "demo")
    w = _ids(portal)
    # Each event costs $0.0075: the second exhausts $0.01.
    put = portal.put(
        "/api/projects/demo/budget", json={"monthly_usd": 0.01, "hard_stop": True}
    )
    assert put.status_code == 200, put.text
    scanner.configure(
        usage=json.dumps([{**GOOD_USAGE, "subject": s * 40} for s in "abc"])
    )
    scanner.hold.touch()  # without the stop, the child would wait forever
    try:
        run = wait_run(portal, "demo", scan(portal, "demo", trigger="manual"))
    finally:
        scanner.hold.unlink()
    assert run["status"] == "cancelled"
    assert run["summary"]["cancelled_by"] == "budget"
    assert run["summary"]["usage"]["calls"] >= 2
    assert w.state.budgets.blocked_scope(w.org_id, w.project_id, w.user_id) == "project"


# ---------------------------------------------------------------------------
# Production: member budgets, the admin rule, removal, audit, /api/v1
# ---------------------------------------------------------------------------


def _uid(user_id: int) -> str:
    with portal_db.get_session() as session:
        return session.get(User, user_id).uid


def _as(v1: V1World, who: str) -> TestClient:
    v1.client.cookies.clear()
    signed_in(v1.client, v1.ids[who])
    return v1.client


def _prod_spend(v1: V1World, cost: str, *, user: str | None = None) -> None:
    row = _row(
        org_id=v1.org_id,
        project_id=v1.project_id,
        user_id=None if user is None else v1.ids[user],
        cost=cost,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    v1.state.spend.add(row)


def test_member_budgets_and_the_admin_self_and_owner_rule(v1: V1World) -> None:
    base = f"{at(ORG)}/api/budgets"
    ben, cy, fay = (_uid(v1.ids[n]) for n in ("ben", "cy", "fay"))

    client = _as(v1, "fay")  # an org admin
    for target in (fay, ben):
        for method in ("PUT", "DELETE"):
            refused = client.request(
                method, f"{base}/members/{target}", json={"monthly_usd": 1}
            )
            assert refused.status_code == 403, (target, method, refused.text)
            assert refused.json()["code"] == "forbidden"
    set_cy = client.put(
        f"{base}/members/{cy}", json={"monthly_usd": 1, "hard_stop": True}
    )
    assert set_cy.status_code == 200, set_cy.text
    assert set_cy.json()["uid"] == cy
    default = client.put(f"{base}/member-default", json={"monthly_usd": 3})
    assert default.status_code == 200, default.text
    unknown = client.put(f"{base}/members/nobody", json={"monthly_usd": 1})
    assert (unknown.status_code, unknown.json()["code"]) == (404, "not_member")

    client = _as(v1, "ben")  # the owner sets anyone's, their own included
    for target in (fay, ben):
        assert (
            client.put(f"{base}/members/{target}", json={"monthly_usd": 2}).status_code
            == 200
        )
    assert client.put(f"{base}/org", json={"monthly_usd": 2.5}).status_code == 409
    listed = client.get(base).json()
    assert listed["member_default"]["monthly_usd"] == 3.0
    assert listed["member_default"]["spent_usd"] is None
    assert sorted((m["uid"], m["monthly_usd"]) for m in listed["members"]) == sorted(
        [(cy, 1.0), (fay, 2.0), (ben, 2.0)]
    )
    assert {m["label"] for m in listed["members"]} == {
        "Cy (@cy)",
        "Fay (@fay)",
        "Ben (@ben)",
    }

    client = _as(v1, "cy")  # a member reads no org budgets
    refused = client.get(base)
    assert refused.status_code == 403
    assert refused.json()["action"] == "org.usage"

    # Cy's override blocks Cy only.
    _prod_spend(v1, "1", user="cy")
    budgets = v1.state.budgets
    assert budgets.blocked_scope(v1.org_id, v1.project_id, v1.ids["cy"]) == "member"
    assert budgets.blocked_scope(v1.org_id, v1.project_id, v1.ids["ben"]) is None

    # Removing the member drops the override from the map (FK cascade + reload).
    client = _as(v1, "ben")
    removed = client.delete(f"{at(ORG)}/api/org/members/{cy}")
    assert removed.status_code == 204, removed.text
    assert v1.ids["cy"] not in budgets.for_org(v1.org_id).members


def test_production_audits_budget_changes_and_crossings(v1: V1World) -> None:
    cy = _uid(v1.ids["cy"])
    client = _as(v1, "ben")
    put = client.put(f"{at(ORG)}/api/budgets/members/{cy}", json={"monthly_usd": 1})
    assert put.status_code == 200, put.text
    _prod_spend(v1, "0.6", user="cy")
    assert v1.state.usage_writer.flush()
    writer = audit_mod.current_writer()
    assert writer is not None and writer.flush()
    with portal_db.get_session() as session:
        found = {
            row.event: row
            for row in session.exec(select(AuditEvent).order_by(AuditEvent.id)).all()
        }
        session.expunge_all()
    budget_set = found["budget_set"]
    assert (budget_set.target, budget_set.org_slug) == (cy, ORG)
    assert budget_set.fields["scope"] == "member"
    crossed = found["budget_threshold_crossed"]
    assert (crossed.target, crossed.org_id, crossed.org_slug) == (cy, v1.org_id, ORG)
    assert crossed.fields == {
        "scope": "member",
        "threshold": 50,
        "spent_usd": "0.60",
        "budget_usd": "1.00",
        "month": current_month(),
    }
    assert "budget_hard_stop_engaged" not in found  # a soft budget


def test_api_v1_answers_403_budget_exceeded_with_its_scope(
    v1: V1World, generator: type[_Generator]
) -> None:
    """The contract (section 6.2 #12): an ``ErrorOut`` with ``scope``."""
    client = _as(v1, "ben")
    put = client.put(
        f"{at(ORG)}/api/budgets/org", json={"monthly_usd": 1, "hard_stop": True}
    )
    assert put.status_code == 200, put.text
    _prod_spend(v1, "1")
    card = {"target": target_body(name="sample.fn"), "hunks": []}
    refused = call(v1, "POST", "/rationale", json=card)
    assert refused.status_code == 403, refused.text
    assert refused.json() == {
        "error": PLATFORM_BUDGET_MESSAGE,
        "code": "budget_exceeded",
        "scope": "org",
    }
    assert ErrorOut.model_validate(refused.json()).scope == "org"
    assert generator.calls == 0
