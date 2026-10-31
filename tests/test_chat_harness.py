"""Tests for the agentic loop and the context window (plan §5.2-5.3, §14).

The loop is driven by a **scripted fake** :class:`ChatClient` that yields
canned events per round, so termination, event ordering, and error recovery
are asserted directly with no provider and no database in the picture —
which is exactly what ``run_turn`` being persistence-free buys.

``build_window`` gets its own tests because its subtle invariant is easy to
break and expensive to debug through integration: both provider APIs reject
a tool result whose matching assistant tool-call has been trimmed away, so
"never orphan a tool message" is asserted structurally.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from whygraph import core
from whygraph.chat import harness
from whygraph.chat.harness import (
    CONTINUE_PROMPT,
    ELIDED_MARKER,
    EMPTY_REPLY_PROMPT,
    BudgetStop,
    ContextTrimmed,
    Continuation,
    EmptyReply,
    RoundLimit,
    RoundUsage,
    ToolCallStarted,
    ToolResultReady,
    TurnCancelled,
    build_system_prompt,
    _specs_tokens,
    _trim_turn,
    begin_turn_messages,
    build_window,
    run_turn,
)
from whygraph.chat.grounding import with_references
from whygraph.chat.modes import MODES, mode_block
from whygraph.chat.tools import ToolRegistry
from whygraph.core.config import MODE_IDS, ChatConfig, Config
from whygraph.core.usage import UsageScope, use_usage_sink
from whygraph.services.llm.chat import (
    ChatClient,
    ChatMessage,
    ModelInfo,
    TextDelta,
    ToolCall,
    ToolCallMade,
    ToolSpec,
    TurnDone,
)
from whygraph.services.llm.exceptions import LlmError

# ---------------------------------------------------------------------------
# Scripted client + stub registry
# ---------------------------------------------------------------------------


class ScriptedClient(ChatClient):
    """A :class:`ChatClient` that replays one canned event list per round.

    Records the requests it received on :attr:`requests`, so tests can
    assert what the loop actually sent back to the model — the tool
    round-trip encoding is as much a part of the contract as the events.
    """

    provider = "scripted"

    def __init__(
        self, rounds: list[list], *, raise_on_round: int | None = None
    ) -> None:
        super().__init__(model="scripted-1")
        self._rounds = rounds
        self._raise_on_round = raise_on_round
        self.requests: list = []

    def stream_turn(self, request):
        index = len(self.requests)
        self.requests.append(request)
        if self._raise_on_round == index:
            raise LlmError("provider exploded")
        if index >= len(self._rounds):
            raise AssertionError(f"loop asked for round {index}, script has none")
        yield from self._rounds[index]

    def list_models(self):
        """Part of the port, unused by the harness (the picker calls it)."""
        return (ModelInfo(id="scripted-1", display_name="Scripted"),)


class StubRegistry:
    """A :class:`ToolRegistry` stand-in recording dispatch order.

    Substituted for the real registry so the loop tests never touch
    CodeGraph, the DB, or the filesystem — those paths have their own suite
    in ``test_chat_tools.py``.
    """

    def __init__(self, results: dict[str, str] | None = None) -> None:
        self._results = results or {}
        self.dispatched: list[tuple[str, dict]] = []
        self.overview_calls = 0

    @property
    def specs(self):
        return ()

    def dispatch(self, name: str, arguments: dict) -> str:
        if name == "get_repo_overview":
            # The system prompt fetches this once per turn; keeping it out of
            # `dispatched` lets tests assert on the *model's* calls alone.
            self.overview_calls += 1
            return json.dumps({"commits": 3})
        self.dispatched.append((name, arguments))
        return self._results.get(name, json.dumps({"ok": name}))

    def dispatch_outcome(self, name: str, arguments: dict) -> tuple[str, str]:
        result = self.dispatch(name, arguments)
        return result, "error" if harness.is_error_result(result) else "ok"


class _SpecRegistry(StubRegistry):
    """A stub registry that offers one tool, for asserting what a request carries."""

    @property
    def specs(self):
        return (ToolSpec(name="search_symbols", description="d", parameters={}),)


def _call(name: str, call_id: str = "c1", **arguments) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _run(client, registry, **kwargs) -> list:
    return list(
        run_turn(
            client=client,
            history=(ChatMessage(role="user", content="why?"),),
            registry=registry,
            max_tool_rounds=kwargs.pop("max_tool_rounds", 8),
            context_token_budget=kwargs.pop("context_token_budget", 60_000),
            **kwargs,
        )
    )


@pytest.fixture(autouse=True)
def _repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A ``.git``-marked cwd so the system prompt can name the repo."""
    root = tmp_path / "myrepo"
    (root / ".git").mkdir(parents=True)
    monkeypatch.chdir(root)
    monkeypatch.setattr(core, "_config", Config(whygraph_db=root / "wg.db"))
    try:
        yield root
    finally:
        core._reset_config()


# ---------------------------------------------------------------------------
# Loop termination and ordering
# ---------------------------------------------------------------------------


def test_turn_with_no_tool_calls_ends_after_one_round() -> None:
    client = ScriptedClient(
        [
            [
                TextDelta(text="Because "),
                TextDelta(text="history."),
                TurnDone("stop", 5, 7),
            ]
        ]
    )
    events = _run(client, StubRegistry())

    assert events == [
        TextDelta(text="Because "),
        TextDelta(text="history."),
        RoundUsage(input_tokens=5, output_tokens=7, finish_reason="stop"),
        TurnDone(finish_reason="stop", input_tokens=5, output_tokens=7),
    ]
    assert len(client.requests) == 1


def test_tool_round_then_text_round() -> None:
    """The canonical shape: call a tool, read the result, then answer."""
    call = _call("search_symbols", query="run_turn")
    client = ScriptedClient(
        [
            [
                TextDelta(text="Looking… "),
                ToolCallMade(call=call),
                TurnDone("tool_calls"),
            ],
            [TextDelta(text="Found it."), TurnDone("stop", 9, 3)],
        ]
    )
    registry = StubRegistry()
    events = _run(client, registry)

    assert events[0] == TextDelta(text="Looking… ")
    # The round's usage closes its provider call, before its tools run.
    assert events[1] == RoundUsage(finish_reason="tool_calls")
    assert events[2] == ToolCallStarted(call=call)
    assert isinstance(events[3], ToolResultReady)
    assert events[3].call is call
    assert events[4] == TextDelta(text="Found it.")
    assert events[5] == RoundUsage(
        input_tokens=9, output_tokens=3, finish_reason="stop"
    )
    assert events[6] == TurnDone(finish_reason="stop", input_tokens=9, output_tokens=3)
    assert len(events) == 7
    assert registry.dispatched == [("search_symbols", {"query": "run_turn"})]


def test_tool_results_are_fed_back_to_the_model() -> None:
    """Round 2's request carries the assistant tool_calls + tool results."""
    call = _call("get_symbol", qualified_name="pkg.a")
    client = ScriptedClient(
        [
            [ToolCallMade(call=call), TurnDone("tool_calls")],
            [TextDelta(text="done"), TurnDone("stop")],
        ]
    )
    _run(client, StubRegistry())

    second = client.requests[1].messages
    assert [m.role for m in second[:2]] == ["system", "system"]
    assert second[2].role == "user"
    assistant = second[3]
    assert assistant.role == "assistant"
    assert assistant.tool_calls == (call,)
    tool_message = second[4]
    assert tool_message.role == "tool"
    assert tool_message.tool_call_id == "c1"
    assert json.loads(tool_message.content) == {"ok": "get_symbol"}


def test_parallel_calls_dispatch_sequentially_in_model_order() -> None:
    """A round with a serial tool (here ``get_evidence``) runs one call at a time."""
    calls = [
        _call("search_symbols", "c1", query="a"),
        _call("get_evidence", "c2", qualified_name="b"),
        _call("read_file", "c3", path="x.py"),
    ]
    client = ScriptedClient(
        [
            [*(ToolCallMade(call=c) for c in calls), TurnDone("tool_calls")],
            [TextDelta(text="ok"), TurnDone("stop")],
        ]
    )
    registry = StubRegistry()
    events = _run(client, registry)

    assert [name for name, _ in registry.dispatched] == [
        "search_symbols",
        "get_evidence",
        "read_file",
    ]
    # Each call's Started strictly precedes its own ResultReady.
    pairs = [e for e in events if isinstance(e, (ToolCallStarted, ToolResultReady))]
    assert [type(e).__name__ for e in pairs] == [
        "ToolCallStarted",
        "ToolResultReady",
    ] * 3
    # And one tool message per call landed, in the same order.
    assert [
        m.tool_call_id for m in client.requests[1].messages if m.role == "tool"
    ] == [
        "c1",
        "c2",
        "c3",
    ]


class _CountSink:
    scope = UsageScope(source="chat")

    def __init__(self) -> None:
        self.counted: list[str] = []
        self.extras: list[dict[str, object]] = []

    def record(self, rec) -> None:  # noqa: ANN001
        pass

    def count_call(self, kind: str, **kw: object) -> None:
        self.counted.append(kind)
        self.extras.append(kw)


class _NamedRegistry(StubRegistry):
    @property
    def specs(self):
        return tuple(
            ToolSpec(name=n, description="d", parameters={})
            for n in ("search_symbols", "get_evidence", "read_file")
        )


def _counted(rounds: list, registry=None) -> list[str]:
    sink = _CountSink()
    with use_usage_sink(sink):
        _run(ScriptedClient(rounds), registry or _NamedRegistry())
    return sink.counted


def test_chat_tool_count_serial_round_counts_each_requested_call() -> None:
    calls = [_call("search_symbols", "c1"), _call("get_evidence", "c2")]
    rounds = [
        [*(ToolCallMade(call=c) for c in calls), TurnDone("tool_calls")],
        [TextDelta(text="ok"), TurnDone("stop")],
    ]
    assert _counted(rounds) == ["search_symbols", "get_evidence"]


def test_chat_tool_count_parallel_round_counts_each_call() -> None:
    calls = [_call("search_symbols", "c1"), _call("read_file", "c2")]
    rounds = [
        [*(ToolCallMade(call=c) for c in calls), TurnDone("tool_calls")],
        [TextDelta(text="ok"), TurnDone("stop")],
    ]
    assert _counted(rounds) == ["search_symbols", "read_file"]


class _OutcomeRegistry(_NamedRegistry):
    """A registry whose ``dispatch_outcome`` reports a fixed outcome per tool."""

    OUTCOMES = {
        "search_symbols": "ok",
        "get_evidence": "error",
        "read_file": "internal",
    }

    def dispatch_outcome(self, name: str, arguments: dict):
        return super().dispatch(name, arguments), self.OUTCOMES.get(name, "error")


def _outcomes(calls: list) -> list[tuple]:
    sink = _CountSink()
    rounds = [
        [*(ToolCallMade(call=c) for c in calls), TurnDone("tool_calls")],
        [TextDelta(text="ok"), TurnDone("stop")],
    ]
    with use_usage_sink(sink):
        _run(ScriptedClient(rounds), _OutcomeRegistry())
    return [
        (
            kind,
            kw["outcome"],
            isinstance(kw["duration_ms"], int) and kw["duration_ms"] >= 0,
        )
        for kind, kw in zip(sink.counted, sink.extras)
    ]


def test_tool_count_outcome_and_duration_serial() -> None:
    calls = [_call("search_symbols", "c1"), _call("get_evidence", "c2")]
    assert _outcomes(calls) == [
        ("search_symbols", "ok", True),
        ("get_evidence", "error", True),
    ]


def test_tool_count_outcome_and_duration_parallel() -> None:
    calls = [_call("search_symbols", "c1"), _call("read_file", "c2")]
    assert _outcomes(calls) == [
        ("search_symbols", "ok", True),
        ("read_file", "internal", True),
    ]


def test_tool_count_outcome_unknown_name_is_not_counted() -> None:
    assert _outcomes([_call("made_up_tool", "c1")]) == []


def test_chat_tool_count_skips_unknown_names_and_the_overview() -> None:
    calls = [_call("made_up_tool", "c1"), _call("get_repo_overview", "c2")]
    rounds = [
        [*(ToolCallMade(call=c) for c in calls), TurnDone("tool_calls")],
        [TextDelta(text="ok"), TurnDone("stop")],
    ]
    assert _counted(rounds) == []


def test_chat_tool_count_is_a_no_op_without_a_sink() -> None:
    rounds = [
        [ToolCallMade(call=_call("search_symbols")), TurnDone("tool_calls")],
        [TextDelta(text="ok"), TurnDone("stop")],
    ]
    events = _run(ScriptedClient(rounds), _NamedRegistry())
    assert any(isinstance(e, ToolResultReady) for e in events)


def test_max_tool_rounds_cuts_the_loop_off() -> None:
    """A model that never stops calling tools still terminates.

    The bound governs *tool* rounds; one extra answer round follows it (see
    :class:`RoundLimit`), so the client sees ``rounds + 1`` requests and only
    ``rounds`` dispatches.
    """
    forever = [
        ToolCallMade(call=_call("search_symbols", query="x")),
        TurnDone("tool_calls"),
    ]
    answer = [TextDelta(text="found"), TurnDone("stop")]
    client = ScriptedClient([list(forever) for _ in range(3)] + [answer])
    registry = StubRegistry()
    events = _run(client, registry, max_tool_rounds=3)

    assert len(client.requests) == 4
    assert len(registry.dispatched) == 3
    # The answer round reports its usage like every other provider call.
    assert events[-4] == RoundLimit(rounds=3)
    assert isinstance(events[-2], RoundUsage)
    assert isinstance(events[-1], TurnDone)


def test_round_limit_of_one_still_dispatches_then_stops() -> None:
    client = ScriptedClient(
        [
            [
                ToolCallMade(call=_call("search_symbols", query="x")),
                TurnDone("tool_calls"),
            ],
            [TextDelta(text="here is what I found"), TurnDone("stop")],
        ]
    )
    registry = StubRegistry()
    events = _run(client, registry, max_tool_rounds=1)
    assert len(registry.dispatched) == 1
    assert isinstance(events[-1], TurnDone)


def test_the_answer_round_forbids_tools_and_yields_prose() -> None:
    """A round-limited turn must not end as tool cards with no answer.

    The exhausted turn's last tool results are in the transcript but were
    never sent; the extra call ships them with ``tool_choice="none"`` so the
    model can only write up what it has. The tools stay on the request: one
    that carries tool history must never drop them.
    """
    forever = [
        ToolCallMade(call=_call("search_symbols", query="x")),
        TurnDone("tool_calls"),
    ]
    client = ScriptedClient(
        [
            list(forever),
            list(forever),
            [
                TextDelta(text="Short version: "),
                TextDelta(text="it's the cache."),
                TurnDone("stop"),
            ],
        ]
    )
    events = _run(client, _SpecRegistry(), max_tool_rounds=2)

    kinds = [type(e).__name__ for e in events]
    assert kinds.count("RoundLimit") == 1
    # Prose after the cap notice, and the turn still ends on TurnDone.
    assert kinds.index("RoundLimit") < kinds.index(
        "TextDelta", kinds.index("RoundLimit")
    )
    assert isinstance(events[-1], TurnDone)
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "Short version: it's the cache."

    # Two tool rounds plus the answer round, which keeps the same tools but
    # forbids calling them.
    assert len(client.requests) == 3
    assert client.requests[-1].tools == client.requests[0].tools
    assert client.requests[-1].tools
    assert client.requests[-1].tool_choice == "none"
    assert {r.tool_choice for r in client.requests[:-1]} == {"auto"}
    # It still carries the full transcript, including the last round's results.
    roles = [m.role for m in client.requests[-1].messages]
    assert roles.count("tool") == 2


def test_a_tool_call_in_the_answer_round_is_dropped() -> None:
    """A provider that returns a call despite ``tool_choice="none"`` can't loop us."""
    client = ScriptedClient(
        [
            [
                ToolCallMade(call=_call("search_symbols", query="x")),
                TurnDone("tool_calls"),
            ],
            [ToolCallMade(call=_call("search_symbols", query="y")), TurnDone("stop")],
            [TextDelta(text="found"), TurnDone("stop")],
        ]
    )
    registry = StubRegistry()
    events = _run(client, registry, max_tool_rounds=1)

    # Only the in-budget round dispatched; the stray call was ignored, and the
    # textless answer round got its one nudge.
    assert len(registry.dispatched) == 1
    assert len(client.requests) == 3
    assert isinstance(events[-1], TurnDone)


def test_tool_error_result_is_recoverable_not_fatal() -> None:
    """An error payload is just a result — the model gets another round."""
    client = ScriptedClient(
        [
            [
                ToolCallMade(call=_call("get_symbol", qualified_name="nope")),
                TurnDone("tool_calls"),
            ],
            [TextDelta(text="That symbol does not exist."), TurnDone("stop")],
        ]
    )
    registry = StubRegistry({"get_symbol": json.dumps({"error": "not found"})})
    events = _run(client, registry)

    result_event = next(e for e in events if isinstance(e, ToolResultReady))
    assert json.loads(result_event.result) == {"error": "not found"}
    assert events[-3] == TextDelta(text="That symbol does not exist.")
    assert isinstance(events[-2], RoundUsage)
    assert isinstance(events[-1], TurnDone)


def test_provider_error_propagates_as_llm_error() -> None:
    """The serve layer converts this into an in-band SSE error frame."""
    client = ScriptedClient([[]], raise_on_round=0)
    with pytest.raises(LlmError, match="provider exploded"):
        _run(client, StubRegistry())


def test_provider_error_mid_loop_propagates_after_partial_output() -> None:
    client = ScriptedClient(
        [
            [
                ToolCallMade(call=_call("search_symbols", query="x")),
                TurnDone("tool_calls"),
            ]
        ],
        raise_on_round=1,
    )
    events: list = []
    with pytest.raises(LlmError):
        for event in run_turn(
            client=client,
            history=(ChatMessage(role="user", content="hi"),),
            registry=StubRegistry(),
            max_tool_rounds=4,
            context_token_budget=60_000,
        ):
            events.append(event)
    # The tool round already happened and was reported before the failure.
    assert any(isinstance(e, ToolResultReady) for e in events)


def test_specs_and_bounds_come_from_config_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core,
        "_config",
        Config(chat=ChatConfig(max_tool_rounds=2, context_token_budget=1234)),
    )
    forever = [
        ToolCallMade(call=_call("search_symbols", query="x")),
        TurnDone("tool_calls"),
    ]
    answer = [TextDelta(text="found"), TurnDone("stop")]
    client = ScriptedClient([list(forever) for _ in range(2)] + [answer])
    events = list(
        run_turn(
            client=client,
            history=(ChatMessage(role="user", content="hi"),),
            registry=StubRegistry(),
        )
    )
    # Two tool rounds from config, plus the answer round.
    assert len(client.requests) == 3
    assert events[-4] == RoundLimit(rounds=2)


def test_registry_specs_are_offered_to_the_model() -> None:
    """A real registry's 23 specs reach the request."""
    client = ScriptedClient([[TextDelta(text="hi"), TurnDone("stop")]])
    registry = ToolRegistry(max_rationale_generations=0)
    list(
        run_turn(
            client=client,
            history=(ChatMessage(role="user", content="hi"),),
            registry=registry,
            max_tool_rounds=1,
        )
    )
    assert len(client.requests[0].tools) == 23


# ---------------------------------------------------------------------------
# Usage: one RoundUsage per provider call, the turn total on TurnDone
# ---------------------------------------------------------------------------


def _done(reason, inp=None, out=None, **fields) -> TurnDone:
    return TurnDone(finish_reason=reason, input_tokens=inp, output_tokens=out, **fields)


def test_three_round_turn_reports_each_round_and_the_total() -> None:
    """Every stream_turn yields one RoundUsage; TurnDone carries the sums."""
    client = ScriptedClient(
        [
            [
                ToolCallMade(call=_call("search_symbols", "c1", query="a")),
                _done(
                    "tool_calls",
                    100,
                    10,
                    cache_read_tokens=80,
                    cache_write_tokens=5,
                    cost_usd=0.01,
                    model="served-a",
                ),
            ],
            [
                ToolCallMade(call=_call("get_symbol", "c2", qualified_name="b")),
                _done("tool_calls", 200, 20, reasoning_tokens=7, model="served-b"),
            ],
            [
                TextDelta(text="Answer."),
                _done("stop", 300, 30, cache_read_tokens=250, cost_usd=0.02),
            ],
        ]
    )
    events = _run(client, StubRegistry())

    usages = [e for e in events if isinstance(e, RoundUsage)]
    assert usages == [
        RoundUsage(
            input_tokens=100,
            output_tokens=10,
            cache_read_tokens=80,
            cache_write_tokens=5,
            model="served-a",
            finish_reason="tool_calls",
        ),
        RoundUsage(
            input_tokens=200,
            output_tokens=20,
            reasoning_tokens=7,
            model="served-b",
            finish_reason="tool_calls",
        ),
        RoundUsage(
            input_tokens=300,
            output_tokens=30,
            cache_read_tokens=250,
            finish_reason="stop",
        ),
    ]
    assert len(usages) == len(client.requests) == 3

    total = events[-1]
    assert total == TurnDone(
        finish_reason="stop",
        input_tokens=600,
        output_tokens=60,
        cache_read_tokens=330,
        cache_write_tokens=5,
        reasoning_tokens=7,
        cost_usd=pytest.approx(0.03),
        # The last model a round reported (round 3 did not say).
        model="served-b",
    )
    # Exactly one TurnDone, and it is last.
    assert sum(isinstance(e, TurnDone) for e in events) == 1


def test_round_usage_precedes_that_rounds_tool_events() -> None:
    """A round's tools run after its provider call, so they follow its usage."""
    call = _call("search_symbols", query="x")
    client = ScriptedClient(
        [
            [
                TextDelta(text="Looking."),
                ToolCallMade(call=call),
                _done("tool_calls", 4, 2),
            ],
            [TextDelta(text="Done."), _done("stop", 6, 3)],
        ]
    )
    kinds = [type(e).__name__ for e in _run(client, StubRegistry())]
    assert kinds == [
        "TextDelta",
        "RoundUsage",
        "ToolCallStarted",
        "ToolResultReady",
        "TextDelta",
        "RoundUsage",
        "TurnDone",
    ]


def test_post_limit_answer_round_reports_its_usage_and_joins_the_total() -> None:
    forever = [
        ToolCallMade(call=_call("search_symbols", query="x")),
        _done("tool_calls", 10, 1),
    ]
    client = ScriptedClient(
        [
            list(forever),
            list(forever),
            [TextDelta(text="Summary."), _done("stop", 50, 5)],
        ]
    )
    events = _run(client, StubRegistry(), max_tool_rounds=2)

    kinds = [type(e).__name__ for e in events]
    assert kinds.count("RoundUsage") == 3
    limit = kinds.index("RoundLimit")
    assert kinds[limit:] == ["RoundLimit", "TextDelta", "RoundUsage", "TurnDone"]
    assert events[-2] == RoundUsage(
        input_tokens=50, output_tokens=5, finish_reason="stop"
    )
    assert events[-1] == _done("stop", 70, 7)


def test_turn_total_is_none_only_when_no_round_reported() -> None:
    client = ScriptedClient(
        [
            [
                ToolCallMade(call=_call("search_symbols", query="x")),
                _done("tool_calls"),
            ],
            [TextDelta(text="ok"), _done("stop")],
        ]
    )
    events = _run(client, StubRegistry())
    assert events[-1] == TurnDone(finish_reason="stop")
    assert [e for e in events if isinstance(e, RoundUsage)] == [
        RoundUsage(finish_reason="tool_calls"),
        RoundUsage(finish_reason="stop"),
    ]


def test_a_round_without_a_turn_done_still_reports_empty_usage() -> None:
    """A stream that ends without its TurnDone still closes the round."""
    client = ScriptedClient([[TextDelta(text="cut")]])
    events = _run(client, StubRegistry())
    assert events == [TextDelta(text="cut"), RoundUsage(), TurnDone()]


# ---------------------------------------------------------------------------
# The budget stop (M2f-2 plan section 4.7)
# ---------------------------------------------------------------------------


class _BudgetSink:
    """A usage sink whose budget is exhausted after ``allowed`` records."""

    def __init__(self, allowed: int, scope: str = "member") -> None:
        self.scope = UsageScope(source="chat")
        self.allowed = allowed
        self.records: list = []
        self.blocked = scope

    def record(self, rec) -> None:  # noqa: ANN001
        self.records.append(rec)

    def blocked_scope(self) -> str | None:
        return self.blocked if len(self.records) >= self.allowed else None


def _tool_round(call_id: str, inp: int) -> list:
    return [
        ToolCallMade(call=_call("search_symbols", call_id, query="x")),
        _done("tool_calls", inp, 1),
    ]


def test_a_budget_spent_before_round_two_stops_the_turn() -> None:
    client = ScriptedClient(
        [_tool_round("c1", 10), [TextDelta(text="never"), _done("stop", 5, 5)]]
    )
    sink = _BudgetSink(allowed=1, scope="project")
    with use_usage_sink(sink):
        events = _run(client, StubRegistry())
    assert len(client.requests) == 1  # round 2 was never sent
    assert len(sink.records) == 1
    kinds = [type(e).__name__ for e in events]
    assert kinds == [
        "RoundUsage",
        "ToolCallStarted",
        "ToolResultReady",
        "BudgetStop",
        "TurnDone",
    ]
    assert events[-2] == BudgetStop(scope="project")
    assert events[-1] == TurnDone(
        finish_reason="tool_calls", input_tokens=10, output_tokens=1
    )


def test_a_budget_spent_before_the_first_round_sends_nothing() -> None:
    client = ScriptedClient([])
    with use_usage_sink(_BudgetSink(allowed=0, scope="org")):
        events = _run(client, StubRegistry())
    assert client.requests == []
    assert events == [BudgetStop(scope="org"), TurnDone()]


def test_a_budget_spent_before_the_answer_round_skips_it() -> None:
    client = ScriptedClient([_tool_round("c1", 10), _tool_round("c2", 20)])
    with use_usage_sink(_BudgetSink(allowed=2)):
        events = _run(client, StubRegistry(), max_tool_rounds=2)
    assert len(client.requests) == 2
    assert not any(isinstance(e, RoundLimit) for e in events)
    assert events[-2:] == [
        BudgetStop(scope="member"),
        TurnDone(finish_reason="tool_calls", input_tokens=30, output_tokens=2),
    ]


def test_an_unbound_or_unblocked_sink_never_stops_a_turn() -> None:
    client = ScriptedClient(
        [_tool_round("c1", 10), [TextDelta(text="ok"), _done("stop")]]
    )
    with use_usage_sink(_BudgetSink(allowed=99)):
        events = _run(client, StubRegistry())
    assert not any(isinstance(e, BudgetStop) for e in events)
    assert len(client.requests) == 2


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def test_system_prompt_names_the_repo_and_embeds_the_overview(_repo_root: Path) -> None:
    stable, overview = build_system_prompt(StubRegistry())
    assert stable.role == overview.role == "system"
    assert "myrepo" in stable.content
    assert overview.content == '{"commits": 3}'
    # No unsubstituted placeholders left behind.
    for message in (stable, overview):
        assert "{{REPO}}" not in message.content
        assert "{{OVERVIEW}}" not in message.content
    assert stable.content.rstrip().endswith("## This repository right now")


def test_a_changed_overview_leaves_the_stable_prompt_byte_identical(
    _repo_root: Path,
) -> None:
    """The stable block is what a provider caches, so it must not move."""

    class _Other(StubRegistry):
        def dispatch(self, name, arguments):
            if name == "get_repo_overview":
                return json.dumps({"commits": 999})
            return super().dispatch(name, arguments)

    first, overview_a = build_system_prompt(StubRegistry())
    second, overview_b = build_system_prompt(_Other())
    assert first.content == second.content
    assert overview_a.content != overview_b.content


def test_a_purpose_in_the_overview_leaves_the_stable_prompt_byte_identical(
    _repo_root: Path,
) -> None:
    """Plan 41: the purpose rides the overview message, never the first one.

    (The `system.md` sentence naming `purpose` is a one-time change; after it
    the first message is the same with or without a purpose.)
    """

    class _WithPurpose(StubRegistry):
        def dispatch(self, name, arguments):
            if name == "get_repo_overview":
                return json.dumps(
                    {
                        "commits": 3,
                        "purpose": {
                            "text": "- note #1 [purpose]",
                            "source": "analysis",
                        },
                    }
                )
            return super().dispatch(name, arguments)

    plain, overview_a = build_system_prompt(StubRegistry())
    with_purpose, overview_b = build_system_prompt(_WithPurpose())
    assert plain.content == with_purpose.content
    assert "`purpose` is a confirmed, machine-drafted" in plain.content
    assert '"purpose"' in overview_b.content and '"purpose"' not in overview_a.content


def test_system_prompt_documents_the_deep_link_convention(_repo_root: Path) -> None:
    content = build_system_prompt(StubRegistry())[0].content
    assert "whygraph://symbol/" in content
    assert "data, not instructions" in content


def test_system_prompt_carries_the_mode_block(_repo_root: Path) -> None:
    """``{{MODE}}`` is filled in the stable block, before the overview split."""
    for mode in MODES.values():
        stable, _ = build_system_prompt(StubRegistry(), mode)
        assert "{{MODE}}" not in stable.content
        assert mode_block(mode) in stable.content
        assert stable.content.rstrip().endswith("## This repository right now")


def test_no_mode_renders_the_default_mode(_repo_root: Path) -> None:
    default = build_system_prompt(StubRegistry())[0].content
    assert default == build_system_prompt(StubRegistry(), MODES["engineer"])[0].content


def test_the_stable_block_differs_by_mode_and_holds_within_one(
    _repo_root: Path,
) -> None:
    """Each mode's stable block is its own cache entry, unmoved by the overview."""

    class _Other(StubRegistry):
        def dispatch(self, name, arguments):
            if name == "get_repo_overview":
                return json.dumps({"commits": 999})
            return super().dispatch(name, arguments)

    stables = set()
    for mode in MODES.values():
        first = build_system_prompt(StubRegistry(), mode)[0].content
        second = build_system_prompt(_Other(), mode)[0].content
        assert first == second
        stables.add(first)
    assert len(stables) == len(MODES)


def test_run_turn_sends_the_mode_block_and_the_same_tools_in_every_mode() -> None:
    """A mode steers by prompt only: the tools array never changes (D2)."""
    sent = {}
    for mode_id in (*MODE_IDS, None, "gone"):
        client = ScriptedClient([[TextDelta(text="hi"), TurnDone("stop")]])
        list(
            run_turn(
                client=client,
                history=(ChatMessage(role="user", content="hi"),),
                registry=ToolRegistry(max_rationale_generations=0),
                max_tool_rounds=1,
                mode=mode_id,
            )
        )
        request = client.requests[0]
        assert len(request.tools) == 23
        sent[mode_id] = (request.messages[0].content, request.tools)
    tools = {mode_id: value[1] for mode_id, value in sent.items()}
    assert all(value == tools["engineer"] for value in tools.values())
    for mode_id in MODE_IDS:
        assert mode_block(MODES[mode_id]) in sent[mode_id][0]
    assert sent[None][0] == sent["gone"][0] == sent["engineer"][0]


def test_system_prompt_is_never_part_of_history() -> None:
    """It is prepended per turn, not carried in the transcript."""
    client = ScriptedClient([[TextDelta(text="x"), TurnDone("stop")]])
    _run(client, StubRegistry())
    sent = client.requests[0].messages
    assert [m.role for m in sent[:2]] == ["system", "system"]
    assert [m.role for m in sent[2:]] == ["user"]


# ---------------------------------------------------------------------------
# build_window (§5.3)
# ---------------------------------------------------------------------------


def _turn(index: int, *, tool_body: str = "x" * 400) -> list[ChatMessage]:
    """One complete turn: user → assistant(+call) → tool → assistant."""
    call = ToolCall(id=f"t{index}", name="get_evidence", arguments={"q": index})
    return [
        ChatMessage(role="user", content=f"question {index}"),
        ChatMessage(role="assistant", content="looking", tool_calls=(call,)),
        ChatMessage(role="tool", content=tool_body, tool_call_id=f"t{index}"),
        ChatMessage(role="assistant", content=f"answer {index}"),
    ]


def _history(count: int, **kwargs) -> list[ChatMessage]:
    return [m for i in range(count) for m in _turn(i, **kwargs)]


def _assert_no_orphans(messages) -> None:
    """Every tool message must have its assistant tool-call still present.

    This is the invariant both provider APIs enforce; violating it makes the
    request fail outright rather than degrade.
    """
    announced = {
        call.id for m in messages if m.role == "assistant" for call in m.tool_calls
    }
    answered = {m.tool_call_id for m in messages if m.role == "tool"}
    assert answered <= announced, "orphaned tool result"
    # And the converse: an assistant tool_call with no result is equally invalid.
    assert announced <= answered, "dangling assistant tool_call"


def test_under_budget_history_is_unchanged() -> None:
    history = _history(3)
    assert build_window(history, token_budget=1_000_000) == tuple(history)


def test_begin_turn_messages_is_the_window_plus_references_and_continue() -> None:
    history = _history(3)
    messages, start = begin_turn_messages(history, token_budget=1_000_000)
    assert messages == list(build_window(history, token_budget=1_000_000))
    assert start == len(messages)
    messages, start = begin_turn_messages(
        history,
        token_budget=1_000_000,
        references="refs",
        continue_from_assistant=True,
    )
    assert start == len(history)
    assert messages[start - 4].content == with_references("question 2", "refs")
    assert messages[-1] == ChatMessage(role="user", content=CONTINUE_PROMPT)
    # The caller's history is never mutated.
    assert history == _history(3)


def test_begin_turn_messages_matches_what_run_turn_sends() -> None:
    history = [ChatMessage(role="user", content="question 0")]
    client = ScriptedClient([[TextDelta("hi"), TurnDone("stop", 1, 1)]])
    list(run_turn(client=client, history=history, registry=StubRegistry()))
    expected, _ = begin_turn_messages(
        history, token_budget=ChatConfig().context_token_budget
    )
    assert list(client.requests[0].messages[-len(expected) :]) == expected


def test_trim_turn_is_callable_on_the_helper_output() -> None:
    messages, start = begin_turn_messages(_history(1), token_budget=1_000_000)
    messages += _turn(5, tool_body="y" * 4_000) + _turn(6, tool_body="z" * 4_000)
    elided = _trim_turn(messages, start, len(messages) - 3, 2, 100)
    assert elided >= 1
    assert messages[start + 2].content == ELIDED_MARKER


def test_specs_tokens_counts_name_description_and_parameters() -> None:
    specs = (
        ToolSpec(name="a", description="x" * 100, parameters={"type": "object"}),
        ToolSpec(name="b", description="", parameters={}),
    )
    text = json.dumps(
        [
            {"name": s.name, "description": s.description, "parameters": s.parameters}
            for s in specs
        ]
    )
    assert _specs_tokens(specs) == len(text) // 4
    assert _specs_tokens(()) == len("[]") // 4
    assert _specs_tokens(ToolRegistry(max_rationale_generations=0).specs) > 1_000


def test_system_prompt_is_pinned_at_the_front() -> None:
    system = ChatMessage(role="system", content="rules")
    window = build_window(_history(1), token_budget=1_000_000, system=system)
    assert window[0] is system


def test_system_prompt_survives_aggressive_trimming() -> None:
    system = ChatMessage(role="system", content="rules " * 500)
    window = build_window(_history(8), token_budget=1000, system=system)
    assert window[0] is system


def test_tool_bodies_are_elided_before_any_turn_is_dropped() -> None:
    """The cheap saving is taken first; the conversation's thread survives."""
    history = _history(5)
    # 5 full turns cost ~550; eliding the oldest 3 brings it to ~259. A
    # budget in between is exactly the case where eliding alone suffices.
    window = build_window(history, token_budget=300)

    users = [m.content for m in window if m.role == "user"]
    assert users == [f"question {i}" for i in range(5)]  # nothing dropped

    elided = [m.content for m in window if m.role == "tool"]
    assert elided[:3] == [ELIDED_MARKER] * 3  # older turns elided
    assert all(c != ELIDED_MARKER for c in elided[3:])  # last two kept
    _assert_no_orphans(window)


def test_oldest_whole_turns_drop_when_eliding_is_not_enough() -> None:
    history = _history(6, tool_body="y" * 50)
    window = build_window(history, token_budget=60)

    users = [m.content for m in window if m.role == "user"]
    assert users, "window must not be empty"
    assert users[-1] == "question 5"  # newest turn always kept
    assert len(users) < 6  # something was dropped
    # Turns drop whole, from the oldest end — never a partial slice.
    assert users == [f"question {i}" for i in range(6 - len(users), 6)]
    _assert_no_orphans(window)


def test_trimming_never_orphans_a_tool_message_at_any_budget() -> None:
    """The invariant holds across the whole budget range, not just one point."""
    history = _history(6)
    for budget in (1000, 500, 250, 100, 50, 10, 1):
        _assert_no_orphans(build_window(history, token_budget=budget))


def test_a_single_over_budget_turn_is_kept_rather_than_emptying() -> None:
    """An empty message list is a silent failure; one big turn is a loud one."""
    history = _turn(0, tool_body="z" * 100_000)
    window = build_window(history, token_budget=1000)
    assert [m.role for m in window] == ["user", "assistant", "tool", "assistant"]
    _assert_no_orphans(window)


def test_already_elided_bodies_are_not_re_marked() -> None:
    history = _history(4, tool_body=ELIDED_MARKER)
    window = build_window(history, token_budget=1)
    assert all(m.content == ELIDED_MARKER for m in window if m.role == "tool")


def test_empty_history_yields_just_the_system_prompt() -> None:
    system = ChatMessage(role="system", content="rules")
    assert build_window((), token_budget=1000, system=system) == (system,)
    assert build_window((), token_budget=1000) == ()


# ---------------------------------------------------------------------------
# How a turn ends (plan 37 §4.1): continuation, nudge, answer round,
# in-turn trimming, the silent retry, tool errors and cancellation
# ---------------------------------------------------------------------------


class _Client(ChatClient):
    """A scripted client whose scripts may raise or run a hook mid-stream.

    Each :meth:`stream_turn` call consumes the next script; an ``Exception``
    item is raised at that point and a callable item is called.
    """

    provider = "scripted"

    def __init__(self, scripts: list[list]) -> None:
        super().__init__(model="scripted-1")
        self._scripts = scripts
        self.requests: list = []

    def stream_turn(self, request):
        index = len(self.requests)
        self.requests.append(request)
        if index >= len(self._scripts):
            raise AssertionError(f"loop asked for call {index}, script has none")
        for item in self._scripts[index]:
            if isinstance(item, BaseException):
                raise item
            if callable(item):
                item()
                continue
            yield item

    def list_models(self):
        """Unused by the harness."""


def _kinds(events) -> list[str]:
    return [type(e).__name__ for e in events]


@pytest.fixture
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "RETRY_DELAY_SEC", 0)


def _dropped() -> httpx.RemoteProtocolError:
    return httpx.RemoteProtocolError("peer closed connection")


def test_a_truncated_reply_with_text_is_continued_once() -> None:
    client = _Client(
        [
            [TextDelta(text="Part one"), _done("max_tokens")],
            [TextDelta(text=" and the rest."), _done("end_turn")],
        ]
    )
    events = _run(client, StubRegistry())
    assert Continuation(attempt=1, finish_reason="max_tokens") in events
    second = client.requests[1].messages
    assert (second[-2].role, second[-2].content) == ("assistant", "Part one")
    assert (second[-1].role, second[-1].content) == ("user", CONTINUE_PROMPT)
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "Part one and the rest."
    assert events[-1].finish_reason == "end_turn"


def test_continue_from_assistant_ends_the_first_request_with_the_prompt() -> None:
    client = _Client([[TextDelta(text=" the rest."), _done("end_turn")]])
    history = (
        ChatMessage(role="user", content="why?"),
        ChatMessage(role="assistant", content="Part one"),
    )
    events = list(
        run_turn(
            client=client,
            history=history,
            registry=StubRegistry(),
            continue_from_assistant=True,
        )
    )
    first = client.requests[0].messages
    assert [(m.role, m.content) for m in first[-3:]] == [
        ("user", "why?"),
        ("assistant", "Part one"),
        ("user", CONTINUE_PROMPT),
    ]
    # The prompt is never an event: only the reply's own text comes out.
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == " the rest."
    assert not any(isinstance(e, Continuation) for e in events)


def test_max_continuations_bounds_the_continuations() -> None:
    truncated = [TextDelta(text="more"), _done("length")]
    client = _Client([list(truncated), list(truncated), list(truncated)])
    events = _run(client, StubRegistry(), max_continuations=1)
    assert len(client.requests) == 2
    assert _kinds(events).count("Continuation") == 1

    client = _Client([list(truncated)])
    events = _run(client, StubRegistry(), max_continuations=0)
    assert len(client.requests) == 1
    assert "Continuation" not in _kinds(events)


def test_a_truncated_round_with_no_text_is_not_continued() -> None:
    client = _Client([[_done("max_tokens")]])
    events = _run(client, StubRegistry())
    assert len(client.requests) == 1
    assert "Continuation" not in _kinds(events)
    assert "EmptyReply" not in _kinds(events)


def test_an_empty_reply_after_a_tool_round_is_nudged_once() -> None:
    client = _Client(
        [
            _tool_round("c1", 10),
            [_done("end_turn")],
            [TextDelta(text="The answer."), _done("end_turn")],
        ]
    )
    events = _run(client, StubRegistry())
    assert [e for e in events if isinstance(e, EmptyReply)] == [EmptyReply()]
    third = client.requests[2].messages
    assert (third[-1].role, third[-1].content) == ("user", EMPTY_REPLY_PROMPT)
    # Nothing empty is appended for the silent round.
    assert [m.role for m in third[2:]] == ["user", "assistant", "tool", "user"]


_BLOCK = '[{"text":"#4","kind":"issue","number":4,"state":"open"}]'


def test_references_go_once_on_the_last_question_of_every_round() -> None:
    client = _Client(
        [
            _tool_round("c1", 10),
            [_done("end_turn")],
            [TextDelta(text="Part"), _done("max_tokens")],
            [TextDelta(text=" two."), _done("end_turn")],
        ]
    )
    history = (
        ChatMessage(role="user", content="first?"),
        ChatMessage(role="assistant", content="one"),
        ChatMessage(role="user", content="why #4?"),
    )
    list(
        run_turn(
            client=client,
            history=history,
            registry=StubRegistry(),
            references=_BLOCK,
        )
    )
    asked = with_references("why #4?", _BLOCK)
    system = client.requests[0].messages[0]
    for request in client.requests:
        messages = request.messages
        assert messages[0] == system  # the system prompt is untouched
        users = [m.content for m in messages if m.role == "user"]
        assert users[:2] == ["first?", asked]
        # In-turn prompts never get the block.
        assert all(_BLOCK not in u for u in users[2:])
    last = [m.content for m in client.requests[-1].messages if m.role == "user"]
    assert last[2:] == [EMPTY_REPLY_PROMPT, CONTINUE_PROMPT]
    assert history[-1].content == "why #4?"  # the caller's history is not mutated


def test_no_references_leave_the_question_as_it_is() -> None:
    client = _Client([[TextDelta(text="ok"), _done("end_turn")]])
    _run(client, StubRegistry(), references=None)
    assert client.requests[0].messages[-1].content == "why?"


def test_a_second_empty_reply_ends_the_turn() -> None:
    client = _Client([_tool_round("c1", 10), [_done("stop")], [_done("stop")]])
    events = _run(client, StubRegistry())
    assert len(client.requests) == 3
    assert _kinds(events).count("EmptyReply") == 1
    assert isinstance(events[-1], TurnDone)


def test_no_nudge_without_tool_rounds_or_after_a_refusal() -> None:
    client = _Client([[_done("end_turn")]])
    _run(client, StubRegistry())
    assert len(client.requests) == 1

    client = _Client([_tool_round("c1", 10), [_done("refusal")]])
    events = _run(client, StubRegistry())
    assert len(client.requests) == 2
    assert "EmptyReply" not in _kinds(events)


def test_a_refusal_ends_the_turn_even_with_text_and_calls() -> None:
    client = _Client(
        [
            [
                TextDelta(text="I can't"),
                ToolCallMade(call=_call("search_symbols", query="x")),
                _done("refusal"),
            ]
        ]
    )
    registry = StubRegistry()
    _run(client, registry)
    assert len(client.requests) == 1
    assert registry.dispatched == []


def test_a_continuation_on_the_last_round_leads_to_an_answer_round_without_a_limit() -> (
    None
):
    client = _Client(
        [
            _tool_round("c1", 10),
            [TextDelta(text="Half"), _done("max_tokens")],
            [TextDelta(text=" done."), _done("end_turn")],
        ]
    )
    events = _run(client, _SpecRegistry(), max_tool_rounds=2)
    assert len(client.requests) == 3
    assert client.requests[-1].tool_choice == "none"
    assert client.requests[-1].tools == client.requests[0].tools
    assert "RoundLimit" not in _kinds(events)
    assert "Continuation" in _kinds(events)


def test_a_nudge_on_the_last_round_leads_to_an_answer_round_without_a_limit() -> None:
    client = _Client(
        [
            _tool_round("c1", 10),
            [_done("end_turn")],
            [TextDelta(text="Answer."), _done("end_turn")],
        ]
    )
    events = _run(client, StubRegistry(), max_tool_rounds=2)
    assert len(client.requests) == 3
    assert client.requests[-1].tool_choice == "none"
    assert "RoundLimit" not in _kinds(events)
    # The answer round never nudges or continues, even when it says nothing.
    client = _Client(
        [_tool_round("c1", 10), [_done("end_turn")], [_done("max_tokens")]]
    )
    _run(client, StubRegistry(), max_tool_rounds=2)
    assert len(client.requests) == 3


def test_tool_rounds_trim_older_results_once_over_budget() -> None:
    big = "x" * 2400  # ~600 tokens per result
    error = json.dumps({"error": "no such symbol", "pad": big})
    registry = StubRegistry({"get_symbol": error, "search_symbols": big})
    anthropic_replay = {"content": [{"type": "thinking"}], "reasoning_content": "r"}
    client = _Client(
        [
            [
                ToolCallMade(call=_call("get_symbol", "c1", qualified_name="a")),
                TurnDone("tool_calls", replay=anthropic_replay),
            ],
            [
                ToolCallMade(call=_call("search_symbols", "c2", query="b")),
                _done("tool_calls"),
            ],
            [
                ToolCallMade(call=_call("search_symbols", "c3", query="c")),
                _done("tool_calls"),
            ],
            [TextDelta(text="Done."), _done("end_turn")],
        ]
    )
    events = _run(client, registry, context_token_budget=1000)

    assert [e for e in events if isinstance(e, ContextTrimmed)] == [
        ContextTrimmed(rounds_elided=1)
    ]
    # Round two still saw round one in full; round three saw it elided.
    tools = [m for m in client.requests[1].messages if m.role == "tool"]
    assert tools[0].content == error
    tools = [m for m in client.requests[2].messages if m.role == "tool"]
    assert [t.content == ELIDED_MARKER for t in tools] == [True, False]
    assert tools[0].is_error is True  # `replace` keeps the flag
    assistant = next(m for m in client.requests[2].messages if m.tool_calls)
    assert assistant.replay == {"reasoning_content": "r"}  # replayed thinking stripped
    tools = [m for m in client.requests[3].messages if m.role == "tool"]
    assert [t.content == ELIDED_MARKER for t in tools] == [True, True, False]


def test_a_tool_error_result_is_flagged() -> None:
    registry = StubRegistry({"get_symbol": json.dumps({"error": "not found"})})
    client = _Client(
        [
            [
                ToolCallMade(call=_call("get_symbol", "c1", qualified_name="a")),
                ToolCallMade(call=_call("search_symbols", "c2", query="b")),
                _done("tool_calls"),
            ],
            [TextDelta(text="ok"), _done("end_turn")],
        ]
    )
    _run(client, registry)
    tools = [m for m in client.requests[1].messages if m.role == "tool"]
    assert [t.is_error for t in tools] == [True, False]


def test_effort_and_timeout_ride_every_request() -> None:
    client = _Client([_tool_round("c1", 1), [TextDelta(text="ok"), _done("end_turn")]])
    _run(client, StubRegistry(), effort="high", stream_timeout_sec=120, max_tokens=8000)
    assert {(r.effort, r.timeout_sec, r.max_tokens) for r in client.requests} == {
        ("high", 120, 8000)
    }


@pytest.mark.usefixtures("_no_retry_delay")
def test_a_dropped_connection_before_any_output_is_retried_once() -> None:
    client = _Client(
        [
            [_dropped()],
            [TextDelta(text="Recovered."), _done("end_turn", 4, 2)],
        ]
    )
    events = _run(client, StubRegistry())
    assert len(client.requests) == 2
    assert _kinds(events) == ["TextDelta", "RoundUsage", "TurnDone"]
    assert events[-1].input_tokens == 4

    # A read error qualifies too, wrapped as the adapters wrap it.
    def _wrapped():
        try:
            raise httpx.ReadError("reset")
        except httpx.ReadError as exc:
            raise LlmError("anthropic stream interrupted: ReadError") from exc

    class _Raises:
        def __call__(self):
            _wrapped()

    client = _Client([[_Raises()], [TextDelta(text="ok"), _done("end_turn")]])
    _run(client, StubRegistry())
    assert len(client.requests) == 2


@pytest.mark.usefixtures("_no_retry_delay")
def test_a_drop_is_not_retried_twice_or_after_output() -> None:
    client = _Client([[_dropped()], [_dropped()]])
    with pytest.raises(httpx.RemoteProtocolError):
        _run(client, StubRegistry())
    assert len(client.requests) == 2

    client = _Client([[TextDelta(text="half"), _dropped()]])
    with pytest.raises(httpx.RemoteProtocolError):
        _run(client, StubRegistry())
    assert len(client.requests) == 1

    client = _Client(
        [[ToolCallMade(call=_call("search_symbols", query="x")), _dropped()]]
    )
    with pytest.raises(httpx.RemoteProtocolError):
        _run(client, StubRegistry())
    assert len(client.requests) == 1


@pytest.mark.usefixtures("_no_retry_delay")
def test_timeouts_and_sdk_connection_errors_are_never_retried() -> None:
    timeout = httpx.ReadTimeout("silent think")
    client = _Client([[timeout]])
    with pytest.raises(httpx.ReadTimeout):
        _run(client, StubRegistry())
    assert len(client.requests) == 1

    class APIConnectionError(Exception):
        pass

    def _sdk_error():
        try:
            raise _dropped()
        except httpx.RemoteProtocolError as exc:
            raise APIConnectionError("connection error") from exc

    client = _Client([[_sdk_error]])
    with pytest.raises(APIConnectionError):
        _run(client, StubRegistry())
    assert len(client.requests) == 1


@pytest.mark.usefixtures("_no_retry_delay")
def test_the_budget_is_checked_before_the_retry() -> None:
    sink = _BudgetSink(allowed=99)

    def _block():
        sink.allowed = 0

    client = _Client([[_block, _dropped()]])
    with use_usage_sink(sink), pytest.raises(httpx.RemoteProtocolError):
        _run(client, StubRegistry())
    assert len(client.requests) == 1


def test_a_stop_during_the_retry_wait_cancels_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(harness, "RETRY_DELAY_SEC", 30)
    cancel = threading.Event()
    # Started by the request itself: a timer started before the turn could fire
    # before the first request on a slow runner, and the turn would stop there.
    client = _Client([[lambda: threading.Timer(0.05, cancel.set).start(), _dropped()]])
    started = time.monotonic()
    with pytest.raises(TurnCancelled):
        _run(client, StubRegistry(), cancel=cancel)
    assert time.monotonic() - started < 5
    assert len(client.requests) == 1


def test_cancel_before_the_first_round_sends_nothing() -> None:
    cancel = threading.Event()
    cancel.set()
    client = _Client([])
    with pytest.raises(TurnCancelled):
        _run(client, StubRegistry(), cancel=cancel)
    assert client.requests == []


def test_cancel_mid_stream_stops_the_turn() -> None:
    cancel = threading.Event()
    client = _Client(
        [[TextDelta(text="a"), cancel.set, TextDelta(text="b"), _done("end_turn")]]
    )
    seen: list = []
    with pytest.raises(TurnCancelled):
        for event in run_turn(
            client=client,
            history=(ChatMessage(role="user", content="why?"),),
            registry=StubRegistry(),
            cancel=cancel,
        ):
            seen.append(event)
    assert seen == [TextDelta(text="a")]
    assert len(client.requests) == 1


def test_cancel_before_a_tool_dispatch_runs_no_further_tool() -> None:
    cancel = threading.Event()

    class _Stopping(StubRegistry):
        def dispatch(self, name, arguments):
            result = super().dispatch(name, arguments)
            if name != "get_repo_overview":
                cancel.set()
            return result

    registry = _Stopping()
    client = _Client(
        [
            [
                ToolCallMade(call=_call("search_symbols", "c1", query="a")),
                ToolCallMade(call=_call("search_symbols", "c2", query="b")),
                _done("tool_calls"),
            ]
        ]
    )
    with pytest.raises(TurnCancelled):
        _run(client, registry, cancel=cancel)
    assert len(registry.dispatched) == 1
    assert len(client.requests) == 1


def test_any_error_while_stopping_is_a_cancellation() -> None:
    cancel = threading.Event()

    def _closed_socket():
        cancel.set()
        raise ValueError("I/O operation on closed SSL socket")

    client = _Client([[TextDelta(text="a"), _closed_socket]])
    with pytest.raises(TurnCancelled) as info:
        _run(client, StubRegistry(), cancel=cancel)
    assert isinstance(info.value.__cause__, ValueError)


def test_reasoning_is_forwarded_and_blocks_the_silent_retry(monkeypatch) -> None:
    from whygraph.services.llm.chat import ReasoningDelta

    monkeypatch.setattr(harness, "RETRY_DELAY_SEC", 0)
    client = _Client(
        [[ReasoningDelta(text="hm"), TextDelta(text="ok"), _done("end_turn")]]
    )
    events = _run(client, StubRegistry())
    assert events[0] == ReasoningDelta(text="hm")

    # Reasoning already shown: a drop after it is not retried silently.
    client = _Client([[ReasoningDelta(text="hm"), _dropped()]])
    with pytest.raises(httpx.RemoteProtocolError):
        _run(client, StubRegistry())
    assert len(client.requests) == 1


def _repeat_script(*calls_per_round: list[ToolCall]) -> list[list]:
    """One tool round per entry, then a closing text round."""
    rounds = [
        [*(ToolCallMade(call=c) for c in calls), _done("tool_calls")]
        for calls in calls_per_round
    ]
    return [*rounds, [TextDelta(text="ok"), _done("end_turn")]]


def _tool_messages(request) -> list[ChatMessage]:
    return [m for m in request.messages if m.role == "tool"]


def test_the_third_identical_call_is_dispatched_with_a_repeated_note() -> None:
    registry = StubRegistry()
    calls = [_call("search_symbols", f"c{i}", query="a") for i in range(1, 5)]
    client = _Client(_repeat_script(*([c] for c in calls)))
    events = _run(client, registry)

    assert len(registry.dispatched) == 4  # every repeat still runs
    results = [e.result for e in events if isinstance(e, ToolResultReady)]
    assert "note" not in json.loads(results[0])
    assert "note" not in json.loads(results[1])
    assert json.loads(results[2]) == {
        "ok": "search_symbols",
        "note": "identical call repeated 3 times in this turn",
    }
    assert json.loads(results[3])["note"] == (
        "identical call repeated 4 times in this turn"
    )
    # The model sees exactly what the event carried.
    seen = [_tool_messages(client.requests[3])[i].content for i in range(3)]
    assert seen == results[:3]
    assert _tool_messages(client.requests[4])[3].content == results[3]


def test_repeated_calls_ignore_argument_order() -> None:
    registry = StubRegistry()
    client = _Client(
        _repeat_script(
            [_call("t", "c1", a=1, b=2)],
            [_call("t", "c2", b=2, a=1)],
            [_call("t", "c3", a=1, b=2)],
        )
    )
    events = _run(client, registry)
    results = [e.result for e in events if isinstance(e, ToolResultReady)]
    assert json.loads(results[2])["note"].startswith("identical call repeated 3")


def test_repeated_counts_are_per_tool_and_per_arguments() -> None:
    registry = StubRegistry()
    client = _Client(
        _repeat_script(
            [_call("t", "c1", q="a"), _call("t", "c2", q="b")],
            [_call("t", "c3", q="a"), _call("u", "c4", q="a")],
            [_call("t", "c5", q="b")],
        )
    )
    events = _run(client, registry)
    results = [e.result for e in events if isinstance(e, ToolResultReady)]
    assert len(results) == 5
    assert all("note" not in json.loads(r) for r in results)


def test_repeated_non_json_results_get_a_prefix() -> None:
    registry = StubRegistry({"t": "[1, 2, 3]"})
    client = _Client(_repeat_script(*([_call("t", f"c{i}", q="a")] for i in (1, 2, 3))))
    events = _run(client, registry)
    results = [e.result for e in events if isinstance(e, ToolResultReady)]
    assert results[1] == "[1, 2, 3]"
    assert results[2] == "[identical call repeated 3 times in this turn] [1, 2, 3]"
    assert _tool_messages(client.requests[3])[2].content == results[2]


def test_repeated_error_results_stay_errors_and_keep_their_note_key() -> None:
    registry = StubRegistry(
        {"t": json.dumps({"error": "nope", "note": "original"})},
    )
    client = _Client(_repeat_script(*([_call("t", f"c{i}", q="a")] for i in (1, 2, 3))))
    events = _run(client, registry)
    results = [e.result for e in events if isinstance(e, ToolResultReady)]
    third = json.loads(results[2])
    assert third["note"] == "original"
    assert third["repeat_note"] == "identical call repeated 3 times in this turn"
    tools = _tool_messages(client.requests[3])
    assert [m.is_error for m in tools] == [True, True, True]


def test_repeated_call_counter_is_per_turn() -> None:
    registry = StubRegistry()
    for _ in range(2):
        client = _Client(
            _repeat_script(*([_call("t", f"c{i}", q="a")] for i in (1, 2)))
        )
        events = _run(client, registry)
        results = [e.result for e in events if isinstance(e, ToolResultReady)]
        assert all("note" not in json.loads(r) for r in results)


# ---------------------------------------------------------------------------
# Parallel read-only tools (plan 37 §7.1.6)
# ---------------------------------------------------------------------------


class _SlowRegistry(StubRegistry):
    """Each call sleeps for its ``delay`` argument; records when calls overlap."""

    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0

    def dispatch(self, name, arguments):
        if name == "get_repo_overview":
            return super().dispatch(name, arguments)
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            time.sleep(arguments.get("delay", 0))
            return super().dispatch(name, arguments)
        finally:
            with self.lock:
                self.running -= 1


def _round(*calls: ToolCall) -> list:
    return [*(ToolCallMade(call=c) for c in calls), _done("tool_calls")]


def test_a_round_of_reads_runs_concurrently_in_model_order() -> None:
    calls = [
        _call("get_symbol", "c1", qualified_name="a", delay=0.6),
        _call("read_file", "c2", path="b.py", delay=0.3),
        _call("search_symbols", "c3", query="c", delay=0.4),
    ]
    registry = _SlowRegistry()
    client = _Client([_round(*calls), [TextDelta(text="ok"), _done("end_turn")]])
    started = time.monotonic()
    events = _run(client, registry)
    elapsed = time.monotonic() - started

    assert registry.peak >= 2
    assert elapsed < 1.1  # not the 1.3 s a sequential round needs
    tool_events = [
        e for e in events if isinstance(e, (ToolCallStarted, ToolResultReady))
    ]
    assert [type(e).__name__ for e in tool_events] == ["ToolCallStarted"] * 3 + [
        "ToolResultReady"
    ] * 3
    # Results in the order the model asked, though c1 finished last.
    assert [e.call.id for e in tool_events[3:]] == ["c1", "c2", "c3"]
    tools = [m for m in client.requests[1].messages if m.role == "tool"]
    assert [m.tool_call_id for m in tools] == ["c1", "c2", "c3"]


def test_a_round_with_a_serial_tool_runs_one_call_at_a_time() -> None:
    calls = [
        _call("get_symbol", "c1", qualified_name="a", delay=0.1),
        _call("get_rationale", "c2", qualified_name="b", delay=0.1),
    ]
    registry = _SlowRegistry()
    client = _Client([_round(*calls), [TextDelta(text="ok"), _done("end_turn")]])
    events = _run(client, registry)
    assert registry.peak == 1
    kinds = [
        type(e).__name__
        for e in events
        if isinstance(e, (ToolCallStarted, ToolResultReady))
    ]
    assert kinds == ["ToolCallStarted", "ToolResultReady"] * 2


def test_each_parallel_call_runs_in_a_copy_of_the_turns_context() -> None:
    import contextvars

    marker: contextvars.ContextVar[str] = contextvars.ContextVar(
        "marker", default="unset"
    )
    seen: list[str] = []

    class _Peek(StubRegistry):
        def dispatch(self, name, arguments):
            if name != "get_repo_overview":
                seen.append(marker.get())
            return super().dispatch(name, arguments)

    token = marker.set("bound")
    try:
        client = _Client(
            [
                _round(
                    _call("get_symbol", "c1", qualified_name="a"),
                    _call("list_dir", "c2", path="."),
                ),
                [TextDelta(text="ok"), _done("end_turn")],
            ]
        )
        _run(client, _Peek())
    finally:
        marker.reset(token)
    assert seen == ["bound", "bound"]


def test_a_stop_while_parallel_tools_run_cancels_the_turn() -> None:
    cancel = threading.Event()
    registry = _SlowRegistry()
    calls = [
        _call("get_symbol", "c1", qualified_name="a", delay=1.5),
        _call("get_symbol", "c2", qualified_name="b", delay=1.5),
    ]
    client = _Client([_round(*calls)])
    threading.Timer(0.1, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(TurnCancelled):
        _run(client, registry, cancel=cancel)
    assert time.monotonic() - started < 1.2  # well before the calls finish
    assert len(client.requests) == 1


def test_elide_keeps_is_error() -> None:
    turn = [
        ChatMessage(role="user", content="q"),
        ChatMessage(
            role="tool", content='{"error": "x"}', tool_call_id="c", is_error=True
        ),
        ChatMessage(role="tool", content='{"ok": 1}', tool_call_id="d"),
    ]
    elided = harness._elide(turn)
    assert [m.is_error for m in elided[1:]] == [True, False]
    assert all(m.content == ELIDED_MARKER for m in elided[1:])


def test_final_round_empty_reply_nudged_once() -> None:
    client = ScriptedClient(
        [
            _tool_round("c1", 10),
            [_done("stop")],
            [TextDelta(text="The answer."), _done("stop")],
        ]
    )
    events = _run(client, StubRegistry(), max_tool_rounds=1)
    assert [e for e in events if isinstance(e, EmptyReply)] == [EmptyReply()]
    assert len(client.requests) == 3
    assert [r.tool_choice for r in client.requests[1:]] == ["none", "none"]
    assert client.requests[2].messages[-1].content == EMPTY_REPLY_PROMPT
    assert "The answer." in "".join(e.text for e in events if isinstance(e, TextDelta))


def test_final_round_calls_despite_none_are_dropped_and_nudged() -> None:
    client = ScriptedClient(
        [
            _tool_round("c1", 10),
            _tool_round("c2", 10),
            [TextDelta(text="Done."), _done("stop")],
        ]
    )
    registry = StubRegistry()
    events = _run(client, registry, max_tool_rounds=1)
    assert len(registry.dispatched) == 1  # the final round's call never ran
    assert _kinds(events).count("EmptyReply") == 1
    last = client.requests[2].messages
    assert (last[-1].role, last[-1].content) == ("user", EMPTY_REPLY_PROMPT)
    assert [m.tool_call_id for m in last if m.role == "tool"] == ["c1"]


def test_final_round_truncated_empty_is_not_nudged() -> None:
    client = ScriptedClient([_tool_round("c1", 10), [_done("max_tokens")]])
    events = _run(client, StubRegistry(), max_tool_rounds=1)
    assert len(client.requests) == 2
    assert "EmptyReply" not in _kinds(events)
