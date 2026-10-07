"""The usage read routes, their CSV exports, and the usage payload fields.

M2f-2 plan sections 4.11 and 4.12. Two routers:

- :data:`usage_router` (both modes): ``GET /api/usage``, ``GET
  /api/usage/calls`` and ``GET /api/usage.csv`` (``org.usage``: owners,
  org admins and the instance-admin ``reader``), and ``GET
  /api/projects/{slug}/usage`` (``project.usage``: a project admin);
- :data:`usage_me_router` (production only, ``require_production`` before
  the org dependency): ``GET /api/usage/me``, ``/api/usage/me/calls`` and
  ``/api/usage/me.csv`` (``org.read``), always **forced to the caller** -
  a ``member`` filter is ignored, never trusted.

**Query parameters.** ``from`` / ``to`` are ISO dates in UTC, ``from``
inclusive, ``to`` exclusive; the default is the current month, and a range
longer than :data:`MAX_RANGE_DAYS` days is refused (``422
range_too_long``). Filters: ``project`` (slug), ``member`` (uid, or
``system`` for the System actor), ``task``, ``source``, ``model`` (the
served model, else the requested one), ``scan_run`` and ``chat_session``
(which requires ``project``: session ids are per project database).
``group`` is one of :data:`GROUPS`. Every query starts with the org and the
``created_at`` range, so it walks ``ix_usage_events_org_time`` (or the
org / user and org / project indexes), and the aggregates run under a
:data:`STATEMENT_TIMEOUT` statement timeout (``503 usage_timeout``).

**Money** is a JSON number: costs rounded to 6 decimal places, budgets to
2. Token sums are ``null`` where no row reported that count.

The payload helpers at the end (:func:`state_usage`, :func:`llm_block`,
:func:`project_usage`, :func:`member_month_spend`,
:func:`project_month_spend`) feed ``/api/portal/state``, the project
summary, the members list and a project's access list.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, case, false, func, null, or_, text
from sqlalchemy import select as sa_select
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, col, select

from .authz import Action, OrgAccess, ProjectRole, Role, allowed, project_allowed
from .budgets import BudgetLimit, month_resets_at
from .csv_export import TRUNCATED_HEADER, csv_line, truncated_line
from .db import get_session
from .deps import (
    ApiError,
    BoundProject,
    PortalState,
    org_access,
    portal_state,
    project_access,
    require_production,
)
from .models import USAGE_SOURCES, USAGE_TASKS, Project, UsageEvent, User
from .usage import SYSTEM_LABEL, actor_label
from .usage_store import COUNTED_COST_SOURCES, current_month

usage_router = APIRouter()
"""The both-modes usage routes; included before the ``/api`` 404 catch-all."""

usage_me_router = APIRouter(dependencies=[Depends(require_production)])
"""A member's own usage: production only (local mode has one user)."""

MAX_RANGE_DAYS = 400
"""The longest ``from`` / ``to`` range (the ledger's retention)."""

PAGE_SIZE = 50
"""Calls per page of ``/api/usage/calls``."""

CSV_MAX_ROWS = 50_000
"""Most data rows one CSV export streams."""

CSV_CHUNK = 1_000
"""Rows read per query while a call CSV streams."""

STATEMENT_TIMEOUT = "10s"
"""The statement timeout of every usage query."""

GROUPS: tuple[str, ...] = (
    "project",
    "member",
    "task",
    "model",
    "source",
    "machine",
    "day",
)
"""The ``group`` values of ``/api/usage`` and ``/api/usage.csv``."""

SORTS: tuple[str, ...] = ("time", "cost")
"""The ``sort`` values of ``/api/usage/calls``."""

PORTAL_LABEL = "Portal"
"""The ``group=machine`` bucket of every call not made through a connection."""

UNKNOWN_MACHINE_LABEL = "Unknown machine"
"""An agent call whose connection is gone and that kept no machine name."""

CSV_CALL_COLUMNS: tuple[str, ...] = (
    "created_at",
    "project_slug",
    "actor_label",
    "source",
    "task",
    "provider",
    "model_requested",
    "model_served",
    "key_scope",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "cost_usd",
    "cost_source",
    "price_version",
    "scan_run_id",
    "chat_session_id",
    "client_name",
    "subject",
)
"""The call-row CSV's header (plan section 4.11)."""

CSV_GROUP_COLUMNS: tuple[str, ...] = (
    "key",
    "label",
    "calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "cost_usd",
    "unpriced_calls",
    "interactive_calls",
    "interactive_cost_usd",
    "scans_calls",
    "scans_cost_usd",
)
"""The aggregated CSV's header; ``group=member`` adds :data:`CSV_MEMBER_COLUMNS`."""

CSV_MEMBER_COLUMNS: tuple[str, ...] = ("top_project_slug", "top_project_cost_usd")

_TOKENS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)

_SIX_DP = Decimal("0.000001")
_CENTS = Decimal("0.01")
_ZERO = Decimal(0)

_E = UsageEvent


# ---------------------------------------------------------------------------
# Money and percentages
# ---------------------------------------------------------------------------


def usd(value: Decimal | None) -> float:
    """A cost as a JSON number, rounded to 6 decimal places (``None`` is ``0``).

    Parameters
    ----------
    value : Decimal or None
        The amount.

    Returns
    -------
    float
        The rounded amount.
    """
    return float(Decimal(value or 0).quantize(_SIX_DP))


def _usd_or_none(value: Decimal | None) -> float | None:
    return None if value is None else usd(value)


def _cents(value: Decimal) -> float:
    return float(Decimal(value).quantize(_CENTS))


def _pct(spent: Decimal, monthly: Decimal) -> float | None:
    if not monthly:
        return None
    return round(float(spent * 100 / monthly), 1)


def _usd_text(value: Decimal | None) -> str | None:
    """A cost for a CSV cell (6 decimal places, empty when ``None``)."""
    return None if value is None else str(Decimal(value).quantize(_SIX_DP))


def _int(value: Any) -> int | None:
    return None if value is None else int(value)


# ---------------------------------------------------------------------------
# The query
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageParams:
    """The raw query parameters of a usage route.

    Attributes
    ----------
    since, until : str or None
        ``from`` / ``to``.
    project, member, task, source, model : str or None
        The filters.
    scan_run, chat_session : int or None
        The id filters.
    group : str or None
        One of :data:`GROUPS`.
    sort : str
        One of :data:`SORTS`.
    before : str or None
        The previous page's ``next``.
    """

    since: str | None = None
    until: str | None = None
    project: str | None = None
    member: str | None = None
    task: str | None = None
    source: str | None = None
    model: str | None = None
    scan_run: int | None = None
    chat_session: int | None = None
    group: str | None = None
    sort: str = "time"
    before: str | None = None


def usage_params(
    since: str | None = Query(default=None, alias="from", max_length=40),
    until: str | None = Query(default=None, alias="to", max_length=40),
    project: str | None = Query(default=None, max_length=100),
    member: str | None = Query(default=None, max_length=100),
    task: str | None = Query(default=None, max_length=40),
    source: str | None = Query(default=None, max_length=40),
    model: str | None = Query(default=None, max_length=200),
    scan_run: int | None = Query(default=None, ge=1),
    chat_session: int | None = Query(default=None, ge=1),
    group: str | None = Query(default=None, max_length=20),
    sort: str = Query(default="time", max_length=10),
    before: str | None = Query(default=None, max_length=400),
) -> UsageParams:
    """Collect the usage query parameters (a FastAPI dependency).

    Returns
    -------
    UsageParams
        The values as given; :func:`build_filter` validates them.
    """
    return UsageParams(
        since=since,
        until=until,
        project=(project or "").strip() or None,
        member=(member or "").strip() or None,
        task=(task or "").strip() or None,
        source=(source or "").strip() or None,
        model=(model or "").strip() or None,
        scan_run=scan_run,
        chat_session=chat_session,
        group=(group or "").strip() or None,
        sort=sort,
        before=before,
    )


@dataclass(frozen=True)
class UsageFilter:
    """A validated usage query: the range and the SQL conditions.

    Attributes
    ----------
    start, end : date
        ``from`` (inclusive) and ``to`` (exclusive).
    conditions : tuple
        The ``WHERE`` terms, the org and the ``created_at`` range first.
    """

    start: date
    end: date
    conditions: tuple[Any, ...]

    @property
    def range(self) -> dict[str, str]:
        """``{"from", "to"}`` as ISO dates."""
        return {"from": self.start.isoformat(), "to": self.end.isoformat()}


def _bad(message: str, code: str) -> ApiError:
    return ApiError(422, message, code=code)


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _next_month(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def _parse_day(raw: str | None) -> date | None:
    if raw is None or not raw.strip():
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise _bad(
            "from and to must be ISO-8601 dates (YYYY-MM-DD)", "bad_date"
        ) from exc


def parse_range(since: str | None, until: str | None) -> tuple[date, date]:
    """Resolve ``from`` / ``to`` into ``(start, end)``, ``end`` exclusive.

    Without either: the current month (UTC). With ``from`` only: to the end
    of that month. With ``to`` only: from the start of the month ``to``'s
    last day is in.

    Parameters
    ----------
    since, until : str or None
        The raw ``from`` / ``to``.

    Returns
    -------
    tuple of (date, date)
        The range.

    Raises
    ------
    ApiError
        ``422 bad_date`` for a malformed date, ``422 bad_range`` when
        ``from`` is not before ``to``, ``422 range_too_long`` past
        :data:`MAX_RANGE_DAYS` days.
    """
    start, end = _parse_day(since), _parse_day(until)
    if start is None and end is None:
        start = date.fromisoformat(f"{current_month()}-01")
        end = _next_month(start)
    elif end is None:
        assert start is not None
        end = _next_month(start)
    elif start is None:
        start = _month_start(end - timedelta(days=1))
    if start >= end:
        raise _bad("from must be before to", "bad_range")
    if (end - start).days > MAX_RANGE_DAYS:
        raise _bad(
            f"a usage range is at most {MAX_RANGE_DAYS} days; narrow it",
            "range_too_long",
        )
    return start, end


def _stamp(day: date) -> str:
    """The ``created_at`` form of a day's start (UTC, seconds)."""
    return f"{day.isoformat()}T00:00:00+00:00"


def build_filter(
    db: Session,
    org_id: int,
    params: UsageParams,
    *,
    project_id: int | None = None,
    user_id: int | None = None,
) -> UsageFilter:
    """Validate ``params`` into a :class:`UsageFilter` for one org.

    Parameters
    ----------
    db : Session
        An open portal DB session (for the slug and uid lookups).
    org_id : int
        The org every row must belong to.
    params : UsageParams
        The raw parameters.
    project_id : int, optional
        Force this project (``/api/projects/{slug}/usage``); the
        ``project`` filter is then ignored.
    user_id : int, optional
        Force this member (``/api/usage/me*``); the ``member`` filter is
        then ignored.

    Returns
    -------
    UsageFilter
        The range and conditions.

    Raises
    ------
    ApiError
        ``422`` with ``bad_date``, ``bad_range``, ``range_too_long`` or
        ``bad_filter``.
    """
    start, end = parse_range(params.since, params.until)
    conditions: list[Any] = [
        col(_E.org_id) == org_id,
        col(_E.created_at) >= _stamp(start),
        col(_E.created_at) < _stamp(end),
    ]
    if project_id is not None:
        conditions.append(col(_E.project_id) == project_id)
    elif params.project is not None:
        found = db.exec(
            select(Project.id).where(
                Project.org_id == org_id, Project.slug == params.project
            )
        ).first()
        if found is not None:
            conditions.append(col(_E.project_id) == found)
        else:  # a removed project's rows keep its slug
            conditions.append(col(_E.project_id).is_(None))
            conditions.append(col(_E.project_slug) == params.project)
    if user_id is not None:
        conditions.append(col(_E.user_id) == user_id)
    elif params.member == "system":
        conditions.append(col(_E.actor_kind) == "system")
    elif params.member is not None:
        found = db.exec(select(User.id).where(User.uid == params.member)).first()
        conditions.append(false() if found is None else col(_E.user_id) == found)
    if params.task is not None:
        if params.task not in USAGE_TASKS:
            raise _bad(f"task is one of {', '.join(USAGE_TASKS)}", "bad_filter")
        conditions.append(col(_E.task) == params.task)
    if params.source is not None:
        if params.source not in USAGE_SOURCES:
            raise _bad(f"source is one of {', '.join(USAGE_SOURCES)}", "bad_filter")
        conditions.append(col(_E.source) == params.source)
    if params.model is not None:
        conditions.append(_model_expr() == params.model)
    if params.scan_run is not None:
        conditions.append(col(_E.scan_run_id) == params.scan_run)
    if params.chat_session is not None:
        if project_id is None and params.project is None:
            raise _bad("chat_session needs project (ids are per project)", "bad_filter")
        conditions.append(col(_E.chat_session_id) == params.chat_session)
    return UsageFilter(start=start, end=end, conditions=tuple(conditions))


def _check_group(group: str | None) -> None:
    if group is not None and group not in GROUPS:
        raise _bad(f"group is one of {', '.join(GROUPS)}", "bad_group")


def _model_expr() -> Any:
    return func.coalesce(col(_E.model_served), col(_E.model_requested))


def _day_expr() -> Any:
    return func.substr(col(_E.created_at), 1, 10)


def _timed(db: Session) -> None:
    """Bound every statement of this transaction by :data:`STATEMENT_TIMEOUT`."""
    db.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))


def _timeout_guard(fn: Callable[[], Any]) -> Any:
    """Run ``fn``; a cancelled statement becomes ``503 usage_timeout``."""
    try:
        return fn()
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) == "57014":  # query_canceled
            raise ApiError(
                503,
                "the usage query took too long; narrow the range or the filters",
                code="usage_timeout",
            ) from exc
        raise


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------


def _totals_columns() -> list[Any]:
    interactive = col(_E.source) != "scan"
    scans = col(_E.source) == "scan"
    return [
        func.count().label("calls"),
        *(func.sum(getattr(_E, name)).label(name) for name in _TOKENS),
        func.sum(col(_E.cost_usd)).label("cost_usd"),
        func.count().filter(col(_E.cost_source) == "unpriced").label("unpriced_calls"),
        func.count().filter(interactive).label("interactive_calls"),
        func.sum(col(_E.cost_usd)).filter(interactive).label("interactive_cost"),
        func.count().filter(scans).label("scans_calls"),
        func.sum(col(_E.cost_usd)).filter(scans).label("scans_cost"),
    ]


def _totals(row: Any) -> dict[str, Any]:
    """``Totals = {calls, <tokens>, cost_usd, unpriced_calls}``."""
    body: dict[str, Any] = {"calls": int(row.calls or 0)}
    for name in _TOKENS:
        body[name] = _int(getattr(row, name))
    body["cost_usd"] = usd(row.cost_usd)
    body["unpriced_calls"] = int(row.unpriced_calls or 0)
    return body


def _split(row: Any) -> dict[str, dict[str, Any]]:
    """``{interactive: {calls, cost_usd}, scans: {calls, cost_usd}}``."""
    return {
        "interactive": {
            "calls": int(row.interactive_calls or 0),
            "cost_usd": usd(row.interactive_cost),
        },
        "scans": {
            "calls": int(row.scans_calls or 0),
            "cost_usd": usd(row.scans_cost),
        },
    }


def _summary_row(db: Session, f: UsageFilter) -> Any:
    return db.execute(sa_select(*_totals_columns()).where(*f.conditions)).one()


def _series(db: Session, f: UsageFilter) -> list[dict[str, Any]]:
    """One entry per day of the range, empty days included."""
    day = _day_expr().label("day")
    rows = db.execute(
        sa_select(
            day,
            func.count().label("calls"),
            func.sum(col(_E.cost_usd)).label("cost_usd"),
            func.sum(col(_E.input_tokens)).label("input_tokens"),
            func.sum(col(_E.output_tokens)).label("output_tokens"),
        )
        .where(*f.conditions)
        .group_by(text("day"))
    ).all()
    by_day = {r.day: r for r in rows}
    series = []
    current = f.start
    while current < f.end:
        key = current.isoformat()
        row = by_day.get(key)
        series.append(
            {
                "day": key,
                "calls": int(row.calls) if row else 0,
                "cost_usd": usd(row.cost_usd if row else None),
                "input_tokens": int(row.input_tokens or 0) if row else 0,
                "output_tokens": int(row.output_tokens or 0) if row else 0,
            }
        )
        current += timedelta(days=1)
    return series


def _group_exprs(group: str) -> list[Any]:
    """The ``GROUP BY`` terms of one :data:`GROUPS` value, labelled ``g0..``."""
    if group == "project":
        terms = [col(_E.project_id), col(_E.project_slug)]
    elif group == "member":
        terms = [
            col(_E.user_id),
            col(_E.actor_kind),
            # A deleted member's rows (user_id NULL) stay apart by label.
            case((col(_E.user_id).is_(None), col(_E.actor_label)), else_=None),
        ]
    elif group == "task":
        terms = [col(_E.task)]
    elif group == "source":
        terms = [col(_E.source)]
    elif group == "model":
        terms = [_model_expr()]
    elif group == "machine":
        terms = [
            col(_E.connection_id),
            # An agent call whose connection was swept keeps its machine name.
            case(
                (
                    and_(col(_E.connection_id).is_(None), col(_E.source) == "agent"),
                    func.coalesce(col(_E.client_name), ""),
                ),
                else_=None,
            ),
        ]
    else:  # day
        terms = [_day_expr()]
    return [term.label(f"g{i}") for i, term in enumerate(terms)]


def _by_name(exprs: list[Any]) -> list[Any]:
    """``GROUP BY`` the output names (one text, so bound constants never differ)."""
    return [text(expr.name) for expr in exprs]


def _label_expr(group: str) -> Any:
    if group == "project":
        return func.max(col(_E.project_name)).label("label")
    if group == "member":
        return func.max(col(_E.actor_label)).label("label")
    if group == "machine":
        return func.max(col(_E.client_name)).label("label")
    return null().label("label")  # the key is the label


def _keys(row: Any, width: int) -> tuple[Any, ...]:
    return tuple(getattr(row, f"g{i}") for i in range(width))


@dataclass(frozen=True)
class _Lookups:
    """Current names for the ids a page of groups mentions."""

    projects: dict[int, str]
    users: dict[int, User]


def _lookups(db: Session, project_ids: set[int], user_ids: set[int]) -> _Lookups:
    projects = (
        {
            p.id: p.name
            for p in db.exec(select(Project).where(col(Project.id).in_(project_ids)))
        }
        if project_ids
        else {}
    )
    users = (
        {u.id: u for u in db.exec(select(User).where(col(User.id).in_(user_ids)))}
        if user_ids
        else {}
    )
    for user in users.values():
        db.expunge(user)
    return _Lookups(projects=projects, users=users)  # type: ignore[arg-type]


def _key_label(
    group: str,
    keys: tuple[Any, ...],
    label: str | None,
    lookups: _Lookups,
    *,
    production: bool,
) -> tuple[Any, str]:
    """A group's ``(key, label)`` for the wire."""
    if group == "project":
        project_id, slug = keys
        return slug, lookups.projects.get(project_id) or label or slug
    if group == "member":
        user_id, kind, _ = keys
        if kind == "system":
            return None, SYSTEM_LABEL
        user = lookups.users.get(user_id) if user_id is not None else None
        if user is None:
            return None, label or "Member"
        return user.uid, actor_label(user, production=production)  # type: ignore[arg-type]
    if group == "machine":
        connection_id, orphan = keys
        if connection_id is not None:
            return connection_id, label or UNKNOWN_MACHINE_LABEL
        if orphan is None:
            return None, PORTAL_LABEL
        return None, orphan or UNKNOWN_MACHINE_LABEL
    (value,) = keys
    return value, value


def _group_rows(
    db: Session, f: UsageFilter, group: str, *, production: bool
) -> list[dict[str, Any]]:
    """The ``groups`` of ``/api/usage`` (and the aggregated CSV's rows)."""
    exprs = _group_exprs(group)
    width = len(exprs)
    rows = db.execute(
        sa_select(*exprs, _label_expr(group), *_totals_columns())
        .where(*f.conditions)
        .group_by(*_by_name(exprs))
    ).all()
    tops: dict[tuple[Any, ...], tuple[Decimal, int, str, str]] = {}
    if group == "member":
        tops = _top_projects(db, f, exprs)
    project_ids = {r.g0 for r in rows if group == "project" and r.g0 is not None}
    project_ids |= {t[3] for t in tops.values() if isinstance(t[3], int)}
    user_ids = {r.g0 for r in rows if group == "member" and r.g0 is not None}
    lookups = _lookups(db, project_ids, user_ids)
    out: list[dict[str, Any]] = []
    for row in rows:
        keys = _keys(row, width)
        key, label = _key_label(group, keys, row.label, lookups, production=production)
        entry = {"key": key, "label": label, **_totals(row), **_split(row)}
        if group == "member":
            top = tops.get(keys)
            entry["top_project"] = (
                None
                if top is None
                else {
                    "slug": top[2],
                    "name": lookups.projects.get(top[3]) or top[4],  # type: ignore[misc]
                    "cost_usd": usd(top[0]),
                }
            )
        entry["_cost"] = row.cost_usd or _ZERO
        out.append(entry)
    if group == "day":
        out.sort(key=lambda e: e["key"])
    else:
        out.sort(key=lambda e: (-e["_cost"], -e["calls"], str(e["label"])))
    for entry in out:
        del entry["_cost"]
    return out


def _top_projects(
    db: Session, f: UsageFilter, member_exprs: list[Any]
) -> dict[tuple[Any, ...], Any]:
    """Each member group's costliest project in the range (ties by calls)."""
    rows = db.execute(
        sa_select(
            *member_exprs,
            col(_E.project_id).label("pid"),
            col(_E.project_slug).label("slug"),
            func.max(col(_E.project_name)).label("name"),
            func.sum(col(_E.cost_usd)).label("cost"),
            func.count().label("calls"),
        )
        .where(*f.conditions)
        .group_by(*_by_name(member_exprs), col(_E.project_id), col(_E.project_slug))
    ).all()
    best: dict[tuple[Any, ...], Any] = {}
    width = len(member_exprs)
    for row in rows:
        keys = _keys(row, width)
        candidate = (row.cost or _ZERO, int(row.calls), row.slug, row.pid, row.name)
        current = best.get(keys)
        if current is None or (candidate[0], candidate[1]) > (current[0], current[1]):
            best[keys] = candidate
    return best


def _aggregate(
    f: UsageFilter, group: str | None, *, production: bool, series: bool = True
) -> dict[str, Any]:
    """``range``, ``totals``, ``split``, ``series`` and (with ``group``) ``groups``."""

    def run() -> dict[str, Any]:
        with get_session() as db:
            _timed(db)
            summary = _summary_row(db, f)
            body: dict[str, Any] = {
                "range": f.range,
                "totals": _totals(summary),
                "split": _split(summary),
            }
            if series:
                body["series"] = _series(db, f)
            body["groups"] = (
                []
                if group is None
                else _group_rows(db, f, group, production=production)
            )
            return body

    return _timeout_guard(run)


def _filter_for(
    org_id: int,
    params: UsageParams,
    *,
    project_id: int | None = None,
    user_id: int | None = None,
) -> UsageFilter:
    with get_session() as db:
        return build_filter(db, org_id, params, project_id=project_id, user_id=user_id)


# ---------------------------------------------------------------------------
# Calls (keyset-paged)
# ---------------------------------------------------------------------------


def _encode_cursor(sort: str, value: Any, row_id: int) -> str:
    raw = json.dumps({"s": sort, "v": value, "i": row_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _bad_cursor() -> ApiError:
    return _bad("before is not a cursor of this listing", "bad_cursor")


def _decode_cursor(raw: str, sort: str) -> tuple[Any, int]:
    try:
        padded = raw + "=" * (-len(raw) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        value, row_id = data["v"], int(data["i"])
        if data["s"] != sort:
            raise _bad_cursor()
        if sort == "time":
            if not isinstance(value, str):
                raise _bad_cursor()
        elif value is not None:
            value = Decimal(str(value))
    except (
        ValueError,
        KeyError,
        TypeError,
        binascii.Error,
        InvalidOperation,
        UnicodeDecodeError,
    ) as exc:
        raise _bad_cursor() from exc
    return value, row_id


def _order(sort: str) -> list[Any]:
    if sort == "cost":
        return [col(_E.cost_usd).desc().nulls_last(), col(_E.id).desc()]
    return [col(_E.created_at).desc(), col(_E.id).desc()]


def _after(sort: str, value: Any, row_id: int) -> Any:
    """The keyset condition: rows strictly after ``(value, row_id)``."""
    if sort == "time":
        return or_(
            col(_E.created_at) < value,
            and_(col(_E.created_at) == value, col(_E.id) < row_id),
        )
    if value is None:
        return and_(col(_E.cost_usd).is_(None), col(_E.id) < row_id)
    return or_(
        col(_E.cost_usd) < value,
        and_(col(_E.cost_usd) == value, col(_E.id) < row_id),
        col(_E.cost_usd).is_(None),
    )


def _call_row(event: UsageEvent, user_uid: str | None) -> dict[str, Any]:
    """``CallRow``: the CSV columns plus ``id``, ``project_name`` and ``user_uid``."""
    body: dict[str, Any] = {"id": event.id}
    for name in CSV_CALL_COLUMNS:
        body[name] = getattr(event, name)
    body["cost_usd"] = _usd_or_none(event.cost_usd)
    body["project_name"] = event.project_name
    body["user_uid"] = user_uid
    return body


def _cursor_value(sort: str, event: UsageEvent) -> Any:
    if sort == "time":
        return event.created_at
    return None if event.cost_usd is None else str(event.cost_usd)


def _query_calls(
    f: UsageFilter,
    sort: str,
    *,
    after: tuple[Any, int] | None,
    limit: int,
) -> tuple[list[tuple[UsageEvent, str | None]], bool]:
    """One page of ``(event, user uid)``; whether more rows follow."""
    conditions = list(f.conditions)
    if after is not None:
        conditions.append(_after(sort, *after))
    with get_session() as db:
        _timed(db)
        rows = db.exec(
            select(UsageEvent, User.uid)
            .outerjoin(User, col(User.id) == col(_E.user_id))
            .where(*conditions)
            .order_by(*_order(sort))
            .limit(limit + 1)
        ).all()
        db.expunge_all()
    return [(e, uid) for e, uid in rows[:limit]], len(rows) > limit


def _calls_page(f: UsageFilter, params: UsageParams) -> dict[str, Any]:
    if params.sort not in SORTS:
        raise _bad(f"sort is one of {', '.join(SORTS)}", "bad_sort")
    after = (
        None if params.before is None else _decode_cursor(params.before, params.sort)
    )
    rows, more = _timeout_guard(
        lambda: _query_calls(f, params.sort, after=after, limit=PAGE_SIZE)
    )
    items = [_call_row(event, uid) for event, uid in rows]
    following = None
    if more and rows:
        last = rows[-1][0]
        following = _encode_cursor(
            params.sort,
            _cursor_value(params.sort, last),
            last.id,  # type: ignore[arg-type]
        )
    return {"items": items, "next": following}


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def _truncated(f: UsageFilter, max_rows: int) -> bool:
    """Whether more than ``max_rows`` calls match (an index probe, not a count)."""

    def probe() -> bool:
        with get_session() as db:
            _timed(db)
            found = db.execute(
                sa_select(col(_E.id))
                .where(*f.conditions)
                .order_by(*_order("time"))
                .offset(max_rows)
                .limit(1)
            ).first()
            return found is not None

    return _timeout_guard(probe)


def _csv_call_cells(event: UsageEvent) -> list[Any]:
    cells = [getattr(event, name) for name in CSV_CALL_COLUMNS]
    cells[CSV_CALL_COLUMNS.index("cost_usd")] = _usd_text(event.cost_usd)
    return cells


def iter_call_csv(
    f: UsageFilter, *, truncated: bool, max_rows: int = CSV_MAX_ROWS
) -> Iterator[str]:
    """Stream the filtered calls as CSV lines, newest first.

    Parameters
    ----------
    f : UsageFilter
        The query.
    truncated : bool
        Whether the export is cut at ``max_rows`` (:func:`_truncated`); if
        so the last line is ``# truncated at <max_rows> rows``.
    max_rows : int, optional
        The cap on data rows.

    Yields
    ------
    str
        The header, one line per call, and the truncation line if any.
    """
    yield csv_line(list(CSV_CALL_COLUMNS))
    after: tuple[Any, int] | None = None
    sent = 0
    while sent < max_rows:
        rows, more = _query_calls(
            f, "time", after=after, limit=min(CSV_CHUNK, max_rows - sent)
        )
        for event, _uid in rows:
            yield csv_line(_csv_call_cells(event))
        sent += len(rows)
        if not more or not rows:
            break
        last = rows[-1][0]
        after = (last.created_at, last.id)  # type: ignore[assignment]
    if truncated:
        yield truncated_line(max_rows)


def _group_csv_lines(
    groups: list[dict[str, Any]], group: str, *, max_rows: int = CSV_MAX_ROWS
) -> list[str]:
    columns = list(CSV_GROUP_COLUMNS)
    if group == "member":
        columns += CSV_MEMBER_COLUMNS
    lines = [csv_line(columns)]
    for entry in groups[:max_rows]:
        cells: list[Any] = [
            entry["key"],
            entry["label"],
            entry["calls"],
            *(entry[name] for name in _TOKENS),
            _usd_text(Decimal(str(entry["cost_usd"]))),
            entry["unpriced_calls"],
            entry["interactive"]["calls"],
            _usd_text(Decimal(str(entry["interactive"]["cost_usd"]))),
            entry["scans"]["calls"],
            _usd_text(Decimal(str(entry["scans"]["cost_usd"]))),
        ]
        if group == "member":
            top = entry["top_project"]
            cells += (
                [None, None]
                if top is None
                else [top["slug"], _usd_text(Decimal(str(top["cost_usd"])))]
            )
        lines.append(csv_line(cells))
    if len(groups) > max_rows:
        lines.append(truncated_line(max_rows))
    return lines


def _csv_response(
    f: UsageFilter, params: UsageParams, *, production: bool, filename: str
) -> StreamingResponse:
    """The call-row CSV, or (with ``group``) the aggregated one."""
    _check_group(params.group)
    max_rows = CSV_MAX_ROWS
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if params.group is not None:
        group = params.group
        groups = _timeout_guard(lambda: _grouped(f, group, production=production))
        if len(groups) > max_rows:
            headers[TRUNCATED_HEADER] = "1"
        body: Iterator[str] = iter(_group_csv_lines(groups, group, max_rows=max_rows))
    else:
        truncated = _truncated(f, max_rows)
        if truncated:
            headers[TRUNCATED_HEADER] = "1"
        body = iter_call_csv(f, truncated=truncated, max_rows=max_rows)
    return StreamingResponse(
        body, media_type="text/csv; charset=utf-8", headers=headers
    )


def _grouped(f: UsageFilter, group: str, *, production: bool) -> list[dict[str, Any]]:
    with get_session() as db:
        _timed(db)
        return _group_rows(db, f, group, production=production)


# ---------------------------------------------------------------------------
# The org's usage (owners, org admins, readers)
# ---------------------------------------------------------------------------


def _production(request: Request) -> bool:
    return portal_state(request).mode == "production"


@usage_router.get("/api/usage")
def get_usage(
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_USAGE)),
    params: UsageParams = Depends(usage_params),
) -> dict[str, Any]:
    """The org's usage: totals, the interactive / scans split, a daily series.

    Returns
    -------
    dict
        ``{range: {from, to}, totals: Totals, split: {interactive, scans},
        series: [{day, calls, cost_usd, input_tokens, output_tokens}],
        groups: [Totals + {key, label, interactive, scans} (+ top_project
        for group=member)]}``; ``groups`` is empty without ``group``.
    """
    _check_group(params.group)
    f = _filter_for(access.org_id, params)
    return _aggregate(f, params.group, production=_production(request))


@usage_router.get("/api/usage/calls")
def get_usage_calls(
    access: OrgAccess = Depends(org_access(Action.ORG_USAGE)),
    params: UsageParams = Depends(usage_params),
) -> dict[str, Any]:
    """The org's calls, newest (``sort=time``) or costliest (``sort=cost``) first.

    Returns
    -------
    dict
        ``{items: [CallRow], next}``: :data:`PAGE_SIZE` rows; ``next`` is
        the opaque ``before`` of the following page (``null`` on the last).
    """
    return _calls_page(_filter_for(access.org_id, params), params)


@usage_router.get("/api/usage.csv")
def get_usage_csv(
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_USAGE)),
    params: UsageParams = Depends(usage_params),
) -> StreamingResponse:
    """The org's calls as CSV, or with ``group`` the aggregated table.

    At most :data:`CSV_MAX_ROWS` rows; a cut export ends with ``# truncated
    at 50000 rows`` and carries ``X-WhyGraph-Truncated: 1``. Every cell a
    spreadsheet would read as a formula is prefixed with ``'``.
    """
    f = _filter_for(access.org_id, params)
    suffix = f"-{params.group}" if params.group else ""
    return _csv_response(
        f,
        params,
        production=_production(request),
        filename=f"whygraph-usage-{access.org_slug}{suffix}.csv",
    )


@usage_router.get("/api/projects/{slug}/usage")
def get_project_usage(
    request: Request,
    project: BoundProject = Depends(project_access(Action.PROJECT_USAGE)),
    params: UsageParams = Depends(usage_params),
) -> dict[str, Any]:
    """One project's usage, with its members' spend (project admins).

    Returns
    -------
    dict
        ``{range, totals, split, series, members: [group=member rows for
        this project]}``; ``members`` is empty in local mode.
    """
    production = _production(request)
    f = _filter_for(project.org_id, params, project_id=project.id)
    body = _aggregate(f, "member" if production else None, production=production)
    body["members"] = body.pop("groups")
    return body


# ---------------------------------------------------------------------------
# My usage (production)
# ---------------------------------------------------------------------------


@usage_me_router.get("/api/usage/me")
def get_my_usage(
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_READ)),
    params: UsageParams = Depends(usage_params),
) -> dict[str, Any]:
    """The caller's own usage in this org (any ``member`` filter is ignored).

    Returns
    -------
    dict
        The ``GET /api/usage`` shape over the caller's rows only.
    """
    _check_group(params.group)
    f = _filter_for(access.org_id, params, user_id=access.user_id)
    return _aggregate(f, params.group, production=True)


@usage_me_router.get("/api/usage/me/calls")
def get_my_usage_calls(
    access: OrgAccess = Depends(org_access(Action.ORG_READ)),
    params: UsageParams = Depends(usage_params),
) -> dict[str, Any]:
    """The caller's own calls (``GET /api/usage/calls``, forced to the caller)."""
    f = _filter_for(access.org_id, params, user_id=access.user_id)
    return _calls_page(f, params)


@usage_me_router.get("/api/usage/me.csv")
def get_my_usage_csv(
    access: OrgAccess = Depends(org_access(Action.ORG_READ)),
    params: UsageParams = Depends(usage_params),
) -> StreamingResponse:
    """The caller's own calls as CSV (``GET /api/usage.csv``, forced to the caller)."""
    f = _filter_for(access.org_id, params, user_id=access.user_id)
    suffix = f"-{params.group}" if params.group else ""
    return _csv_response(
        f,
        params,
        production=True,
        filename=f"whygraph-my-usage-{access.org_slug}{suffix}.csv",
    )


# ---------------------------------------------------------------------------
# Payload fields (plan section 4.12)
# ---------------------------------------------------------------------------


def _gauge(spent: Decimal, limit: BudgetLimit | None) -> dict[str, Any]:
    """``{spent_usd, budget_usd, pct, hard_stop, blocked}`` of one scope."""
    if limit is None:
        return {
            "spent_usd": usd(spent),
            "budget_usd": None,
            "pct": None,
            "hard_stop": False,
            "blocked": False,
        }
    return {
        "spent_usd": usd(spent),
        "budget_usd": _cents(limit.monthly_usd),
        "pct": _pct(spent, limit.monthly_usd),
        "hard_stop": limit.hard_stop,
        "blocked": limit.hard_stop and spent >= limit.monthly_usd,
    }


def state_usage(state: PortalState, access: OrgAccess | None) -> dict[str, Any] | None:
    """The ``usage`` block of ``GET /api/portal/state`` (everything the banners need).

    Parameters
    ----------
    state : PortalState
        The portal (its spend book and budget map; no SQL but the names of
        the projects over 50%).
    access : OrgAccess or None
        The caller's access to the request's org.

    Returns
    -------
    dict or None
        ``{month, resets_at, me, org, projects_over}``: ``me`` for a
        production membership (``null`` in local mode and for a
        ``reader``), ``org`` and ``projects_over`` only with
        ``org.usage``. ``None`` without access (before setup, no org).
    """
    if access is None:
        return None
    month = current_month()
    budgets = state.budgets.for_org(access.org_id)
    me = None
    if state.mode == "production" and access.role is not Role.READER:
        limit = None
        if budgets is not None:
            limit = budgets.members.get(access.user_id) or budgets.member_default
        me = _gauge(state.spend.spent(access.org_id, user_id=access.user_id), limit)
    org = projects_over = None
    if allowed(access.role, Action.ORG_USAGE):
        org = _gauge(
            state.spend.spent(access.org_id),
            None if budgets is None else budgets.org,
        )
        projects_over = _projects_over(state, access.org_id, budgets)
    return {
        "month": month,
        "resets_at": month_resets_at(month),
        "me": me,
        "org": org,
        "projects_over": projects_over,
    }


def _projects_over(
    state: PortalState, org_id: int, budgets: Any
) -> list[dict[str, Any]]:
    """The org's projects at or over 50% of their budget, highest first."""
    if budgets is None or not budgets.projects:
        return []
    over: list[tuple[float, int]] = []
    for project_id, limit in budgets.projects.items():
        pct = _pct(state.spend.spent(org_id, project_id=project_id), limit.monthly_usd)
        if pct is not None and pct >= 50:
            over.append((pct, project_id))
    if not over:
        return []
    with get_session() as db:
        names = {
            p.id: (p.slug, p.name)
            for p in db.exec(
                select(Project).where(
                    Project.org_id == org_id,
                    col(Project.id).in_([pid for _, pid in over]),
                )
            )
        }
    over.sort(key=lambda item: (-item[0], item[1]))
    return [
        {"slug": names[pid][0], "name": names[pid][1], "pct": pct}
        for pct, pid in over
        if pid in names
    ]


def llm_block(
    state: PortalState,
    org_id: int,
    project_id: int,
    role: ProjectRole,
    user_id: int,
) -> tuple[str | None, str | None]:
    """A caller's ``(llm_block, llm_block_scope)`` on a project.

    The same rule as :func:`~whygraph.portal.deps.bind_project`, computed
    without a context (the projects list binds none): a viewer is
    ``"role"``; anyone an exhausted hard-stopped budget covers is
    ``"budget_exceeded"`` with its scope; the role wins.

    Parameters
    ----------
    state : PortalState
        The portal (its budget map).
    org_id, project_id : int
        The project.
    role : ProjectRole
        The caller's effective role on it.
    user_id : int
        The caller.

    Returns
    -------
    tuple of (str or None, str or None)
        ``("role", None)``, ``("budget_exceeded", scope)`` or
        ``(None, None)``.
    """
    if role is ProjectRole.VIEWER:
        return "role", None
    blocked = state.budgets.blocked_scope(org_id, project_id, user_id)
    if blocked is not None:
        return "budget_exceeded", blocked
    return None, None


def project_usage(
    state: PortalState, org_id: int, project_id: int, role: ProjectRole
) -> dict[str, Any] | None:
    """A project payload's ``usage`` block (``None`` without ``project.usage``).

    Parameters
    ----------
    state : PortalState
        The portal (its spend book and budget map).
    org_id, project_id : int
        The project.
    role : ProjectRole
        The caller's effective role on it.

    Returns
    -------
    dict or None
        ``{month_spend_usd, budget: {monthly_usd, hard_stop} | null, pct}``.
    """
    if not project_allowed(role, Action.PROJECT_USAGE):
        return None
    spent = state.spend.spent(org_id, project_id=project_id)
    budgets = state.budgets.for_org(org_id)
    limit = None if budgets is None else budgets.projects.get(project_id)
    return {
        "month_spend_usd": usd(spent),
        "budget": None
        if limit is None
        else {"monthly_usd": _cents(limit.monthly_usd), "hard_stop": limit.hard_stop},
        "pct": None if limit is None else _pct(spent, limit.monthly_usd),
    }


def _this_month(org_id: int) -> list[Any]:
    """The org's priced rows of the current UTC month."""
    month = current_month()
    return [
        col(_E.org_id) == org_id,
        col(_E.created_at) >= f"{month}-01T00:00:00+00:00",
        col(_E.cost_source).in_(COUNTED_COST_SOURCES),
        col(_E.cost_usd).is_not(None),
        col(_E.user_id).is_not(None),
    ]


def member_month_spend(org_id: int) -> dict[int, dict[str, float]]:
    """Each member's spend this month, with the interactive / scans split.

    One grouped query (``GET /api/org/members`` for ``org.usage`` callers).

    Parameters
    ----------
    org_id : int
        The organization.

    Returns
    -------
    dict
        ``users.id`` -> ``{"total", "interactive", "scans"}`` in USD; a
        member who spent nothing is absent.
    """
    scans = col(_E.source) == "scan"
    with get_session() as db:
        rows = db.execute(
            sa_select(
                col(_E.user_id),
                func.sum(col(_E.cost_usd)),
                func.sum(col(_E.cost_usd)).filter(~scans),
                func.sum(col(_E.cost_usd)).filter(scans),
            )
            .where(*_this_month(org_id))
            .group_by(col(_E.user_id))
        ).all()
    return {
        user_id: {
            "total": usd(total),
            "interactive": usd(interactive),
            "scans": usd(scan),
        }
        for user_id, total, interactive, scan in rows
    }


def member_spend_fields(spend: dict[int, dict[str, float]], user_id: int) -> dict:
    """A member row's ``month_spend_usd`` and ``month_split``.

    Parameters
    ----------
    spend : dict
        :func:`member_month_spend`'s result.
    user_id : int
        The member.

    Returns
    -------
    dict
        ``{"month_spend_usd", "month_split": {"interactive", "scans"}}``.
    """
    mine = spend.get(user_id) or {"total": 0.0, "interactive": 0.0, "scans": 0.0}
    return {
        "month_spend_usd": mine["total"],
        "month_split": {"interactive": mine["interactive"], "scans": mine["scans"]},
    }


def project_month_spend(org_id: int, project_id: int) -> dict[int, float]:
    """Each person's spend on one project this month (one grouped query).

    Parameters
    ----------
    org_id, project_id : int
        The project.

    Returns
    -------
    dict
        ``users.id`` -> USD; a person who spent nothing is absent.
    """
    with get_session() as db:
        rows = db.execute(
            sa_select(col(_E.user_id), func.sum(col(_E.cost_usd)))
            .where(*_this_month(org_id), col(_E.project_id) == project_id)
            .group_by(col(_E.user_id))
        ).all()
    return {user_id: usd(total) for user_id, total in rows}


def account_usage(
    state: PortalState, user_id: int, orgs: list[tuple[int, str, str, str]]
) -> dict[str, Any]:
    """``GET /api/account/usage``: the caller's spend this month in each org.

    Parameters
    ----------
    state : PortalState
        The portal (its spend book and budget map).
    user_id : int
        The caller.
    orgs : list of (org_id, slug, name, url)
        The caller's memberships (never an instance admin's ``reader``
        access), in display order.

    Returns
    -------
    dict
        ``{month, resets_at, orgs: [{slug, name, url, spent_usd, calls,
        budget_usd | null, pct | null, hard_stop}]}``; the budget is the
        member's override, else the org's member default.
    """
    month = current_month()
    calls: dict[int, int] = {}
    if orgs:
        with get_session() as db:
            calls = {
                org_id: int(count)
                for org_id, count in db.execute(
                    sa_select(col(_E.org_id), func.count())
                    .where(
                        col(_E.org_id).in_([org[0] for org in orgs]),
                        col(_E.user_id) == user_id,
                        col(_E.created_at) >= f"{month}-01T00:00:00+00:00",
                    )
                    .group_by(col(_E.org_id))
                ).all()
            }
    out = []
    for org_id, slug, name, url in orgs:
        budgets = state.budgets.for_org(org_id)
        limit = None
        if budgets is not None:
            limit = budgets.members.get(user_id) or budgets.member_default
        gauge = _gauge(state.spend.spent(org_id, user_id=user_id), limit)
        out.append(
            {
                "slug": slug,
                "name": name,
                "url": url,
                "spent_usd": gauge["spent_usd"],
                "calls": calls.get(org_id, 0),
                "budget_usd": gauge["budget_usd"],
                "pct": gauge["pct"],
                "hard_stop": gauge["hard_stop"],
            }
        )
    return {"month": month, "resets_at": month_resets_at(month), "orgs": out}


__all__ = [
    "CSV_CALL_COLUMNS",
    "CSV_GROUP_COLUMNS",
    "CSV_MAX_ROWS",
    "GROUPS",
    "MAX_RANGE_DAYS",
    "PAGE_SIZE",
    "PORTAL_LABEL",
    "UsageFilter",
    "UsageParams",
    "account_usage",
    "build_filter",
    "iter_call_csv",
    "llm_block",
    "member_month_spend",
    "member_spend_fields",
    "parse_range",
    "project_month_spend",
    "project_usage",
    "state_usage",
    "usage_me_router",
    "usage_params",
    "usage_router",
    "usd",
]
