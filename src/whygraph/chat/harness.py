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
harness adds its own that describe what the *harness* did — a tool started,
a tool finished, the round bound was reached, a cut-off reply is being
continued, an empty reply was nudged, the turn's older tool results were
elided — because the UI shows tool activity as first-class cards and each
way a turn ends as a notice, not as narrated text. ``ToolCallMade`` is
translated into :class:`ToolCallStarted` rather than forwarded, so a
consumer never has to know which layer produced an event.

Usage is reported per provider call: each round's ``TurnDone`` becomes a
:class:`RoundUsage` (yielded as soon as that round's stream ends, before
its tools run), and the one ``TurnDone`` the harness yields last carries
the **turn total**.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import httpx
from importlib import resources

from whygraph.analyze.prompt import render as render_prompt
from whygraph.core import get_config
from whygraph.core.usage import record_usage, usage_blocked
from whygraph.mcp.targets import repo_root
from whygraph.services.llm.chat import (
    REFUSAL_REASONS,
    ChatClient,
    ChatMessage,
    ChatRequest,
    ReasoningDelta,
    TextDelta,
    ToolCall,
    ToolCallMade,
    TurnDone,
)
from whygraph.services.llm.exceptions import UNREACHABLE_CAUSES

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

    Emitted **before** one final model call that keeps the tools on the
    request but forbids calling them (``tool_choice="none"``), so the turn
    always ends in prose. Only when the pending message is tool results: an
    answer round reached after a continuation or a nudge used the last round
    has nothing to report. Earlier this event was terminal on the
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
    finish_reason : str or None
        The round's raw provider stop reason.
    replay : Mapping[str, Any] or None
        The round's provider reasoning payload (see
        :attr:`~whygraph.services.llm.chat.TurnDone.replay`), for the serve
        layer to persist on the round's row.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    model: str | None = None
    finish_reason: str | None = None
    replay: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class BudgetStop:
    """The turn stops: an exhausted hard-stopped budget covers the caller.

    Checked (:func:`~whygraph.core.usage.usage_blocked`) before every
    provider call - each round and the post-limit answer round - so a
    budget exhausted mid-turn ends it before the next call (M2f-2 plan
    section 4.7). The final :class:`TurnDone` follows. The harness stays
    persistence-free: the serve layer writes the budget row.

    Attributes
    ----------
    scope : str
        Which budget is exhausted: ``"org"``, ``"project"`` or ``"member"``.
    """

    scope: str


RETRY_DELAY_SEC = 2.0
"""Pause before the one silent retry of a round whose connection dropped."""

CONTINUE_PROMPT = (
    "Continue exactly where you stopped. Do not repeat anything already written."
)
"""Sent (never shown or stored) after a reply cut off by the output limit."""

EMPTY_REPLY_PROMPT = (
    "You ended your turn without replying. "
    "Answer the question now from what you gathered."
)
"""Sent (never shown or stored) once when a round after tool use says nothing."""


class TurnCancelled(Exception):
    """The turn's ``cancel`` event was set: the user stopped it.

    Raised out of :func:`run_turn` instead of a provider error, because
    stopping a stream can surface as almost any exception in the reader.
    """


@dataclass(frozen=True, slots=True)
class Continuation:
    """A reply was cut off by the output limit; the loop asks for the rest.

    The truncated text stays on screen and the continuation's text follows
    it. At most ``[chat].max_continuations`` per turn.

    Attributes
    ----------
    attempt : int
        Which continuation this is, from 1.
    finish_reason : str or None
        The truncated round's raw stop reason (``max_tokens`` / ``length``).
    """

    attempt: int
    finish_reason: str | None


@dataclass(frozen=True, slots=True)
class EmptyReply:
    """A round after tool use said nothing; the loop asks once for an answer."""


@dataclass(frozen=True, slots=True)
class ContextTrimmed:
    """This turn outgrew the context budget; its older tool results were elided.

    Attributes
    ----------
    rounds_elided : int
        How many of this turn's tool rounds lost their result bodies.
    """

    rounds_elided: int


HarnessEvent = (
    TextDelta
    | ReasoningDelta
    | ToolCallStarted
    | ToolResultReady
    | RoundUsage
    | RoundLimit
    | Continuation
    | EmptyReply
    | ContextTrimmed
    | BudgetStop
    | TurnDone
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
        finish_reason=done.finish_reason,
        replay=done.replay,
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


def build_system_prompt(registry: ToolRegistry) -> tuple[ChatMessage, ChatMessage]:
    """Render the system prompt with this repository's live facts.

    Two messages: the **stable** prompt (everything before the overview,
    with the repository name filled in), which is byte-identical from turn
    to turn so a provider can cache it, then the **overview** of live repo
    facts (counts, scan times), which changes as the repository does.

    Parameters
    ----------
    registry : ToolRegistry
        Used to fetch the repo overview through the same dispatch path the
        model uses.

    Returns
    -------
    tuple[ChatMessage, ChatMessage]
        Two ``role="system"`` messages, stable first. Never persisted —
        they are re-rendered per turn so the facts in them cannot go stale.
    """
    template = _packaged_prompt_text()
    text = render_prompt(template, repo_root().name, placeholder=_REPO_PLACEHOLDER)
    stable, _, _ = text.partition(_OVERVIEW_PLACEHOLDER)
    return (
        ChatMessage(role="system", content=stable.rstrip() + "\n"),
        ChatMessage(role="system", content=_overview_text(registry)),
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


def _check(cancel: threading.Event | None) -> None:
    """Raise :class:`TurnCancelled` when the turn was stopped."""
    if cancel is not None and cancel.is_set():
        raise TurnCancelled


def _truncated(done: TurnDone) -> bool:
    """Whether a round stopped at the output limit."""
    return done.finish_reason in ("max_tokens", "length")


def _last_is_tool_result(messages: Sequence[ChatMessage]) -> bool:
    """Whether the pending message is tool results, i.e. a round limit was hit."""
    return bool(messages) and messages[-1].role == "tool"


def _is_dropped_stream(exc: BaseException) -> bool:
    """Whether ``exc`` is a connection dropped mid-stream, worth one retry.

    Only ``httpx.RemoteProtocolError`` / ``httpx.ReadError`` qualify. A
    timeout would time out again at double the cost, and the SDKs' own
    connection errors were already retried before any response arrived.
    """
    cause: BaseException | None = exc
    seen = 0
    while cause is not None and seen < 10:
        if type(cause).__name__ in UNREACHABLE_CAUSES:
            return False
        if isinstance(cause, (httpx.RemoteProtocolError, httpx.ReadError)):
            return True
        cause = cause.__cause__
        seen += 1
    return False


def _is_error_result(result: str) -> bool:
    """Whether a tool result is an error payload (a JSON object with ``error``)."""
    try:
        value = json.loads(result)
    except ValueError:
        return False  # a truncated result does not decode; not flagged
    return isinstance(value, dict) and "error" in value


def _trim_turn(
    messages: list[ChatMessage],
    turn_start: int,
    latest_round: int,
    tool_rounds: int,
    token_budget: int,
) -> int:
    """Elide this turn's older tool results once it outgrows the budget.

    Only results of this turn's rounds before the latest one lose their
    bodies (to :data:`ELIDED_MARKER`); no message is removed, so no call is
    orphaned. Eliding edits the request's prefix, which invalidates any
    replayed Anthropic thinking, so every assistant message also loses its
    replayed ``content`` (the documented recovery is to strip it).

    Returns
    -------
    int
        The number of tool rounds elided, or ``0`` when nothing new was.
    """
    if tool_rounds < 2:
        return 0
    if sum(_estimate_tokens(m) for m in messages) <= token_budget:
        return 0
    changed = False
    for index in range(turn_start, latest_round):
        message = messages[index]
        if message.role == "tool" and message.content != ELIDED_MARKER:
            messages[index] = replace(message, content=ELIDED_MARKER)
            changed = True
    if not changed:
        return 0
    for index, message in enumerate(messages):
        if (
            message.role == "assistant"
            and message.replay
            and "content" in message.replay
        ):
            kept = {k: v for k, v in message.replay.items() if k != "content"}
            messages[index] = replace(message, replay=kept or None)
    return sum(
        1
        for message in messages[turn_start:latest_round]
        if message.role == "assistant" and message.tool_calls
    )


def run_turn(
    *,
    client: ChatClient,
    history: Sequence[ChatMessage],
    registry: ToolRegistry | None = None,
    max_tool_rounds: int | None = None,
    context_token_budget: int | None = None,
    max_tokens: int | None = None,
    stream_timeout_sec: int | None = None,
    effort: str | None = None,
    cancel: threading.Event | None = None,
    max_continuations: int | None = None,
    continue_from_assistant: bool = False,
) -> Iterator[HarnessEvent]:
    """Run one user turn to completion, yielding harness events.

    The loop: stream a round → forward its text → if it ended with tool
    calls, dispatch them **sequentially in the order the model asked**,
    append the assistant message and one tool message per result, and
    stream again. Every provider call goes through one call site, which is
    also where usage is recorded. A round ends the turn unless one of these
    applies, in order:

    * it asked for tools and rounds remain - another round;
    * it was cut off by the output limit with text - one
      :class:`Continuation` (``max_continuations``), never a retry;
    * it said nothing after this turn ran tools - one :class:`EmptyReply`
      nudge.

    A refusal ends the turn at once. Once ``max_tool_rounds`` calls are used
    the next is the **answer round**: the tools stay on the request with
    ``tool_choice="none"``, preceded by one :class:`RoundLimit` when the
    pending message is tool results, so the turn ends in prose rather than a
    bare cap notice. A connection dropped mid-stream before anything arrived
    is retried once, silently.

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
        Per-call output cap. ``None`` reads ``[chat].max_tokens`` (whose
        own default lets the adapter pick).
    stream_timeout_sec : int, optional
        Per-read stream timeout sent on every request. ``None`` reads
        ``[chat].stream_timeout_sec``.
    effort : str, optional
        Effort level sent on every request, already resolved by the caller
        (the session's level, else ``[chat].effort``). ``None`` sends none.
    cancel : threading.Event, optional
        Set from another thread to stop the turn: checked before every
        round, on every streamed event and before every tool dispatch.
    max_continuations : int, optional
        Continuations after a truncated reply. ``None`` reads
        ``[chat].max_continuations``.
    continue_from_assistant : bool, optional
        The history ends with a reply the output limit cut off, not with a
        user message (the Continue button). :data:`CONTINUE_PROMPT` is
        appended to the working messages before the first round, so the model
        carries on from where it stopped. The prompt is never yielded or
        persisted.

    Yields
    ------
    HarnessEvent
        Text deltas, one :class:`RoundUsage` per provider call (each
        before that round's tool start/result pairs), at most one
        :class:`RoundLimit`, :class:`Continuation`, :class:`EmptyReply`
        and :class:`ContextTrimmed` events as they happen, a
        :class:`BudgetStop` when an exhausted budget ends the turn before
        a provider call, and one :class:`TurnDone` last, carrying the
        turn's total usage.

    Raises
    ------
    LlmError
        Propagated from the client. The caller (the serve layer) turns it
        into an in-band SSE ``error`` frame, because HTTP status is already
        committed once streaming starts.
    TurnCancelled
        When ``cancel`` is set; anything raised while it is set becomes
        this, because stopping a stream can surface as any error.
    """
    config = get_config().chat
    if registry is None:
        registry = ToolRegistry()
    if max_tool_rounds is None:
        max_tool_rounds = config.max_tool_rounds
    if context_token_budget is None:
        context_token_budget = config.context_token_budget
    if stream_timeout_sec is None:
        stream_timeout_sec = config.stream_timeout_sec
    if max_tokens is None:
        max_tokens = config.max_tokens
    if max_continuations is None:
        max_continuations = config.max_continuations

    system = build_system_prompt(registry)
    # The working transcript: the windowed history plus whatever this turn
    # appends. Windowing between turns happens once, up front; inside the
    # turn only stale tool results are elided (`_trim_turn`), never a
    # message, because the next round may depend on any of them.
    messages: list[ChatMessage] = list(
        build_window(history, token_budget=context_token_budget)
    )
    turn_start = len(messages)
    if continue_from_assistant:
        messages.append(ChatMessage(role="user", content=CONTINUE_PROMPT))

    # One TurnDone per provider call, in order; the turn total is their sum.
    rounds: list[TurnDone] = []
    rounds_used = continuations = tool_rounds = 0
    nudged = trimmed = False
    latest_round = turn_start
    while True:
        _check(cancel)
        final = rounds_used >= max_tool_rounds
        if (blocked := usage_blocked()) is not None:
            yield BudgetStop(scope=blocked)
            break
        if final and _last_is_tool_result(messages):
            # Rounds exhausted with the model still asking for tools: their
            # results were never sent, so ending here would ship tool cards
            # and no answer.
            _log.info("chat turn hit the %d-round tool limit", max_tool_rounds)
            yield RoundLimit(rounds=max_tool_rounds)
        request = ChatRequest(
            messages=(*system, *messages),
            # Always the same tools: a request carrying tool history must
            # never drop them, and a changed tool set invalidates replayed
            # thinking. The answer round only forbids calling them.
            tools=registry.specs,
            tool_choice="none" if final else "auto",
            max_tokens=max_tokens,
            timeout_sec=stream_timeout_sec,
            effort=effort,
        )
        for attempt in range(2):
            text_parts: list[str] = []
            calls: list[ToolCall] = []
            reasoned = False
            done = TurnDone()
            started = time.monotonic()
            stream = client.stream_turn(request)
            try:
                for event in stream:
                    _check(cancel)
                    if isinstance(event, TextDelta):
                        text_parts.append(event.text)
                        yield event
                    elif isinstance(event, ReasoningDelta):
                        reasoned = True
                        yield event
                    elif isinstance(event, ToolCallMade):
                        calls.append(event.call)
                    else:  # TurnDone
                        done = event
                break
            except TurnCancelled:
                raise
            except Exception as exc:
                if cancel is not None and cancel.is_set():
                    raise TurnCancelled from exc
                if (
                    attempt
                    or text_parts
                    or calls
                    or reasoned
                    or not _is_dropped_stream(exc)
                ):
                    raise
                _log.warning(
                    "chat round retry after %s (request id %s)",
                    type(exc).__name__,
                    getattr(exc, "request_id", None),
                )
                if usage_blocked() is not None:
                    raise
                if cancel is not None:
                    if cancel.wait(RETRY_DELAY_SEC):
                        raise TurnCancelled from exc
                else:
                    time.sleep(RETRY_DELAY_SEC)
            finally:
                close = getattr(stream, "close", None)
                if close is not None:
                    close()

        # The provider call is over: record and report its usage before any
        # of its tools run. This round's TurnDone, never the turn total.
        rounds_used += 1
        rounds.append(done)
        record_usage(
            "chat",
            done,
            provider=client.provider,
            model_requested=client.model,
            started=started,
        )
        yield _round_usage(done)

        text = "".join(text_parts)
        if done.finish_reason in REFUSAL_REASONS:
            break
        if calls and not final:
            tool_rounds += 1
            latest_round = len(messages)
            messages.append(
                ChatMessage(
                    role="assistant",
                    content=text,
                    tool_calls=tuple(calls),
                    # Round two must carry round one's reasoning: the
                    # providers that reason across tool calls expect it.
                    replay=done.replay,
                )
            )
            for call in calls:
                _check(cancel)
                yield ToolCallStarted(call=call)
                result = registry.dispatch(call.name, call.arguments)
                yield ToolResultReady(call=call, result=result)
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=result,
                        tool_call_id=call.id,
                        is_error=_is_error_result(result),
                    )
                )
            _log.debug(
                "chat round %d dispatched %d tool call(s)", rounds_used, len(calls)
            )
            elided = _trim_turn(
                messages, turn_start, latest_round, tool_rounds, context_token_budget
            )
            if elided and not trimmed:
                trimmed = True
                yield ContextTrimmed(rounds_elided=elided)
            continue
        if final:
            # The answer round never continues or nudges; a call it made
            # anyway is dropped - there is no round left to run it.
            break
        if _truncated(done) and text and continuations < max_continuations:
            continuations += 1
            messages.append(
                ChatMessage(role="assistant", content=text, replay=done.replay)
            )
            messages.append(ChatMessage(role="user", content=CONTINUE_PROMPT))
            yield Continuation(attempt=continuations, finish_reason=done.finish_reason)
            continue
        if not text and tool_rounds and not nudged and not _truncated(done):
            nudged = True
            messages.append(ChatMessage(role="user", content=EMPTY_REPLY_PROMPT))
            yield EmptyReply()
            continue
        break
    yield _turn_total(rounds)


__all__ = [
    "BudgetStop",
    "CONTINUE_PROMPT",
    "ContextTrimmed",
    "Continuation",
    "ELIDED_MARKER",
    "EMPTY_REPLY_PROMPT",
    "EmptyReply",
    "HarnessEvent",
    "RoundLimit",
    "RoundUsage",
    "ToolCallStarted",
    "ToolResultReady",
    "TurnCancelled",
    "build_system_prompt",
    "build_window",
    "run_turn",
]
