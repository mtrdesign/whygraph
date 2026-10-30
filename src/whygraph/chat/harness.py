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

import contextvars
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import httpx
from importlib import resources

from whygraph.analyze.prompt import render as render_prompt
from whygraph.core import get_config
from whygraph.core.usage import (
    CallOutcome,
    count_agent_call,
    record_usage,
    usage_blocked,
)
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
    ToolSpec,
    TurnDone,
)
from whygraph.services.llm.exceptions import UNREACHABLE_CAUSES

from .grounding import with_references
from .modes import Mode, get_mode, mode_block
from .tools import SERIAL_TOOLS, ToolRegistry

_log = logging.getLogger(__name__)

_REPO_PLACEHOLDER = "{{REPO}}"
_OVERVIEW_PLACEHOLDER = "{{OVERVIEW}}"
_MODE_PLACEHOLDER = "{{MODE}}"

ELIDED_MARKER = "[result elided]"
"""Replaces a stale tool-result body when trimming for the context budget."""

REPEAT_CALL_LIMIT = 3
"""From this many identical tool calls in one turn on, the result is annotated.

Identical means the same tool name and the same arguments (key order does
not matter). The call is still dispatched - a lazy backfill can change a
result within a turn - but the model is told it is repeating itself.
"""

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


def build_system_prompt(
    registry: ToolRegistry, mode: Mode | None = None
) -> tuple[ChatMessage, ChatMessage]:
    """Render the system prompt with this repository's live facts.

    Two messages: the **stable** prompt (everything before the overview,
    with the repository name and the mode's block filled in), which is
    byte-identical from turn to turn within a mode so a provider can cache
    it, then the **overview** of live repo facts (counts, scan times), which
    changes as the repository does.

    Parameters
    ----------
    registry : ToolRegistry
        Used to fetch the repo overview through the same dispatch path the
        model uses.
    mode : Mode, optional
        The chat mode whose block fills ``{{MODE}}``. ``None`` (default)
        uses the default mode.

    Returns
    -------
    tuple[ChatMessage, ChatMessage]
        Two ``role="system"`` messages, stable first. Never persisted —
        they are re-rendered per turn so the facts in them cannot go stale.
    """
    template = _packaged_prompt_text()
    text = render_prompt(template, repo_root().name, placeholder=_REPO_PLACEHOLDER)
    text = render_prompt(
        text, mode_block(mode or get_mode(None)), placeholder=_MODE_PLACEHOLDER
    )
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


def _specs_tokens(specs: Sequence[ToolSpec]) -> int:
    """Estimate the tokens the tool specs add to every request.

    ``len(json.dumps(...)) // 4`` over each spec's name, description and
    parameters, the same approximation as :func:`_estimate_tokens`. Pure and
    uncached (``ToolSpec`` holds a ``dict``, so a spec tuple is unhashable).
    """
    return (
        len(
            json.dumps(
                [
                    {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.parameters,
                    }
                    for spec in specs
                ]
            )
        )
        // 4
    )


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
            is_error=m.is_error,
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


PARALLEL_TOOL_WORKERS = 4
"""Most tool calls of one round that run at the same time."""

_PARALLEL_POLL_SEC = 0.1
"""How often a wait on a parallel tool call checks for a Stop."""


def _timed_dispatch(
    registry: ToolRegistry, call: ToolCall
) -> tuple[str, CallOutcome, int]:
    """Run one tool call; return its result, outcome and duration in ms.

    Runs where the call runs (the worker thread of a parallel round), so a
    pooled call is timed on its own and never by the wait for its siblings.
    """
    started = time.monotonic()
    result, outcome = registry.dispatch_outcome(call.name, call.arguments)
    return result, outcome, round((time.monotonic() - started) * 1000)


def _dispatch_parallel(
    registry: ToolRegistry,
    calls: Sequence[ToolCall],
    cancel: threading.Event | None,
) -> Iterator[tuple[str, CallOutcome, int]]:
    """Run ``calls`` concurrently; yield their results in submission order.

    Each item is :func:`_timed_dispatch`'s ``(result, outcome, duration_ms)``.

    Each call runs in its own copy of the current context, so the bound
    project, strict mode and the usage sink reach it. A Stop while waiting
    raises :class:`TurnCancelled` and cancels the calls not yet started (a
    running one finishes on its own, as a sequential one would).
    """
    executor = ThreadPoolExecutor(
        max_workers=PARALLEL_TOOL_WORKERS, thread_name_prefix="chat-tool"
    )
    try:
        futures = [
            executor.submit(
                contextvars.copy_context().run,
                _timed_dispatch,
                registry,
                call,
            )
            for call in calls
        ]
        for future in futures:
            while True:
                _check(cancel)
                try:
                    result = future.result(timeout=_PARALLEL_POLL_SEC)
                except TimeoutError:
                    continue
                break
            yield result
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


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


def is_error_result(result: str) -> bool:
    """Whether a tool result is an error payload (a JSON object with ``error``)."""
    try:
        value = json.loads(result)
    except ValueError:
        return False  # a truncated result does not decode; not flagged
    return isinstance(value, dict) and "error" in value


def _note_repeat(result: str, count: int) -> str:
    """Annotate the result of a call repeated ``count`` times in this turn.

    A JSON object gains a ``note`` key (``repeat_note`` when it already has
    one); any other result is prefixed with the same sentence in brackets.
    """
    text = f"identical call repeated {count} times in this turn"
    try:
        value = json.loads(result)
    except ValueError:
        value = None
    if isinstance(value, dict):
        value["repeat_note" if "note" in value else "note"] = text
        return json.dumps(value)
    return f"[{text}] {result}"


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


def begin_turn_messages(
    history: Sequence[ChatMessage],
    *,
    token_budget: int,
    references: str | None = None,
    continue_from_assistant: bool = False,
) -> tuple[list[ChatMessage], int]:
    """Build the working transcript a turn's first request starts from.

    The pure part of :func:`run_turn` that decides what a request carries
    besides the system prompt and tools: the windowed history
    (:func:`build_window`), the question's References block, and the
    continue prompt. A round's request is these messages (plus whatever the
    turn appended, trimmed by :func:`_trim_turn`) behind the system
    messages; the offline replay of ``scripts/eval_tool_choice.py`` calls
    this and :func:`_trim_turn` so a change to either reaches it.

    Parameters
    ----------
    history : Sequence[ChatMessage]
        The conversation so far, ending with the user's question.
    token_budget : int
        The history token budget handed to :func:`build_window`.
    references : str or None, optional
        The question's References block, appended to the last user message.
    continue_from_assistant : bool, optional
        Whether the turn continues a truncated reply.

    Returns
    -------
    tuple[list[ChatMessage], int]
        The transcript (a new list) and ``turn_start``, the index where this
        turn's own messages begin.
    """
    messages: list[ChatMessage] = list(build_window(history, token_budget=token_budget))
    turn_start = len(messages)
    if references:
        last_user = next(
            (i for i in range(turn_start - 1, -1, -1) if messages[i].role == "user"),
            None,
        )
        if last_user is not None:
            question = messages[last_user]
            messages[last_user] = replace(
                question, content=with_references(question.content, references)
            )
    if continue_from_assistant:
        messages.append(ChatMessage(role="user", content=CONTINUE_PROMPT))
    return messages, turn_start


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
    mode: str | None = None,
    references: str | None = None,
) -> Iterator[HarnessEvent]:
    """Run one user turn to completion, yielding harness events.

    The loop: stream a round → forward its text → if it ended with tool
    calls, dispatch them (results **in the order the model asked**),
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

    A call repeated identically (same tool, same arguments) :data:`REPEAT_CALL_LIMIT`
    times or more in the turn is still dispatched, but its result carries a
    note saying how many times it has been made.

    A round whose calls are all plain reads (none in
    :data:`~whygraph.chat.tools.SERIAL_TOOLS`) runs them concurrently, each
    in its own context copy: every :class:`ToolCallStarted` first, then the
    results in the model's order, so events and rows stay deterministic. A
    round with a serial tool (rationale generation, lazy backfill, the chart
    tools) runs one call at a time, because those share the turn's budget
    or the registry's chart refs.

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
    mode : str, optional
        The chat mode id, already resolved by the caller (the session's
        mode). It changes only the system prompt's mode block, never the
        tools sent. ``None`` or an unknown id runs the default mode.
    references : str, optional
        This turn's References block (the list from
        :func:`~whygraph.chat.grounding.references_suffix`), appended once,
        before the first round, to the last user message of ``history``
        with :func:`~whygraph.chat.grounding.with_references`. Never to an
        in-turn continue or empty-reply prompt. Past turns' blocks are
        already in ``history`` (the caller replays them), so the caller
        passes ``None`` on a Continue or a Regenerate.

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

    system = build_system_prompt(registry, get_mode(mode))
    # The working transcript: the windowed history plus whatever this turn
    # appends. Windowing between turns happens once, up front; inside the
    # turn only stale tool results are elided (`_trim_turn`), never a
    # message, because the next round may depend on any of them.
    messages, turn_start = begin_turn_messages(
        history,
        token_budget=context_token_budget,
        references=references,
        continue_from_assistant=continue_from_assistant,
    )

    # One TurnDone per provider call, in order; the turn total is their sum.
    rounds: list[TurnDone] = []
    rounds_used = continuations = tool_rounds = 0
    seen_calls: Counter[tuple[str, str]] = Counter()
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
            tool_names = {spec.name for spec in registry.specs}
            parallel = None
            if len(calls) > 1 and not any(c.name in SERIAL_TOOLS for c in calls):
                # Every call is a read with its own handle: run them at once,
                # but report results in the order the model asked for them.
                _check(cancel)
                for call in calls:
                    yield ToolCallStarted(call=call)
                parallel = _dispatch_parallel(registry, calls, cancel)
            for call in calls:
                if parallel is None:
                    _check(cancel)
                    yield ToolCallStarted(call=call)
                    result, outcome, duration_ms = _timed_dispatch(registry, call)
                else:
                    result, outcome, duration_ms = next(parallel)
                if call.name in tool_names:
                    # Names only; the harness's own overview call never gets here.
                    count_agent_call(
                        call.name, outcome=outcome, duration_ms=duration_ms
                    )
                key = (
                    call.name,
                    json.dumps(call.arguments, sort_keys=True, separators=(",", ":")),
                )
                seen_calls[key] += 1
                if seen_calls[key] >= REPEAT_CALL_LIMIT:
                    result = _note_repeat(result, seen_calls[key])
                yield ToolResultReady(call=call, result=result)
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=result,
                        tool_call_id=call.id,
                        is_error=is_error_result(result),
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
            if not text and tool_rounds and not nudged and not _truncated(done):
                # The answer round came back empty (with or without calls it
                # made despite tool_choice="none", which are dropped): one
                # nudge, and the next round is still the final one.
                nudged = True
                messages.append(ChatMessage(role="user", content=EMPTY_REPLY_PROMPT))
                yield EmptyReply()
                continue
            # Otherwise the answer round never continues; a call it made
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
