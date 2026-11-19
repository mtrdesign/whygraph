"""The admin Dashboard's stats routes (plan 59 section 4.12).

Three ``GET`` routes on :func:`~whygraph.portal.deps.instance_access` (base
host, admin session, production only):

- ``/api/admin/stats/overview``: the seven KPI tiles and the attention list
  (D58), nothing more;
- ``/api/admin/stats/series?metric=spend|scans``: LLM spend per day by
  provider (the top five and ``other``) or scans per day by status, both
  stacked on the server with :func:`~whygraph.chat.charts.build_stack`;
- ``/api/admin/stats/top?metric=spend|storage|agent_calls``: the top orgs.

Every route takes ``from`` / ``to`` with
:func:`~whygraph.portal.usage_routes.parse_range` (the current month without
them, at most 400 days, ``422`` on a bad range); the SPA always sends both.
Every statement runs under the usage routes' statement timeout, and a
cancelled one answers ``503 usage_timeout`` for that call only (D56). Days
are bucketed with ``substr(col, 1, 10)`` (timestamps are ISO text in UTC);
storage reads carry each project's latest sample forward (section 4.10);
every agent-call figure counts only :data:`~whygraph.portal.models.AGENT_CALL_SOURCES`
(the Chat assistant's own tool calls are not agent calls).

Soft-deleted orgs and projects are left out of the Users / Orgs / Projects
counts, of the top lists and of every attention kind but the one that names
them (a deleted item still holding data, D14). The money, token, agent-call
and scan figures are ledgers of what happened, and the Storage tile is what
the data directory holds until a purge, so they keep a deleted item's rows:
the LLM spend tile is :meth:`~whygraph.portal.usage_store.SpendBook.total`,
which counts every org.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, text
from sqlalchemy import select as sa_select
from sqlmodel import Session, col, select

from whygraph.chat.charts import build_stack

from . import updates
from .authz import Role
from .db import get_session
from .deps import PortalState, instance_access, portal_state
from .models import (
    AGENT_CALL_SOURCES,
    AgentCallDay,
    Membership,
    Organization,
    Project,
    ScanRun,
    UsageEvent,
    User,
)
from .security import Principal
from .storage import latest_project_bytes
from .usage import actor_label
from .usage_routes import (
    ADMIN_GROUPS,
    UsageFilter,
    _aggregate,
    _stamp,
    _timed,
    _timeout_guard,
    parse_range,
    usd,
)
from .usage_store import COUNTED_COST_SOURCES

admin_stats_router = APIRouter()
"""The Dashboard's stats routes (production only, through ``instance_access``)."""

TOP_PROVIDERS = 5
"""Providers the spend series keeps apart; the rest are summed as ``other``."""

OTHER = "other"
"""The spend series' bucket of every provider past :data:`TOP_PROVIDERS`."""

FAILED_SCAN_WINDOW = timedelta(hours=24)
"""How recent (by ``queued_at``) a failing project's run must be to be listed."""

FAILING_STATUSES: tuple[str, ...] = ("failed", "interrupted")
"""A newest finished run in one of these makes a project failing (section 4.8)."""

AGENT_CALL_DAYS = 7
"""The Agent calls tile's window, in UTC days ending today."""

MAX_TOP = 50
"""The largest ``limit`` of the top list."""

_E = UsageEvent


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    """A timestamp in the stored form (``models._now``: UTC, seconds)."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _tile(value: Any, previous: Any) -> dict[str, Any]:
    return {"value": value, "previous": previous}


# ---------------------------------------------------------------------------
# The overview
# ---------------------------------------------------------------------------


def _storage_on(db: Session, day: date) -> int | None:
    """The total of every project's latest sample on or before ``day``.

    ``None`` when no project had been measured by then.
    """
    rows = db.execute(
        text(
            "SELECT DISTINCT ON (project_id) "
            "history_bytes + git_bytes + codegraph_bytes + worktree_bytes "
            "+ runs_bytes FROM storage_samples WHERE day <= :day "
            "ORDER BY project_id, day DESC"
        ),
        {"day": day.isoformat()},
    ).all()
    if not rows:
        return None
    return sum(int(size) for (size,) in rows)


def _previous_month_spend(db: Session, today: date) -> Decimal:
    """Last month's priced spend from its first day through the same day number.

    The day is clamped to that month's length. The filter is the spend
    book's (counted cost sources, a cost) with no user condition, so scan
    spend is in it as it is in :meth:`SpendBook.total`.
    """
    month_start = today.replace(day=1)
    prev_start = (month_start - timedelta(days=1)).replace(day=1)
    days = min(today.day, (month_start - prev_start).days)
    prev_end = prev_start + timedelta(days=days)
    total = db.execute(
        sa_select(func.sum(col(_E.cost_usd))).where(
            col(_E.created_at) >= _stamp(prev_start),
            col(_E.created_at) < _stamp(prev_end),
            col(_E.cost_source).in_(COUNTED_COST_SOURCES),
            col(_E.cost_usd).is_not(None),
        )
    ).scalar()
    return Decimal(total or 0)


def _kpis(
    db: Session,
    state: PortalState,
    start: date,
    end: date,
    now: datetime,
    storage: dict[int, tuple[int, int]],
) -> dict[str, dict[str, Any]]:
    """The seven tiles, each ``{value, previous}`` (section 4.12's tile table)."""
    since, until = _stamp(start), _stamp(end)
    today = now.date()

    users, new_users = db.execute(
        sa_select(
            func.count(),
            func.count().filter(
                col(User.created_at) >= since, col(User.created_at) < until
            ),
        ).where(col(User.deleted_at).is_(None))
    ).one()
    orgs, new_orgs = db.execute(
        sa_select(
            func.count(),
            func.count().filter(
                col(Organization.created_at) >= since,
                col(Organization.created_at) < until,
            ),
        ).where(col(Organization.deleted_at).is_(None))
    ).one()
    projects = db.execute(
        sa_select(func.count())
        .select_from(Project)
        .join(Organization, col(Organization.id) == col(Project.org_id))
        .where(
            col(Project.deleted_at).is_(None), col(Organization.deleted_at).is_(None)
        )
    ).scalar_one()

    tokens = db.execute(
        sa_select(
            func.coalesce(func.sum(col(_E.input_tokens)), 0)
            + func.coalesce(func.sum(col(_E.output_tokens)), 0)
        ).where(col(_E.created_at) >= _stamp(today.replace(day=1)))
    ).scalar_one()

    first = (today - timedelta(days=AGENT_CALL_DAYS - 1)).isoformat()
    prior = (today - timedelta(days=2 * AGENT_CALL_DAYS - 1)).isoformat()
    calls, calls_before = db.execute(
        sa_select(
            func.coalesce(
                func.sum(col(AgentCallDay.calls)).filter(
                    col(AgentCallDay.day) >= first
                ),
                0,
            ),
            func.coalesce(
                func.sum(col(AgentCallDay.calls)).filter(col(AgentCallDay.day) < first),
                0,
            ),
        ).where(
            col(AgentCallDay.source).in_(AGENT_CALL_SOURCES),
            col(AgentCallDay.day) >= prior,
            col(AgentCallDay.day) <= today.isoformat(),
        )
    ).one()

    return {
        "users": _tile(int(users), int(users) - int(new_users)),
        "orgs": _tile(int(orgs), int(orgs) - int(new_orgs)),
        "projects": _tile(int(projects), None),
        "llm_spend_mtd": _tile(
            usd(state.spend.total()), usd(_previous_month_spend(db, today))
        ),
        "tokens_mtd": _tile(int(tokens), None),
        "storage_bytes": _tile(
            sum(size for _, size in storage.values()), _storage_on(db, start)
        ),
        "agent_calls_7d": _tile(int(calls), int(calls_before)),
    }


def _org_ref(slug: str, name: str) -> dict[str, str]:
    return {"slug": slug, "name": name}


def _project_ref(org_slug: str, project: Project) -> dict[str, str]:
    return {"org": org_slug, "slug": project.slug, "name": project.name}


def _user_ref(user: User) -> dict[str, str]:
    return {"uid": user.uid, "label": actor_label(user, production=True)}  # type: ignore[arg-type]


def _item(kind: str, **fields: Any) -> dict[str, Any]:
    """One attention item: ``kind`` plus the fields that are set."""
    return {"kind": kind, **{k: v for k, v in fields.items() if v is not None}}


def _failing(db: Session, now: datetime) -> list[dict[str, Any]]:
    """Live projects whose newest finished run failed, queued in the last 24 h.

    An access-lost project is listed as such instead (section 4.12's
    "Projects failing" rule). Newest first.
    """
    last_run = (
        select(ScanRun.id)
        .where(
            col(ScanRun.project_id) == col(Project.id),
            col(ScanRun.finished_at).is_not(None),
        )
        .order_by(col(ScanRun.id).desc())
        .limit(1)
        .correlate(Project)
        .scalar_subquery()
    )
    rows = db.exec(
        select(Project, Organization.slug, Organization.name, ScanRun)
        .join(Organization, col(Organization.id) == col(Project.org_id))
        .join(ScanRun, col(ScanRun.id) == last_run)
        .where(
            col(Project.deleted_at).is_(None),
            col(Organization.deleted_at).is_(None),
            col(Project.access_lost_reason).is_(None),
            col(ScanRun.status).in_(FAILING_STATUSES),
            col(ScanRun.queued_at).is_not(None),
            col(ScanRun.queued_at) >= _iso(now - FAILED_SCAN_WINDOW),
        )
    ).all()
    items = [
        _item(
            "scan_failed",
            org=_org_ref(org_slug, org_name),
            project=_project_ref(org_slug, project),
            at=run.finished_at,
            text=run.status,
        )
        for project, org_slug, org_name, run in rows
    ]
    items.sort(key=lambda item: item["at"], reverse=True)
    return items


def _access_lost(db: Session) -> list[dict[str, Any]]:
    rows = db.exec(
        select(Project, Organization.slug, Organization.name)
        .join(Organization, col(Organization.id) == col(Project.org_id))
        .where(
            col(Project.deleted_at).is_(None),
            col(Organization.deleted_at).is_(None),
            col(Project.access_lost_reason).is_not(None),
        )
        .order_by(Organization.slug, Project.slug)
    ).all()
    return [
        _item(
            "access_lost",
            org=_org_ref(org_slug, org_name),
            project=_project_ref(org_slug, project),
            at=project.access_lost_at,
            text=project.access_lost_reason,
        )
        for project, org_slug, org_name in rows
    ]


def _budgets_stopped(db: Session, state: PortalState) -> list[dict[str, Any]]:
    """One item per exhausted hard-stopped budget of a live org.

    The ``_gauge`` rule (``hard_stop and spent >= monthly_usd``) on the
    org's org and project budgets, and on each member's own budget (an
    override, else the member default) against what that member spent.
    ``text`` is the scope (``org``, ``project`` or ``member``).
    """
    found: list[tuple[int, str, int | None, int | None]] = []
    for org_id in state.budgets.org_ids():
        budgets = state.budgets.for_org(org_id)
        if budgets is None:
            continue
        limit = budgets.org
        if (
            limit is not None
            and limit.hard_stop
            and state.spend.spent(org_id) >= limit.monthly_usd
        ):
            found.append((org_id, "org", None, None))
        for project_id, limit in sorted(budgets.projects.items()):
            spent = state.spend.spent(org_id, project_id=project_id)
            if limit.hard_stop and spent >= limit.monthly_usd:
                found.append((org_id, "project", project_id, None))
        for user_id, spent in sorted(state.spend.spent_by_user(org_id).items()):
            limit = budgets.members.get(user_id) or budgets.member_default
            if limit is not None and limit.hard_stop and spent >= limit.monthly_usd:
                found.append((org_id, "member", None, user_id))
    if not found:
        return []
    orgs = {
        org.id: org
        for org in db.exec(
            select(Organization).where(
                col(Organization.id).in_({f[0] for f in found}),
                col(Organization.deleted_at).is_(None),
            )
        )
    }
    project_ids = {f[2] for f in found if f[2] is not None}
    projects = (
        {
            p.id: p
            for p in db.exec(
                select(Project).where(
                    col(Project.id).in_(project_ids), col(Project.deleted_at).is_(None)
                )
            )
        }
        if project_ids
        else {}
    )
    user_ids = {f[3] for f in found if f[3] is not None}
    users = (
        {u.id: u for u in db.exec(select(User).where(col(User.id).in_(user_ids)))}
        if user_ids
        else {}
    )
    items = []
    for org_id, scope, project_id, user_id in found:
        org = orgs.get(org_id)
        if org is None:
            continue
        fields: dict[str, Any] = {"org": _org_ref(org.slug, org.name), "text": scope}
        if project_id is not None:
            project = projects.get(project_id)
            if project is None:
                continue
            fields["project"] = _project_ref(org.slug, project)
        if user_id is not None:
            user = users.get(user_id)
            if user is None:
                continue
            fields["user"] = _user_ref(user)
        items.append(_item("budget_stopped", **fields))
    return items


def _instance_items(state: PortalState) -> list[dict[str, Any]]:
    """The address check's problems, then an available update."""
    items = [_item("base_check", text=problem) for problem in state.base_check or []]
    block = updates.update_block(state)
    if block is not None and block.get("available"):
        items.append(_item("update", text=block["latest"], url=block["notes_url"]))
    return items


def _password_accounts(db: Session) -> list[dict[str, Any]]:
    """Password accounts that own live orgs, then those that are not admins (D28)."""
    owners = db.exec(
        select(User, func.count(col(Membership.org_id)))
        .join(Membership, col(Membership.user_id) == col(User.id))
        .join(Organization, col(Organization.id) == col(Membership.org_id))
        .where(
            col(User.password_hash).is_not(None),
            col(User.deleted_at).is_(None),
            col(Membership.role) == Role.OWNER.value,
            col(Organization.deleted_at).is_(None),
        )
        .group_by(col(User.id))
        .order_by(User.id)
    ).all()
    items = [
        _item("password_owner", user=_user_ref(user), count=int(count))
        for user, count in owners
    ]
    stranded = db.exec(
        select(User)
        .where(
            col(User.password_hash).is_not(None),
            col(User.deleted_at).is_(None),
            col(User.is_instance_admin).is_(False),
        )
        .order_by(User.id)
    ).all()
    items += [_item("password_not_admin", user=_user_ref(user)) for user in stranded]
    return items


def _suspended(db: Session) -> list[dict[str, Any]]:
    rows = db.exec(
        select(Organization)
        .where(
            col(Organization.deleted_at).is_(None),
            col(Organization.suspended_at).is_not(None),
        )
        .order_by(Organization.slug)
    ).all()
    return [
        _item(
            "org_suspended",
            org=_org_ref(org.slug, org.name),
            at=org.suspended_at,
            text=org.suspended_reason,
        )
        for org in rows
    ]


def _deleted_with_data(
    db: Session, storage: dict[int, tuple[int, int]]
) -> list[dict[str, Any]]:
    """Soft-deleted orgs and projects whose latest samples still hold bytes (D14).

    A deleted org is one item with every project's bytes; a deleted project
    of a live org is its own item. The latest sample is carried forward,
    and the storage prune keeps each project's newest row, so the figure
    lasts until a purge.
    """
    by_org: dict[int, int] = {}
    for org_id, size in storage.values():
        by_org[org_id] = by_org.get(org_id, 0) + size
    items = []
    for org in db.exec(
        select(Organization)
        .where(col(Organization.deleted_at).is_not(None))
        .order_by(Organization.slug)
    ).all():
        size = by_org.get(org.id, 0)  # type: ignore[arg-type]
        if size > 0:
            items.append(
                _item(
                    "deleted_with_data",
                    org=_org_ref(org.slug, org.name),
                    at=org.deleted_at,
                    bytes=size,
                )
            )
    for project, org_slug, org_name in db.exec(
        select(Project, Organization.slug, Organization.name)
        .join(Organization, col(Organization.id) == col(Project.org_id))
        .where(
            col(Project.deleted_at).is_not(None),
            col(Organization.deleted_at).is_(None),
        )
        .order_by(Organization.slug, Project.slug)
    ).all():
        stored = storage.get(project.id)  # type: ignore[arg-type]
        if stored is not None and stored[1] > 0:
            items.append(
                _item(
                    "deleted_with_data",
                    org=_org_ref(org_slug, org_name),
                    project=_project_ref(org_slug, project),
                    at=project.deleted_at,
                    bytes=stored[1],
                )
            )
    return items


def _attention(
    db: Session,
    state: PortalState,
    now: datetime,
    storage: dict[int, tuple[int, int]],
) -> list[dict[str, Any]]:
    """Every attention item, in section 4.12's order of kinds."""
    failing = _failing(db, now)
    lost = _access_lost(db)
    stopped = _budgets_stopped(db, state)
    instance = _instance_items(state)
    password = _password_accounts(db)
    suspended = _suspended(db)
    deleted = _deleted_with_data(db, storage)
    return [*failing, *lost, *stopped, *instance, *password, *suspended, *deleted]


@admin_stats_router.get("/api/admin/stats/overview")
def get_admin_stats_overview(
    request: Request,
    since: str | None = Query(default=None, alias="from", max_length=40),
    until: str | None = Query(default=None, alias="to", max_length=40),
    _: Principal = Depends(instance_access()),
) -> dict[str, Any]:
    """The Dashboard's seven tiles and its attention list (D58).

    Returns
    -------
    dict
        ``{from, to, kpis, attention}``. ``kpis`` holds exactly
        ``users``, ``orgs``, ``projects``, ``llm_spend_mtd``,
        ``tokens_mtd``, ``storage_bytes`` and ``agent_calls_7d``, each
        ``{value, previous}`` (``previous`` is ``null`` where the tile has
        nothing to compare with). ``attention`` is a list of ``{kind,
        org?, project?, user?, at?, bytes?, text?}`` (plus ``count`` on
        ``password_owner`` and ``url`` on ``update``); the kinds, in
        order: ``scan_failed`` (newest first), ``access_lost``,
        ``budget_stopped``, ``base_check``, ``update``,
        ``password_owner``, ``password_not_admin``, ``org_suspended``,
        ``deleted_with_data``. ``503 usage_timeout`` when a statement
        runs past the timeout.
    """
    state = portal_state(request)
    start, end = parse_range(since, until)
    now = _utcnow()

    def run() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with get_session() as db:
            _timed(db)
            storage = latest_project_bytes(db)
            kpis = _kpis(db, state, start, end, now, storage)
            return kpis, _attention(db, state, now, storage)

    kpis, attention = _timeout_guard(run)
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "kpis": kpis,
        "attention": attention,
    }


# ---------------------------------------------------------------------------
# The two series
# ---------------------------------------------------------------------------


def _days(start: date, end: date) -> list[str]:
    days, current = [], start
    while current < end:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _fill_days(rows: list[list[Any]], start: date, end: date) -> list[list[Any]]:
    """Add ``[day, <the first series value>, 0]`` for each day with no row.

    :func:`build_stack` takes its x axis from the rows alone, so a day with
    no row would drop off the chart. The rows come back sorted by day, then
    series (the axis order); the filler's series is the first sorted row's.
    A range with no row at all stays empty.
    """
    if not rows:
        return []

    def order(row: list[Any]) -> tuple[str, str]:
        return row[0], str(row[1])

    rows = sorted(rows, key=order)
    present = {row[0] for row in rows}
    series = rows[0][1]
    filled = rows + [
        [day, series, 0] for day in _days(start, end) if day not in present
    ]
    return sorted(filled, key=order)


def _spend_rows(db: Session, start: date, end: date) -> list[list[Any]]:
    """``[day, provider, cost_usd]``: the top providers by spend, the rest ``other``."""
    day = func.substr(col(_E.created_at), 1, 10).label("day")
    found = db.execute(
        sa_select(
            day,
            col(_E.provider).label("provider"),
            func.sum(col(_E.cost_usd)).label("cost"),
        )
        .where(
            col(_E.created_at) >= _stamp(start),
            col(_E.created_at) < _stamp(end),
            col(_E.cost_source).in_(COUNTED_COST_SOURCES),
            col(_E.cost_usd).is_not(None),
        )
        .group_by(text("day"), text("provider"))
    ).all()
    totals: dict[str, Decimal] = {}
    for row in found:
        totals[row.provider] = totals.get(row.provider, Decimal(0)) + (row.cost or 0)
    top = set(sorted(totals, key=lambda name: (-totals[name], name))[:TOP_PROVIDERS])
    cells: dict[tuple[str, str], Decimal] = {}
    for row in found:
        key = (row.day, row.provider if row.provider in top else OTHER)
        cells[key] = cells.get(key, Decimal(0)) + (row.cost or 0)
    return [[d, provider, usd(cost)] for (d, provider), cost in cells.items()]


def _scan_rows(db: Session, start: date, end: date) -> list[list[Any]]:
    """``[day, status, runs]`` by ``queued_at``; runs without one are left out."""
    day = func.substr(col(ScanRun.queued_at), 1, 10).label("day")
    found = db.execute(
        sa_select(day, col(ScanRun.status).label("status"), func.count().label("runs"))
        .where(
            col(ScanRun.queued_at).is_not(None),
            col(ScanRun.queued_at) >= _stamp(start),
            col(ScanRun.queued_at) < _stamp(end),
        )
        .group_by(text("day"), text("status"))
    ).all()
    return [[row.day, row.status, int(row.runs)] for row in found]


_SERIES = {
    "spend": (("day", "provider", "cost_usd"), _spend_rows),
    "scans": (("day", "status", "runs"), _scan_rows),
}


@admin_stats_router.get("/api/admin/stats/series")
def get_admin_stats_series(
    metric: Literal["spend", "scans"] = Query(...),
    since: str | None = Query(default=None, alias="from", max_length=40),
    until: str | None = Query(default=None, alias="to", max_length=40),
    _: Principal = Depends(instance_access()),
) -> dict[str, Any]:
    """A stacked daily series: spend by provider, or scans by status.

    Returns
    -------
    dict
        ``{from, to, columns, rows, stack}``: ``columns`` is ``["day",
        "provider", "cost_usd"]`` (``spend``) or ``["day", "status",
        "runs"]`` (``scans``); ``rows`` has one row per day and series,
        with ``[day, <series>, 0]`` for a day that had nothing (none when
        the range has no row at all); ``stack`` is
        :func:`~whygraph.chat.charts.build_stack`'s dict (``series``,
        ``series_index``, ``series_values``, ``x_values``, ``cells``,
        ``filled_cells``). ``422`` for another metric or a bad range,
        ``503 usage_timeout`` on a timeout.
    """
    start, end = parse_range(since, until)
    columns, query = _SERIES[metric]

    def run() -> list[list[Any]]:
        with get_session() as db:
            _timed(db)
            return query(db, start, end)

    rows = _fill_days(_timeout_guard(run), start, end)
    stack = build_stack(
        rows=rows, x_index=0, y_index=2, series_index=1, series_name=columns[1]
    )
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "columns": list(columns),
        "rows": rows,
        "stack": stack,
    }


# ---------------------------------------------------------------------------
# The top list
# ---------------------------------------------------------------------------


def _live_orgs(db: Session) -> dict[int, tuple[str, str]]:
    return {
        org_id: (slug, name)
        for org_id, slug, name in db.exec(
            select(Organization.id, Organization.slug, Organization.name).where(
                col(Organization.deleted_at).is_(None)
            )
        )
    }


def _top_spend(start: date, end: date) -> list[dict[str, Any]]:
    """Live orgs by spend in the range: the usage aggregate's ``org`` group."""
    group = "org"
    assert group in ADMIN_GROUPS
    live = sa_select(Organization.id).where(col(Organization.deleted_at).is_(None))
    f = UsageFilter(
        start=start,
        end=end,
        conditions=(
            col(_E.created_at) >= _stamp(start),
            col(_E.created_at) < _stamp(end),
            col(_E.org_id).in_(live),
        ),
    )
    body = _aggregate(f, group, production=True, series=False)
    return [
        {"slug": g["key"], "name": g["label"], "value": g["cost_usd"]}
        for g in body["groups"]
        if g["cost_usd"] > 0
    ]


def _top_storage(db: Session) -> list[dict[str, Any]]:
    """Live orgs by the latest samples of their projects, carried forward."""
    orgs = _live_orgs(db)
    sizes: dict[int, int] = {}
    for org_id, size in latest_project_bytes(db).values():
        if org_id in orgs:
            sizes[org_id] = sizes.get(org_id, 0) + size
    return [
        {"slug": orgs[org_id][0], "name": orgs[org_id][1], "value": size}
        for org_id, size in sizes.items()
        if size > 0
    ]


def _top_agent_calls(db: Session, start: date, end: date) -> list[dict[str, Any]]:
    """Live orgs by agent calls in the range (agent sources only)."""
    orgs = _live_orgs(db)
    rows = db.execute(
        sa_select(col(AgentCallDay.org_id), func.sum(col(AgentCallDay.calls)))
        .where(
            col(AgentCallDay.source).in_(AGENT_CALL_SOURCES),
            col(AgentCallDay.day) >= start.isoformat(),
            col(AgentCallDay.day) < end.isoformat(),
        )
        .group_by(col(AgentCallDay.org_id))
    ).all()
    return [
        {"slug": orgs[org_id][0], "name": orgs[org_id][1], "value": int(calls)}
        for org_id, calls in rows
        if org_id in orgs and calls
    ]


@admin_stats_router.get("/api/admin/stats/top")
def get_admin_stats_top(
    metric: Literal["spend", "storage", "agent_calls"] = Query(...),
    limit: int = Query(default=10, ge=1, le=MAX_TOP),
    since: str | None = Query(default=None, alias="from", max_length=40),
    until: str | None = Query(default=None, alias="to", max_length=40),
    _: Principal = Depends(instance_access()),
) -> dict[str, Any]:
    """The top live orgs by spend, storage or agent calls.

    ``spend`` and ``agent_calls`` cover the range; ``storage`` is each
    org's projects' latest samples now (a level, not a flow), soft-deleted
    projects of a live org included until they are purged.

    Returns
    -------
    dict
        ``{from, to, metric, items: [{slug, name, value}]}``, highest
        first (ties by slug), at most ``limit`` items, none with a zero
        value. ``value`` is USD (``spend``), bytes (``storage``) or calls
        (``agent_calls``). ``422`` for another metric, a bad ``limit`` or a
        bad range, ``503 usage_timeout`` on a timeout.
    """
    start, end = parse_range(since, until)
    if metric == "spend":
        items = _top_spend(start, end)
    else:

        def run() -> list[dict[str, Any]]:
            with get_session() as db:
                _timed(db)
                if metric == "storage":
                    return _top_storage(db)
                return _top_agent_calls(db, start, end)

        items = _timeout_guard(run)
    items.sort(key=lambda item: (-item["value"], item["slug"]))
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "metric": metric,
        "items": items[:limit],
    }


__all__ = [
    "AGENT_CALL_DAYS",
    "FAILED_SCAN_WINDOW",
    "FAILING_STATUSES",
    "MAX_TOP",
    "OTHER",
    "TOP_PROVIDERS",
    "admin_stats_router",
    "get_admin_stats_overview",
    "get_admin_stats_series",
    "get_admin_stats_top",
]
