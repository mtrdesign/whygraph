"""The agent-call counter (M2f-3 plan sections 4.10, 6.1 #5 / #11, 6.2 #8).

Unit tests for :func:`whygraph.core.usage.count_agent_call`, the MCP
offload wrapper's kinds, :meth:`PortalUsageSink.count_call` and the
:class:`~whygraph.portal.agent_calls.AgentCallBook`; Postgres tests for the
batched upsert, its retry when a project vanishes, the prune and the
lifespan's flushes; and end to end: local MCP calls (a tool, a resource, a
prompt) and ``/api/v1`` data calls each add one count, while the v1 status
route, ``DELETE .../token``, a ``429`` and Explorer calls add none.
"""

# ruff: noqa: F811 -- pytest fixtures (`env`, `local`, `v1`, ...) are imported

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterator

import anyio
import mcp.types as mcp_types
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import select

from test_portal_app import (  # noqa: F401 -- fixtures
    env,
    make_repo,
    portal_client,
    prod_portal,
    production_env,
    seed_codegraph,
)
from test_portal_mcp import MCP_HEADERS, _rpc, _setup_project, _sse_json
from test_portal_usage import local, stub_llm  # noqa: F401 -- fixtures
from test_portal_v1 import V1World, call, target_body, v1  # noqa: F401
from whygraph.core import usage as core_usage
from whygraph.core.usage import UsageScope, count_agent_call, use_usage_sink
from whygraph.mcp import area_history, evidence, notes, prompts, rationale, resources
from whygraph.mcp import server as mcp_server
from whygraph.mcp.errors import WhyGraphError
from whygraph.portal import agent_calls
from whygraph.portal import app as portal_app
from whygraph.portal import codegraph_host
from whygraph.portal import db as portal_db
from whygraph.portal.agent_calls import AgentCallBook, CallKey, CallTally
from whygraph.portal.codegraph_host import BRIDGED_TOOLS, build_codegraph_server
from whygraph.portal.models import AgentCallDay, ConnectionToken, Organization, Project
from whygraph.portal.throttle import Throttle
from whygraph.portal.usage import Attribution, PortalUsageSink
from whygraph.portal.usage_store import PriceBook, SpendBook, UsageWriter

MCP_KINDS = sorted(
    [
        # tools
        "whygraph_evidence_for",
        "whygraph_rationale_brief",
        "whygraph_area_history",
        "whygraph_propose_note",
        # resources
        "whygraph_commit",
        "whygraph_pull_request",
        "whygraph_issue",
        "whygraph_repo_overview",
        "whygraph_note",
        # prompts
        "whygraph_pre_edit_brief",
        "whygraph_why_was_this_written",
        "whygraph_triage_commit",
    ]
)


class _Clock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


def _add(book: AgentCallBook, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "org_id": 1,
        "project_id": 10,
        "source": "mcp",
        "user_id": 100,
        "connection_id": None,
        "kind": "whygraph_evidence_for",
    }
    values.update(overrides)
    book.add(**values)


# ---------------------------------------------------------------------------
# count_agent_call and the offload wrapper (section 6.1 #11)
# ---------------------------------------------------------------------------


class _CountingSink:
    def __init__(self) -> None:
        self.scope = UsageScope(source="mcp")
        self.kinds: list[str] = []
        self.extras: list[dict[str, Any]] = []

    def record(self, rec: Any) -> None:  # pragma: no cover - not used
        pass

    def blocked_scope(self) -> str | None:
        return None

    def count_call(self, kind: str, **kw: Any) -> None:
        self.kinds.append(kind)
        self.extras.append(kw)


class _PlainSink:
    scope = UsageScope(source="mcp")

    def record(self, rec: Any) -> None:  # pragma: no cover - not used
        pass

    def blocked_scope(self) -> str | None:
        return None


class _BrokenSink(_PlainSink):
    def count_call(self, kind: str, **kw: Any) -> None:
        raise RuntimeError("boom")


def test_count_agent_call_is_a_no_op_unbound_or_without_count_call() -> None:
    count_agent_call("whygraph_evidence_for")  # unbound: nothing happens
    with use_usage_sink(_PlainSink()):  # type: ignore[arg-type]
        count_agent_call("whygraph_evidence_for")
    counting = _CountingSink()
    with use_usage_sink(counting):  # type: ignore[arg-type]
        count_agent_call("whygraph_issue")
    assert counting.kinds == ["whygraph_issue"]


def test_count_agent_call_passes_outcome_and_duration_to_the_sink() -> None:
    sink = _CountingSink()
    with use_usage_sink(sink):  # type: ignore[arg-type]
        count_agent_call("whygraph_issue")
        count_agent_call("whygraph_issue", outcome="error", duration_ms=12)
    assert sink.extras == [
        {"outcome": "ok", "duration_ms": None},
        {"outcome": "error", "duration_ms": 12},
    ]


def test_count_agent_call_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[str] = []
    monkeypatch.setattr(
        core_usage._log, "exception", lambda msg, *args: logged.append(msg % args)
    )
    with use_usage_sink(_BrokenSink()):  # type: ignore[arg-type]
        count_agent_call("whygraph_issue")
    assert logged == ["could not count an agent call of kind whygraph_issue"]


def test_the_mcp_kinds_are_the_twelve_registered_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str]] = []

    def capture(fn: Any, *, kind: str) -> Any:
        seen.append((kind, fn.__name__))
        return fn

    monkeypatch.setattr(mcp_server, "offload", capture)
    registrar = mcp_server.ThreadOffload(FastMCP("kinds"))
    for module in (evidence, rationale, area_history, notes, resources, prompts):
        module.register(registrar)
    assert sorted(kind for kind, _ in seen) == MCP_KINDS
    # Resources and prompts register private functions: never their names.
    assert not any(kind.startswith("_") for kind, _ in seen)
    assert any(name.startswith("_") for _, name in seen)


def test_codegraph_kinds_are_code_defined() -> None:
    """The bridge counts only its own constants, never a requested name."""
    for kind in BRIDGED_TOOLS:
        assert re.fullmatch(r"codegraph_[a-z]+", kind) and len(kind) <= 64
    sink = _CountingSink()
    handler = build_codegraph_server(SimpleNamespace(codegraph_host=None))  # type: ignore[arg-type]
    call = handler.request_handlers[mcp_types.CallToolRequest]

    async def _run() -> None:
        with use_usage_sink(sink):  # type: ignore[arg-type]
            for name in ("codegraph_search", "codegraph_callers", "x" * 100):
                request = mcp_types.CallToolRequest(
                    params=mcp_types.CallToolRequestParams(name=name, arguments={})
                )
                try:
                    await call(request)
                except McpError as exc:
                    assert exc.error.code == mcp_types.INVALID_PARAMS

    asyncio.run(_run())
    assert sink.kinds == ["codegraph_search"]
    assert sink.kinds[0] is BRIDGED_TOOLS[BRIDGED_TOOLS.index("codegraph_search")]


def test_offload_counts_after_the_body_with_its_outcome() -> None:
    order: list[str] = []
    sink = _CountingSink()

    def body() -> str:
        order.append(f"body before {sink.kinds}")
        return "done"

    async def async_body() -> str:
        return "async done"

    wrapped = mcp_server.offload(body, kind="whygraph_commit")
    wrapped_async = mcp_server.offload(async_body, kind="whygraph_issue")

    async def run() -> tuple[str, str]:
        with use_usage_sink(sink):  # type: ignore[arg-type]
            return await wrapped(), await wrapped_async()

    assert asyncio.run(run()) == ("done", "async done")
    assert order == ["body before []"]
    assert sink.kinds == ["whygraph_commit", "whygraph_issue"]
    assert [kw["outcome"] for kw in sink.extras] == ["ok", "ok"]
    assert all(isinstance(kw["duration_ms"], int) for kw in sink.extras)


def _raises(exc: BaseException) -> Any:
    def body() -> str:
        raise exc

    return body


def _araises(exc: BaseException) -> Any:
    async def body() -> str:
        raise exc

    return body


@pytest.mark.parametrize("make", [_raises, _araises])
@pytest.mark.parametrize(
    ("exc", "outcome"),
    [
        (WhyGraphError("nope", code="not_found"), "error"),
        (ToolError("nope"), "error"),
        (RuntimeError("boom"), "internal"),
    ],
)
def test_offload_counts_the_failure_kind_and_reraises(
    make: Any, exc: Exception, outcome: str
) -> None:
    sink = _CountingSink()
    wrapped = mcp_server.offload(make(exc), kind="whygraph_commit")

    async def run() -> None:
        with use_usage_sink(sink):  # type: ignore[arg-type]
            await wrapped()

    with pytest.raises(type(exc)) as raised:
        asyncio.run(run())
    assert raised.value is exc
    assert sink.kinds == ["whygraph_commit"]
    assert sink.extras[0]["outcome"] == outcome
    assert isinstance(sink.extras[0]["duration_ms"], int)


@pytest.mark.parametrize("is_async", [False, True])
def test_offload_counts_a_returned_error_payload_as_an_error(is_async: bool) -> None:
    sink = _CountingSink()

    def body() -> dict:
        return {"error": "no such commit"}

    async def abody() -> dict:
        return {"error": "no such commit"}

    wrapped = mcp_server.offload(abody if is_async else body, kind="whygraph_commit")

    async def run() -> dict:
        with use_usage_sink(sink):  # type: ignore[arg-type]
            return await wrapped()

    assert asyncio.run(run()) == {"error": "no such commit"}
    assert sink.extras[0]["outcome"] == "error"


@pytest.mark.parametrize("is_async", [False, True])
def test_offload_counts_a_cancellation_as_ok_without_a_duration(
    is_async: bool,
) -> None:
    sink = _CountingSink()

    def body() -> str:
        time.sleep(0.2)
        return "late"

    async def abody() -> str:
        await asyncio.sleep(5)
        return "late"

    wrapped = mcp_server.offload(abody if is_async else body, kind="whygraph_commit")

    async def run() -> None:
        with use_usage_sink(sink):  # type: ignore[arg-type]
            task = asyncio.ensure_future(wrapped())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert sink.kinds == ["whygraph_commit"]
    assert sink.extras == [{"outcome": "ok", "duration_ms": None}]


def test_the_bridge_outcome_per_fault_constant() -> None:
    def result(text: str, *, error: bool = True) -> mcp_types.CallToolResult:
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=text)], isError=error
        )

    own_faults = [
        codegraph_host.RESTARTED,
        codegraph_host.TIMED_OUT.format(seconds="30"),
        codegraph_host.PAUSED.format(seconds="12"),
        codegraph_host.REFRESH_FAILED,
        codegraph_host.NO_BINARY,
        codegraph_host.NOT_RUNNING,
    ]
    for text in own_faults:
        assert codegraph_host.bridge_outcome(result(text)) == "internal", text
    errors = [
        codegraph_host.BUSY,
        codegraph_host.TURNED_OFF,
        codegraph_host.NOT_INDEXED,
        codegraph_host.STILL_UPDATING.format(changed=3, so_far=2, eta=9),
        "symbol not found",
    ]
    for text in errors:
        assert codegraph_host.bridge_outcome(result(text)) == "error", text
    assert codegraph_host.bridge_outcome(result("fine", error=False)) == "ok"
    # Even a fault text is "ok" on a result that is not an error.
    assert codegraph_host.bridge_outcome(result(own_faults[0], error=False)) == "ok"
    empty = mcp_types.CallToolResult(content=[], isError=True)
    assert codegraph_host.bridge_outcome(empty) == "error"


def test_the_bridge_counts_after_the_call_with_its_outcome() -> None:
    sink = _CountingSink()
    handler = build_codegraph_server(SimpleNamespace(codegraph_host=None))  # type: ignore[arg-type]
    call = handler.request_handlers[mcp_types.CallToolRequest]

    async def _run() -> None:
        with use_usage_sink(sink):  # type: ignore[arg-type]
            await call(
                mcp_types.CallToolRequest(
                    params=mcp_types.CallToolRequestParams(
                        name="codegraph_search", arguments={}
                    )
                )
            )

    asyncio.run(_run())
    # No bound target: the "not running" fault, an internal error.
    assert sink.kinds == ["codegraph_search"]
    assert sink.extras[0]["outcome"] == "internal"
    assert isinstance(sink.extras[0]["duration_ms"], int)


# ---------------------------------------------------------------------------
# PortalUsageSink.count_call
# ---------------------------------------------------------------------------


def _sink(source: str, book: AgentCallBook, **attribution: Any) -> PortalUsageSink:
    values: dict[str, Any] = {
        "org_id": 1,
        "org_slug": "acme",
        "project_id": 10,
        "project_slug": "demo",
        "project_name": "Demo",
        "actor_kind": "member",
        "user_id": 100,
        "actor_label": "Tess",
        "config": None,
        "connection_id": None,
    }
    values.update(attribution)
    return PortalUsageSink(
        UsageScope(source=source),
        Attribution(**values),
        UsageWriter(),
        SpendBook(),
        PriceBook(),
        None,
        book,
    )


def test_the_portal_sink_counts_agent_and_chat_sources() -> None:
    book = AgentCallBook(
        clock=_Clock(datetime(2026, 10, 9, 23, 0, tzinfo=timezone.utc))
    )
    _sink("mcp", book).count_call("whygraph_evidence_for")
    _sink("agent", book, connection_id=7).count_call("v1:evidence")
    _sink("chat", book, connection_id=7).count_call("find_changes")
    for source in ("explorer", "scan"):
        _sink(source, book).count_call("whygraph_evidence_for")
    assert sorted(book.pending(10)) == [
        (CallKey(1, 10, "2026-10-09", "agent", 100, 7, "v1:evidence"), CallTally(1)),
        (
            CallKey(1, 10, "2026-10-09", "chat", None, None, "find_changes"),
            CallTally(1),
        ),
        (
            CallKey(1, 10, "2026-10-09", "mcp", 100, None, "whygraph_evidence_for"),
            CallTally(1),
        ),
    ]


# ---------------------------------------------------------------------------
# AgentCallBook (section 6.1 #5)
# ---------------------------------------------------------------------------


def test_the_book_counts_per_key_and_rolls_over_at_utc_midnight() -> None:
    clock = _Clock(datetime(2026, 10, 9, 23, 59, tzinfo=timezone.utc))
    book = AgentCallBook(clock=clock)
    _add(book)
    _add(book)
    _add(book, kind="whygraph_issue")
    _add(book, user_id=None)
    _add(book, project_id=11)
    clock.moment += timedelta(minutes=2)
    _add(book)
    assert len(book) == 5
    assert dict(book.pending(10)) == {
        CallKey(1, 10, "2026-10-09", "mcp", 100, None, "whygraph_evidence_for"): (
            CallTally(2, 0, 0, 0)
        ),
        CallKey(1, 10, "2026-10-09", "mcp", 100, None, "whygraph_issue"): (
            CallTally(1, 0, 0, 0)
        ),
        CallKey(1, 10, "2026-10-09", "mcp", None, None, "whygraph_evidence_for"): (
            CallTally(1, 0, 0, 0)
        ),
        CallKey(1, 10, "2026-10-10", "mcp", 100, None, "whygraph_evidence_for"): (
            CallTally(1, 0, 0, 0)
        ),
    }
    # A local-time clock still files the call under its UTC day.
    clock.moment = datetime(2026, 10, 11, 1, 0, tzinfo=timezone(timedelta(hours=3)))
    _add(book, project_id=12)
    assert [key.day for key, _ in book.pending(12)] == ["2026-10-10"]


def test_the_book_tallies_outcomes_and_durations_and_restores_elementwise() -> None:
    book = AgentCallBook()
    _add(book, outcome="ok", duration_ms=10)
    _add(book, outcome="error", duration_ms=5)
    _add(book, outcome="internal")
    _add(book, outcome="ok", duration_ms=None)
    [(key, tally)] = book.pending(10)
    assert tally == CallTally(4, 1, 1, 15)
    assert tally + CallTally(1, 0, 0, 1) == CallTally(5, 1, 1, 16)
    batch = book._take()
    _add(book, outcome="error", duration_ms=2)
    book._restore(batch)
    assert dict(book.pending(10)) == {key: CallTally(5, 2, 1, 17)}


def test_has_any_names_only_its_own_org() -> None:
    book = AgentCallBook()
    assert not book.has_any(1)
    _add(book, org_id=2, project_id=20)
    assert book.has_any(2) and not book.has_any(1)


def test_has_any_can_be_limited_to_sources() -> None:
    book = AgentCallBook()
    _add(book, source="chat", user_id=None)
    assert book.has_any(1) and not book.has_any(1, sources=("mcp", "agent"))
    _add(book, source="agent")
    assert book.has_any(1, sources=("mcp", "agent"))


def test_due_after_the_interval_or_above_the_key_cap() -> None:
    book = AgentCallBook(flush_every=3600, max_keys=3)
    assert not book.due()  # empty
    for n in range(3):
        _add(book, kind=f"k{n}")
    assert not book.due()  # three keys, not above the cap, interval not passed
    _add(book, kind="k3")
    assert book.due()  # above the cap
    timed = AgentCallBook(flush_every=0)
    assert not timed.due()
    _add(timed)
    assert timed.due()


def test_the_watcher_flushes_early_above_the_key_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_calls, "CHECK_EVERY_SEC", 0.01)
    flushed: list[int] = []

    class _Book(AgentCallBook):
        def flush(self) -> int:
            flushed.append(len(self._take()))
            return flushed[-1]

    book = _Book(flush_every=3600, max_keys=2)
    for n in range(3):
        _add(book, kind=f"k{n}")

    async def run() -> None:
        with anyio.move_on_after(0.5):
            await portal_app._flush_agent_calls(book)

    anyio.run(run)
    assert flushed == [3]
    assert len(book) == 0


# ---------------------------------------------------------------------------
# Postgres: the upsert, the vanished-project retry, the prune
# ---------------------------------------------------------------------------


def _org_and_projects(names: tuple[str, ...] = ("demo", "other")) -> SimpleNamespace:
    with portal_db.get_session() as session:
        org = Organization(slug="acme", name="Acme")
        session.add(org)
        session.flush()
        ids = {}
        for name in names:
            project = Project(
                org_id=org.id,
                slug=name,
                name=name.title(),
                source="local",
                root=f"/{name}",
            )
            session.add(project)
            session.flush()
            ids[name] = project.id
        return SimpleNamespace(org_id=org.id, **ids)


def _rows() -> list[tuple]:
    with portal_db.get_session() as session:
        return [
            (r.project_id, r.day, r.source, r.user_id, r.connection_id, r.kind, r.calls)
            for r in session.exec(
                select(AgentCallDay).order_by(
                    AgentCallDay.project_id, AgentCallDay.kind
                )
            ).all()
        ]


@pytest.fixture
def pg(portal_database: str) -> Iterator[SimpleNamespace]:
    yield _org_and_projects()


def test_flush_upserts_and_adds_to_an_existing_row(pg: SimpleNamespace) -> None:
    clock = _Clock(datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc))
    book = AgentCallBook(clock=clock)
    for _ in range(2):
        _add(book, org_id=pg.org_id, project_id=pg.demo, user_id=None)
    _add(book, org_id=pg.org_id, project_id=pg.demo, source="agent", connection_id=9)
    assert book.flush() == 2
    assert len(book) == 0 and book.flush() == 0
    # The same keys again: the counts add up (NULLs are not distinct).
    _add(book, org_id=pg.org_id, project_id=pg.demo, user_id=None)
    _add(
        book, org_id=pg.org_id, project_id=pg.demo, user_id=None, kind="whygraph_issue"
    )
    assert book.flush() == 2
    assert sorted(_rows(), key=repr) == sorted(
        [
            (pg.demo, "2026-10-09", "mcp", None, None, "whygraph_evidence_for", 3),
            (pg.demo, "2026-10-09", "agent", 100, 9, "whygraph_evidence_for", 1),
            (pg.demo, "2026-10-09", "mcp", None, None, "whygraph_issue", 1),
        ],
        key=repr,
    )


def test_flush_skips_a_project_already_gone(pg: SimpleNamespace) -> None:
    book = AgentCallBook()
    _add(book, org_id=pg.org_id, project_id=pg.demo)
    _add(book, org_id=pg.org_id, project_id=999_999)  # never existed
    _add(book, org_id=pg.org_id + 1, project_id=pg.other)  # not that org's
    assert book.flush() == 3
    assert [row[0] for row in _rows()] == [pg.demo]


def test_a_project_deleted_mid_flush_is_retried_without_it(
    pg: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The EXISTS snapshot saw it, the FK check did not: the batch is retried."""
    real = agent_calls._upsert
    calls: list[int] = []

    def racing(batch: dict) -> None:
        calls.append(len(batch))
        if len(calls) == 1:
            with portal_db.get_session() as session:
                session.delete(session.get(Project, pg.demo))
            raise IntegrityError(
                "INSERT ...", {}, Exception("fk_agent_call_days_project")
            )
        real(batch)

    monkeypatch.setattr(agent_calls, "_upsert", racing)
    book = AgentCallBook()
    _add(book, org_id=pg.org_id, project_id=pg.demo)
    _add(book, org_id=pg.org_id, project_id=pg.other)
    _add(book, org_id=pg.org_id, project_id=pg.other, kind="whygraph_issue")
    assert book.flush() == 2
    assert calls == [3, 2]
    assert {row[0] for row in _rows()} == {pg.other}
    assert sum(row[-1] for row in _rows()) == 2


def test_a_retry_that_fails_again_drops_the_batch(
    pg: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def always(batch: dict) -> None:
        raise IntegrityError("INSERT ...", {}, Exception("still"))

    monkeypatch.setattr(agent_calls, "_upsert", always)
    book = AgentCallBook()
    _add(book, org_id=pg.org_id, project_id=pg.demo)
    assert book.flush() == 0
    assert len(book) == 0 and _rows() == []


def test_a_failed_flush_keeps_the_counts(
    pg: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(batch: dict) -> None:
        raise OperationalError("INSERT ...", {}, Exception("server closed"))

    book = AgentCallBook(flush_every=0)
    _add(book, org_id=pg.org_id, project_id=pg.demo)
    with monkeypatch.context() as patch:
        patch.setattr(agent_calls, "_upsert", down)
        assert book.flush() == 0
    _add(book, org_id=pg.org_id, project_id=pg.demo)
    assert len(book) == 1
    assert book.flush() == 1
    assert [row[-1] for row in _rows()] == [2]


def test_the_upsert_sums_the_outcome_counters(pg: SimpleNamespace) -> None:
    book = AgentCallBook()
    for outcome, ms in (("ok", 10), ("error", 5), ("internal", None)):
        _add(
            book, org_id=pg.org_id, project_id=pg.demo, outcome=outcome, duration_ms=ms
        )
    assert book.flush() == 1
    _add(book, org_id=pg.org_id, project_id=pg.demo, outcome="error", duration_ms=7)
    assert book.flush() == 1
    with portal_db.get_session() as session:
        [row] = session.exec(select(AgentCallDay)).all()
        got = (row.calls, row.errors, row.internal_errors, row.duration_ms)
    assert got == (4, 2, 1, 22)


def test_prune_deletes_rows_past_the_retention(pg: SimpleNamespace) -> None:
    now = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    for day in ("2025-09-01", "2025-09-04", "2026-10-09"):
        book = AgentCallBook(
            clock=_Clock(datetime.fromisoformat(day + "T12:00:00+00:00"))
        )
        _add(book, org_id=pg.org_id, project_id=pg.demo)
        book.flush()
    # 400 days before 2026-10-09 is 2025-09-04.
    assert agent_calls.prune(now=now) == 1
    assert [row[1] for row in _rows()] == ["2025-09-04", "2026-10-09"]


def test_rows_go_with_their_project(pg: SimpleNamespace) -> None:
    book = AgentCallBook()
    _add(book, org_id=pg.org_id, project_id=pg.demo)
    _add(book, org_id=pg.org_id, project_id=pg.other)
    book.flush()
    with portal_db.get_session() as session:
        session.delete(session.get(Project, pg.demo))
    assert {row[0] for row in _rows()} == {pg.other}


# ---------------------------------------------------------------------------
# End to end (section 6.2 #8)
# ---------------------------------------------------------------------------


def _mcp(client: Any, method: str, params: dict | None = None) -> dict:
    response = client.post("/mcp/demo", json=_rpc(method, params), headers=MCP_HEADERS)
    assert response.status_code == 200, response.text
    return _sse_json(response.text)


def _all_rows() -> list[SimpleNamespace]:
    with portal_db.get_session() as session:
        return [
            SimpleNamespace(**row.model_dump())
            for row in session.exec(select(AgentCallDay)).all()
        ]


def _counts(state: Any) -> dict[tuple[str, str], int]:
    """``(source, kind) -> calls`` of everything counted, flushed or not."""
    state.agent_calls.flush()
    rows = _all_rows()
    totals: dict[tuple[str, str], int] = {}
    for row in rows:
        totals[(row.source, row.kind)] = (
            totals.get((row.source, row.kind), 0) + row.calls
        )
    return totals


def test_local_mcp_calls_count_under_their_registered_names(
    local: SimpleNamespace,
) -> None:
    client = local.client
    _mcp(client, "tools/list")
    _mcp(client, "resources/templates/list")
    _mcp(client, "prompts/list")
    assert _counts(local.state) == {}  # protocol chatter is not agent activity
    args = {"path": "sample.py", "line_start": 1, "line_end": 3}
    for _ in range(2):  # the second answer is the same: still counted
        _mcp(
            client,
            "tools/call",
            {"name": "whygraph_evidence_for", "arguments": args},
        )
    _mcp(client, "resources/read", {"uri": "whygraph://repo/overview"})
    _mcp(client, "resources/read", {"uri": f"whygraph://commit/{local.shas[0]}"})
    _mcp(
        client,
        "prompts/get",
        {"name": "whygraph_triage_commit", "arguments": {"sha": local.shas[0]}},
    )
    assert _counts(local.state) == {
        ("mcp", "whygraph_evidence_for"): 2,
        ("mcp", "whygraph_repo_overview"): 1,
        ("mcp", "whygraph_commit"): 1,
        ("mcp", "whygraph_triage_commit"): 1,
    }
    rows = _all_rows()
    assert {(r.org_id, r.project_id, r.user_id, r.connection_id) for r in rows} == {
        (local.org_id, local.project_id, local.user_id, None)
    }
    assert {r.day for r in rows} == {datetime.now(timezone.utc).date().isoformat()}


def test_explorer_and_chat_calls_are_not_counted(local: SimpleNamespace) -> None:
    client = local.client
    assert (
        client.get(
            "/api/projects/demo/node/evidence", params={"qualified_name": "sample.fn"}
        ).status_code
        == 200
    )
    assert client.get("/api/projects/demo/commit/" + local.shas[0]).status_code == 200
    assert client.get("/api/projects/demo/chat/sessions").status_code == 200
    assert len(local.state.agent_calls) == 0
    assert _counts(local.state) == {}


def test_api_v1_data_calls_count_with_the_connection(v1: V1World) -> None:
    assert call(v1, "GET", "/overview").status_code == 200
    assert call(v1, "GET", "/history", params={"path": "sample.py"}).status_code == 200
    assert call(v1, "GET", "/prs/1").status_code == 404  # a data 404 still counts
    response = call(
        v1, "POST", "/evidence", json={"target": target_body(), "hunks": []}
    )
    assert response.status_code == 200, response.text
    assert _counts(v1.state) == {
        ("agent", "v1:overview"): 1,
        ("agent", "v1:history"): 1,
        ("agent", "v1:pr"): 1,
        ("agent", "v1:evidence"): 1,
    }
    with portal_db.get_session() as session:
        token_id = session.exec(select(ConnectionToken.id)).one()
    rows = _all_rows()
    assert {(r.org_id, r.project_id, r.user_id, r.connection_id) for r in rows} == {
        (v1.org_id, v1.project_id, v1.ids["ben"], token_id)
    }


def test_the_status_route_a_429_and_the_token_delete_are_not_counted(
    v1: V1World,
) -> None:
    assert call(v1, "GET").status_code == 200  # the link-status probe
    v1.state.v1_token = Throttle(0, 60)
    refused = call(v1, "GET", "/overview")
    assert refused.status_code == 429, refused.text
    assert call(v1, "DELETE", "/token").status_code == 204
    assert len(v1.state.agent_calls) == 0
    assert _counts(v1.state) == {}


def test_the_lifespan_flushes_the_book_on_shutdown(env: SimpleNamespace) -> None:
    root = make_repo(env.shared, "demo")
    seed_codegraph(root)
    with portal_client() as client:
        _setup_project(client, root)
        state = client.app.state.portal
        state.agent_calls.flush_every = 3600  # the watcher must not get there first
        _mcp(client, "resources/read", {"uri": "whygraph://repo/overview"})
        assert len(state.agent_calls) == 1
    assert len(state.agent_calls) == 0
    rows = _all_rows()
    assert [(r.source, r.kind, r.calls) for r in rows] == [
        ("mcp", "whygraph_repo_overview", 1)
    ]
