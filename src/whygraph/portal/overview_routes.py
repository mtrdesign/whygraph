"""The project Overview's data: ``GET /api/projects/{slug}/overview`` (M2f-3).

Plan section 4.10. One route on :data:`overview_router` (both modes),
``project_access(Action.PROJECT_READ)``; it reads only the portal database
(never the project's), so it answers for an uninitialized or linked project
too:

- ``coverage.points`` - the ``summary.coverage`` snapshots the runner writes
  after each run that scanned, last :data:`COVERAGE_DAYS` days, oldest first,
  thinned evenly to at most :data:`MAX_POINTS`;
- ``events`` - the runs the coverage chart marks (imports, full scans,
  describes, failures, budget stops), same window;
- ``last_failure`` - the newest failed / interrupted run after the last ok
  one (its message as the runner stored it);
- ``usage`` - this month's spend, budget and task split, only with
  ``project.usage``;
- ``agents`` - :data:`AGENT_DAYS` days of agent calls (``agent_call_days``
  plus the unflushed book), the LLM calls agents caused (the usage ledger,
  ``source`` ``mcp`` / ``agent``), the top kinds, and in production the
  people (``project.usage``) and the connections (``project.configure``).
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func
from sqlmodel import Session, col, select

from . import connections
from .authz import Action, project_allowed
from .db import get_session
from .deps import BoundProject, PortalState, portal_state, project_access
from .models import (
    AGENT_CALL_SOURCES,
    AgentCallDay,
    ConnectionToken,
    Membership,
    ScanRun,
    UsageEvent,
    User,
)
from .usage import actor_label
from .usage_routes import project_usage, usd
from .usage_store import current_month

overview_router = APIRouter()
"""The Overview route (both modes); included before the ``/api`` 404 catch-all."""

COVERAGE_DAYS = 180
"""How far back the coverage chart and its events reach."""

MAX_POINTS = 120
"""Most coverage points one answer carries (thinned evenly above it)."""

MAX_EVENTS = 200
"""Most chart events one answer carries (the newest)."""

AGENT_DAYS = 30
"""The agent-activity window, in UTC days (today included)."""

TOP_KINDS = 8
"""How many call kinds ``agents.by_kind`` lists."""

REMOVED_MEMBER_LABEL = "Removed member"
"""The ``people`` label of calls by someone no longer in the org."""

_FINISHED = ("ok", "failed", "interrupted", "cancelled")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _summary(raw: str | None) -> dict[str, Any]:
    """A run's ``summary`` as a dict; ``{}`` when absent or not a JSON object."""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _run_at(run: ScanRun) -> str | None:
    return run.finished_at or run.started_at or run.queued_at


def _thin(points: list[dict], limit: int = MAX_POINTS) -> list[dict]:
    """At most ``limit`` points, evenly spaced, the first and last kept."""
    n = len(points)
    if n <= limit:
        return points
    return [points[round(i * (n - 1) / (limit - 1))] for i in range(limit)]


def _event_kind(run: ScanRun, summary: dict[str, Any]) -> str | None:
    """The chart marker a finished run gets, or ``None`` (quick scans, syncs)."""
    if (
        summary.get("cancelled_by") == "budget"
        or summary.get("analyze_skipped") == "budget"
    ):
        return "budget_stop"
    if run.status in ("failed", "interrupted"):
        return "failed"
    if run.status != "ok":
        return None
    if summary.get("cloned") is True or run.trigger == "initial":
        return "import"
    if run.trigger == "describe":
        return "describe"
    if run.analyze and run.kind == "scan":
        return "full_scan"
    return None


def _coverage_point(run: ScanRun, coverage: Any) -> dict[str, Any] | None:
    if not isinstance(coverage, dict):
        return None
    return {
        "run_id": run.id,
        "at": coverage.get("at") or run.finished_at,
        "commits": coverage.get("commits"),
        "described": coverage.get("described"),
        "described_pct": coverage.get("described_pct"),
        "rationale_cards": coverage.get("rationale_cards"),
    }


def _history(
    db: Session, project_id: int, now: datetime
) -> tuple[list[dict], list[dict]]:
    """The coverage points and the chart events of the last :data:`COVERAGE_DAYS`."""
    cutoff = (now - timedelta(days=COVERAGE_DAYS)).isoformat(timespec="seconds")
    runs = db.exec(
        select(ScanRun)
        .where(
            ScanRun.project_id == project_id,
            col(ScanRun.status).in_(_FINISHED),
            func.coalesce(
                col(ScanRun.finished_at),
                col(ScanRun.started_at),
                col(ScanRun.queued_at),
            )
            >= cutoff,
        )
        .order_by(col(ScanRun.id))
    ).all()
    points: list[dict] = []
    events: list[dict] = []
    for run in runs:
        summary = _summary(run.summary)
        if "coverage" in summary:
            point = _coverage_point(run, summary["coverage"])
            if point is not None:
                points.append(point)
        kind = _event_kind(run, summary)
        if kind is not None:
            events.append({"run_id": run.id, "at": _run_at(run), "kind": kind})
    return _thin(points), events[-MAX_EVENTS:]


_FAILURE_MESSAGES = {
    "failed": "The scan failed.",
    "interrupted": "The scan was interrupted.",
}


def _last_failure(db: Session, project_id: int) -> dict[str, Any] | None:
    """The newest failed / interrupted run after the last ok one, or ``None``."""
    last_ok = db.exec(
        select(func.max(ScanRun.id)).where(
            ScanRun.project_id == project_id, ScanRun.status == "ok"
        )
    ).one()
    conditions = [
        ScanRun.project_id == project_id,
        col(ScanRun.status).in_(("failed", "interrupted")),
    ]
    if last_ok is not None:
        conditions.append(col(ScanRun.id) > last_ok)
    run = db.exec(
        select(ScanRun).where(*conditions).order_by(col(ScanRun.id).desc()).limit(1)
    ).first()
    if run is None:
        return None
    error = _summary(run.summary).get("error")
    return {
        "run_id": run.id,
        "at": _run_at(run),
        "message": error
        if isinstance(error, str) and error
        else _FAILURE_MESSAGES[run.status],
    }


def _usage(
    db: Session, state: PortalState, project: BoundProject, now: datetime
) -> dict[str, Any] | None:
    """This month's spend, budget and task split (``None`` without ``project.usage``)."""
    block = project_usage(state, project.org_id, project.id, project.role)
    if block is None:
        return None
    month = current_month(now)
    rows = db.exec(
        select(
            UsageEvent.task,
            func.count(),
            func.sum(UsageEvent.cost_usd),
        )
        .where(
            UsageEvent.org_id == project.org_id,
            UsageEvent.project_id == project.id,
            col(UsageEvent.created_at) >= f"{month}-01T00:00:00+00:00",
        )
        .group_by(UsageEvent.task)
    ).all()
    by_task = sorted(
        (
            {"task": task, "calls": int(calls), "cost_usd": usd(cost)}
            for task, calls, cost in rows
        ),
        key=lambda item: (-item["cost_usd"], -item["calls"], item["task"]),
    )
    budget = block["budget"]
    return {
        "month": month,
        "spent_usd": block["month_spend_usd"],
        "budget_usd": None if budget is None else budget["monthly_usd"],
        "pct": block["pct"],
        "by_task": by_task,
    }


def _agent_rows(
    db: Session, state: PortalState, project_id: int, first_day: str
) -> list[tuple[str, str, int | None, int | None, str, int]]:
    """``(day, source, user_id, connection_id, kind, calls)`` since ``first_day``.

    The flushed rows plus the book's unflushed counts.
    """
    rows = [
        (day, source, user_id, connection_id, kind, int(calls))
        for day, source, user_id, connection_id, kind, calls in db.exec(
            select(
                AgentCallDay.day,
                AgentCallDay.source,
                AgentCallDay.user_id,
                AgentCallDay.connection_id,
                AgentCallDay.kind,
                AgentCallDay.calls,
            ).where(
                AgentCallDay.project_id == project_id,
                col(AgentCallDay.day) >= first_day,
            )
        ).all()
    ]
    for key, calls in state.agent_calls.pending(project_id):
        if key.day >= first_day:
            rows.append(
                (key.day, key.source, key.user_id, key.connection_id, key.kind, calls)
            )
    return rows


def _last_call_day(db: Session, state: PortalState, project_id: int) -> str | None:
    flushed = db.exec(
        select(func.max(AgentCallDay.day)).where(AgentCallDay.project_id == project_id)
    ).one()
    pending = [key.day for key, _ in state.agent_calls.pending(project_id)]
    days = [day for day in (flushed, *pending) if day]
    return max(days) if days else None


def _people(
    db: Session,
    org_id: int,
    rows: list[tuple[str, str, int | None, int | None, str, int]],
) -> list[dict[str, Any]]:
    """Calls per person (and machine), labelled from the live rows."""
    user_ids = {user_id for _, _, user_id, _, _, _ in rows if user_id is not None}
    connection_ids = {cid for _, _, _, cid, _, _ in rows if cid is not None}
    users = (
        {
            user.id: user
            for user in db.exec(
                select(User)
                .join(Membership, col(Membership.user_id) == col(User.id))
                .where(Membership.org_id == org_id, col(User.id).in_(user_ids))
            ).all()
        }
        if user_ids
        else {}
    )
    machines = (
        dict(
            db.exec(
                select(ConnectionToken.id, ConnectionToken.client_name).where(
                    col(ConnectionToken.id).in_(connection_ids)
                )
            ).all()
        )
        if connection_ids
        else {}
    )
    groups: dict[tuple, dict[str, Any]] = {}
    for day, _, user_id, connection_id, _, calls in rows:
        user = users.get(user_id) if user_id is not None else None
        if user is None:
            key: tuple = ("removed",)
            label = REMOVED_MEMBER_LABEL
        elif connection_id is not None and connection_id in machines:
            key = ("connection", connection_id)
            label = f"{machines[connection_id]} ({user.display_name})"
        else:
            key = ("user", user_id)
            label = actor_label(user, production=True)  # type: ignore[arg-type]
        group = groups.setdefault(key, {"label": label, "calls": 0, "last_day": day})
        group["calls"] += calls
        group["last_day"] = max(group["last_day"], day)
    return sorted(groups.values(), key=lambda g: (-g["calls"], g["label"]))


def _connections(db: Session, project_id: int) -> list[dict[str, Any]]:
    """The project's live connections, as ``GET /connections`` lists them."""
    return [
        {
            "client_name": info.client_name,
            "user_label": f"{info.user_name} (@{info.user_login})"
            if info.user_login
            else info.user_name,
            "last_used_at": info.last_used_at,
        }
        for info in connections.list_for_project(db, project_id)
    ]


def _agents(
    db: Session, state: PortalState, project: BoundProject, now: datetime
) -> dict[str, Any]:
    """The ``agents`` block (plan section 4.10)."""
    production = state.mode == "production"
    role = project.role
    today = now.astimezone(timezone.utc).date()
    days = [
        (today - timedelta(days=n)).isoformat() for n in range(AGENT_DAYS - 1, -1, -1)
    ]
    first_day = days[0]
    rows = _agent_rows(db, state, project.id, first_day)
    per_day = {day: {"day": day, "mcp": 0, "agent": 0} for day in days}
    kinds: Counter[str] = Counter()
    total = 0
    for day, source, _, _, kind, calls in rows:
        if day in per_day and source in AGENT_CALL_SOURCES:
            per_day[day][source] += calls
        kinds[kind] += calls
        total += calls
    llm_calls, llm_cost = db.exec(
        select(func.count(), func.sum(UsageEvent.cost_usd)).where(
            UsageEvent.org_id == project.org_id,
            UsageEvent.project_id == project.id,
            col(UsageEvent.source).in_(AGENT_CALL_SOURCES),
            col(UsageEvent.created_at) >= f"{first_day}T00:00:00+00:00",
        )
    ).one()
    can_usage = project_allowed(role, Action.PROJECT_USAGE)
    return {
        "days": [per_day[day] for day in days],
        "total_calls": total,
        "llm_calls": int(llm_calls or 0),
        "llm_cost_usd": usd(llm_cost) if can_usage else None,
        "by_kind": [
            {"kind": kind, "calls": calls}
            for kind, calls in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))[
                :TOP_KINDS
            ]
        ],
        "people": _people(db, project.org_id, rows)
        if production and can_usage
        else None,
        "connections": _connections(db, project.id)
        if production and project_allowed(role, Action.PROJECT_CONFIGURE)
        else None,
        "last_call_day": _last_call_day(db, state, project.id),
    }


@overview_router.get("/api/projects/{slug}/overview")
def get_overview(
    request: Request,
    project: BoundProject = Depends(project_access(Action.PROJECT_READ)),
) -> dict[str, Any]:
    """The project Overview's coverage history, events, usage and agent activity."""
    state = portal_state(request)
    now = _utcnow()
    with get_session() as db:
        points, events = _history(db, project.id, now)
        return {
            "coverage": {"points": points},
            "events": events,
            "last_failure": _last_failure(db, project.id),
            "usage": _usage(db, state, project, now),
            "agents": _agents(db, state, project, now),
        }


__all__ = [
    "AGENT_DAYS",
    "COVERAGE_DAYS",
    "MAX_EVENTS",
    "MAX_POINTS",
    "REMOVED_MEMBER_LABEL",
    "TOP_KINDS",
    "overview_router",
]
