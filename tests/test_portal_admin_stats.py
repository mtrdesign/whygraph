"""The admin Dashboard's stats routes (plan 59 section 4.12, step 19).

``/api/admin/stats/overview`` (the seven tiles and the attention list, D58),
``/api/admin/stats/series`` (``spend`` / ``scans``, stacked on the server)
and ``/api/admin/stats/top`` (``spend`` / ``storage`` / ``agent_calls``),
plus the adapters they need: ``ADMIN_GROUPS`` (the org-facing ``GROUPS``
unchanged), :meth:`SpendBook.total`, :meth:`BudgetBook.org_ids` and the
public :func:`~whygraph.chat.charts.build_stack`.

The world is :func:`~test_portal_connect.cn`: Ada (a password instance
admin), Ben (owner of ``acme``), Dee (owner of ``bravo``), projects ``api``
and ``web`` in ``acme`` and ``lib`` in ``bravo``. Ledger rows, samples, runs
and counters are written directly; the spend book is seeded.
"""

# ruff: noqa: F811 -- pytest fixtures are imported from other test modules

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text

from test_portal_app import (  # noqa: F401 -- fixtures
    admin_signed_in,
    at,
    env,
    production_env,
    signed_in,
)
from test_portal_connect import cn  # noqa: F401 -- `cn` is a fixture
from test_portal_hosts_isolation import _insert_project
from whygraph.chat.charts import build_stack
from whygraph.portal import admin_stats, storage, updates, usage_routes
from whygraph.portal import db as portal_db
from whygraph.portal.budgets import BudgetBook, reload_org_budgets
from whygraph.portal.models import (
    AgentCallDay,
    Budget,
    Organization,
    Project,
    ScanRun,
    StorageSample,
    UsageEvent,
    User,
)
from whygraph.portal.orgs import add_member, create_org
from whygraph.portal.usage_store import SpendBook, UsageRow, current_month

SEVEN = {
    "users",
    "orgs",
    "projects",
    "llm_spend_mtd",
    "tokens_mtd",
    "storage_bytes",
    "agent_calls_7d",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> date:
    return _now().date()


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _stamp(day: date, hour: int = 12) -> str:
    return f"{day.isoformat()}T{hour:02d}:00:00+00:00"


def last_30() -> dict[str, str]:
    """The Dashboard's default range: the last 30 days, today included."""
    today = _today()
    return {
        "from": (today - timedelta(days=29)).isoformat(),
        "to": (today + timedelta(days=1)).isoformat(),
    }


def get(n: SimpleNamespace, path: str, **params: Any):  # noqa: ANN201
    """``GET`` an admin route on the base host with Ada's admin session."""
    n.client.cookies.clear()
    admin_signed_in(n.client, n.ids["ada"])
    return n.client.get(at() + path, params=params)


def ok(n: SimpleNamespace, path: str, **params: Any) -> dict:
    response = get(n, path, **params)
    assert response.status_code == 200, response.text
    return response.json()


def event(org_id: int, project_id: int | None, **fields: Any) -> None:
    """Insert one ``usage_events`` row (defaults: a priced chat call, now)."""
    row: dict[str, Any] = {
        "org_id": org_id,
        "project_id": project_id,
        "project_slug": "x",
        "project_name": "X",
        "actor_kind": "system",
        "actor_label": "System",
        "source": "chat",
        "task": "chat",
        "provider": "anthropic",
        "model_requested": "claude-x",
        "key_scope": "org",
        "cost_source": "estimated",
        "cost_usd": Decimal("1"),
        "created_at": _iso(_now()),
    }
    row.update(fields)
    if isinstance(row["cost_usd"], str):
        row["cost_usd"] = Decimal(row["cost_usd"])
    with portal_db.get_session() as db:
        db.add(UsageEvent(**row))


def calls(n: SimpleNamespace, slug: str, day: date, count: int, source: str) -> None:
    """Insert one ``agent_call_days`` counter row for project ``slug``."""
    with portal_db.get_session() as db:
        project = db.get(Project, n.projects[slug])
        db.add(
            AgentCallDay(
                org_id=project.org_id,
                project_id=project.id,
                day=day.isoformat(),
                source=source,
                kind="whygraph_evidence_for",
                calls=count,
            )
        )


def sample(n: SimpleNamespace, slug: str, day: date, size: int) -> None:
    """Insert one storage sample of ``size`` bytes (all in ``git_bytes``)."""
    with portal_db.get_session() as db:
        project = db.get(Project, n.projects[slug])
        db.add(
            StorageSample(
                org_id=project.org_id,
                project_id=project.id,
                day=day.isoformat(),
                history_bytes=0,
                git_bytes=size,
                codegraph_bytes=0,
                worktree_bytes=0,
                runs_bytes=0,
                measured_at=_stamp(day),
            )
        )


def run(n: SimpleNamespace, slug: str, status: str, **fields: Any) -> int:
    with portal_db.get_session() as db:
        row = ScanRun(project_id=n.projects[slug], trigger="manual", status=status)
        for name, value in fields.items():
            setattr(row, name, value)
        db.add(row)
        db.flush()
        return row.id


def set_row(model: type, row_id: int, **fields: Any) -> None:
    with portal_db.get_session() as db:
        row = db.get(model, row_id)
        for name, value in fields.items():
            setattr(row, name, value)
        db.add(row)


def read_stack(stack: dict) -> dict | None:
    """The SPA's ``readStack`` (``chartSpec.ts``), in Python."""
    if (
        not isinstance(stack.get("series_index"), int)
        or not isinstance(stack.get("series_values"), list)
        or not isinstance(stack.get("x_values"), list)
        or not isinstance(stack.get("cells"), dict)
    ):
        return None
    return {
        "seriesIndex": stack["series_index"],
        "seriesValues": [str(v) for v in stack["series_values"]],
        "xValues": [str(v) for v in stack["x_values"]],
        "cells": stack["cells"],
        "filledCells": stack.get("filled_cells", 0),
    }


def quiet_instance(n: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """No address problem and no update, whatever the start-up checks found."""
    deadline = time.monotonic() + 5
    while n.state.base_check is None and time.monotonic() < deadline:
        time.sleep(0.01)
    n.state.base_check = []
    monkeypatch.setattr(updates, "update_block", lambda state: None)


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


def test_spend_book_total_sums_every_org_this_month() -> None:
    book = SpendBook()
    assert book.total() == 0
    book.seed(
        current_month(),
        [(1, 10, None, Decimal("1.5")), (2, None, 7, Decimal("2")), (1, None, None, 1)],
    )
    assert book.total() == Decimal("4.5")
    row = UsageRow(
        org_id=3,
        project_id=None,
        project_slug="x",
        project_name="X",
        actor_kind="system",
        user_id=None,
        actor_label="System",
        source="scan",
        task="analyze",
        provider="openai",
        model_requested="m",
        key_scope="org",
        cost_source="provider",
        created_at=_iso(_now()),
        cost_usd=Decimal("0.5"),
    )
    book.add(row)
    assert book.total() == Decimal("5")
    stale = SpendBook()
    stale.seed("2000-01", [(1, None, None, Decimal("9"))])
    assert stale.total() == 0  # a month that is not this one reads 0


def test_budget_book_org_ids_lists_the_orgs_with_budgets(
    cn: SimpleNamespace,
) -> None:
    book = BudgetBook(SpendBook())
    assert book.org_ids() == []
    with portal_db.get_session() as db:
        for slug in ("bravo", "acme"):
            db.add(
                Budget(
                    org_id=cn.orgs[slug],
                    scope="org",
                    monthly_usd=Decimal("5"),
                    hard_stop=False,
                )
            )
    for slug in ("bravo", "acme"):
        reload_org_budgets(book, cn.orgs[slug])
    assert book.org_ids() == sorted(cn.orgs.values())


def test_build_stack_is_public_and_unchanged() -> None:
    stack = build_stack(
        rows=[["d1", "a", 1], ["d1", "b", 2], ["d2", "a", 3]],
        x_index=0,
        y_index=2,
        series_index=1,
        series_name="s",
    )
    assert stack == {
        "series": "s",
        "series_index": 1,
        "series_values": ["a", "b"],
        "x_values": ["d1", "d2"],
        "cells": {"d1": {"a": 1, "b": 2}, "d2": {"a": 3, "b": 0}},
        "filled_cells": 1,
    }


def test_admin_groups_add_org_and_leave_groups_alone() -> None:
    assert "org" not in usage_routes.GROUPS
    assert usage_routes.ADMIN_GROUPS == (*usage_routes.GROUPS, "org")


# ---------------------------------------------------------------------------
# The overview
# ---------------------------------------------------------------------------


def test_overview_returns_exactly_the_seven_tiles(
    cn: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet_instance(cn, monkeypatch)
    today = _today()
    old = _stamp(today - timedelta(days=400))
    # Users: 7 live (Eve soft-deleted), Ben and Cy created before the range.
    set_row(User, cn.ids["eve"], deleted_at=_iso(_now()))
    set_row(User, cn.ids["ben"], created_at=old)
    set_row(User, cn.ids["cy"], created_at=old)
    # Orgs: acme and bravo live, bravo created before the range; a deleted one.
    with portal_db.get_session() as db:
        gone = create_org(db, slug="gone", name="Gone").id
    set_row(Organization, gone, deleted_at=_iso(_now()))
    set_row(Organization, cn.orgs["bravo"], created_at=old)
    # Projects: web soft-deleted.
    set_row(Project, cn.projects["web"], deleted_at=_iso(_now()))
    # Spend: the book says 7.5 this month; last month to this day says 3.
    cn.state.spend.seed(
        cn.state.spend.month,
        [
            (cn.orgs["acme"], cn.projects["api"], cn.ids["ben"], Decimal("5")),
            (cn.orgs["bravo"], None, None, Decimal("2.5")),
        ],
    )
    month_start = today.replace(day=1)
    prev_start = (month_start - timedelta(days=1)).replace(day=1)
    prev_len = (month_start - prev_start).days
    event(  # scan spend has no user and is counted
        cn.orgs["acme"],
        cn.projects["api"],
        source="scan",
        task="analyze",
        cost_usd="2",
        created_at=_stamp(prev_start, 0),
    )
    event(
        cn.orgs["bravo"],
        None,
        cost_usd="1",
        created_at=_stamp(prev_start + timedelta(days=min(today.day, prev_len) - 1)),
    )
    event(cn.orgs["acme"], None, cost_usd="9", cost_source="unpriced")  # not counted
    if today.day < prev_len:  # the day after "the same day" last month
        event(
            cn.orgs["acme"],
            None,
            cost_usd="100",
            created_at=_stamp(prev_start + timedelta(days=today.day), 0),
        )
    # Tokens: input + output this month; last month's rows are not counted.
    event(cn.orgs["acme"], None, input_tokens=100, output_tokens=20, cost_usd=None)
    event(cn.orgs["bravo"], None, input_tokens=5, output_tokens=None)
    event(
        cn.orgs["acme"],
        None,
        input_tokens=1000,
        output_tokens=1000,
        cost_usd=None,
        cost_source="unpriced",
        created_at=_stamp(prev_start),
    )
    # Storage: api 100 before the range then 150 today; lib 40 inside it.
    start = today - timedelta(days=29)
    sample(cn, "api", start - timedelta(days=5), 100)
    sample(cn, "api", today, 150)
    sample(cn, "lib", start + timedelta(days=3), 40)
    # Agent calls: 3 + 2 in the last 7 days, 4 the week before; chat and
    # older days are not counted.
    calls(cn, "api", today, 3, "mcp")
    calls(cn, "lib", today - timedelta(days=6), 2, "agent")
    calls(cn, "api", today - timedelta(days=7), 4, "mcp")
    calls(cn, "api", today, 100, "chat")
    calls(cn, "api", today - timedelta(days=14), 50, "mcp")

    body = ok(cn, "/api/admin/stats/overview", **last_30())
    assert set(body) == {"from", "to", "kpis", "attention"}
    assert (body["from"], body["to"]) == (last_30()["from"], last_30()["to"])
    kpis = body["kpis"]
    assert set(kpis) == SEVEN
    assert all(set(tile) == {"value", "previous"} for tile in kpis.values())
    assert kpis["users"] == {"value": 6, "previous": 2}
    assert kpis["orgs"] == {"value": 2, "previous": 1}
    assert kpis["projects"] == {"value": 2, "previous": None}
    assert kpis["llm_spend_mtd"] == {"value": 7.5, "previous": 3.0}
    assert kpis["tokens_mtd"] == {"value": 125, "previous": None}
    assert kpis["storage_bytes"] == {"value": 190, "previous": 100}
    assert kpis["agent_calls_7d"] == {"value": 5, "previous": 4}
    assert body["attention"] == []


def test_overview_previous_storage_is_null_before_any_sample(
    cn: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet_instance(cn, monkeypatch)
    kpis = ok(cn, "/api/admin/stats/overview", **last_30())["kpis"]
    assert kpis["storage_bytes"] == {"value": 0, "previous": None}
    assert kpis["llm_spend_mtd"] == {"value": 0.0, "previous": 0.0}
    sample(cn, "api", _today(), 10)
    kpis = ok(cn, "/api/admin/stats/overview", **last_30())["kpis"]
    assert kpis["storage_bytes"] == {"value": 10, "previous": None}


def test_storage_totals_carry_forward_across_days_with_no_row(
    cn: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet_instance(cn, monkeypatch)
    today = _today()
    # api measured 20 days ago only, web 10 days ago then 2 days ago.
    sample(cn, "api", today - timedelta(days=20), 1000)
    sample(cn, "web", today - timedelta(days=10), 50)
    sample(cn, "web", today - timedelta(days=2), 70)
    since = today - timedelta(days=5)  # no row on that day: both carried
    rng = {"from": since.isoformat(), "to": (today + timedelta(days=1)).isoformat()}
    kpis = ok(cn, "/api/admin/stats/overview", **rng)["kpis"]
    assert kpis["storage_bytes"] == {"value": 1070, "previous": 1050}
    top = ok(cn, "/api/admin/stats/top", metric="storage", **rng)
    assert top["items"] == [{"slug": "acme", "name": "Acme", "value": 1070}]


def test_overview_lists_each_attention_kind(
    cn: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = _now()
    quiet_instance(cn, monkeypatch)
    cn.state.base_check = ["acme.whygraph.test has no DNS record"]
    monkeypatch.setattr(
        updates,
        "update_block",
        lambda state: {
            "available": True,
            "latest": "9.9.9",
            "notes_url": "https://example.test/notes/9.9.9",
        },
    )
    root = tmp_path / "cli"
    root.mkdir()
    cn.projects["cli"] = _insert_project(
        cn.orgs["acme"], "cli", "Cli project", root, cn.ids["ben"]
    )
    # Failing: api's newest finished run failed an hour ago; cli failed too
    # but lost GitHub access (listed as access lost); lib's failure is old.
    run(cn, "api", "ok", queued_at=_iso(now - timedelta(hours=3)), finished_at="a")
    failed_at = _iso(now - timedelta(minutes=50))
    run(
        cn,
        "api",
        "failed",
        queued_at=_iso(now - timedelta(hours=1)),
        finished_at=failed_at,
    )
    run(cn, "api", "running", queued_at=_iso(now))  # unfinished: not the newest
    run(cn, "cli", "failed", queued_at=_iso(now), finished_at=_iso(now))
    lost_at = _iso(now - timedelta(days=1))
    set_row(
        Project,
        cn.projects["cli"],
        access_lost_reason="uninstalled",
        access_lost_at=lost_at,
    )
    old = _iso(now - timedelta(hours=30))
    run(cn, "web", "interrupted", queued_at=old, finished_at=old)
    # Budgets: acme's org budget and api's project budget are hard-stopped
    # and exhausted; Ben's member budget too; a soft budget is not listed.
    with portal_db.get_session() as db:
        for scope, ids, hard in (
            ("org", {}, True),
            ("project", {"project_id": cn.projects["api"]}, True),
            ("member", {"user_id": cn.ids["ben"]}, True),
            ("member", {"user_id": cn.ids["cy"]}, False),
        ):
            db.add(
                Budget(
                    org_id=cn.orgs["acme"],
                    scope=scope,
                    monthly_usd=Decimal("1"),
                    hard_stop=hard,
                    **ids,
                )
            )
    reload_org_budgets(cn.state.budgets, cn.orgs["acme"])
    cn.state.spend.seed(
        cn.state.spend.month,
        [
            (cn.orgs["acme"], cn.projects["api"], cn.ids["ben"], Decimal("2")),
            (cn.orgs["acme"], cn.projects["web"], cn.ids["cy"], Decimal("2")),
        ],
    )
    # Password accounts: Ada (an admin) owns acme and the deleted bravo;
    # Pat is a password account that is not an admin.
    with portal_db.get_session() as db:
        add_member(db, org_id=cn.orgs["acme"], user_id=cn.ids["ada"], role="owner")
        add_member(db, org_id=cn.orgs["bravo"], user_id=cn.ids["ada"], role="owner")
        pat = User(display_name="Pat", email="pat@example.com", password_hash="x")
        db.add(pat)
        db.flush()
        pat_uid = pat.uid
        ada = db.get(User, cn.ids["ada"])
        ada_uid, ada_name = ada.uid, ada.display_name
    # Suspended: acme. Deleted with data: web (a project of a live org) and
    # bravo (an org, lib's bytes).
    suspended_at = _iso(now - timedelta(days=2))
    set_row(
        Organization,
        cn.orgs["acme"],
        suspended_at=suspended_at,
        suspended_reason="unpaid",
    )
    web_deleted = _iso(now - timedelta(days=3))
    bravo_deleted = _iso(now - timedelta(days=4))
    set_row(Project, cn.projects["web"], deleted_at=web_deleted)
    set_row(Organization, cn.orgs["bravo"], deleted_at=bravo_deleted)
    sample(cn, "web", _today(), 500)
    sample(cn, "lib", _today() - timedelta(days=1), 700)
    sample(cn, "api", _today(), 9)  # live: not an attention item

    acme = {"slug": "acme", "name": "Acme"}
    attention = ok(cn, "/api/admin/stats/overview", **last_30())["attention"]
    assert attention == [
        {
            "kind": "scan_failed",
            "org": acme,
            "project": {"org": "acme", "slug": "api", "name": "Api project"},
            "at": failed_at,
            "text": "failed",
        },
        {
            "kind": "access_lost",
            "org": acme,
            "project": {"org": "acme", "slug": "cli", "name": "Cli project"},
            "at": lost_at,
            "text": "uninstalled",
        },
        {"kind": "budget_stopped", "org": acme, "text": "org"},
        {
            "kind": "budget_stopped",
            "org": acme,
            "project": {"org": "acme", "slug": "api", "name": "Api project"},
            "text": "project",
        },
        {
            "kind": "budget_stopped",
            "org": acme,
            "user": {"uid": cn.uids["ben"], "label": "Ben (@ben)"},
            "text": "member",
        },
        {"kind": "base_check", "text": "acme.whygraph.test has no DNS record"},
        {
            "kind": "update",
            "text": "9.9.9",
            "url": "https://example.test/notes/9.9.9",
        },
        {
            "kind": "password_owner",
            "user": {"uid": ada_uid, "label": ada_name},
            "count": 1,
        },
        {"kind": "password_not_admin", "user": {"uid": pat_uid, "label": "Pat"}},
        {"kind": "org_suspended", "org": acme, "at": suspended_at, "text": "unpaid"},
        {
            "kind": "deleted_with_data",
            "org": {"slug": "bravo", "name": "Bravo"},
            "at": bravo_deleted,
            "bytes": 700,
        },
        {
            "kind": "deleted_with_data",
            "org": acme,
            "project": {"org": "acme", "slug": "web", "name": "Web project"},
            "at": web_deleted,
            "bytes": 500,
        },
    ]


def test_failed_scans_are_listed_newest_first(
    cn: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet_instance(cn, monkeypatch)
    now = _now()
    older = _iso(now - timedelta(hours=5))
    newer = _iso(now - timedelta(hours=1))
    run(cn, "api", "interrupted", queued_at=older, finished_at=older)
    run(cn, "lib", "failed", queued_at=newer, finished_at=newer)
    run(cn, "web", "failed", finished_at=newer)  # no queued_at: left out
    attention = ok(cn, "/api/admin/stats/overview", **last_30())["attention"]
    assert [(a["kind"], a["project"]["slug"], a["text"]) for a in attention] == [
        ("scan_failed", "lib", "failed"),
        ("scan_failed", "api", "interrupted"),
    ]


def test_the_deleted_figure_survives_a_prune(
    cn: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet_instance(cn, monkeypatch)
    today = _today()
    deleted_at = _iso(_now() - timedelta(days=500))
    set_row(Project, cn.projects["web"], deleted_at=deleted_at)
    sample(cn, "web", today - timedelta(days=600), 300)  # older than the prune
    sample(cn, "web", today - timedelta(days=450), 400)  # its newest row
    assert storage.prune() == 1
    attention = ok(cn, "/api/admin/stats/overview", **last_30())["attention"]
    assert attention == [
        {
            "kind": "deleted_with_data",
            "org": {"slug": "acme", "name": "Acme"},
            "project": {"org": "acme", "slug": "web", "name": "Web project"},
            "at": deleted_at,
            "bytes": 400,
        }
    ]


# ---------------------------------------------------------------------------
# The series
# ---------------------------------------------------------------------------


def test_series_spend_stacks_the_top_providers_and_fills_empty_days(
    cn: SimpleNamespace,
) -> None:
    today = _today()
    d1, d3 = today - timedelta(days=2), today
    acme, bravo = cn.orgs["acme"], cn.orgs["bravo"]
    for n, provider in enumerate(("a", "b", "c", "d", "e", "f", "g")):
        event(
            acme, None, provider=provider, cost_usd=str(10 - n), created_at=_stamp(d1)
        )
    event(bravo, None, provider="a", cost_usd="1.5", created_at=_stamp(d3))
    event(bravo, None, provider="g", cost_usd="0.25", created_at=_stamp(d3))
    event(bravo, None, provider="z", cost_usd=None, cost_source="unpriced")
    rng = {"from": d1.isoformat(), "to": (today + timedelta(days=1)).isoformat()}
    body = ok(cn, "/api/admin/stats/series", metric="spend", **rng)
    assert set(body) == {"from", "to", "columns", "rows", "stack"}
    assert body["columns"] == ["day", "provider", "cost_usd"]
    d2 = (d1 + timedelta(days=1)).isoformat()
    assert body["rows"] == [
        [d1.isoformat(), "a", 10.0],
        [d1.isoformat(), "b", 9.0],
        [d1.isoformat(), "c", 8.0],
        [d1.isoformat(), "d", 7.0],
        [d1.isoformat(), "e", 6.0],
        [d1.isoformat(), "other", 9.0],  # f (5) + g (4)
        [d2, "a", 0],  # a day with no row still appears
        [d3.isoformat(), "a", 1.5],
        [d3.isoformat(), "other", 0.25],
    ]
    stack = read_stack(body["stack"])
    assert stack is not None
    assert stack["xValues"] == [d1.isoformat(), d2, d3.isoformat()]
    assert stack["seriesValues"] == ["a", "other", "b", "c", "d", "e"]
    assert stack["cells"][d2] == {s: 0 for s in stack["seriesValues"]}
    assert body["stack"]["series"] == "provider"


def test_series_scans_by_status_by_queued_at(cn: SimpleNamespace) -> None:
    today = _today()
    yesterday = today - timedelta(days=1)
    run(cn, "api", "ok", queued_at=_stamp(yesterday))
    run(cn, "api", "ok", queued_at=_stamp(yesterday, 13))
    run(cn, "lib", "failed", queued_at=_stamp(yesterday))
    run(cn, "web", "queued", queued_at=_stamp(today - timedelta(days=4)))
    run(cn, "web", "ok")  # NULL queued_at: before the ux revision, left out
    rng = {
        "from": (today - timedelta(days=2)).isoformat(),
        "to": (today + timedelta(days=1)).isoformat(),
    }
    body = ok(cn, "/api/admin/stats/series", metric="scans", **rng)
    assert body["columns"] == ["day", "status", "runs"]
    first = (today - timedelta(days=2)).isoformat()
    assert body["rows"] == [
        [first, "failed", 0],
        [yesterday.isoformat(), "failed", 1],
        [yesterday.isoformat(), "ok", 2],
        [today.isoformat(), "failed", 0],
    ]
    stack = read_stack(body["stack"])
    assert stack is not None and stack["seriesValues"] == ["ok", "failed"]
    assert stack["xValues"] == [first, yesterday.isoformat(), today.isoformat()]


def test_series_with_no_row_has_an_empty_stack(cn: SimpleNamespace) -> None:
    for metric in ("spend", "scans"):
        body = ok(cn, "/api/admin/stats/series", metric=metric, **last_30())
        assert body["rows"] == []
        stack = read_stack(body["stack"])
        assert stack is not None and stack["xValues"] == []


def test_bad_metrics_and_ranges_are_422(cn: SimpleNamespace) -> None:
    assert get(cn, "/api/admin/stats/series", metric="users").status_code == 422
    assert get(cn, "/api/admin/stats/series").status_code == 422
    assert get(cn, "/api/admin/stats/top", metric="tokens").status_code == 422
    assert get(cn, "/api/admin/stats/top", metric="spend", limit=0).status_code == 422
    for path, extra in (
        ("/api/admin/stats/overview", {}),
        ("/api/admin/stats/series", {"metric": "spend"}),
        ("/api/admin/stats/top", {"metric": "spend"}),
    ):
        for since, until, code in (
            ("2025-01-01", "2026-02-06", "range_too_long"),
            ("2026-03-02", "2026-03-01", "bad_range"),
            ("soon", None, "bad_date"),
        ):
            params = {"from": since, **({"to": until} if until else {}), **extra}
            response = get(cn, path, **params)
            assert (response.status_code, response.json()["code"]) == (422, code)
    # Without a range: the current month.
    body = ok(cn, "/api/admin/stats/series", metric="scans")
    assert body["from"] == f"{current_month()}-01"


# ---------------------------------------------------------------------------
# The top list
# ---------------------------------------------------------------------------


def test_top_spend_uses_the_admin_org_group(cn: SimpleNamespace) -> None:
    acme, bravo = cn.orgs["acme"], cn.orgs["bravo"]
    with portal_db.get_session() as db:
        gone = create_org(db, slug="gone", name="Gone").id
    event(acme, None, cost_usd="1.25")
    event(acme, cn.projects["api"], cost_usd="2", source="scan", task="analyze")
    event(bravo, None, cost_usd="5")
    event(gone, None, cost_usd="50")
    event(bravo, None, cost_usd="70", created_at=_stamp(_today() - timedelta(days=40)))
    set_row(Organization, gone, deleted_at=_iso(_now()))
    body = ok(cn, "/api/admin/stats/top", metric="spend", **last_30())
    assert body == {
        **last_30(),
        "metric": "spend",
        "items": [
            {"slug": "bravo", "name": "Bravo", "value": 5.0},
            {"slug": "acme", "name": "Acme", "value": 3.25},
        ],
    }
    one = ok(cn, "/api/admin/stats/top", metric="spend", limit=1, **last_30())
    assert [item["slug"] for item in one["items"]] == ["bravo"]


def test_usage_group_org_is_still_refused_on_the_org_routes(
    cn: SimpleNamespace,
) -> None:
    cn.client.cookies.clear()
    signed_in(cn.client, cn.ids["ben"])
    for path in ("/api/usage", "/api/usage.csv", "/api/usage/me"):
        response = cn.client.get(at("acme") + path, params={"group": "org"})
        assert (response.status_code, response.json()["code"]) == (422, "bad_group")


def test_top_storage_and_agent_calls(cn: SimpleNamespace) -> None:
    today = _today()
    sample(cn, "api", today - timedelta(days=3), 10)
    sample(cn, "web", today, 5)
    sample(cn, "lib", today, 100)
    storage_top = ok(cn, "/api/admin/stats/top", metric="storage", **last_30())
    assert storage_top["items"] == [
        {"slug": "bravo", "name": "Bravo", "value": 100},
        {"slug": "acme", "name": "Acme", "value": 15},
    ]
    calls(cn, "api", today, 4, "mcp")
    calls(cn, "web", today - timedelta(days=1), 3, "agent")
    calls(cn, "lib", today, 5, "mcp")
    calls(cn, "lib", today - timedelta(days=60), 500, "mcp")  # out of range
    agent_top = ok(cn, "/api/admin/stats/top", metric="agent_calls", **last_30())
    assert agent_top["items"] == [
        {"slug": "acme", "name": "Acme", "value": 7},
        {"slug": "bravo", "name": "Bravo", "value": 5},
    ]
    set_row(Organization, cn.orgs["bravo"], deleted_at=_iso(_now()))
    for metric in ("storage", "agent_calls"):
        items = ok(cn, "/api/admin/stats/top", metric=metric, **last_30())["items"]
        assert [item["slug"] for item in items] == ["acme"]


def test_a_chat_tool_call_is_not_an_agent_call(
    cn: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet_instance(cn, monkeypatch)
    calls(cn, "api", _today(), 9, "chat")
    kpis = ok(cn, "/api/admin/stats/overview", **last_30())["kpis"]
    assert kpis["agent_calls_7d"] == {"value": 0, "previous": 0}
    top = ok(cn, "/api/admin/stats/top", metric="agent_calls", **last_30())
    assert top["items"] == []


# ---------------------------------------------------------------------------
# Timeouts and access
# ---------------------------------------------------------------------------


def test_a_statement_timeout_is_a_503(
    cn: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet_instance(cn, monkeypatch)
    monkeypatch.setattr(usage_routes, "STATEMENT_TIMEOUT", "50ms")

    def slow(db, *args):  # noqa: ANN001, ANN202
        db.execute(text("SELECT pg_sleep(1)"))
        return []

    monkeypatch.setattr(admin_stats, "_failing", slow)
    monkeypatch.setitem(admin_stats._SERIES, "scans", (("day", "status", "runs"), slow))
    monkeypatch.setattr(admin_stats, "_top_agent_calls", slow)
    for path, params in (
        ("/api/admin/stats/overview", {}),
        ("/api/admin/stats/series", {"metric": "scans"}),
        ("/api/admin/stats/top", {"metric": "agent_calls"}),
    ):
        response = get(cn, path, **params, **last_30())
        assert (response.status_code, response.json()["code"]) == (
            503,
            "usage_timeout",
        ), path
    # The other calls still answer.
    assert get(cn, "/api/admin/stats/series", metric="spend").status_code == 200


def test_the_stats_need_an_admin_session(cn: SimpleNamespace) -> None:
    paths = (
        "/api/admin/stats/overview",
        "/api/admin/stats/series?metric=spend",
        "/api/admin/stats/top?metric=spend",
    )
    cn.client.cookies.clear()
    for path in paths:
        response = cn.client.get(at() + path)
        assert (response.status_code, response.json()["code"]) == (
            401,
            "admin_login_required",
        )
    signed_in(cn.client, cn.ids["ben"])  # an everyday session
    for path in paths:
        assert cn.client.get(at() + path).status_code == 401
        assert cn.client.get(at("acme") + path).status_code == 404  # base host only
