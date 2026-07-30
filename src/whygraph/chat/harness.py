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
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from importlib import resources

from whygraph.analyze.prompt import render as render_prompt
from whygraph.core import get_config
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


HarnessEvent = TextDelta | ToolCallStarted | ToolResultReady | RoundLimit | TurnDone
"""What :func:`run_turn` yields."""


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
        Text deltas, tool start/result pairs, at most one
        :class:`RoundLimit`, and one :class:`TurnDone` last.

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

    last_done = TurnDone()
    for round_index in range(max_tool_rounds):
        request = ChatRequest(
            messages=(system, *messages),
            tools=registry.specs,
            max_tokens=max_tokens,
        )

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for event in client.stream_turn(request):
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
                yield event
            elif isinstance(event, ToolCallMade):
                calls.append(event.call)
            else:  # TurnDone
                last_done = event

        if not calls:
            yield last_done
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
    for event in client.stream_turn(
        ChatRequest(messages=(system, *messages), tools=(), max_tokens=max_tokens)
    ):
        if isinstance(event, TextDelta):
            yield event
        elif isinstance(event, TurnDone):
            last_done = event
        # A ToolCallMade cannot arrive with `tools=()`; if a provider sends one
        # regardless, dropping it is correct — there is no round left to run it.
    yield last_done


__all__ = [
    "ELIDED_MARKER",
    "HarnessEvent",
    "RoundLimit",
    "ToolCallStarted",
    "ToolResultReady",
    "build_system_prompt",
    "build_window",
    "run_turn",
]
