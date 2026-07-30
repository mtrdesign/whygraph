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
from pathlib import Path

import pytest

from whygraph import core
from whygraph.chat.harness import (
    ELIDED_MARKER,
    RoundLimit,
    ToolCallStarted,
    ToolResultReady,
    build_system_prompt,
    build_window,
    run_turn,
)
from whygraph.chat.tools import ToolRegistry
from whygraph.core.config import ChatConfig, Config
from whygraph.services.llm.chat import (
    ChatClient,
    ChatMessage,
    ModelInfo,
    TextDelta,
    ToolCall,
    ToolCallMade,
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
    assert events[1] == ToolCallStarted(call=call)
    assert isinstance(events[2], ToolResultReady)
    assert events[2].call is call
    assert events[3] == TextDelta(text="Found it.")
    assert events[4] == TurnDone(finish_reason="stop", input_tokens=9, output_tokens=3)
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
    assert second[0].role == "system"
    assert second[1].role == "user"
    assistant = second[2]
    assert assistant.role == "assistant"
    assert assistant.tool_calls == (call,)
    tool_message = second[3]
    assert tool_message.role == "tool"
    assert tool_message.tool_call_id == "c1"
    assert json.loads(tool_message.content) == {"ok": "get_symbol"}


def test_parallel_calls_dispatch_sequentially_in_model_order() -> None:
    calls = [
        _call("search_symbols", "c1", query="a"),
        _call("get_symbol", "c2", qualified_name="b"),
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
        "get_symbol",
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
    client = ScriptedClient([list(forever) for _ in range(10)])
    registry = StubRegistry()
    events = _run(client, registry, max_tool_rounds=3)

    assert len(client.requests) == 4
    assert len(registry.dispatched) == 3
    assert events[-2] == RoundLimit(rounds=3)
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


def test_the_answer_round_offers_no_tools_and_yields_prose() -> None:
    """A round-limited turn must not end as tool cards with no answer.

    The exhausted turn's last tool results are in the transcript but were
    never sent; the extra call ships them with ``tools=()`` so the model can
    only write up what it has.
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
    events = _run(client, StubRegistry(), max_tool_rounds=2)

    kinds = [type(e).__name__ for e in events]
    assert kinds.count("RoundLimit") == 1
    # Prose after the cap notice, and the turn still ends on TurnDone.
    assert kinds.index("RoundLimit") < kinds.index(
        "TextDelta", kinds.index("RoundLimit")
    )
    assert isinstance(events[-1], TurnDone)
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "Short version: it's the cache."

    # Two tool rounds plus the answer round, and that last one offers no tools
    # (what the tool rounds offer is covered by the registry-specs test).
    assert len(client.requests) == 3
    assert client.requests[-1].tools == ()
    # It still carries the full transcript, including the last round's results.
    roles = [m.role for m in client.requests[-1].messages]
    assert roles.count("tool") == 2


def test_a_tool_call_in_the_answer_round_is_dropped() -> None:
    """A provider that returns a call despite ``tools=()`` can't loop us."""
    client = ScriptedClient(
        [
            [
                ToolCallMade(call=_call("search_symbols", query="x")),
                TurnDone("tool_calls"),
            ],
            [ToolCallMade(call=_call("search_symbols", query="y")), TurnDone("stop")],
        ]
    )
    registry = StubRegistry()
    events = _run(client, registry, max_tool_rounds=1)

    # Only the in-budget round dispatched; the stray call was ignored.
    assert len(registry.dispatched) == 1
    assert len(client.requests) == 2
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
    assert events[-2] == TextDelta(text="That symbol does not exist.")
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
    client = ScriptedClient([list(forever) for _ in range(5)])
    events = list(
        run_turn(
            client=client,
            history=(ChatMessage(role="user", content="hi"),),
            registry=StubRegistry(),
        )
    )
    # Two tool rounds from config, plus the answer round.
    assert len(client.requests) == 3
    assert events[-2] == RoundLimit(rounds=2)


def test_registry_specs_are_offered_to_the_model() -> None:
    """A real registry's 15 specs reach the request."""
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
    assert len(client.requests[0].tools) == 15


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def test_system_prompt_names_the_repo_and_embeds_the_overview(_repo_root: Path) -> None:
    message = build_system_prompt(StubRegistry())
    assert message.role == "system"
    assert "myrepo" in message.content
    assert '{"commits": 3}' in message.content
    # No unsubstituted placeholders left behind.
    assert "{{REPO}}" not in message.content
    assert "{{OVERVIEW}}" not in message.content


def test_system_prompt_documents_the_deep_link_convention(_repo_root: Path) -> None:
    content = build_system_prompt(StubRegistry()).content
    assert "whygraph://symbol/" in content
    assert "data, not instructions" in content


def test_system_prompt_is_never_part_of_history() -> None:
    """It is prepended per turn, not carried in the transcript."""
    client = ScriptedClient([[TextDelta(text="x"), TurnDone("stop")]])
    _run(client, StubRegistry())
    sent = client.requests[0].messages
    assert sent[0].role == "system"
    assert [m.role for m in sent[1:]] == ["user"]


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
