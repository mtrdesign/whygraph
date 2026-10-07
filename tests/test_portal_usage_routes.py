"""The usage read routes, prices, CSV and the usage payload fields (M2f-2 4.11, 4.12).

Plan section 6.2 #6 (visibility by role, payload gating), #7 (price
override and revert, audited), #8 (``/api/account/usage`` by membership
only), #10 (read API schemas, grouping, range rules, keyset paging) and #11
(CSV escaping, both shapes, truncation, a member forced to themselves).

Ledger rows are recorded the way the portal records them: a
:class:`~whygraph.portal.usage_store.UsageRow` counted by the spend book
and written by the usage writer (flushed before reading).
"""

# ruff: noqa: F811 -- pytest fixtures are imported by name

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlmodel import select

from test_portal_app import (  # noqa: F401 -- fixtures
    at,
    env,
    production_env,
    signed_in,
)
from test_portal_hosts_isolation import _insert_project
from test_portal_identity_routes import audit_log, events  # noqa: F401
from test_portal_usage import local, stub_llm  # noqa: F401 -- fixtures
from test_portal_v1 import ORG, SLUG, V1World, v1  # noqa: F401 -- `v1` is a fixture
from whygraph.portal import db as portal_db
from whygraph.portal import usage_routes
from whygraph.portal.budgets import reload_org_budgets
from whygraph.portal.csv_export import csv_cell
from whygraph.portal.deps import ApiError
from whygraph.portal.models import (
    Budget,
    ConnectionToken,
    Membership,
    Organization,
    Project,
    ProjectGrant,
    User,
)
from whygraph.portal.orgs import add_member, create_org
from whygraph.portal.prices import bundled_prices
from whygraph.portal.usage_routes import (
    CSV_CALL_COLUMNS,
    CSV_GROUP_COLUMNS,
    PAGE_SIZE,
    parse_range,
)
from whygraph.portal.usage_store import UsageRow, current_month

SEPT = {"from": "2026-09-01", "to": "2026-10-01"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record(state: Any, org_id: int, project_id: int | None, **kw: Any) -> UsageRow:
    """Count and ledger one call (the portal's own path)."""
    user_id = kw.pop("user_id", None)
    fields: dict[str, Any] = {
        "org_id": org_id,
        "project_id": project_id,
        "project_slug": "demo",
        "project_name": "Demo",
        "actor_kind": "system" if user_id is None else "member",
        "user_id": user_id,
        "actor_label": "System" if user_id is None else "Tess",
        "source": "chat",
        "task": "chat",
        "provider": "anthropic",
        "model_requested": "claude-x",
        "key_scope": "org",
        "cost_source": "estimated",
        "created_at": _now(),
        "cost_usd": Decimal("1"),
    }
    fields.update(kw)
    if isinstance(fields["cost_usd"], str):
        fields["cost_usd"] = Decimal(fields["cost_usd"])
    row = UsageRow(**fields)
    state.spend.add(row)
    assert state.usage_writer.submit(row)
    return row


def _flush(state: Any) -> None:
    assert state.usage_writer.flush()


def _csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def test_the_range_rules() -> None:
    month = current_month()
    start = date.fromisoformat(f"{month}-01")
    default = parse_range(None, None)
    assert default[0] == start and default[1] > start and default[1].day == 1
    assert parse_range("2026-02-10", None) == (date(2026, 2, 10), date(2026, 3, 1))
    assert parse_range(None, "2026-03-01") == (date(2026, 2, 1), date(2026, 3, 1))
    assert parse_range("2025-12-15", "2026-01-15") == (
        date(2025, 12, 15),
        date(2026, 1, 15),
    )
    assert parse_range("2025-01-01", "2026-02-05")[1] == date(2026, 2, 5)  # 400 days
    for since, until, code in (
        ("2026-13-01", None, "bad_date"),
        ("yesterday", None, "bad_date"),
        ("2026-03-01", "2026-03-01", "bad_range"),
        ("2026-03-02", "2026-03-01", "bad_range"),
        ("2025-01-01", "2026-02-06", "range_too_long"),
    ):
        with pytest.raises(ApiError) as caught:
            parse_range(since, until)
        assert (caught.value.status, caught.value.code) == (422, code)


def test_a_cancelled_statement_is_a_503() -> None:
    class _Canceled(Exception):
        sqlstate = "57014"

    def boom() -> None:
        raise OperationalError("SELECT", {}, _Canceled())

    with pytest.raises(ApiError) as caught:
        usage_routes._timeout_guard(boom)
    assert (caught.value.status, caught.value.code) == (
        503,
        "usage_timeout",
    )


def test_the_csv_helper_is_the_audit_ones() -> None:
    from whygraph.portal import audit_store

    assert audit_store.csv_cell is csv_cell
    for value in ("=1+1", "+x", "-2", "@cmd", "\tx", "\rx"):
        assert csv_cell(value) == "'" + value


# ---------------------------------------------------------------------------
# The read API (section 6.2 #10), local mode
# ---------------------------------------------------------------------------


def _seed_september(w: SimpleNamespace) -> None:
    """Three September calls in ``demo``, one in October, one in another org."""
    state = w.state
    _record(
        state,
        w.org_id,
        w.project_id,
        user_id=w.user_id,
        model_served="claude-x-2026",
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=100,
        cost_usd="1.5",
        created_at="2026-09-03T10:00:00+00:00",
    )
    _record(
        state,
        w.org_id,
        w.project_id,
        source="scan",
        task="analyze",
        input_tokens=500,
        output_tokens=50,
        cost_usd="0.25",
        created_at="2026-09-03T11:00:00+00:00",
    )
    _record(
        state,
        w.org_id,
        w.project_id,
        user_id=w.user_id,
        source="explorer",
        task="rationale",
        model_requested="gpt-z",
        cost_source="unpriced",
        cost_usd=None,
        created_at="2026-09-10T09:00:00+00:00",
    )
    _record(state, w.org_id, w.project_id, created_at="2026-10-02T00:00:00+00:00")
    with portal_db.get_session() as session:
        other = create_org(session, slug="other", name="Other").id
    _record(state, other, None, created_at="2026-09-05T00:00:00+00:00")
    _flush(state)


def test_totals_split_series_and_groups(local: SimpleNamespace) -> None:
    w = local
    _seed_september(w)
    body = w.client.get("/api/usage", params=SEPT).json()
    assert set(body) == {"range", "totals", "split", "series", "groups"}
    assert body["range"] == {"from": "2026-09-01", "to": "2026-10-01"}
    assert body["totals"] == {
        "calls": 3,
        "input_tokens": 1500,
        "output_tokens": 250,
        "cache_read_tokens": 100,
        "cache_write_tokens": None,  # never reported
        "reasoning_tokens": None,
        "cost_usd": 1.75,
        "unpriced_calls": 1,
    }
    assert body["split"] == {
        "interactive": {"calls": 2, "cost_usd": 1.5},
        "scans": {"calls": 1, "cost_usd": 0.25},
    }
    assert len(body["series"]) == 30  # one per day, empty days included
    assert body["series"][0] == {
        "day": "2026-09-01",
        "calls": 0,
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    assert body["series"][2] == {
        "day": "2026-09-03",
        "calls": 2,
        "cost_usd": 1.75,
        "input_tokens": 1500,
        "output_tokens": 250,
    }
    assert body["groups"] == []  # no group asked for

    def groups(group: str, **params: str) -> list[dict]:
        response = w.client.get("/api/usage", params={**SEPT, "group": group, **params})
        assert response.status_code == 200, response.text
        return response.json()["groups"]

    models = groups("model")
    assert [(g["key"], g["label"], g["calls"], g["cost_usd"]) for g in models] == [
        ("claude-x-2026", "claude-x-2026", 1, 1.5),  # the served model wins
        ("claude-x", "claude-x", 1, 0.25),
        ("gpt-z", "gpt-z", 1, 0.0),
    ]
    assert models[2]["unpriced_calls"] == 1
    assert models[1]["scans"] == {"calls": 1, "cost_usd": 0.25}
    assert [(g["key"], g["calls"]) for g in groups("source")] == [
        ("chat", 1),
        ("scan", 1),
        ("explorer", 1),
    ]
    assert {g["key"] for g in groups("task")} == {"chat", "analyze", "rationale"}
    assert [(g["key"], g["calls"]) for g in groups("day")] == [
        ("2026-09-03", 2),
        ("2026-09-10", 1),
    ]
    (project,) = groups("project")
    assert (project["key"], project["label"], project["calls"]) == ("demo", "demo", 3)
    # Local mode answers the production tabs with what it has: one machine.
    (machine,) = groups("machine")
    assert (machine["key"], machine["label"], machine["calls"]) == (None, "Portal", 3)
    members = groups("member")
    with portal_db.get_session() as session:
        uid = session.get(User, w.user_id).uid
    assert [(g["key"], g["label"], g["calls"]) for g in members] == [
        (uid, "Tess", 2),
        (None, "System", 1),
    ]
    assert members[0]["top_project"] == {
        "slug": "demo",
        "name": "demo",
        "cost_usd": 1.5,
    }

    refused = w.client.get("/api/usage", params={"group": "colour"})
    assert (refused.status_code, refused.json()["code"]) == (422, "bad_group")
    too_long = w.client.get(
        "/api/usage", params={"from": "2024-01-01"} | {"to": "2026-01-01"}
    )
    assert (too_long.status_code, too_long.json()["code"]) == (422, "range_too_long")


def test_the_filters(local: SimpleNamespace) -> None:
    w = local
    _seed_september(w)
    _record(
        w.state,
        w.org_id,
        None,  # a removed project's rows keep its slug
        project_slug="gone",
        project_name="Gone",
        scan_run_id=None,
        chat_session_id=None,
        created_at="2026-09-20T00:00:00+00:00",
    )
    _record(
        w.state,
        w.org_id,
        w.project_id,
        user_id=w.user_id,
        chat_session_id=7,
        created_at="2026-09-21T00:00:00+00:00",
    )
    _flush(w.state)
    with portal_db.get_session() as session:
        uid = session.get(User, w.user_id).uid

    def calls(**params: Any) -> int:
        response = w.client.get("/api/usage", params={**SEPT, **params})
        assert response.status_code == 200, response.text
        return response.json()["totals"]["calls"]

    assert calls() == 5
    assert calls(project="demo") == 4
    assert calls(project="gone") == 1
    assert calls(project="nope") == 0
    assert calls(member=uid) == 3
    assert calls(member="system") == 2
    assert calls(member="nobody") == 0
    assert calls(task="analyze") == 1
    assert calls(source="explorer") == 1
    assert calls(model="claude-x-2026") == 1
    assert calls(model="claude-x") == 3  # requested, nothing served
    assert calls(project="demo", chat_session=7) == 1
    for params in (
        {"task": "paint"},
        {"source": "fax"},
        {"chat_session": 7},  # ids are per project
    ):
        refused = w.client.get("/api/usage", params=params)
        assert (refused.status_code, refused.json()["code"]) == (422, "bad_filter")
    bad_date = w.client.get("/api/usage", params={"from": "soon"})
    assert (bad_date.status_code, bad_date.json()["code"]) == (422, "bad_date")


def test_keyset_paging_by_time_and_by_cost(local: SimpleNamespace) -> None:
    w = local
    costs = [None if i % 7 == 0 else str(i % 5) for i in range(120)]
    for i, cost in enumerate(costs):
        _record(
            w.state,
            w.org_id,
            w.project_id,
            cost_usd=cost,
            cost_source="unpriced" if cost is None else "estimated",
            created_at=f"2026-09-{1 + i % 28:02d}T{i % 24:02d}:00:00+00:00",
            subject=f"s{i}",
        )
    _flush(w.state)

    def walk(sort: str) -> list[dict]:
        seen: list[dict] = []
        before = None
        while True:
            params = {**SEPT, "sort": sort}
            if before is not None:
                params["before"] = before
            body = w.client.get("/api/usage/calls", params=params).json()
            assert len(body["items"]) <= PAGE_SIZE
            seen += body["items"]
            before = body["next"]
            if before is None:
                return seen

    by_time = walk("time")
    assert len(by_time) == 120 and len({r["id"] for r in by_time}) == 120
    assert [(r["created_at"], r["id"]) for r in by_time] == sorted(
        ((r["created_at"], r["id"]) for r in by_time), reverse=True
    )
    by_cost = walk("cost")
    assert {r["id"] for r in by_cost} == {r["id"] for r in by_time}
    priced = [r for r in by_cost if r["cost_usd"] is not None]
    unpriced = [r for r in by_cost if r["cost_usd"] is None]
    assert by_cost == priced + unpriced  # nulls last
    assert [(r["cost_usd"], r["id"]) for r in priced] == sorted(
        ((r["cost_usd"], r["id"]) for r in priced), reverse=True
    )
    assert [r["id"] for r in unpriced] == sorted(
        (r["id"] for r in unpriced), reverse=True
    )
    row = by_time[0]
    assert set(row) == {"id", *CSV_CALL_COLUMNS, "project_name", "user_uid"}
    assert (row["project_slug"], row["project_name"], row["user_uid"]) == (
        "demo",
        "Demo",
        None,
    )

    first = w.client.get("/api/usage/calls", params={**SEPT, "sort": "time"}).json()
    for bad in ("garbage!", first["next"][:-3] + "zzz"):
        refused = w.client.get(
            "/api/usage/calls", params={**SEPT, "sort": "time", "before": bad}
        )
        assert refused.status_code == 422, bad
        assert refused.json()["code"] == "bad_cursor"
    crossed = w.client.get(
        "/api/usage/calls", params={**SEPT, "sort": "cost", "before": first["next"]}
    )
    assert (crossed.status_code, crossed.json()["code"]) == (422, "bad_cursor")
    bad_sort = w.client.get("/api/usage/calls", params={"sort": "name"})
    assert (bad_sort.status_code, bad_sort.json()["code"]) == (422, "bad_sort")


def test_local_mode_has_no_my_usage_and_no_project_members(
    local: SimpleNamespace,
) -> None:
    w = local
    _record(w.state, w.org_id, w.project_id, user_id=w.user_id)
    _flush(w.state)
    for path in ("/api/usage/me", "/api/usage/me/calls", "/api/usage/me.csv"):
        assert w.client.get(path).status_code == 404, path
    body = w.client.get("/api/projects/demo/usage").json()
    assert set(body) == {"range", "totals", "split", "series", "members"}
    assert (body["totals"]["calls"], body["members"]) == (1, [])
    state = w.client.get("/api/portal/state").json()["usage"]
    assert state["me"] is None  # per-member budgets are production's
    assert state["org"] == {
        "spent_usd": 1.0,
        "budget_usd": None,
        "pct": None,
        "hard_stop": False,
        "blocked": False,
    }
    assert state["projects_over"] == []
    assert state["month"] == current_month()


# ---------------------------------------------------------------------------
# CSV (section 6.2 #11)
# ---------------------------------------------------------------------------


def test_csv_escaping_both_shapes_and_truncation(
    local: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = local
    _record(
        w.state,
        w.org_id,
        w.project_id,
        user_id=w.user_id,
        subject="=HYPERLINK(1)",
        cost_usd="0.123456",
        input_tokens=10,
        created_at="2026-09-02T00:00:00+00:00",
    )
    _record(w.state, w.org_id, w.project_id, created_at="2026-09-01T00:00:00+00:00")
    _flush(w.state)

    calls = w.client.get("/api/usage.csv", params=SEPT)
    assert calls.status_code == 200
    assert calls.headers["content-type"].startswith("text/csv")
    assert "whygraph-usage-local.csv" in calls.headers["content-disposition"]
    assert "x-whygraph-truncated" not in calls.headers
    lines = _csv(calls.text)
    assert lines[0] == list(CSV_CALL_COLUMNS)
    newest = dict(zip(CSV_CALL_COLUMNS, lines[1]))
    assert newest["subject"] == "'=HYPERLINK(1)"  # never a formula
    assert (newest["cost_usd"], newest["input_tokens"], newest["actor_label"]) == (
        "0.123456",
        "10",
        "Tess",
    )
    assert len(lines) == 3

    grouped = w.client.get("/api/usage.csv", params={**SEPT, "group": "member"})
    assert grouped.status_code == 200
    rows = _csv(grouped.text)
    assert rows[0] == [*CSV_GROUP_COLUMNS, "top_project_slug", "top_project_cost_usd"]
    assert [r[1] for r in rows[1:]] == ["System", "Tess"]  # by cost
    assert rows[2][-2:] == ["demo", "0.123456"]
    by_task = _csv(
        w.client.get("/api/usage.csv", params={**SEPT, "group": "task"}).text
    )
    assert by_task[0] == list(CSV_GROUP_COLUMNS)

    monkeypatch.setattr(usage_routes, "CSV_MAX_ROWS", 1)
    cut = w.client.get("/api/usage.csv", params=SEPT)
    assert cut.headers["x-whygraph-truncated"] == "1"
    lines = cut.text.splitlines()
    assert len(lines) == 3  # the header, one row, the marker
    assert lines[-1] == "# truncated at 1 rows"
    cut_groups = w.client.get("/api/usage.csv", params={**SEPT, "group": "member"})
    assert cut_groups.headers["x-whygraph-truncated"] == "1"
    assert cut_groups.text.splitlines()[-1] == "# truncated at 1 rows"


# ---------------------------------------------------------------------------
# Prices (section 6.2 #7)
# ---------------------------------------------------------------------------


def test_price_override_and_revert_are_audited(
    local: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    w = local
    table = bundled_prices()
    listed = w.client.get("/api/prices").json()
    assert listed["as_of"] == table.as_of
    assert len(listed["rows"]) == len(table.rows)
    assert {r["origin"] for r in listed["rows"]} == {"bundled"}
    assert all(r["updated_at"] is None for r in listed["rows"])
    (provider, model), _price = sorted(table.rows.items())[0]

    body = {
        "provider": provider,
        "model": model,
        "input_per_mtok": 1.25,
        "output_per_mtok": "5",
        "cache_read_per_mtok": 0.1,
    }
    put = w.client.put("/api/prices", json=body)
    assert put.status_code == 200, put.text
    row = put.json()
    assert row == {
        "provider": provider,
        "model": model,
        "input_per_mtok": 1.25,
        "output_per_mtok": 5.0,
        "cache_read_per_mtok": 0.1,
        "cache_write_per_mtok": None,
        "origin": "override",
        "updated_at": row["updated_at"],
    }
    assert row["updated_at"]
    vendor = {
        "provider": "openrouter",
        "model": "acme/model-1",  # an OpenRouter id: why prices go by body
        "input_per_mtok": 0,
        "output_per_mtok": 0,
    }
    assert w.client.put("/api/prices", json=vendor).status_code == 200
    rows = {
        (r["provider"], r["model"]): r
        for r in w.client.get("/api/prices").json()["rows"]
    }
    assert rows[(provider, model)]["origin"] == "override"
    assert rows[("openrouter", "acme/model-1")]["origin"] == "override"
    assert len(rows) == len(table.rows) + 1
    overrides = w.state.prices.for_org(w.org_id)
    assert overrides[(provider, model)][0].input == Decimal("1.25")

    for bad, code in (
        ({**body, "input_per_mtok": -1}, "invalid_price"),
        ({**body, "output_per_mtok": 10_001}, "invalid_price"),
        ({**body, "cache_write_per_mtok": "1e40"}, "invalid_price"),
        ({**body, "provider": "acme"}, "bad_provider"),
        ({**body, "model": "  "}, "bad_model"),
    ):
        refused = w.client.put("/api/prices", json=bad)
        assert refused.status_code == 422, (bad, refused.text)
        assert refused.json()["code"] == code, bad
    assert w.client.put("/api/prices", json={**body, "extra": 1}).status_code == 422

    for _ in range(2):  # idempotent
        reverted = w.client.delete(
            "/api/prices", params={"provider": provider, "model": model}
        )
        assert reverted.status_code == 204
    rows = {
        (r["provider"], r["model"]): r
        for r in w.client.get("/api/prices").json()["rows"]
    }
    assert rows[(provider, model)]["origin"] == "bundled"
    assert (provider, model) not in w.state.prices.for_org(w.org_id)

    recorded = [e for e in events(audit_log) if e["event"].startswith("price_")]
    assert [(e["event"], e["provider"], e["model"]) for e in recorded] == [
        ("price_override_set", provider, model),
        ("price_override_set", "openrouter", "acme/model-1"),
        ("price_override_removed", provider, model),  # once, not twice
    ]
    assert recorded[0]["input_per_mtok"] == "1.250000"
    assert recorded[0]["cache_write_per_mtok"] is None


# ---------------------------------------------------------------------------
# Production: visibility and payloads (section 6.2 #6, #8, #11)
# ---------------------------------------------------------------------------


def _as(v1: V1World, who: str) -> TestClient:
    v1.client.cookies.clear()
    if who == "ada":
        with portal_db.get_session() as session:
            user_id = session.exec(
                select(User.id).where(User.email == "ada@example.com")
            ).one()
    else:
        user_id = v1.ids[who]
    signed_in(v1.client, user_id)
    return v1.client


def _uid(user_id: int) -> str:
    with portal_db.get_session() as session:
        return session.get(User, user_id).uid


def _project(v1: V1World, tmp: Path, slug: str, *, restricted: bool = False) -> int:
    root = tmp / slug
    root.mkdir(parents=True)
    project_id = _insert_project(v1.org_id, slug, slug.title(), root, v1.ids["ben"])
    if restricted:
        with portal_db.get_session() as session:
            session.get(Project, project_id).restricted = True
    return project_id


def _seed_prod(v1: V1World) -> None:
    """Ben, Cy and the System each spend on ``api`` this month."""
    state = v1.state
    labels = {"ben": "Ben (@ben)", "cy": "Cy (@cy)", "fay": "Fay (@fay)"}
    for who, cost, source in (
        ("ben", "2", "chat"),
        ("cy", "0.5", "explorer"),
        ("cy", "0.25", "scan"),
    ):
        _record(
            state,
            v1.org_id,
            v1.project_id,
            user_id=v1.ids[who],
            actor_label=labels[who],
            project_slug=SLUG,
            project_name="Api project",
            source=source,
            task="analyze" if source == "scan" else "chat",
            cost_usd=cost,
        )
    _record(
        state,
        v1.org_id,
        v1.project_id,
        project_slug=SLUG,
        project_name="Api project",
        source="scan",
        task="analyze",
        cost_usd="1",
    )
    _flush(state)


def test_who_sees_which_usage(v1: V1World, tmp_path: Path) -> None:
    _seed_prod(v1)
    base = at(ORG)
    ben, cy = _uid(v1.ids["ben"]), _uid(v1.ids["cy"])

    client = _as(v1, "cy")  # a member: their own usage only
    for path in ("/api/usage", "/api/usage/calls", "/api/usage.csv"):
        refused = client.get(base + path)
        assert refused.status_code == 403, path
        assert refused.json()["action"] == "org.usage"
    mine = client.get(base + "/api/usage/me", params={"member": ben}).json()
    assert (mine["totals"]["calls"], mine["totals"]["cost_usd"]) == (2, 0.75)
    assert mine["split"] == {
        "interactive": {"calls": 1, "cost_usd": 0.5},
        "scans": {"calls": 1, "cost_usd": 0.25},
    }
    grouped = client.get(base + "/api/usage/me", params={"group": "member"}).json()
    assert [(g["key"], g["label"]) for g in grouped["groups"]] == [(cy, "Cy (@cy)")]
    my_calls = client.get(base + "/api/usage/me/calls", params={"member": ben}).json()
    assert {r["user_uid"] for r in my_calls["items"]} == {cy}
    my_csv = _csv(client.get(base + "/api/usage/me.csv", params={"member": ben}).text)
    assert {r[2] for r in my_csv[1:]} == {"Cy (@cy)"}
    assert len(my_csv) == 3
    assert client.get(base + "/api/projects/api/usage").status_code == 403
    assert client.get(base + "/api/prices").status_code == 200
    assert client.put(base + "/api/prices", json={}).status_code in (403, 422)
    refused = client.delete(
        base + "/api/prices", params={"provider": "openai", "model": "x"}
    )
    assert (refused.status_code, refused.json()["action"]) == (403, "org.budgets")

    client = _as(v1, "fay")  # an org admin: everyone
    everyone = client.get(base + "/api/usage", params={"group": "member"}).json()
    assert everyone["totals"]["calls"] == 4
    assert [(g["label"], g["cost_usd"]) for g in everyone["groups"]] == [
        ("Ben (@ben)", 2.0),
        ("System", 1.0),
        ("Cy (@cy)", 0.75),
    ]
    assert [g["key"] for g in everyone["groups"]] == [ben, None, cy]
    cy_row = everyone["groups"][2]
    assert cy_row["interactive"] == {"calls": 1, "cost_usd": 0.5}
    assert cy_row["top_project"] == {
        "slug": SLUG,
        "name": "Api project",
        "cost_usd": 0.75,
    }
    project = client.get(base + "/api/projects/api/usage").json()
    assert [m["key"] for m in project["members"]] == [ben, None, cy]
    assert client.get(base + "/api/usage.csv").status_code == 200
    fay_put = client.put(
        base + "/api/prices",
        json={
            "provider": "openai",
            "model": "x",
            "input_per_mtok": 1,
            "output_per_mtok": 1,
        },
    )
    assert fay_put.status_code == 200, fay_put.text

    client = _as(v1, "ada")  # the instance admin reads, writes nothing
    read = client.get(base + "/api/usage").json()
    assert read["totals"]["calls"] == 4
    assert client.get(base + "/api/usage/me").json()["totals"]["calls"] == 0
    for method in ("PUT", "DELETE"):
        refused = client.request(
            method,
            base + "/api/prices",
            params={"provider": "openai", "model": "x"},
            json={
                "provider": "openai",
                "model": "x",
                "input_per_mtok": 2,
                "output_per_mtok": 2,
            },
        )
        assert (refused.status_code, refused.json()["code"]) == (403, "forbidden")
    refused = client.get(base + "/api/projects/api/usage")
    assert refused.status_code == 403  # a reader is a viewer on every project

    # A project admin by grant reads that project's usage, and only that one's.
    web = _project(v1, tmp_path, "web")
    _project(v1, tmp_path, "secret", restricted=True)
    with portal_db.get_session() as session:
        session.add(
            ProjectGrant(
                org_id=v1.org_id,
                project_id=v1.project_id,
                user_id=v1.ids["cy"],
                role="admin",
            )
        )
    _record(v1.state, v1.org_id, web, project_slug="web", project_name="Web")
    _flush(v1.state)
    client = _as(v1, "cy")
    granted = client.get(base + "/api/projects/api/usage")
    assert granted.status_code == 200, granted.text
    assert granted.json()["totals"]["calls"] == 4  # never web's call
    assert client.get(base + "/api/projects/web/usage").status_code == 403
    for method, path in (
        ("GET", "/api/projects/secret/usage"),
        ("PUT", "/api/projects/secret/budget"),
        ("DELETE", "/api/projects/secret/budget"),
    ):
        hidden = client.request(method, base + path, json={"monthly_usd": 1})
        assert hidden.status_code == 404, (method, path)
        assert hidden.json() == {"error": "project 'secret' not found"}
    assert client.get(base + "/api/usage").status_code == 403  # still a member


def _budget(v1: V1World, scope: str, amount: str, **ids: int) -> None:
    with portal_db.get_session() as session:
        session.add(
            Budget(
                org_id=v1.org_id,
                scope=scope,
                monthly_usd=Decimal(amount),
                hard_stop=True,
                updated_at=_now(),
                **ids,
            )
        )
    reload_org_budgets(v1.state.budgets, v1.org_id)


def test_the_payload_fields_follow_the_role(v1: V1World) -> None:
    _seed_prod(v1)  # ben 2, cy 0.75, the System 1 - all on api
    _budget(v1, "org", "100")
    _budget(v1, "member_default", "1")
    _budget(v1, "project", "3.5", project_id=v1.project_id)
    base = at(ORG)

    client = _as(v1, "cy")
    usage = client.get(base + "/api/portal/state").json()["usage"]
    assert usage["me"] == {
        "spent_usd": 0.75,
        "budget_usd": 1.0,
        "pct": 75.0,
        "hard_stop": True,
        "blocked": False,
    }
    assert (usage["org"], usage["projects_over"]) == (None, None)
    (listed,) = client.get(base + "/api/projects").json()["projects"]
    assert (listed["llm_block"], listed["llm_block_scope"], listed["usage"]) == (
        "budget_exceeded",
        "project",  # 3.75 of 3.5
        None,
    )
    details = client.get(base + "/api/projects/api").json()
    assert (details["llm_block"], details["llm_block_scope"]) == (
        "budget_exceeded",
        "project",
    )
    assert details["usage"] is None
    members = client.get(base + "/api/org/members").json()
    assert all("month_spend_usd" not in m for m in members)

    client = _as(v1, "ben")
    usage = client.get(base + "/api/portal/state").json()["usage"]
    assert usage["me"]["blocked"] is True  # 2 of a hard 1
    assert usage["org"] == {
        "spent_usd": 3.75,
        "budget_usd": 100.0,
        "pct": 3.8,
        "hard_stop": True,
        "blocked": False,
    }
    assert usage["projects_over"] == [
        {"slug": SLUG, "name": "Api project", "pct": 107.1}
    ]
    details = client.get(base + "/api/projects/api").json()
    assert details["usage"] == {
        "month_spend_usd": 3.75,
        "budget": {"monthly_usd": 3.5, "hard_stop": True},
        "pct": 107.1,
    }
    # the member budget comes first (section 4.7)
    assert (details["llm_block"], details["llm_block_scope"]) == (
        "budget_exceeded",
        "member",
    )
    members = {m["uid"]: m for m in client.get(base + "/api/org/members").json()}
    ben, cy, fay = (_uid(v1.ids[n]) for n in ("ben", "cy", "fay"))
    assert (members[ben]["month_spend_usd"], members[ben]["month_split"]) == (
        2.0,
        {"interactive": 2.0, "scans": 0.0},
    )
    assert members[cy]["month_split"] == {"interactive": 0.5, "scans": 0.25}
    assert members[fay]["month_spend_usd"] == 0.0
    people = {
        p["uid"]: p
        for p in client.get(base + "/api/projects/api/access").json()["people"]
    }
    assert (people[ben]["month_spend_usd"], people[cy]["month_spend_usd"]) == (
        2.0,
        0.75,
    )
    assert people[fay]["month_spend_usd"] == 0.0

    # A viewer's block is their role, whatever the budgets say.
    with portal_db.get_session() as session:
        session.add(
            ProjectGrant(
                org_id=v1.org_id,
                project_id=v1.project_id,
                user_id=v1.ids["cy"],
                role="viewer",
            )
        )
    client = _as(v1, "cy")
    (listed,) = client.get(base + "/api/projects").json()["projects"]
    assert (listed["llm_block"], listed["llm_block_scope"]) == ("role", None)

    client = _as(v1, "ada")  # a reader: no "me", the org's numbers
    usage = client.get(base + "/api/portal/state").json()["usage"]
    assert usage["me"] is None and usage["org"]["spent_usd"] == 3.75
    # The base host names no org.
    assert client.get(at() + "/api/portal/state").json()["usage"] is None


def test_account_usage_lists_memberships_only(v1: V1World) -> None:
    _seed_prod(v1)
    _budget(v1, "member_default", "4")
    with portal_db.get_session() as session:
        other = create_org(session, slug="other", name="Beta").id
        add_member(session, org_id=other, user_id=v1.ids["ben"], role="member")
    client = _as(v1, "ben")
    for host in (at(), at(ORG), at("other")):
        body = client.get(host + "/api/account/usage").json()
        assert body["month"] == current_month()
        assert body["resets_at"].endswith("-01T00:00:00+00:00")
        assert [o["slug"] for o in body["orgs"]] == ["acme", "other"]  # Acme, Beta
    acme, beta = body["orgs"]
    assert acme == {
        "slug": "acme",
        "name": "Acme",
        "url": at(ORG),
        "spent_usd": 2.0,
        "calls": 1,
        "budget_usd": 4.0,
        "pct": 50.0,
        "hard_stop": True,
    }
    assert (beta["spent_usd"], beta["calls"], beta["budget_usd"]) == (0.0, 0, None)
    client = _as(v1, "ada")  # an instance admin reads orgs, but is in none
    assert client.get(at() + "/api/account/usage").json()["orgs"] == []
    with portal_db.get_session() as session:
        assert session.get(Membership, (other, v1.ids["cy"])) is None
    client = _as(v1, "cy")
    assert [
        o["slug"] for o in client.get(at() + "/api/account/usage").json()["orgs"]
    ] == ["acme"]


def test_machines_deleted_members_and_top_projects(v1: V1World) -> None:
    state = v1.state
    with portal_db.get_session() as session:
        token_id = session.exec(select(ConnectionToken.id)).one()
        gone = User(display_name="Gone", github_id=6999, github_login="gone")
        session.add(gone)
        session.flush()
        gone_id = gone.id
        assert session.get(Organization, v1.org_id) is not None
    common = {"project_slug": SLUG, "project_name": "Api project"}
    _record(
        state,
        v1.org_id,
        v1.project_id,
        user_id=v1.ids["ben"],
        actor_label="Ben (@ben)",
        source="agent",
        connection_id=token_id,
        client_name="laptop",
        cost_usd="1",
        **common,
    )
    _record(  # its connection was swept: the machine name is kept
        state,
        v1.org_id,
        v1.project_id,
        user_id=v1.ids["ben"],
        actor_label="Ben (@ben)",
        source="agent",
        client_name="old-laptop",
        cost_usd="0.5",
        **common,
    )
    _record(  # a removed project costs Ben more
        state,
        v1.org_id,
        None,
        user_id=v1.ids["ben"],
        actor_label="Ben (@ben)",
        project_slug="old",
        project_name="Old",
        cost_usd="3",
    )
    _record(
        state,
        v1.org_id,
        v1.project_id,
        user_id=gone_id,
        actor_label="Gone (@gone)",
        cost_usd="0.25",
        **common,
    )
    _flush(state)
    with portal_db.get_session() as session:
        session.delete(session.get(User, gone_id))  # the rows keep the label
    client = _as(v1, "ben")
    base = at(ORG) + "/api/usage"

    def groups(group: str) -> list[tuple[Any, str, float]]:
        body = client.get(base, params={"group": group}).json()
        return [(g["key"], g["label"], g["cost_usd"]) for g in body["groups"]]

    assert groups("machine") == [
        (None, "Portal", 3.25),
        (token_id, "laptop", 1.0),
        (None, "old-laptop", 0.5),
    ]
    ben = _uid(v1.ids["ben"])
    assert groups("member") == [(ben, "Ben (@ben)", 4.5), (None, "Gone (@gone)", 0.25)]
    body = client.get(base, params={"group": "member"}).json()
    assert body["groups"][0]["top_project"] == {
        "slug": "old",
        "name": "Old",
        "cost_usd": 3.0,
    }
    assert groups("project") == [("old", "Old", 3.0), (SLUG, "Api project", 1.75)]
