"""The usage ledger in the portal (M2f-2 plan sections 4.5 and 4.6).

Unit tests for the :class:`~whygraph.portal.usage_store.SpendBook`, the
pricing of one call and :class:`~whygraph.portal.usage.PortalUsageSink`;
Postgres tests for the :class:`~whygraph.portal.usage_store.UsageWriter`,
the start-up seeding (and its degraded mode), the price overrides, the
writer's place in the lifespan and the prune; and end-to-end attribution:
an Explorer evidence read that backfills, an Explorer Generate, a chat
turn, a local MCP call and an ``/api/v1`` rationale call each write rows
with the right actor, label, source, task, session / connection and key
scope.

Only the LLM is stubbed, at the client factory, so every metered function
(and its ``record_usage``) runs for real.
"""

# ruff: noqa: F811 -- pytest fixtures (`env`, `production_env`, `v1`) are imported

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from sqlmodel import select

from test_portal_app import (  # noqa: F401 -- fixtures
    env,
    make_repo,
    manual_ctx,
    portal_client,
    prod_portal,
    production_env,
    seed_codegraph,
)
from test_portal_mcp import MCP_HEADERS, _rpc, _setup_project
from test_portal_v1 import V1World, call, target_body, v1  # noqa: F401
from whygraph.core.config import Config, LlmConfig
from whygraph.core.context import use_project
from whygraph.core.usage import UsageRecord, UsageScope
from whygraph.db import get_session as project_session
from whygraph.db.models import ChatSession as ChatSessionRow
from whygraph.db.models import Commit
from whygraph.portal import audit_store, usage_store, v1_routes
from whygraph.portal import db as portal_db
from whygraph.portal.app import create_portal_app
from whygraph.portal.estimate import ORG_PRICES_LABEL
from whygraph.portal.models import (
    BudgetAlert,
    Budget,
    ConnectionToken,
    Organization,
    PriceOverride,
    Project,
    ScanRun,
    UsageEvent,
    User,
)
from whygraph.portal.prices import Price
from whygraph.portal.runner import ScanRunner
from whygraph.portal.secrets import LLM_API_KEY, put_secret
from whygraph.portal.usage import (
    Attribution,
    PortalUsageSink,
    actor_label,
    price_usage,
)
from whygraph.portal.usage_store import (
    PriceBook,
    SpendBook,
    UsageRow,
    UsageWriter,
    load_price_overrides,
    reload_org_prices,
    seed_spend_book,
)
from whygraph.serve import chat as serve_chat
from whygraph.services.git import Repository
from whygraph.services.llm import (
    CompletionRequest,
    CompletionResponse,
    LlmClient,
    LlmClientFactory,
)
from whygraph.services.llm.chat import TextDelta, ToolCall, ToolCallMade, TurnDone

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
STAMP = NOW.isoformat(timespec="seconds")

_CARD = json.dumps(
    {
        "purpose": "Holds the sample lines.",
        "why": "Two commits built it up.",
        "constraints": [],
        "tradeoffs": [],
        "risks": [],
    }
)


def _row(**overrides: Any) -> UsageRow:
    values: dict[str, Any] = {
        "org_id": 1,
        "project_id": 10,
        "project_slug": "demo",
        "project_name": "Demo",
        "actor_kind": "member",
        "user_id": 100,
        "actor_label": "Tess",
        "source": "explorer",
        "task": "analyze",
        "provider": "anthropic",
        "model_requested": "claude-opus-4-7",
        "key_scope": "org",
        "cost_source": "estimated",
        "created_at": STAMP,
        "cost_usd": Decimal("0.5"),
    }
    values.update(overrides)
    return UsageRow(**values)


def _rec(**overrides: Any) -> UsageRecord:
    values: dict[str, Any] = {
        "task": "analyze",
        "provider": "anthropic",
        "model_requested": "claude-opus-4-7",
        "model_served": None,
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
        "provider_cost_usd": None,
        "subject": None,
        "duration_ms": 12,
    }
    values.update(overrides)
    return UsageRecord(**values)


# ---------------------------------------------------------------------------
# SpendBook (unit)
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


def test_spend_book_counts_per_org_project_and_user() -> None:
    book = SpendBook(clock=_Clock(NOW))
    book.add(_row())
    book.add(_row(user_id=101, cost_usd=Decimal("0.25")))
    book.add(_row(project_id=11, user_id=None, actor_kind="system"))
    assert book.spent(1) == Decimal("1.25")
    assert book.spent(1, project_id=10) == Decimal("0.75")
    assert book.spent(1, project_id=11) == Decimal("0.5")
    assert book.spent(1, user_id=100) == Decimal("0.5")
    assert book.spent(1, user_id=101) == Decimal("0.25")
    assert book.spent(2) == 0
    with pytest.raises(ValueError):
        book.spent(1, project_id=10, user_id=100)


def test_spend_book_ignores_unpriced_rows() -> None:
    book = SpendBook(clock=_Clock(NOW))
    assert book.add(_row(cost_usd=None, cost_source="unpriced")) is None
    assert book.add(_row(cost_source="unpriced")) is None
    assert book.spent(1) == 0


def test_spend_book_rolls_over_and_reads_zero_for_a_stale_month() -> None:
    clock = _Clock(NOW)
    book = SpendBook(clock=clock)
    book.add(_row())
    clock.moment = datetime(2026, 11, 1, 0, 0, 1, tzinfo=timezone.utc)
    # A new month lifts the spend before any new row arrives...
    assert book.spent(1) == 0
    # ...a late row of the old month changes nothing...
    assert book.add(_row(created_at="2026-10-31T23:59:59+00:00")) is not None
    assert book.spent(1) == 0
    # ...and the first row of the new month rolls the book over.
    book.add(_row(created_at="2026-11-01T00:00:01+00:00", cost_usd=Decimal("2")))
    assert book.month == "2026-11"
    assert book.spent(1) == Decimal("2")
    assert book.add(_row(created_at="2026-10-31T23:59:59+00:00")) is None


def test_spend_book_seed_and_the_add_hook() -> None:
    book = SpendBook(clock=_Clock(NOW))
    book.seed(
        "2026-10",
        [(1, 10, 100, Decimal("1")), (1, None, None, Decimal("2")), (2, 20, 200, 3)],
    )
    assert book.spent(1) == Decimal("3")
    assert book.spent(1, project_id=10) == Decimal("1")
    assert book.spent(2, user_id=200) == Decimal("3")
    seen: list[tuple[UsageRow, Any]] = []
    book.on_add = lambda row, totals: seen.append((row, totals))
    row = _row()
    totals = book.add(row)
    assert seen == [(row, totals)]
    assert (totals.org, totals.project, totals.user) == (
        Decimal("3.5"),
        Decimal("1.5"),
        Decimal("1.5"),
    )

    def _boom(row: UsageRow, totals: Any) -> None:
        raise RuntimeError("hook down")

    book.on_add = _boom
    book.add(_row())  # a failing hook never stops the count
    assert book.spent(1) == Decimal("4")


# ---------------------------------------------------------------------------
# Pricing and the sink (unit)
# ---------------------------------------------------------------------------


def test_price_usage_estimates_from_the_bundled_table() -> None:
    priced = price_usage(
        _rec(),
        provider="anthropic",
        model_requested="claude-opus-4-7",
        overrides={},
        config=Config(),
    )
    assert priced.cost_source == "estimated"
    assert priced.price_version is not None
    assert priced.price_version.startswith("bundled:")
    assert priced.cost_usd is not None and priced.cost_usd > 0


def test_price_usage_prefers_an_org_override() -> None:
    overrides = {
        ("anthropic", "claude-opus-4-7"): (
            Price(input=Decimal("1"), output=Decimal("2")),
            "2026-10-01T00:00:00+00:00",
        )
    }
    priced = price_usage(
        _rec(),
        provider="anthropic",
        model_requested="claude-opus-4-7",
        overrides=overrides,
        config=None,
    )
    assert (priced.cost_usd, priced.cost_source, priced.price_version) == (
        Decimal("1.000000"),
        "estimated",
        "org:2026-10-01T00:00:00+00:00",
    )


def test_price_usage_falls_back_to_the_served_model() -> None:
    priced = price_usage(
        _rec(model_served="gpt-4o"),
        provider="openai",
        model_requested="my-alias",
        overrides={},
        config=None,
    )
    assert priced.cost_source == "estimated"


def test_price_usage_openrouter_cost_and_its_custom_endpoint_rule() -> None:
    rec = _rec(provider_cost_usd=0.0421, model_served="openai/gpt-4o")
    priced = price_usage(
        rec,
        provider="openrouter",
        model_requested="openai/gpt-4o",
        overrides={},
        config=None,
    )
    assert (priced.cost_usd, priced.cost_source, priced.price_version) == (
        Decimal("0.042100"),
        "provider",
        None,
    )
    # Another provider's "cost" is never trusted.
    assert (
        price_usage(
            _rec(provider_cost_usd=9.0),
            provider="anthropic",
            model_requested="claude-opus-4-7",
            overrides={},
            config=None,
        ).cost_source
        == "estimated"
    )
    # A custom endpoint may be another service: never priced from the bundled
    # table (OpenRouter has no endpoint setting, so its cost is always its own).
    custom = Config(
        llm=LlmConfig(openai=replace(LlmConfig().openai, base_url="http://x"))
    )
    priced = price_usage(
        _rec(), provider="openai", model_requested="gpt-4o", overrides={}, config=custom
    )
    assert (priced.cost_usd, priced.cost_source) == (None, "unpriced")


def test_price_usage_without_tokens_is_unpriced() -> None:
    rec = _rec(input_tokens=None, output_tokens=None)
    priced = price_usage(
        rec,
        provider="anthropic",
        model_requested="claude-opus-4-7",
        overrides={},
        config=None,
    )
    assert (priced.cost_usd, priced.cost_source) == (None, "unpriced")
    unknown = price_usage(
        _rec(), provider="ollama", model_requested="llama3", overrides={}, config=None
    )
    assert unknown.cost_source == "unpriced"


def test_actor_label() -> None:
    principal = SimpleNamespace(display_name="Ada Lovelace", github_login="ada")
    assert actor_label(principal, production=True) == "Ada Lovelace (@ada)"
    assert actor_label(principal, production=False) == "Ada Lovelace"
    no_login = SimpleNamespace(display_name="Bo", github_login=None)
    assert actor_label(no_login, production=True) == "Bo"


class _FakeWriter:
    def __init__(self) -> None:
        self.rows: list[UsageRow] = []

    def submit(self, row: UsageRow) -> bool:
        self.rows.append(row)
        return True


def test_portal_sink_attributes_prices_counts_and_queues() -> None:
    writer, book = _FakeWriter(), SpendBook()
    scope = UsageScope(source="chat")
    sink = PortalUsageSink(
        scope,
        Attribution(
            org_id=1,
            org_slug="acme",
            project_id=10,
            project_slug="demo",
            project_name="Demo",
            actor_kind="member",
            user_id=100,
            actor_label="Tess",
            config=Config(),
            key_scopes={"anthropic": "project"},
            connection_id=7,
            client_name="laptop",
        ),
        writer,  # type: ignore[arg-type]
        book,
        PriceBook(),
    )
    scope.chat_session_id = 5
    sink.record(_rec(task="chat", subject="x"))
    sink.record(_rec(task="chat", provider="openai", model_requested="gpt-4o"))
    first, second = writer.rows
    assert (first.source, first.chat_session_id, first.task) == ("chat", 5, "chat")
    assert (first.org_id, first.project_id, first.user_id) == (1, 10, 100)
    assert (first.connection_id, first.client_name) == (7, "laptop")
    assert (first.key_scope, second.key_scope) == ("project", "none")
    assert first.cost_source == "estimated" and first.cost_usd is not None
    assert first.duration_ms == 12
    assert book.spent(1, user_id=100) == first.cost_usd + second.cost_usd
    assert sink.blocked_scope() is None


# ---------------------------------------------------------------------------
# Postgres: the writer, seeding, prices, prune
# ---------------------------------------------------------------------------


def _org_and_project(name: str = "acme") -> tuple[int, int, int]:
    """An org, a member and a project, inserted directly; returns their ids."""
    with portal_db.get_session() as session:
        org = Organization(slug=name, name=name.title())
        user = User(display_name="Tess")
        session.add_all([org, user])
        session.flush()
        project = Project(
            org_id=org.id, slug="demo", name="Demo", source="local", root="/nowhere"
        )
        session.add(project)
        session.flush()
        return org.id, project.id, user.id


@pytest.fixture
def world(portal_database: str) -> Iterator[SimpleNamespace]:
    org_id, project_id, user_id = _org_and_project()
    writer = UsageWriter(batch_wait=30.0)
    writer.start()
    try:
        yield SimpleNamespace(
            org_id=org_id, project_id=project_id, user_id=user_id, writer=writer
        )
    finally:
        writer.stop()


def _ledger() -> list[UsageEvent]:
    with portal_db.get_session() as session:
        rows = session.exec(select(UsageEvent).order_by(UsageEvent.id)).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def test_writer_writes_rows(world: SimpleNamespace) -> None:
    row = _row(
        org_id=world.org_id,
        project_id=world.project_id,
        user_id=world.user_id,
        subject="a" * 300,
        actor_label="leaked ghp_" + "A" * 36,
        input_tokens=10,
        output_tokens=2,
        duration_ms=5,
        price_version="bundled:2026-10-07",
    )
    assert world.writer.submit(row)
    assert world.writer.flush()
    (stored,) = _ledger()
    assert (stored.org_id, stored.project_id, stored.user_id) == (
        world.org_id,
        world.project_id,
        world.user_id,
    )
    assert stored.subject == "a" * 200  # capped
    assert "A" * 36 not in stored.actor_label  # redacted
    assert stored.cost_usd == Decimal("0.500000")
    assert (stored.input_tokens, stored.output_tokens, stored.duration_ms) == (10, 2, 5)
    assert stored.price_version == "bundled:2026-10-07"


def test_writer_nulls_gone_references_and_drops_gone_orgs(
    world: SimpleNamespace,
) -> None:
    gone = _row(
        org_id=world.org_id,
        project_id=999_999,
        user_id=999_999,
        scan_run_id=999_999,
        connection_id=999_999,
    )
    orphan = _row(org_id=999_999, project_id=None, user_id=None, actor_kind="system")
    for row in (gone, orphan, _row(org_id=world.org_id, project_id=None, user_id=None)):
        assert world.writer.submit(row)
    assert world.writer.flush()
    rows = _ledger()
    assert len(rows) == 2  # the gone org's row was dropped, not the batch
    first = rows[0]
    assert (
        first.project_id,
        first.user_id,
        first.scan_run_id,
        first.connection_id,
    ) == (
        None,
        None,
        None,
        None,
    )
    assert first.actor_kind == "member" and first.actor_label == "Tess"


def test_writer_keeps_live_run_and_connection_ids(world: SimpleNamespace) -> None:
    with portal_db.get_session() as session:
        run = ScanRun(project_id=world.project_id, trigger="manual")
        token = ConnectionToken(
            uid="t1",
            token_hash="h" * 64,
            user_id=world.user_id,
            org_id=world.org_id,
            project_id=world.project_id,
            client_name="laptop",
        )
        session.add_all([run, token])
        session.flush()
        run_id, token_id = run.id, token.id
    world.writer.submit(
        _row(
            org_id=world.org_id,
            project_id=world.project_id,
            user_id=world.user_id,
            scan_run_id=run_id,
            connection_id=token_id,
            client_name="laptop",
        )
    )
    assert world.writer.flush()
    (stored,) = _ledger()
    assert (stored.scan_run_id, stored.connection_id, stored.client_name) == (
        run_id,
        token_id,
        "laptop",
    )


def test_writer_retries_a_failed_batch_row_by_row(world: SimpleNamespace) -> None:
    bad = _row(org_id=world.org_id, task="nope")  # violates ck_usage_events_task
    good = _row(org_id=world.org_id, project_id=None, user_id=None)
    world.writer.submit(bad)
    world.writer.submit(good)
    assert world.writer.flush()
    assert [r.task for r in _ledger()] == ["analyze"]


def test_a_full_queue_drops_rows_but_the_book_still_counts(
    portal_database: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logging.getLogger("whygraph"), "propagate", True)
    writer = UsageWriter(maxsize=1)  # never started: nothing drains it
    book = SpendBook()
    sink = PortalUsageSink(
        UsageScope(source="explorer"),
        Attribution(
            org_id=1,
            org_slug="acme",
            project_id=None,
            project_slug="demo",
            project_name="Demo",
            actor_kind="member",
            user_id=5,
            actor_label="Tess",
            config=Config(),
        ),
        writer,
        book,
        PriceBook(),
    )
    with caplog.at_level(logging.WARNING, logger="whygraph.portal.usage_store"):
        for _ in range(3):
            sink.record(_rec())
    assert "queue full, dropped 1 row(s)" in caplog.text
    assert caplog.text.count("queue full") == 1  # one line a minute
    unit = price_usage(
        _rec(),
        provider="anthropic",
        model_requested="claude-opus-4-7",
        overrides={},
        config=None,
    ).cost_usd
    assert unit is not None and unit > 0
    assert book.spent(1, user_id=5) == 3 * unit
    assert writer.flush() is False  # not running


def test_seed_spend_book_reads_this_months_priced_rows(world: SimpleNamespace) -> None:
    month = NOW.strftime("%Y-%m")
    rows = [
        _row(org_id=world.org_id, project_id=world.project_id, user_id=world.user_id),
        _row(
            org_id=world.org_id,
            project_id=world.project_id,
            user_id=None,
            actor_kind="system",
            cost_source="provider",
            cost_usd=Decimal("1"),
        ),
        _row(org_id=world.org_id, cost_source="unpriced", cost_usd=None),
        _row(org_id=world.org_id, created_at="2026-09-30T23:59:59+00:00"),
    ]
    for row in rows:
        world.writer.submit(row)
    assert world.writer.flush()
    book = SpendBook(clock=_Clock(NOW))
    seed_spend_book(book, now=NOW)
    assert book.month == month
    assert book.spent(world.org_id) == Decimal("1.5")
    assert book.spent(world.org_id, project_id=world.project_id) == Decimal("1.5")
    assert book.spent(world.org_id, user_id=world.user_id) == Decimal("0.5")


def test_price_overrides_load_and_reload(world: SimpleNamespace) -> None:
    with portal_db.get_session() as session:
        session.add(
            PriceOverride(
                org_id=world.org_id,
                provider="anthropic",
                model="claude-opus-4-7",
                input_per_mtok=Decimal("1.5"),
                output_per_mtok=Decimal("3"),
                cache_read_per_mtok=None,
                cache_write_per_mtok=Decimal("2"),
                updated_at="2026-10-02T00:00:00+00:00",
            )
        )
    loaded = load_price_overrides()
    price, updated_at = loaded[world.org_id][("anthropic", "claude-opus-4-7")]
    assert price == Price(Decimal("1.5"), Decimal("3"), None, Decimal("2"))
    assert updated_at == "2026-10-02T00:00:00+00:00"

    book = PriceBook()
    book.replace_all(loaded)
    with portal_db.get_session() as session:
        for row in session.exec(select(PriceOverride)).all():
            session.delete(row)
    reload_org_prices(book, world.org_id)
    assert book.for_org(world.org_id) == {}


def test_prune_deletes_old_rows_and_alerts(world: SimpleNamespace) -> None:
    old = (NOW - timedelta(days=401)).isoformat(timespec="seconds")
    recent = (NOW - timedelta(days=399)).isoformat(timespec="seconds")
    for stamp in (old, recent):
        world.writer.submit(_row(org_id=world.org_id, created_at=stamp))
    assert world.writer.flush()
    with portal_db.get_session() as session:
        budget = Budget(
            org_id=world.org_id,
            scope="org",
            monthly_usd=Decimal("10"),
            hard_stop=False,
            updated_at=STAMP,
        )
        session.add(budget)
        session.flush()
        for month in ("2025-08", "2025-09", "2025-10"):
            session.add(
                BudgetAlert(
                    budget_id=budget.id,
                    month=month,
                    threshold=50,
                    spent_usd=Decimal("5"),
                )
            )
    assert usage_store.prune(now=NOW) == 1
    assert [r.created_at for r in _ledger()] == [recent]
    with portal_db.get_session() as session:
        months = sorted(a.month for a in session.exec(select(BudgetAlert)).all())
    assert months == ["2025-09", "2025-10"]  # 13 months back from 2026-10
    assert usage_store.RETENTION_DAYS == audit_store.RETENTION_DAYS


# ---------------------------------------------------------------------------
# Start-up and the lifespan
# ---------------------------------------------------------------------------


def test_startup_seeds_the_book_and_the_prices(env: SimpleNamespace) -> None:
    with portal_client() as client:
        client.post("/api/portal/setup", json={"display_name": "Tess"})
        state = client.app.state.portal
        org_id = state.builtin_org_id
        with portal_db.get_session() as session:
            session.add(
                PriceOverride(
                    org_id=org_id,
                    provider="openai",
                    model="gpt-4o",
                    input_per_mtok=Decimal("1"),
                    output_per_mtok=Decimal("1"),
                )
            )
        project = Project(org_id=org_id, slug="p", name="P", source="local", root="/x")
        with portal_db.get_session() as session:
            session.add(project)
            session.flush()
            project_id = project.id
        state.usage_writer.submit(
            _row(
                org_id=org_id,
                project_id=project_id,
                user_id=None,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        )
        assert state.usage_writer.flush()
    with portal_client() as client:
        state = client.app.state.portal
        assert state.degraded is None
        assert state.spend.spent(org_id, project_id=project_id) == Decimal("0.5")
        assert ("openai", "gpt-4o") in state.prices.for_org(org_id)


def test_a_failing_seed_leaves_the_portal_degraded(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(book: SpendBook, **kwargs: Any) -> None:
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(usage_store, "seed_spend_book", _boom)
    with portal_client() as client:
        state = client.get("/api/portal/state")
        assert state.status_code == 200
        error = state.json()["error"]
        assert "could not read this month's spend" in error
        assert "ledger unreadable" in error
        assert client.get("/api/projects").status_code == 503
        portal = client.app.state.portal
        assert not portal.usage_writer.running  # nothing started when degraded


def test_writer_start_and_stop_order(
    production_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit writer -> usage writer -> runner; stopped runner -> usage -> audit."""
    order: list[str] = []

    def _spy(cls: type, name: str, label: str) -> None:
        original = getattr(cls, name)

        def wrapper(self, *args: Any, **kwargs: Any) -> Any:
            order.append(label)
            return original(self, *args, **kwargs)

        async def awrapper(self, *args: Any, **kwargs: Any) -> Any:
            order.append(label)
            return await original(self, *args, **kwargs)

        import inspect

        monkeypatch.setattr(
            cls, name, awrapper if inspect.iscoroutinefunction(original) else wrapper
        )

    _spy(audit_store.AuditWriter, "start", "audit.start")
    _spy(audit_store.AuditWriter, "stop", "audit.stop")
    _spy(UsageWriter, "start", "usage.start")
    _spy(UsageWriter, "stop", "usage.stop")
    _spy(ScanRunner, "start", "runner.start")
    _spy(ScanRunner, "shutdown", "runner.shutdown")
    with prod_portal():
        pass
    assert order == [
        "audit.start",
        "usage.start",
        "runner.start",
        "runner.shutdown",
        "usage.stop",
        "audit.stop",
    ]


def test_local_mode_runs_the_usage_writer_without_an_audit_writer(
    env: SimpleNamespace,
) -> None:
    app = create_portal_app(port=8765)
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="http://127.0.0.1:8765"):
        assert app.state.portal.usage_writer.running
    assert not app.state.portal.usage_writer.running


# ---------------------------------------------------------------------------
# End to end: who a call is charged to
# ---------------------------------------------------------------------------


class _LedgerClient(LlmClient):
    """A completion client for any provider: a rationale card every time."""

    provider = "stub"

    def __init__(self, provider: str, model: str) -> None:
        super().__init__(model=model)
        self.provider = provider

    @classmethod
    def from_config(cls, config: Any, **overrides: Any) -> "_LedgerClient":
        raise NotImplementedError

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        return CompletionResponse(
            text=_CARD,
            model=f"{self.model}-served",
            provider=self.provider,
            input_tokens=1000,
            output_tokens=100,
        )


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every completion client the factory makes is a :class:`_LedgerClient`."""

    def make(self, provider: str, *, model: str | None = None, **kw: Any) -> LlmClient:
        return _LedgerClient(provider, model or "default-model")

    monkeypatch.setattr(LlmClientFactory, "make", make)


def _seed_undescribed(root: Path) -> list[str]:
    """The repo's two commits as WhyGraph rows, without descriptions."""
    shas = []
    newest, oldest = list(Repository(root).commits)
    with use_project(manual_ctx(root)), project_session() as session:
        for commit, when in ((oldest, "2026-01-01"), (newest, "2026-02-01")):
            shas.append(commit.sha)
            session.add(
                Commit(
                    sha=commit.sha,
                    parent_shas="",
                    author_name="Test User",
                    author_email="tester@example.com",
                    authored_at=f"{when}T00:00:00+00:00",
                    committed_at=f"{when}T00:00:00+00:00",
                    subject=f"commit of {when}",
                    body="",
                    files_changed=1,
                    insertions=1,
                    deletions=0,
                    scanned_at="2026-05-01T00:00:00+00:00",
                )
            )
    return shas


@pytest.fixture
def local(env: SimpleNamespace, stub_llm: None) -> Iterator[SimpleNamespace]:
    """A local portal with ``demo``: two undescribed commits, a CodeGraph index."""
    root = make_repo(env.shared, "demo")
    seed_codegraph(root)
    with portal_client() as client:
        _setup_project(client, root)
        shas = _seed_undescribed(root)
        state = client.app.state.portal
        with portal_db.get_session() as session:
            project = session.exec(select(Project).where(Project.slug == "demo")).one()
            user = session.exec(select(User)).one()
            ids = (project.id, project.org_id, user.id)
        yield SimpleNamespace(
            client=client,
            root=root,
            state=state,
            shas=shas,
            project_id=ids[0],
            org_id=ids[1],
            user_id=ids[2],
        )


def _key(w: SimpleNamespace, provider: str, *, project: bool) -> None:
    with portal_db.get_session() as session:
        put_secret(
            session,
            kind=LLM_API_KEY,
            value="sk-test-key",
            provider=provider,
            project_id=w.project_id if project else None,
            org_id=w.org_id,
        )
    w.state.contexts.invalidate()


def _flushed(state: Any) -> list[UsageEvent]:
    assert state.usage_writer.flush()
    return _ledger()


def _assert_local_member(rows: list[UsageEvent], w: SimpleNamespace) -> None:
    for row in rows:
        assert (row.org_id, row.project_id) == (w.org_id, w.project_id)
        assert (row.project_slug, row.actor_kind) == ("demo", "member")
        assert (row.user_id, row.actor_label) == (w.user_id, "Tess")
        assert row.connection_id is None and row.client_name is None


def test_an_explorer_evidence_read_records_its_backfill(
    local: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-the-environment")
    local.state.contexts.invalidate()
    response = local.client.get(
        "/api/projects/demo/node/evidence", params={"qualified_name": "sample.fn"}
    )
    assert response.status_code == 200, response.text
    rows = _flushed(local.state)
    assert sorted(r.subject for r in rows) == sorted(local.shas)
    _assert_local_member(rows, local)
    assert {(r.source, r.task) for r in rows} == {("explorer", "analyze")}
    assert {r.key_scope for r in rows} == {"environment"}
    assert {r.provider for r in rows} == {"anthropic"}
    assert all(r.cost_source == "estimated" and r.cost_usd for r in rows)
    assert local.state.spend.spent(local.org_id, user_id=local.user_id) == sum(
        r.cost_usd for r in rows
    )


def test_an_explorer_generate_records_the_card_and_the_backfill(
    local: SimpleNamespace,
) -> None:
    _key(local, "anthropic", project=False)
    response = local.client.post(
        "/api/projects/demo/node/rationale", params={"qualified_name": "sample.fn"}
    )
    assert response.status_code == 200, response.text
    rows = _flushed(local.state)
    _assert_local_member(rows, local)
    assert {r.source for r in rows} == {"explorer"}
    assert sorted((r.task, r.subject) for r in rows) == sorted(
        [("analyze", sha) for sha in local.shas] + [("rationale", "sample.fn")]
    )
    assert {r.key_scope for r in rows} == {"org"}
    assert {r.model_served for r in rows} == {"claude-opus-4-7-served"}


class _TwoRounds:
    """A chat client: a tool-only round, then an answer round."""

    provider = "openai"
    model = "gpt-4o"

    def __init__(self) -> None:
        self.calls = 0

    def stream_turn(self, request):  # noqa: ANN001, ANN201
        self.calls += 1
        if self.calls == 1:
            yield ToolCallMade(
                call=ToolCall(id="u1", name="no_such_tool", arguments={})
            )
            yield TurnDone("tool_calls", 40, 4, model="gpt-4o-2026-01-01")
        else:
            yield TextDelta(text="Done.")
            yield TurnDone("stop", 60, 6, model="gpt-4o-2026-01-01")


def test_a_chat_turn_records_one_row_per_round(
    local: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _key(local, "openai", project=True)
    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: _TwoRounds())
    created = local.client.post(
        "/api/projects/demo/chat/sessions",
        json={"provider": "openai", "model": "gpt-4o"},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    response = local.client.post(
        f"/api/projects/demo/chat/sessions/{session_id}/messages",
        json={"content": "why?"},
    )
    assert response.status_code == 200, response.text
    rows = _flushed(local.state)
    _assert_local_member(rows, local)
    assert [(r.input_tokens, r.output_tokens) for r in rows] == [(40, 4), (60, 6)]
    assert {(r.source, r.task, r.chat_session_id) for r in rows} == {
        ("chat", "chat", session_id)
    }
    assert {(r.provider, r.model_requested, r.model_served) for r in rows} == {
        ("openai", "gpt-4o", "gpt-4o-2026-01-01")
    }
    assert {r.key_scope for r in rows} == {"project"}
    with use_project(manual_ctx(local.root)), project_session() as session:
        assert session.get(ChatSessionRow, session_id) is not None


def test_a_local_mcp_call_records_with_source_mcp(local: SimpleNamespace) -> None:
    response = local.client.post(
        "/mcp/demo",
        json=_rpc(
            "tools/call",
            {
                "name": "whygraph_evidence_for",
                "arguments": {"path": "sample.py", "line_start": 1, "line_end": 3},
            },
        ),
        headers=MCP_HEADERS,
    )
    assert response.status_code == 200, response.text
    rows = _flushed(local.state)
    assert sorted(r.subject for r in rows) == sorted(local.shas)
    _assert_local_member(rows, local)
    assert {(r.source, r.task, r.key_scope) for r in rows} == {
        ("mcp", "analyze", "none")
    }


def test_routes_that_do_not_spend_bind_no_sink(local: SimpleNamespace) -> None:
    """``usage_source`` defaults to none: only the three mounts and MCP bind one."""
    assert local.client.get("/api/projects/demo").status_code == 200
    assert _flushed(local.state) == []


def test_an_api_v1_rationale_call_records_the_connection(
    v1: V1World, stub_llm: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(v1_routes, "_missing_key", lambda *a, **k: None)
    response = call(
        v1,
        "POST",
        "/rationale",
        json={"target": target_body(name="sample.fn"), "hunks": []},
    )
    assert response.status_code == 200, response.text
    rows = _flushed(v1.state)
    with portal_db.get_session() as session:
        token_id = session.exec(select(ConnectionToken.id)).one()
    assert {r.task for r in rows} == {"analyze", "rationale"}
    assert [r.subject for r in rows if r.task == "rationale"] == ["sample.fn"]
    for row in rows:
        assert (row.org_id, row.project_id, row.project_slug) == (
            v1.org_id,
            v1.project_id,
            "api",
        )
        assert (row.source, row.actor_kind) == ("agent", "member")
        assert (row.user_id, row.actor_label) == (v1.ids["ben"], "Ben (@ben)")
        assert (row.connection_id, row.client_name) == (token_id, "laptop")
        assert row.key_scope == "none"
        assert row.chat_session_id is None


def test_the_scan_estimate_uses_the_orgs_price_overrides(
    local: SimpleNamespace,
) -> None:
    before = local.client.get("/api/projects/demo/scan-estimate").json()
    assert before["cost"]["prices_as_of"] != ORG_PRICES_LABEL
    local.state.prices.set_org(
        local.org_id,
        {
            ("anthropic", "claude-opus-4-7"): (
                Price(input=Decimal("1"), output=Decimal("1")),
                "2026-10-01T00:00:00+00:00",
            )
        },
    )
    after = local.client.get("/api/projects/demo/scan-estimate").json()
    assert after["cost"]["prices_as_of"] == ORG_PRICES_LABEL
