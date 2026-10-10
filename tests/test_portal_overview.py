"""``GET /api/projects/{slug}/overview`` (M2f-3 plan sections 4.10, 6.2 #9).

The shape; the coverage points (``summary.coverage`` as the runner writes
it, seeded directly here), their 180-day window and the thinning to 120;
the chart events; ``last_failure`` only after the last ok run; ``usage``
only with ``project.usage``; the agent block's 30 zero-filled days from the
counters and the unflushed book; and in production ``people`` (with
``project.usage``) and ``connections`` (with ``project.configure``).
"""

# ruff: noqa: F811 -- pytest fixtures (`env`, `local`, `v1`, ...) are imported

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlmodel import delete

from test_portal_app import (  # noqa: F401 -- fixtures
    at,
    env,
    prod_portal,
    production_env,
    signed_in,
)
from test_portal_usage import local, stub_llm  # noqa: F401 -- fixtures
from test_portal_v1 import V1World, _mint, call, v1  # noqa: F401
from whygraph.portal import db as portal_db
from whygraph.portal.models import (
    AgentCallDay,
    ConnectionToken,
    Membership,
    ScanRun,
    UsageEvent,
)

NOW = datetime.now(timezone.utc)
TODAY = NOW.date()


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the route's and the counter's clock to NOW, so a run across midnight stays green."""
    monkeypatch.setattr("whygraph.portal.overview_routes._utcnow", lambda: NOW)
    monkeypatch.setattr("whygraph.portal.agent_calls._utcnow", lambda: NOW)


_KEYS = {"coverage", "events", "last_failure", "usage", "agents"}
_AGENT_KEYS = {
    "days",
    "total_calls",
    "llm_calls",
    "llm_cost_usd",
    "by_kind",
    "people",
    "connections",
    "last_call_day",
}


def _stamp(days_ago: float = 0, minutes: int = 0) -> str:
    moment = NOW - timedelta(days=days_ago) + timedelta(minutes=minutes)
    return moment.isoformat(timespec="seconds")


def _run(
    project_id: int,
    *,
    trigger: str = "manual",
    status: str = "ok",
    analyze: bool = False,
    kind: str = "scan",
    summary: Any = None,
    days_ago: float = 1,
    minutes: int = 0,
) -> int:
    finished = _stamp(days_ago, minutes)
    raw = (
        summary if isinstance(summary, str) or summary is None else json.dumps(summary)
    )
    with portal_db.get_session() as session:
        run = ScanRun(
            project_id=project_id,
            kind=kind,
            trigger=trigger,
            analyze=analyze,
            status=status,
            queued_at=finished,
            started_at=finished,
            finished_at=finished,
            summary=raw,
        )
        session.add(run)
        session.flush()
        return run.id


def _coverage(commits: int, described: int, at: str, cards: int = 0) -> dict:
    return {
        "coverage": {
            "commits": commits,
            "described": described,
            "described_pct": round(100 * described / commits, 1),
            "pull_requests": 2,
            "issues": 1,
            "rationale_cards": cards,
            "at": at,
        }
    }


def _overview(client: Any, prefix: str = "", slug: str = "demo") -> dict:
    response = client.get(f"{prefix}/api/projects/{slug}/overview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == _KEYS
    assert set(body["agents"]) == _AGENT_KEYS
    return body


def _usage_event(w: Any, *, source: str, cost: str, days_ago: float = 0) -> None:
    with portal_db.get_session() as session:
        session.add(
            UsageEvent(
                org_id=w.org_id,
                project_id=w.project_id,
                project_slug="demo",
                project_name="Demo",
                actor_kind="member",
                user_id=w.user_id,
                actor_label="Tess",
                source=source,
                task="analyze",
                provider="anthropic",
                model_requested="claude-opus-4-7",
                key_scope="org",
                cost_source="estimated",
                input_tokens=10,
                output_tokens=1,
                cost_usd=Decimal(cost),
                created_at=_stamp(days_ago),
            )
        )


def _calls(w: Any, day: str, kind: str, calls: int, source: str = "mcp") -> None:
    with portal_db.get_session() as session:
        session.add(
            AgentCallDay(
                org_id=w.org_id,
                project_id=w.project_id,
                day=day,
                source=source,
                user_id=w.user_id,
                kind=kind,
                calls=calls,
            )
        )


# ---------------------------------------------------------------------------
# Coverage, events, last failure
# ---------------------------------------------------------------------------


def test_an_empty_overview(local: SimpleNamespace) -> None:
    body = _overview(local.client)
    assert body["coverage"] == {"points": []}
    assert body["events"] == []
    assert body["last_failure"] is None
    agents = body["agents"]
    assert len(agents["days"]) == 30
    assert agents["days"][-1] == {"day": TODAY.isoformat(), "mcp": 0, "agent": 0}
    assert agents["days"][0]["day"] == (TODAY - timedelta(days=29)).isoformat()
    assert (agents["total_calls"], agents["llm_calls"], agents["by_kind"]) == (0, 0, [])
    assert agents["last_call_day"] is None
    # The local user is the project admin: usage, but no people / connections.
    assert body["usage"] == {
        "month": TODAY.strftime("%Y-%m"),
        "spent_usd": 0.0,
        "budget_usd": None,
        "pct": None,
        "by_task": [],
    }
    assert agents["llm_cost_usd"] == 0.0
    assert agents["people"] is None and agents["connections"] is None


def test_coverage_points_and_events(local: SimpleNamespace) -> None:
    pid = local.project_id
    _run(pid, trigger="initial", summary=_coverage(10, 0, "old"), days_ago=200)
    imported = _run(
        pid, trigger="initial", summary=_coverage(10, 0, _stamp(9)), days_ago=9
    )
    full = _run(
        pid,
        analyze=True,
        summary=_coverage(10, 4, _stamp(8), cards=2),
        days_ago=8,
    )
    hooked = _run(pid, trigger="hook", summary=_coverage(12, 4, _stamp(7)), days_ago=7)
    _run(pid, trigger="hook", summary="not json", days_ago=6, minutes=1)
    _run(pid, kind="sync", trigger="push", summary={"scanned": True}, days_ago=6)
    stopped = _run(
        pid,
        trigger="describe",
        analyze=True,
        status="cancelled",
        summary={"cancelled_by": "budget"},
        days_ago=5,
    )
    _run(pid, status="cancelled", summary={"cancelled_by": "user"}, days_ago=5)
    described = _run(pid, trigger="describe", analyze=True, days_ago=4)
    failed = _run(pid, status="failed", summary={"error": "boom"}, days_ago=3)
    with portal_db.get_session() as session:  # queued / running: never charted
        session.add(ScanRun(project_id=pid, trigger="manual", status="queued"))
    body = _overview(local.client)
    assert body["coverage"]["points"] == [
        {
            "run_id": imported,
            "at": _stamp(9),
            "commits": 10,
            "described": 0,
            "described_pct": 0.0,
            "rationale_cards": 0,
        },
        {
            "run_id": full,
            "at": _stamp(8),
            "commits": 10,
            "described": 4,
            "described_pct": 40.0,
            "rationale_cards": 2,
        },
        {
            "run_id": hooked,
            "at": _stamp(7),
            "commits": 12,
            "described": 4,
            "described_pct": 33.3,
            "rationale_cards": 0,
        },
    ]
    assert [(e["run_id"], e["kind"]) for e in body["events"]] == [
        (imported, "import"),
        (full, "full_scan"),
        (stopped, "budget_stop"),
        (described, "describe"),
        (failed, "failed"),
    ]
    assert body["events"][0]["at"] == _stamp(9)
    assert body["last_failure"] == {
        "run_id": failed,
        "at": _stamp(3),
        "message": "boom",
    }
    # A newer interruption is the last failure; an ok run after it clears it.
    interrupted = _run(pid, status="interrupted", days_ago=2)
    assert _overview(local.client)["last_failure"] == {
        "run_id": interrupted,
        "at": _stamp(2),
        "message": "The scan was interrupted.",
    }
    _run(pid, trigger="hook", days_ago=1)
    assert _overview(local.client)["last_failure"] is None


def test_coverage_points_are_thinned_to_120(local: SimpleNamespace) -> None:
    pid = local.project_id
    with portal_db.get_session() as session:
        runs = [
            ScanRun(
                project_id=pid,
                trigger="hook",
                analyze=False,
                status="ok",
                finished_at=_stamp(100, minutes=n),
                summary=json.dumps(_coverage(300, n, _stamp(100, minutes=n))),
            )
            for n in range(300)
        ]
        session.add_all(runs)
        session.flush()
        ids = [run.id for run in runs]
    points = _overview(local.client)["coverage"]["points"]
    assert len(points) == 120
    assert points[0]["run_id"] == ids[0] and points[-1]["run_id"] == ids[-1]
    assert [p["run_id"] for p in points] == sorted({p["run_id"] for p in points})


# ---------------------------------------------------------------------------
# Usage and agent activity
# ---------------------------------------------------------------------------


def test_usage_and_agent_activity(local: SimpleNamespace) -> None:
    w = local
    _usage_event(w, source="mcp", cost="0.25")
    _usage_event(w, source="explorer", cost="1.00")
    _usage_event(w, source="mcp", cost="0.50", days_ago=40)  # outside 30 days
    three = (TODAY - timedelta(days=3)).isoformat()
    _calls(w, three, "whygraph_evidence_for", 5)
    _calls(w, three, "whygraph_commit", 1)
    _calls(w, (TODAY - timedelta(days=40)).isoformat(), "whygraph_issue", 7)
    w.state.agent_calls.add(
        org_id=w.org_id,
        project_id=w.project_id,
        source="mcp",
        user_id=w.user_id,
        connection_id=None,
        kind="whygraph_commit",
    )
    body = _overview(w.client)
    agents = body["agents"]
    by_day = {d["day"]: d for d in agents["days"]}
    assert by_day[three] == {"day": three, "mcp": 6, "agent": 0}
    assert by_day[TODAY.isoformat()] == {"day": TODAY.isoformat(), "mcp": 1, "agent": 0}
    assert sum(d["mcp"] + d["agent"] for d in agents["days"]) == 7
    assert agents["total_calls"] == 7
    assert agents["by_kind"] == [
        {"kind": "whygraph_evidence_for", "calls": 5},
        {"kind": "whygraph_commit", "calls": 2},
    ]
    assert agents["last_call_day"] == TODAY.isoformat()
    assert (agents["llm_calls"], agents["llm_cost_usd"]) == (1, 0.25)
    usage = body["usage"]
    assert usage["month"] == TODAY.strftime("%Y-%m")
    # This month's ledger, every source (the 40-day-old row is last month's).
    assert usage["by_task"] == [{"task": "analyze", "calls": 2, "cost_usd": 1.25}]


# ---------------------------------------------------------------------------
# Production: people and connections by action
# ---------------------------------------------------------------------------


def _prod_overview(w: V1World, user: str) -> dict:
    w.client.cookies.clear()
    signed_in(w.client, w.ids[user])
    try:
        return _overview(w.client, prefix=at("acme"), slug="api")
    finally:
        w.client.cookies.clear()


def test_production_people_and_connections_by_action(v1: V1World) -> None:
    cy_token = _mint(v1.ids["cy"], v1.project_id, name="cys-box")
    assert call(v1, "GET", "/overview").status_code == 200
    assert call(v1, "GET", "/overview").status_code == 200
    assert call(v1, "GET", "/prs/1", token=cy_token).status_code == 404
    v1.state.agent_calls.flush()

    owner = _prod_overview(v1, "ben")
    agents = owner["agents"]
    today = TODAY.isoformat()
    assert agents["days"][-1] == {"day": today, "mcp": 0, "agent": 3}
    assert agents["people"] == [
        {"label": "laptop (Ben)", "calls": 2, "last_day": today},
        {"label": "cys-box (Cy)", "calls": 1, "last_day": today},
    ]
    assert sorted(
        (c["client_name"], c["user_label"]) for c in agents["connections"]
    ) == [
        ("cys-box", "Cy (@cy)"),
        ("laptop", "Ben (@ben)"),
    ]
    assert owner["usage"] is not None and agents["llm_cost_usd"] == 0.0

    # A contributor reads the activity, never who or what it cost.
    member = _prod_overview(v1, "cy")
    assert member["agents"]["total_calls"] == 3
    assert member["agents"]["people"] is None
    assert member["agents"]["connections"] is None
    assert member["agents"]["llm_cost_usd"] is None
    assert member["usage"] is None

    # A removed member's calls stay, under one label; a deleted token's
    # calls fall back to the person.
    with portal_db.get_session() as session:
        session.execute(
            delete(Membership).where(
                Membership.org_id == v1.org_id, Membership.user_id == v1.ids["cy"]
            )
        )
        session.execute(
            delete(ConnectionToken).where(ConnectionToken.user_id == v1.ids["ben"])
        )
    people = _prod_overview(v1, "ben")["agents"]["people"]
    assert people == [
        {"label": "Ben (@ben)", "calls": 2, "last_day": today},
        {"label": "Removed member", "calls": 1, "last_day": today},
    ]


def test_an_overview_of_another_orgs_project_is_not_found(v1: V1World) -> None:
    v1.client.cookies.clear()
    signed_in(v1.client, v1.ids["ben"])
    response = v1.client.get(at("acme") + "/api/projects/nope/overview")
    assert response.status_code == 404
    v1.client.cookies.clear()
