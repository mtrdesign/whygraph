"""The agentic loop: messages plus tools in, a stream of events out.

Two pure-ish pieces live here.

:func:`run_turn` is the loop. It streams an assistant turn, forwards text
as it arrives, dispatches any tools the model asked for, appends the
results, and goes round again — until a turn ends without tool calls or the
round bound is hit. It is deliberately **persistence-free**: it takes
messages and yields events, and :mod:`whygraph.serve.chat` owns rows. That
split is what makes the loop testable against a scripted fake client, with
no database in the picture.

:func:`build_window` is the context trimmer. It is a pure function so its
one genuinely subtle invariant — never orphan a tool message — can be
tested directly rather than inferred from integration behaviour.

Event vocabulary
----------------
The port's three events (:class:`~whygraph.services.llm.chat.TextDelta`,
``ToolCallMade``, ``TurnDone``) describe what the *provider* did. The
harness adds three that describe what the *harness* did — a tool started,
a tool finished, the round bound was reached — because the UI shows tool
activity as first-class cards, not as narrated text. ``ToolCallMade`` is
translated into :class:`ToolCallStarted` rather than forwarded, so a
consumer never has to know which layer produced an event.

Usage is reported per provider call: each round's ``TurnDone`` becomes a
:class:`RoundUsage` (yielded as soon as that round's stream ends, before
its tools run), and the one ``TurnDone`` the harness yields last carries
the **turn total**.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from importlib import resources

from whygraph.analyze.prompt import render as render_prompt
from whygraph.core import get_config
from whygraph.core.usage import record_usage
from whygraph.mcp.targets import repo_root
from whygraph.services.llm.chat import (
    ChatClient,
    ChatMessage,
    ChatRequest,
    TextDelta,
    ToolCall,
    ToolCallMade,
    TurnDone,
)

from .tools import ToolRegistry

_log = logging.getLogger(__name__)

_REPO_PLACEHOLDER = "{{REPO}}"
_OVERVIEW_PLACEHOLDER = "{{OVERVIEW}}"

ELIDED_MARKER = "[result elided]"
"""Replaces a stale tool-result body when trimming for the context budget."""

_ELIDE_KEEP_TURNS = 2
"""Tool results in the last N turns keep their bodies; older ones are elided.

Two is enough for the model to still see what it just learned while making
the bulk of the transcript cheap — the same shape as Claude Code's
context-editing behaviour.
"""


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """A tool call is about to run.

    Emitted before dispatch so the UI can show a live "running" card — the
    reason a slow tool (a rationale generation, which can take tens of
    seconds) reads as work in progress rather than a hung stream.

    Attributes
    ----------
    call : ToolCall
        The call being dispatched.
    """

    call: ToolCall


@dataclass(frozen=True, slots=True)
class ToolResultReady:
    """A tool call finished.

    Attributes
    ----------
    call : ToolCall
        The call this answers.
    result : str
        The JSON result string, already truncated by the registry.
    """

    call: ToolCall
    result: str


@dataclass(frozen=True, slots=True)
class RoundLimit:
    """The turn hit ``max_tool_rounds``; an answer round follows.

    Emitted **before** one final model call made with no tools offered, so
    the turn always ends in prose. Earlier this event was terminal on the
    reasoning that a forced no-tools turn only papers over a misbehaving
    loop. Observed behaviour overruled that: the exhausted turns were not
    loops but ordinary broad questions ("what shipped lately") where the
    model had no efficient tool for the job, and the last round's tool
    results are appended to the transcript yet never sent — so the user got
    tool cards, an amber banner, and no answer at all. One extra call is
    cheap next to a turn that cost eight and said nothing.

    Attributes
    ----------
    rounds : int
        The bound that was reached.
    """

    rounds: int


@dataclass(frozen=True, slots=True)
class RoundUsage:
    """One provider call (one round) finished; what it reported.

    Yielded once per :meth:`~whygraph.services.llm.chat.ChatClient.stream_turn`
    - every tool round and the post-limit answer round - right after that
    stream ends and **before** the round's tools are dispatched, so the
    round's :class:`ToolCallStarted` / :class:`ToolResultReady` events follow
    it. The serve layer uses it to close the round's buffer and to attribute
    tokens and the served model per row. Token fields follow
    :class:`~whygraph.services.llm.chat.TurnDone`'s meaning.

    Attributes
    ----------
    input_tokens : int or None
        Every prompt token of the call, cache reads and writes included.
    output_tokens : int or None
        Every completion token, reasoning included.
    cache_read_tokens : int or None
        The cached-read subset of ``input_tokens``.
    cache_write_tokens : int or None
        The cache-write subset of ``input_tokens``.
    reasoning_tokens : int or None
        The reasoning subset of ``output_tokens``.
    model : str or None
        The model the provider served, when it said.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    model: str | None = None


HarnessEvent = (
    TextDelta | ToolCallStarted | ToolResultReady | RoundUsage | RoundLimit | TurnDone
)
"""What :func:`run_turn` yields."""

_TOTALLED_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "cost_usd",
)
"""The :class:`TurnDone` fields summed into the turn total."""


def _round_usage(done: TurnDone) -> RoundUsage:
    """The :class:`RoundUsage` event for one round's ``TurnDone``."""
    return RoundUsage(
        input_tokens=done.input_tokens,
        output_tokens=done.output_tokens,
        cache_read_tokens=done.cache_read_tokens,
        cache_write_tokens=done.cache_write_tokens,
        reasoning_tokens=done.reasoning_tokens,
        model=done.model,
    )


def _turn_total(rounds: Sequence[TurnDone]) -> TurnDone:
    """Sum the rounds' usage into the final ``TurnDone``.

    Each field is the sum of the rounds that reported it, ``None`` only when
    none did. ``finish_reason`` and ``model`` are the last round's (the last
    reported model, for ``model``).
    """
    totals: dict = {}
    for name in _TOTALLED_FIELDS:
        values = [getattr(d, name) for d in rounds if getattr(d, name) is not None]
        totals[name] = sum(values) if values else None
    models = [d.model for d in rounds if d.model]
    return TurnDone(
        finish_reason=rounds[-1].finish_reason if rounds else None,
        model=models[-1] if models else None,
        **totals,
    )


def _packaged_prompt_text() -> str:
    """Read the bundled system-prompt template."""
    return (resources.files("whygraph.chat") / "prompts" / "system.md").read_text(
        encoding="utf-8"
    )


def _overview_text(registry: ToolRegistry) -> str:
    """Render live repo facts for the system prompt, or a fallback line.

    Goes through the registry's own ``get_repo_overview`` dispatch so an
    unscanned repo produces the same ``{"error": ...}`` payload the model
    would see from the tool — one code path, no second failure mode.
    """
    return registry.dispatch("get_repo_overview", {})


def build_system_prompt(registry: ToolRegistry) -> ChatMessage:
    """Render the system prompt with this repository's live facts.

    Parameters
    ----------
    registry : ToolRegistry
        Used to fetch the repo overview through the same dispatch path the
        model uses.

    Returns
    -------
    ChatMessage
        A ``role="system"`` message. Never persisted — it is re-rendered
        per turn so the facts in it cannot go stale.
    """
    template = _packaged_prompt_text()
    text = render_prompt(template, repo_root().name, placeholder=_REPO_PLACEHOLDER)
    return ChatMessage(
        role="system",
        content=render_prompt(
            text, _overview_text(registry), placeholder=_OVERVIEW_PLACEHOLDER
        ),
    )


# ---------------------------------------------------------------------------
# Context window (§5.3)
# ---------------------------------------------------------------------------


def _estimate_tokens(message: ChatMessage) -> int:
    """Approximate a message's token cost as ``len(content) // 4``.

    The standard harness approximation. A real tokenizer would be more
    accurate but buys nothing here: the budget exists to prevent runaway
    growth, and it has enough headroom that a 20% estimation error changes
    no decision. It also keeps this function pure and provider-agnostic —
    the same window is sent to two different tokenizers.
    """
    size = len(message.content) // 4
    for call in message.tool_calls:
        size += (len(call.name) + len(str(call.arguments))) // 4
    return size


def _split_turns(history: Sequence[ChatMessage]) -> list[list[ChatMessage]]:
    """Group ``history`` into turns, each starting at a ``user`` message.

    A "turn" is one user message plus everything that followed it — the
    assistant's replies and every tool call/result pair. Trimming happens
    only at these boundaries because both provider APIs reject a tool
    result whose matching assistant tool-call is missing, so slicing
    mid-turn would produce a request the provider refuses outright.

    Any leading non-user messages (there shouldn't be, but a hand-built
    history could) form their own leading group so nothing is dropped
    silently.
    """
    turns: list[list[ChatMessage]] = []
    for message in history:
        if message.role == "user" or not turns:
            turns.append([message])
        else:
            turns[-1].append(message)
    return turns


def _elide(turn: Sequence[ChatMessage]) -> list[ChatMessage]:
    """Replace tool-result bodies in ``turn`` with :data:`ELIDED_MARKER`.

    Tool results are the bulkiest and least durably valuable part of a
    transcript — once the assistant has summarized what it found, the raw
    JSON is dead weight. Eliding them first buys budget without losing the
    conversation's thread; dropping whole turns loses the thread.
    """
    return [
        ChatMessage(
            role=m.role,
            content=ELIDED_MARKER,
            tool_calls=m.tool_calls,
            tool_call_id=m.tool_call_id,
        )
        if m.role == "tool" and m.content != ELIDED_MARKER
        else m
        for m in turn
    ]


def build_window(
    history: Sequence[ChatMessage],
    *,
    token_budget: int,
    system: ChatMessage | None = None,
) -> tuple[ChatMessage, ...]:
    """Trim ``history`` to fit ``token_budget``, tool-pair-safe.

    The order of operations matters and is deliberate:

    1. The system prompt is **pinned** — never trimmed, never counted
       against the drop decision (a request without it is a different
       assistant).
    2. **Elide before dropping.** Tool-result bodies older than the last
       two turns become :data:`ELIDED_MARKER`.
    3. **Drop whole turns**, oldest first, only if still over budget.
    4. **Never return an empty history.** A single turn that is itself over
       budget is kept — the provider may reject it, which is a clear error,
       whereas an empty message list is a silent one.

    Parameters
    ----------
    history : Sequence[ChatMessage]
        The conversation so far, oldest first, excluding the system prompt.
    token_budget : int
        Approximate ceiling for the returned messages (see
        :func:`_estimate_tokens`).
    system : ChatMessage or None, optional
        The system prompt to pin at the front. ``None`` returns history
        only.

    Returns
    -------
    tuple[ChatMessage, ...]
        ``(system?, *kept_history)`` — ready for :class:`ChatRequest`.
    """
    turns = _split_turns(history)

    def _finish(kept: list[list[ChatMessage]]) -> tuple[ChatMessage, ...]:
        flat = [m for turn in kept for m in turn]
        return (system, *flat) if system is not None else tuple(flat)

    def _total(kept: list[list[ChatMessage]]) -> int:
        return sum(_estimate_tokens(m) for turn in kept for m in turn)

    if _total(turns) <= token_budget:
        return _finish(turns)

    # Step 2 — elide stale tool results, newest _ELIDE_KEEP_TURNS intact.
    cutoff = max(0, len(turns) - _ELIDE_KEEP_TURNS)
    working = [_elide(t) if i < cutoff else t for i, t in enumerate(turns)]
    if _total(working) <= token_budget:
        return _finish(working)

    # Step 3 — drop whole turns, oldest first, keeping at least one.
    while len(working) > 1 and _total(working) > token_budget:
        working.pop(0)
    return _finish(working)


# ---------------------------------------------------------------------------
# The loop (§5.2)
# ---------------------------------------------------------------------------


def run_turn(
    *,
    client: ChatClient,
    history: Sequence[ChatMessage],
    registry: ToolRegistry | None = None,
    max_tool_rounds: int | None = None,
    context_token_budget: int | None = None,
    max_tokens: int | None = None,
) -> Iterator[HarnessEvent]:
    """Run one user turn to completion, yielding harness events.

    The loop: stream a turn → forward its text → if it ended with tool
    calls, dispatch them **sequentially in the order the model asked**,
    append the assistant message and one tool message per result, and
    stream again. Stop when a turn ends with no tool calls, or when
    ``max_tool_rounds`` is reached — then a single :class:`RoundLimit`,
    followed by one last call with **no tools offered** so the turn ends in
    prose rather than a bare cap notice.

    Sequential dispatch rather than concurrent is a real choice: tool
    latency here is dominated by local SQLite reads, so parallelism would
    buy almost nothing while making event ordering nondeterministic and the
    rationale budget racy.

    Parameters
    ----------
    client : ChatClient
        The provider adapter to stream from.
    history : Sequence[ChatMessage]
        Persisted conversation including the just-added user message,
        oldest first, without a system prompt. Windowed internally.
    registry : ToolRegistry, optional
        The turn's tool registry. ``None`` (default) builds a fresh one,
        which is what resets the rationale-generation budget per turn —
        pass an explicit instance only to observe or preset the budget.
    max_tool_rounds : int, optional
        Round bound. ``None`` reads ``[chat].max_tool_rounds``.
    context_token_budget : int, optional
        History budget. ``None`` reads ``[chat].context_token_budget``.
    max_tokens : int, optional
        Per-turn output cap, forwarded to the provider.

    Yields
    ------
    HarnessEvent
        Text deltas, one :class:`RoundUsage` per provider call (each
        before that round's tool start/result pairs), at most one
        :class:`RoundLimit`, and one :class:`TurnDone` last, carrying the
        turn's total usage.

    Raises
    ------
    LlmError
        Propagated from the client. The caller (the serve layer) turns it
        into an in-band SSE ``error`` frame, because HTTP status is already
        committed once streaming starts.
    """
    config = get_config().chat
    if registry is None:
        registry = ToolRegistry()
    if max_tool_rounds is None:
        max_tool_rounds = config.max_tool_rounds
    if context_token_budget is None:
        context_token_budget = config.context_token_budget

    system = build_system_prompt(registry)
    # The working transcript: the windowed history plus whatever this turn
    # appends. Windowing happens once, up front — the messages added during
    # this turn are exactly the ones the model most needs to see, so
    # re-trimming mid-turn could drop a tool result the next round depends
    # on.
    messages: list[ChatMessage] = list(
        build_window(history, token_budget=context_token_budget)
    )

    # One TurnDone per provider call, in order; the turn total is their sum.
    rounds: list[TurnDone] = []
    for round_index in range(max_tool_rounds):
        request = ChatRequest(
            messages=(system, *messages),
            tools=registry.specs,
            max_tokens=max_tokens,
        )

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        done = TurnDone()
        started = time.monotonic()
        for event in client.stream_turn(request):
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
                yield event
            elif isinstance(event, ToolCallMade):
                calls.append(event.call)
            else:  # TurnDone
                done = event

        # The provider call is over: record and report its usage before any
        # of its tools run. This round's TurnDone, never the turn total.
        rounds.append(done)
        record_usage(
            "chat",
            done,
            provider=client.provider,
            model_requested=client.model,
            started=started,
        )
        yield _round_usage(done)

        if not calls:
            yield _turn_total(rounds)
            return

        messages.append(
            ChatMessage(
                role="assistant",
                content="".join(text_parts),
                tool_calls=tuple(calls),
            )
        )
        for call in calls:
            yield ToolCallStarted(call=call)
            result = registry.dispatch(call.name, call.arguments)
            yield ToolResultReady(call=call, result=result)
            messages.append(
                ChatMessage(role="tool", content=result, tool_call_id=call.id)
            )
        _log.debug(
            "chat round %d dispatched %d tool call(s)", round_index + 1, len(calls)
        )

    # Rounds exhausted with the model still asking for tools. Its final round's
    # results are already in `messages` but were never sent anywhere, so ending
    # here ships tool cards and no answer. One more call with no tools offered
    # leaves the model nothing to do but write up what it already gathered.
    _log.info("chat turn hit the %d-round tool limit", max_tool_rounds)
    yield RoundLimit(rounds=max_tool_rounds)
    done = TurnDone()
    started = time.monotonic()
    for event in client.stream_turn(
        ChatRequest(messages=(system, *messages), tools=(), max_tokens=max_tokens)
    ):
        if isinstance(event, TextDelta):
            yield event
        elif isinstance(event, TurnDone):
            done = event
        # A ToolCallMade cannot arrive with `tools=()`; if a provider sends one
        # regardless, dropping it is correct — there is no round left to run it.
    # The answer round is a provider call like any other: report it.
    rounds.append(done)
    record_usage(
        "chat",
        done,
        provider=client.provider,
        model_requested=client.model,
        started=started,
    )
    yield _round_usage(done)
    yield _turn_total(rounds)


__all__ = [
    "ELIDED_MARKER",
    "HarnessEvent",
    "RoundLimit",
    "RoundUsage",
    "ToolCallStarted",
    "ToolResultReady",
    "build_system_prompt",
    "build_window",
    "run_turn",
]
