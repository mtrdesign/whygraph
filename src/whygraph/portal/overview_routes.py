"""The project Overview's data: ``GET /api/projects/{slug}/overview`` (M2f-3).

Plan section 4.10. One route on :data:`overview_router` (both modes),
``project_access(Action.PROJECT_READ)``; it reads the portal database (never
the project's WhyGraph DB; the ``codegraph`` block also counts the rows of
the project's CodeGraph index), so it answers for an uninitialized or linked
project too:

- ``coverage.points`` - the ``summary.coverage`` snapshots the runner writes
  after each run that scanned, last :data:`COVERAGE_DAYS` days, oldest first,
  thinned evenly to at most :data:`MAX_POINTS`;
- ``events`` - the runs the coverage chart marks (imports, first scans,
  full scans, describes, failures, budget stops), same window;
- ``last_failure`` - the newest failed / interrupted run after the last ok
  one (its message as the runner stored it);
- ``usage`` - this month's spend, budget and task split, only with
  ``project.usage``;
- ``codegraph`` - local mode only (``null`` otherwise): the shared
  CodeGraph server's state, this project's index refreshes and call
  tallies (:meth:`~whygraph.portal.codegraph_host.CodeGraphHost.snapshot`),
  the index file count and ``last_index_failure`` (plan section 4.2c);
- ``agents`` - :data:`AGENT_DAYS` days of agent calls (``agent_call_days``
  plus the unflushed book), the LLM calls agents caused (the usage ledger,
  ``source`` ``mcp`` / ``agent``), the top kinds, and in production the
  people (``project.usage``) and the connections (``project.configure``).
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func
from sqlmodel import Session, col, select

from whygraph.core.safe_paths import UnsafePathError, check_inside

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

CODEGRAPH_RUNS_SCANNED = 50
"""How many recent runs ``last_index_failure`` looks through."""

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
    # Only a clone is an import (production GitHub); a local first scan is not.
    if summary.get("cloned") is True:
        return "import"
    if run.trigger == "initial":
        return "first_scan"
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


def _last_index_failure(db: Session, project_id: int) -> dict[str, Any] | None:
    """The newest run whose ``summary.codegraph.status`` is ``failed`` after the last ``ok`` one.

    Mirrors :func:`_last_failure`, which sees only ``failed`` / ``interrupted``
    runs and so misses a non-strict scan's CodeGraph failure.
    """
    runs = db.exec(
        select(ScanRun)
        .where(
            ScanRun.project_id == project_id,
            col(ScanRun.status).in_(_FINISHED),
            col(ScanRun.summary).is_not(None),
            col(ScanRun.summary).contains("codegraph"),
        )
        .order_by(col(ScanRun.id).desc())
        .limit(CODEGRAPH_RUNS_SCANNED)
    ).all()
    for run in runs:
        block = _summary(run.summary).get("codegraph")
        if not isinstance(block, dict):
            continue
        status = block.get("status")
        if status == "ok":
            return None
        if status == "failed":
            error = block.get("error")
            return {
                "run_id": run.id,
                "at": _run_at(run),
                "message": error
                if isinstance(error, str) and error
                else "The code index could not be refreshed.",
            }
    return None


def _index_files(project: BoundProject) -> int | None:
    """``SELECT count(*) FROM files`` in the project's CodeGraph DB, or ``None`` on any error."""
    try:
        path = check_inside(project.root, ".codegraph/codegraph.db")
        if not path.is_file():
            return None
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)) as c:
            return int(c.execute("SELECT count(*) FROM files").fetchone()[0])
    except (UnsafePathError, OSError, sqlite3.Error, TypeError, ValueError):
        return None


def _codegraph(
    db: Session, state: PortalState, project: BoundProject
) -> dict[str, Any] | None:
    """The ``codegraph`` block (``None`` outside local mode or without a host)."""
    host = state.codegraph_host
    if host is None:
        return None
    block = host.snapshot(project.id)
    files = _index_files(project)
    return {
        **block,
        "enabled": bool(project.ctx.config.scan_serve_codegraph),
        "index": {"present": files is not None, "files": files},
        "last_index_failure": _last_index_failure(db, project.id),
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


class _AgentRow(NamedTuple):
    """One counter row: flushed (``agent_call_days``) or pending in the book."""

    day: str
    source: str
    user_id: int | None
    connection_id: int | None
    kind: str
    calls: int
    errors: int
    internal_errors: int
    duration_ms: int


def _agent_rows(
    db: Session, state: PortalState, project_id: int, first_day: str
) -> list[_AgentRow]:
    """The counter rows since ``first_day``.

    The flushed rows plus the book's unflushed counts.
    """
    rows = [
        _AgentRow(day, source, user_id, connection_id, kind, *map(int, counts))
        for day, source, user_id, connection_id, kind, *counts in db.exec(
            select(
                AgentCallDay.day,
                AgentCallDay.source,
                AgentCallDay.user_id,
                AgentCallDay.connection_id,
                AgentCallDay.kind,
                AgentCallDay.calls,
                AgentCallDay.errors,
                AgentCallDay.internal_errors,
                AgentCallDay.duration_ms,
            ).where(
                AgentCallDay.project_id == project_id,
                col(AgentCallDay.day) >= first_day,
            )
        ).all()
    ]
    for key, tally in state.agent_calls.pending(project_id):
        if key.day >= first_day:
            rows.append(
                _AgentRow(
                    key.day,
                    key.source,
                    key.user_id,
                    key.connection_id,
                    key.kind,
                    *tally,
                )
            )
    return rows


def _kind_entries(rows: list[_AgentRow]) -> tuple[list[dict[str, Any]], int, int]:
    """The ``by_kind`` entries (top :data:`TOP_KINDS`) and the block's error totals.

    ``errors`` / ``internal_errors`` / ``mean_ms`` are ``None`` for a ``v1:``
    kind (not measured, never 0). ``mean_ms`` averages only the rows that
    carry a duration, so rows from before the upgrade, and rows holding only
    cancelled calls, cannot pull it down. The totals cover every kind, not
    only the ones listed.
    """
    calls: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    internal: Counter[str] = Counter()
    timed_ms: Counter[str] = Counter()
    timed_calls: Counter[str] = Counter()
    for row in rows:
        calls[row.kind] += row.calls
        errors[row.kind] += row.errors
        internal[row.kind] += row.internal_errors
        if row.duration_ms > 0:
            timed_ms[row.kind] += row.duration_ms
            timed_calls[row.kind] += row.calls
    entries = []
    for kind, n in sorted(calls.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_KINDS]:
        measured = not kind.startswith("v1:")
        entries.append(
            {
                "kind": kind,
                "calls": n,
                "errors": errors[kind] if measured else None,
                "internal_errors": internal[kind] if measured else None,
                "mean_ms": round(timed_ms[kind] / timed_calls[kind])
                if measured and timed_calls[kind]
                else None,
            }
        )
    return entries, sum(errors.values()), sum(internal.values())


def _last_call_day(db: Session, state: PortalState, project_id: int) -> str | None:
    flushed = db.exec(
        select(func.max(AgentCallDay.day)).where(
            AgentCallDay.project_id == project_id,
            col(AgentCallDay.source).in_(AGENT_CALL_SOURCES),
        )
    ).one()
    pending = [
        key.day
        for key, _ in state.agent_calls.pending(project_id)
        if key.source in AGENT_CALL_SOURCES
    ]
    days = [day for day in (flushed, *pending) if day]
    return max(days) if days else None


def _people(
    db: Session,
    org_id: int,
    rows: list[_AgentRow],
) -> list[dict[str, Any]]:
    """Calls per person (and machine), labelled from the live rows."""
    user_ids = {row.user_id for row in rows if row.user_id is not None}
    connection_ids = {
        row.connection_id for row in rows if row.connection_id is not None
    }
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
    for row in rows:
        day, user_id, connection_id, calls = (
            row.day,
            row.user_id,
            row.connection_id,
            row.calls,
        )
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
    all_rows = _agent_rows(db, state, project.id, first_day)
    rows = [row for row in all_rows if row[1] in AGENT_CALL_SOURCES]
    chat_rows = [row for row in all_rows if row.source == "chat"]
    per_day = {day: {"day": day, "mcp": 0, "agent": 0} for day in days}
    total = 0
    for row in rows:
        if row.day in per_day:
            per_day[row.day][row.source] += row.calls
        total += row.calls
    by_kind, errors, internal_errors = _kind_entries(rows)
    chat_by_kind, chat_errors, chat_internal = _kind_entries(chat_rows)
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
        "errors": errors,
        "internal_errors": internal_errors,
        "by_kind": by_kind,
        "people": _people(db, project.org_id, rows)
        if production and can_usage
        else None,
        "connections": _connections(db, project.id)
        if production and project_allowed(role, Action.PROJECT_CONFIGURE)
        else None,
        "last_call_day": _last_call_day(db, state, project.id),
        "chat_tools": {
            "total_calls": sum(row.calls for row in chat_rows),
            "errors": chat_errors,
            "internal_errors": chat_internal,
            "by_kind": chat_by_kind,
        },
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
            "codegraph": _codegraph(db, state, project),
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
