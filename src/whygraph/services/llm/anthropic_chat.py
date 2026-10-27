"""Streaming, tool-calling chat adapter for the ``anthropic`` SDK.

Anthropic's Messages API differs from the chat-completions shape in four
ways this adapter absorbs, so the harness sees one uniform event stream:

1. **System prompts are a parameter, not a message.** Every
   ``role="system"`` :class:`ChatMessage` is pulled out and joined into
   the ``system=`` argument (same as the non-streaming adapter).
2. **Tool schemas use ``input_schema``**, not ``parameters``.
3. **Tool calls are content blocks.** An assistant turn that calls tools
   carries ``tool_use`` blocks alongside its ``text`` blocks, and results
   come back as ``tool_result`` blocks inside a **``user``** message —
   there is no ``role="tool"``.
4. **``max_tokens`` is required**, so a request that omits it gets
   :data:`DEFAULT_MAX_TOKENS` (32000: thinking counts against it, so the
   old 4096 could end a round with thinking and nothing else).

Tool-call arguments arrive as ``input_json_delta`` fragments attached to a
known block index, and the SDK signals block completion explicitly
(``content_block_stop``). Calls are still emitted only **at the end of the
message**, once the stop reason is known: a ``max_tokens`` stop cuts the
last block off mid-input, and a refusal can cut a ``tool_use`` short, and
neither may reach the dispatcher. The harness dispatches only after the
stream ends, so nothing visible changes.

Three contracts shape what is sent back within a turn:

* **Preserved thinking.** Every finished content block (thinking with its
  signature, text, tool use) is captured from the SDK's accumulated block
  into :attr:`TurnDone.replay` and the next round sends it back verbatim.
  The blocks are bound to the exact request prefix, so they are never
  persisted across turns and the harness strips them when it trims.
* **Prompt caching.** Exactly three ``cache_control`` breakpoints: the last
  tool, the stable system block, and the last block of the last message.
* **Thinking.** ``thinking={"type": "adaptive", "display": "summarized"}``;
  its ``thinking_delta``s stream as :class:`ReasoningDelta`. A model that
  refuses the parameter is asked once more without it and remembered.
* **Effort.** ``output_config.effort``; a 400 that names it (and is not a
  thinking-signature error) becomes ``LlmError(code="effort_unsupported")``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import anthropic
import httpx

from .chat import (
    REFUSAL_REASONS,
    ChatClient,
    ChatMessage,
    ChatRequest,
    ChatStreamEvent,
    ModelInfo,
    ReasoningDelta,
    TextDelta,
    ToolCall,
    ToolCallMade,
    ToolSpec,
    TurnDone,
)
from ._abort import shutdown_stream
from .exceptions import LlmError, LlmKeyMissing
from .usage import ANTHROPIC_USAGE_KEYS, anthropic_raw_usage, anthropic_usage_fields

_log = logging.getLogger(__name__)

_ENV_VAR = "ANTHROPIC_API_KEY"

DEFAULT_MAX_TOKENS = 32_000
"""Output cap when a request sets none. Thinking counts against it."""

_CACHE = {"type": "ephemeral"}
"""The ``cache_control`` value every breakpoint carries."""

_NO_THINKING: set[str] = set()
"""Models that refused adaptive thinking in this process (sent without it)."""

_THINKING_UNSUPPORTED = re.compile(r"not supported|unsupported|budget_tokens")
_NOT_THINKING_ERROR = re.compile(r"signature|block_binding")
"""A 400 about the ``thinking`` parameter itself, never about replayed blocks."""

_EFFORT_ERROR = re.compile(r"\b(effort|output_config)\b")
_NOT_EFFORT_ERROR = re.compile(r"signature|block_binding|thinking\.type")
"""A 400 naming the effort parameter, unless it is about replayed thinking."""


@dataclass(slots=True)
class _BlockAccumulator:
    """Scratch space for one streaming ``tool_use`` content block.

    Attributes
    ----------
    call_id : str
        The block's ``id`` — announced up front in ``content_block_start``.
    name : str
        Tool name, also announced up front.
    json_parts : list[str]
        ``partial_json`` fragments in arrival order, joined on flush.
    """

    call_id: str = ""
    name: str = ""
    json_parts: list[str] = field(default_factory=list)

    def flush(self) -> ToolCall:
        """Materialize the block into a :class:`ToolCall`.

        Raises
        ------
        LlmError
            If the joined fragments are not a JSON object.
        """
        raw = "".join(self.json_parts).strip()
        if not raw:
            parsed: object = {}
        else:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise LlmError(
                    f"anthropic stream: malformed input for tool "
                    f"{self.name or '<unnamed>'}: {raw[:200]!r}"
                ) from exc
        if not isinstance(parsed, dict):
            raise LlmError(
                f"anthropic stream: tool {self.name or '<unnamed>'} input "
                f"must be a JSON object, got {type(parsed).__name__}"
            )
        return ToolCall(id=self.call_id, name=self.name, arguments=parsed)


def _tool_payload(tools: tuple[ToolSpec, ...]) -> list[dict]:
    """Render :class:`ToolSpec`s into Anthropic's ``tools`` array.

    The last tool carries a cache breakpoint: the tool set is a fixed module
    tuple, so the whole array is a stable prefix.
    """
    payload = [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.parameters,
        }
        for spec in tools
    ]
    if payload:
        payload[-1]["cache_control"] = dict(_CACHE)
    return payload


def _system_payload(parts: list[str]) -> list[dict]:
    """Render the system messages as text blocks, the first one cached.

    The harness sends the stable prompt first and the volatile repository
    overview second, so the breakpoint sits after the part that never
    changes. Empty parts are skipped (the API refuses empty text blocks).
    """
    blocks = [{"type": "text", "text": part} for part in parts if part]
    if blocks:
        blocks[0]["cache_control"] = dict(_CACHE)
    return blocks


def _cache_last_block(messages: list[dict]) -> None:
    """Put the third breakpoint on the last block of the last message.

    A plain-string message becomes a one-block list so the block can carry
    it. Blocks are copies (see :func:`_message_payload`), so a replayed
    block never keeps a breakpoint into the next round.
    """
    if not messages:
        return
    last = messages[-1]
    content = last["content"]
    if isinstance(content, str):
        last["content"] = [
            {"type": "text", "text": content, "cache_control": dict(_CACHE)}
        ]
    elif content:
        content[-1]["cache_control"] = dict(_CACHE)


def _replay_block(block: Any) -> dict | None:
    """One SDK content block as the dict sent back verbatim, or ``None``.

    Built explicitly per type, so nothing the SDK adds (``citations``,
    ``parsed_output``, ...) leaks into the request.
    """
    kind = getattr(block, "type", None)
    if kind == "text":
        return {"type": "text", "text": getattr(block, "text", "")}
    if kind == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", ""),
            "input": getattr(block, "input", None) or {},
        }
    if kind == "thinking":
        # Keep the block even when its text is empty: the signature is what
        # the API checks.
        return {
            "type": "thinking",
            "thinking": getattr(block, "thinking", "") or "",
            "signature": getattr(block, "signature", "") or "",
        }
    if kind == "redacted_thinking":
        return {"type": "redacted_thinking", "data": getattr(block, "data", "")}
    return None


def _message_payload(messages: tuple[ChatMessage, ...]) -> list[dict]:
    """Render non-system :class:`ChatMessage`s into Anthropic messages.

    Assistant turns with tool calls become ``text`` + ``tool_use`` block
    lists. Consecutive ``role="tool"`` messages are **merged into one
    ``user`` message** of ``tool_result`` blocks — the shape Anthropic
    requires when a turn made several parallel calls.
    """
    payload: list[dict] = []
    for message in messages:
        if message.role == "system":
            continue

        if message.role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": message.content,
            }
            if message.is_error:
                block["is_error"] = True
            # Fold into the preceding user block-list when that message
            # is itself a run of tool results, so parallel calls answer
            # in a single user turn.
            if (
                payload
                and payload[-1]["role"] == "user"
                and isinstance(payload[-1]["content"], list)
                and payload[-1]["content"]
                and payload[-1]["content"][0].get("type") == "tool_result"
            ):
                payload[-1]["content"].append(block)
            else:
                payload.append({"role": "user", "content": [block]})
            continue

        if message.role == "assistant" and not (
            message.content
            or message.tool_calls
            or (message.replay or {}).get("content")
        ):
            # Anthropic rejects an empty non-final assistant message, so one
            # such row would fail every later turn of the session. The
            # history sanitizer drops them first; this is defence in depth.
            continue

        replayed = (message.replay or {}).get("content")
        if message.role == "assistant" and replayed:
            # Within a turn: the round's own blocks, thinking and signature
            # included, exactly as they arrived. Copied, so adding a cache
            # breakpoint never edits the harness's message.
            payload.append(
                {"role": "assistant", "content": [dict(b) for b in replayed]}
            )
            continue

        if message.role == "assistant" and message.tool_calls:
            blocks: list[dict] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                }
                for call in message.tool_calls
            )
            payload.append({"role": "assistant", "content": blocks})
            continue

        payload.append({"role": message.role, "content": message.content})
    return payload


def _finished_calls(
    completed: list[tuple[int, _BlockAccumulator]],
    finish_reason: str | None,
    last_index: int | None,
) -> Iterator[ToolCallMade]:
    """Emit the message's tool calls once its stop reason is known.

    A refusal dispatches nothing. On ``max_tokens`` the message's last block
    was cut off, so a ``tool_use`` there is dropped, as is any other call
    whose input does not parse; with any other stop reason malformed input
    still raises :class:`LlmError`.
    """
    if finish_reason in REFUSAL_REASONS:
        return
    truncated = finish_reason == "max_tokens"
    for index, accumulator in completed:
        if truncated and index == last_index:
            _log.info(
                "anthropic: dropped tool call %s cut off by max_tokens",
                accumulator.name,
            )
            continue
        try:
            call = accumulator.flush()
        except LlmError:
            if not truncated:
                raise
            _log.info("anthropic: dropped malformed tool call %s", accumulator.name)
            continue
        yield ToolCallMade(call=call)


def _thinking_unsupported(exc: Exception) -> bool:
    """Whether a 400 says this model takes no adaptive ``thinking`` parameter.

    Narrow on purpose: the message must name ``thinking`` and say it is not
    supported (or ask for ``budget_tokens``, an older model's form), and
    must not be about a replayed block's signature.
    """
    if not isinstance(exc, anthropic.BadRequestError):
        return False
    message = str(exc)
    return (
        "thinking" in message
        and bool(_THINKING_UNSUPPORTED.search(message))
        and not _NOT_THINKING_ERROR.search(message)
    )


def _effort_code(exc: Exception, request: ChatRequest) -> str | None:
    """``"effort_unsupported"`` when a 400 rejects the effort parameter.

    Narrow on purpose: only with an effort on the request, only a
    ``BadRequestError`` whose message names ``effort`` / ``output_config``,
    and never one about replayed thinking (``signature``,
    ``block_binding``, ``thinking.type``), which a broad match would
    mislabel. A model without the parameter (Claude Haiku 4.5) lands here.
    """
    if not request.effort or not isinstance(exc, anthropic.BadRequestError):
        return None
    message = str(exc)
    if _EFFORT_ERROR.search(message) and not _NOT_EFFORT_ERROR.search(message):
        return "effort_unsupported"
    return None


class AnthropicChatAdapter(ChatClient):
    """Streaming chat adapter for the ``anthropic`` SDK.

    Parameters
    ----------
    model : str
        Anthropic model identifier.
    api_key : str, optional
        Explicit key. ``None`` (default) lets the SDK read
        ``ANTHROPIC_API_KEY`` from the environment.
    timeout_sec : int, optional
        Default per-request timeout.
    client : anthropic.Anthropic, optional
        Inject a preconfigured SDK client (the test seam).

    Notes
    -----
    SDK construction is deferred to first use — unlike the non-streaming
    :class:`~whygraph.services.llm.AnthropicAdapter`, which builds eagerly.
    The chat path constructs clients while merely *listing* providers for
    the picker, so an unconfigured machine must not raise at construction.
    """

    provider = "anthropic"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        timeout_sec: int = 60,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        super().__init__(model=model)
        self._injected_client = client
        self._api_key = api_key
        self._default_timeout = timeout_sec
        self.__sdk_client: anthropic.Anthropic | None = None
        # The stream being read, for `abort()` from another thread.
        self._active: Any = None

    @property
    def _client(self) -> anthropic.Anthropic:
        if self._injected_client is not None:
            return self._injected_client
        if self.__sdk_client is None:
            # Check for a key before handing off: with none resolvable the SDK
            # raises a bare `TypeError`, which neither reads as a
            # configuration problem nor is safe to catch broadly (it would
            # also swallow genuine bugs in our own translation code).
            if not (self._api_key or os.environ.get(_ENV_VAR)):
                raise LlmKeyMissing(
                    f"anthropic is not configured — set {_ENV_VAR} or "
                    "[llm.anthropic].api_key in whygraph.toml"
                )
            self.__sdk_client = (
                anthropic.Anthropic(api_key=self._api_key)
                if self._api_key
                else anthropic.Anthropic()
            )
        return self.__sdk_client

    def list_models(self) -> tuple[ModelInfo, ...]:
        """List models via ``client.models.list()``.

        Anthropic is the only one of the four providers that returns a
        human display name, so the dropdown gets "Claude Opus 5" rather
        than a bare id here.

        Note that a **scoped API key can 401 on this endpoint while working
        fine for ``/messages``** — the caller is expected to fall back.
        """
        try:
            # Iterating the pager auto-paginates; `.data` would be page one only.
            return tuple(
                ModelInfo(
                    id=model.id,
                    display_name=getattr(model, "display_name", None) or model.id,
                )
                for model in self._client.models.list()
            )
        except anthropic.AnthropicError as exc:
            raise LlmError(f"anthropic model listing failed: {exc}") from exc

    def abort(self) -> None:
        """Wake the reader of the in-flight stream by shutting its socket down.

        See :meth:`whygraph.services.llm.chat.ChatClient.abort`. Never
        closes the stream from this thread and never raises.
        """
        shutdown_stream(self._active)

    def stream_turn(self, request: ChatRequest) -> Iterator[ChatStreamEvent]:
        """Stream one turn from the Messages API.

        See :meth:`whygraph.services.llm.chat.ChatClient.stream_turn` for
        the contract.
        """
        system_parts = [m.content for m in request.messages if m.role == "system"]
        messages = _message_payload(request.messages)
        if not messages:
            raise LlmError(
                "AnthropicChatAdapter requires at least one non-system message"
            )
        _cache_last_block(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": request.max_tokens or DEFAULT_MAX_TOKENS,
            "timeout": request.timeout_sec or self._default_timeout,
        }
        system = _system_payload(system_parts)
        if system:
            kwargs["system"] = system
        if request.effort:
            kwargs["output_config"] = {"effort": request.effort}
        if request.tools:
            kwargs["tools"] = _tool_payload(request.tools)
            if request.tool_choice == "none":
                kwargs["tool_choice"] = {"type": "none"}
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if self.model not in _NO_THINKING:
            # Summarized thinking, shown as reasoning; the blocks themselves
            # are replayed verbatim within the turn.
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}

        blocks: dict[int, _BlockAccumulator] = {}
        # Finished `tool_use` blocks, held until the stop reason is known.
        completed: list[tuple[int, _BlockAccumulator]] = []
        # Every finished block, as sent back next round (preserved thinking).
        replay_blocks: list[dict] = []
        last_index: int | None = None
        finish_reason: str | None = None
        # Usage arrives in pieces: `message_start` carries the prompt side,
        # each `message_delta` a *cumulative* snapshot that can also carry
        # final input / cache counts, and the SDK's `message_stop` the final
        # message. The latest non-null value of each field wins.
        raw_usage: dict[str, int | None] = dict.fromkeys(ANTHROPIC_USAGE_KEYS)
        served_model: str | None = None

        def _absorb(usage: Any) -> None:
            if usage is None:
                return
            for key, value in anthropic_raw_usage(usage).items():
                if value is not None:
                    raw_usage[key] = value

        def _absorb_message(message: Any) -> None:
            nonlocal served_model
            if message is None:
                return
            model = getattr(message, "model", None)
            if isinstance(model, str) and model:
                served_model = model
            _absorb(getattr(message, "usage", None))

        started = False
        while True:
            try:
                with self._client.messages.stream(**kwargs) as stream:
                    self._active = stream
                    for event in stream:
                        started = True
                        event_type = getattr(event, "type", None)

                        if event_type == "content_block_start":
                            block = getattr(event, "content_block", None)
                            last_index = getattr(event, "index", 0) or 0
                            if getattr(block, "type", None) == "tool_use":
                                blocks[last_index] = _BlockAccumulator(
                                    call_id=getattr(block, "id", "") or "",
                                    name=getattr(block, "name", "") or "",
                                )
                            continue

                        if event_type == "content_block_delta":
                            delta = getattr(event, "delta", None)
                            delta_type = getattr(delta, "type", None)
                            if delta_type == "text_delta":
                                text = getattr(delta, "text", None)
                                if text:
                                    yield TextDelta(text=text)
                            elif delta_type == "thinking_delta":
                                thought = getattr(delta, "thinking", None)
                                if thought:
                                    yield ReasoningDelta(text=thought)
                            elif delta_type == "input_json_delta":
                                index = getattr(event, "index", 0) or 0
                                accumulator = blocks.get(index)
                                fragment = getattr(delta, "partial_json", None)
                                if accumulator is not None and fragment:
                                    accumulator.json_parts.append(fragment)
                            continue

                        if event_type == "content_block_stop":
                            index = getattr(event, "index", 0) or 0
                            accumulator = blocks.pop(index, None)
                            if accumulator is not None:
                                completed.append((index, accumulator))
                            # The SDK's stream helper carries the accumulated
                            # block, signature included; a hand-rolled copy
                            # would lose the `signature_delta`.
                            block = getattr(event, "content_block", None)
                            if block is not None:
                                replayed = _replay_block(block)
                                if replayed is not None:
                                    replay_blocks.append(replayed)
                                else:
                                    _log.debug(
                                        "anthropic: not replaying a %s block",
                                        getattr(block, "type", None),
                                    )
                            continue

                        if event_type == "message_delta":
                            delta = getattr(event, "delta", None)
                            reason = getattr(delta, "stop_reason", None)
                            if reason is not None:
                                finish_reason = reason
                            _absorb(getattr(event, "usage", None))
                            continue

                        if event_type in ("message_start", "message_stop"):
                            # `message_stop` from the SDK's stream helper carries
                            # the final message snapshot: its model is the one
                            # actually served (it can change after a fallback).
                            _absorb_message(getattr(event, "message", None))
                            continue
                break
            except LlmError:
                raise
            except anthropic.AnthropicError as exc:
                if not started and "thinking" in kwargs and _thinking_unsupported(exc):
                    # This model takes no adaptive thinking: ask once more without
                    # it, and never send it to this model again in this process.
                    _NO_THINKING.add(self.model)
                    del kwargs["thinking"]
                    continue
                raise LlmError(
                    f"anthropic API error: {exc}",
                    request_id=getattr(exc, "request_id", None),
                    code=_effort_code(exc, request),
                ) from exc
            except httpx.TransportError as exc:
                # Once a stream has started the SDK no longer wraps transport
                # errors: a read timeout or a dropped connection arrives raw.
                raise LlmError(
                    f"anthropic stream interrupted: {type(exc).__name__}"
                ) from exc
            finally:
                self._active = None

        # A stream cut short before content_block_stop still owes its
        # accumulated calls.
        completed.extend(sorted(blocks.items()))
        calls = list(_finished_calls(completed, finish_reason, last_index))
        yield from calls

        replay = None
        sent = {event.call.id for event in calls}
        announced = {b["id"] for b in replay_blocks if b["type"] == "tool_use"}
        if replay_blocks and announced <= sent:
            replay = {"content": replay_blocks}
        # A dropped call's `tool_use` block would go back with no result, so
        # a round that dropped one replays nothing and is rebuilt as text
        # plus the calls that ran.
        yield TurnDone(
            finish_reason=finish_reason,
            model=served_model,
            replay=replay,
            **anthropic_usage_fields(raw_usage),
        )


__all__ = ["AnthropicChatAdapter"]
