"""Streaming, tool-calling chat adapter for OpenAI-compatible endpoints.

Covers three providers with one implementation — ``openai``, ``deepseek``,
and ``openrouter`` all speak the chat-completions API, differing only in
``base_url`` and which env var holds the key. :func:`make_chat_client`
passes the right pair per tag.

The interesting work is normalizing the wire protocol's **fragmented tool
calls**. OpenAI streams a tool call across many chunks: the first delta
for an index carries the call id and function name, subsequent deltas
carry slices of the argument JSON string. Nothing in the stream announces
that a call is complete except the choice's ``finish_reason``. So this
adapter accumulates per-index state and flushes it into
:class:`ToolCallMade` events once the choice finishes — the port's
contract is one event per *complete* call, never per fragment. A
``content_filter`` finish dispatches nothing, and a ``length`` finish drops
the call it cut off (and any other whose arguments do not parse).

Two of the three providers reason across tool calls and expect that
reasoning **echoed back** on the assistant message: DeepSeek as
``reasoning_content`` (thinking is on by default, and a request with tools
fails without it), OpenRouter as ``reasoning_details``, unmodified. The
adapter captures each into :attr:`TurnDone.replay` and sends it back only to
the provider that produced it.

Effort (``request.effort``) maps per provider: ``reasoning_effort`` for
``openai`` and ``deepseek`` (DeepSeek's passed through unclamped, and omitted
on a request that runs with thinking disabled), ``extra_body.reasoning.effort``
for ``openrouter``. A 400 that names the parameter becomes
``LlmError(code="effort_unsupported")``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import openai

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
from .usage import UsageFields, openai_usage_fields

_log = logging.getLogger(__name__)

_MERGED_DETAIL_FIELDS = ("text", "summary", "data")
"""``reasoning_details`` fields whose fragments are concatenated; every other
field (``signature`` among them) takes the last non-null value."""

_EFFORT_PARAMS = frozenset({"reasoning_effort", "reasoning", "thinking"})
"""Values of a 400's structured ``param`` that name the effort parameter."""

_EFFORT_MESSAGE = re.compile(r"reasoning_effort|reasoning\.effort")
"""Fallback when a compat endpoint names the parameter only in the message."""


@dataclass(slots=True)
class _CallAccumulator:
    """Scratch space for one tool call being streamed in fragments.

    Attributes
    ----------
    call_id : str
        Provider-assigned id. Set from the first delta that carries one;
        later deltas for the same index repeat or omit it.
    name : str
        Function name, same arrival pattern as ``call_id``.
    argument_parts : list[str]
        Argument-JSON slices in arrival order, joined on flush.
    """

    call_id: str = ""
    name: str = ""
    argument_parts: list[str] = field(default_factory=list)

    def absorb(self, delta) -> None:
        """Fold one ``tool_calls`` delta entry into this accumulator."""
        if getattr(delta, "id", None):
            self.call_id = delta.id
        function = getattr(delta, "function", None)
        if function is None:
            return
        if getattr(function, "name", None):
            self.name = function.name
        fragment = getattr(function, "arguments", None)
        if fragment:
            self.argument_parts.append(fragment)

    def flush(self) -> ToolCall:
        """Materialize the accumulated fragments into a :class:`ToolCall`.

        Returns
        -------
        ToolCall
            With ``arguments`` parsed from the joined JSON fragments. An
            empty argument string is treated as ``{}`` — providers omit
            the payload entirely for zero-argument tools.

        Raises
        ------
        LlmError
            If the joined fragments are not a JSON object. This is a
            provider-side failure (a truncated or malformed stream), so
            it surfaces as :class:`LlmError` rather than reaching the
            dispatcher with garbage.
        """
        raw = "".join(self.argument_parts).strip()
        if not raw:
            parsed: object = {}
        else:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise LlmError(
                    f"openai stream: malformed arguments for tool "
                    f"{self.name or '<unnamed>'}: {raw[:200]!r}"
                ) from exc
        if not isinstance(parsed, dict):
            raise LlmError(
                f"openai stream: tool {self.name or '<unnamed>'} arguments "
                f"must be a JSON object, got {type(parsed).__name__}"
            )
        return ToolCall(id=self.call_id, name=self.name, arguments=parsed)


def _tool_payload(tools: tuple[ToolSpec, ...]) -> list[dict]:
    """Render :class:`ToolSpec`s into the chat-completions ``tools`` array."""
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description
                + (
                    "\n\nExample arguments:\n"
                    + "\n".join(json.dumps(e) for e in spec.examples)
                    if spec.examples
                    else ""
                ),
                "parameters": spec.parameters,
            },
        }
        for spec in tools
    ]


def _as_dict(entry: Any) -> dict:
    """A ``reasoning_details`` entry as a plain dict (the SDK leaves extras raw)."""
    if isinstance(entry, Mapping):
        return dict(entry)
    dump = getattr(entry, "model_dump", None)
    return dict(dump()) if callable(dump) else {}


def _merge_reasoning_details(merged: list[dict], fragments: Any) -> None:
    """Fold one delta's ``reasoning_details`` fragments into ``merged``.

    Fragments sharing an ``index`` build one entry: :data:`_MERGED_DETAIL_FIELDS`
    are concatenated, every other field keeps its last non-null value (the
    ``signature`` arrives on the last fragment and is never concatenated).
    An entry without an ``index`` is kept as its own entry.
    """
    for raw in fragments or []:
        fragment = _as_dict(raw)
        index = fragment.get("index")
        target = None
        if index is not None:
            target = next((e for e in merged if e.get("index") == index), None)
        if target is None:
            merged.append(fragment)
            continue
        for key, value in fragment.items():
            if value is None:
                continue
            if key in _MERGED_DETAIL_FIELDS and isinstance(value, str):
                target[key] = (target.get(key) or "") + value
            else:
                target[key] = value


def _reasoning_text(fragments: Any) -> Iterator[str]:
    """The readable text of ``reasoning_details`` fragments (``text`` or ``summary``).

    Encrypted entries carry ``data`` only and show nothing.
    """
    for raw in fragments or []:
        fragment = _as_dict(raw)
        for key in ("text", "summary"):
            value = fragment.get(key)
            if isinstance(value, str) and value:
                yield value


def _message_payload(
    messages: tuple[ChatMessage, ...], provider: str = "openai"
) -> list[dict]:
    """Render :class:`ChatMessage`s into the chat-completions wire shape.

    Assistant tool calls become a ``tool_calls`` array with each
    argument object re-serialized to the JSON *string* the API expects;
    ``role="tool"`` messages carry their ``tool_call_id``. An assistant
    message's replay payload goes back only to the provider that produced
    it: ``reasoning_content`` to ``deepseek``, ``reasoning_details`` to
    ``openrouter``; plain ``openai`` gets neither.
    """
    payload: list[dict] = []
    for message in messages:
        entry: dict = {"role": message.role, "content": message.content}
        replay = message.replay or {}
        if message.role == "assistant":
            if provider == "deepseek" and "reasoning_content" in replay:
                entry["reasoning_content"] = replay["reasoning_content"]
            if provider == "openrouter" and replay.get("reasoning_details"):
                entry["reasoning_details"] = replay["reasoning_details"]
        if message.role == "tool":
            entry["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
                for call in message.tool_calls
            ]
            # The API rejects `content: ""` alongside tool_calls on some
            # compat endpoints; None is universally accepted.
            if not message.content:
                entry["content"] = None
        payload.append(entry)
    return payload


def _deepseek_needs_fallback(request: ChatRequest) -> bool:
    """Whether a DeepSeek request carries an assistant message with no reasoning."""
    return any(
        m.role == "assistant" and "reasoning_content" not in (m.replay or {})
        for m in request.messages
    )


def _effort_code(exc: Exception, request: ChatRequest) -> str | None:
    """``"effort_unsupported"`` when a 400 rejects the effort parameter.

    Narrow on purpose: only with an effort on the request, only a
    ``BadRequestError``, and only when the structured ``param`` names the
    parameter (``reasoning_effort`` / ``reasoning`` / ``thinking``) or, as a
    fallback for endpoints that fill only the message, the message mentions
    ``reasoning_effort`` / ``reasoning.effort``. A broader match would
    mislabel unrelated 400s. A non-reasoning model such as ``gpt-4o`` or
    ``gpt-4o-mini`` (both in ``FALLBACK_MODELS``) lands here by design.
    """
    if not request.effort or not isinstance(exc, openai.BadRequestError):
        return None
    if getattr(exc, "param", None) in _EFFORT_PARAMS:
        return "effort_unsupported"
    if _EFFORT_MESSAGE.search(str(exc)):
        return "effort_unsupported"
    return None


def _finished_calls(
    accumulators: dict[int, _CallAccumulator], finish_reason: str | None
) -> Iterator[ToolCallMade]:
    """Emit the accumulated tool calls in index order, by the finish reason.

    ``content_filter`` dispatches nothing. ``length`` cut the last call off,
    so it is dropped, as is any other whose arguments do not parse; with any
    other finish reason malformed arguments still raise :class:`LlmError`.
    """
    if finish_reason in REFUSAL_REASONS:
        return
    truncated = finish_reason == "length"
    indices = sorted(accumulators)
    for index in indices:
        accumulator = accumulators[index]
        if truncated and index == indices[-1]:
            _log.info(
                "dropped tool call %s cut off by the length limit", accumulator.name
            )
            continue
        try:
            call = accumulator.flush()
        except LlmError:
            if not truncated:
                raise
            _log.info("dropped malformed tool call %s", accumulator.name)
            continue
        yield ToolCallMade(call=call)


class OpenAIChatAdapter(ChatClient):
    """Streaming chat adapter for any OpenAI-compatible endpoint.

    Parameters
    ----------
    provider : str
        Tag reported as :attr:`provider` — ``"openai"``, ``"deepseek"``,
        or ``"openrouter"``. Instance-level (not class-level as on
        :class:`~whygraph.services.llm.LlmClient`) precisely because one
        class serves three providers.
    model : str
        Model identifier.
    api_key : str, optional
        Explicit key. ``None`` (default) falls back to ``env_var`` from
        the environment, then to whatever the SDK itself resolves.
    base_url : str, optional
        Endpoint override. ``None`` keeps the SDK's OpenAI default.
    env_var : str, optional
        Environment variable consulted when ``api_key`` is ``None``.
        Default ``"OPENAI_API_KEY"``.
    timeout_sec : int, optional
        Default per-request timeout.
    client : openai.OpenAI, optional
        Inject a preconfigured SDK client (the test seam).

    Notes
    -----
    SDK construction is deferred to first use, mirroring
    :class:`~whygraph.services.llm.OpenAIAdapter` — the SDK validates
    credentials in its constructor, so eager construction would make
    merely *listing* providers raise on an unconfigured machine.
    """

    def __init__(
        self,
        *,
        provider: str = "openai",
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        env_var: str = "OPENAI_API_KEY",
        timeout_sec: int = 60,
        client: openai.OpenAI | None = None,
    ) -> None:
        super().__init__(model=model)
        self.provider = provider
        self._injected_client = client
        self._api_key = api_key
        self._base_url = base_url
        self._env_var = env_var
        self._default_timeout = timeout_sec
        self.__sdk_client: openai.OpenAI | None = None
        # The stream being read, for `abort()` from another thread.
        self._active: Any = None

    @property
    def _client(self) -> openai.OpenAI:
        if self._injected_client is not None:
            return self._injected_client
        if self.__sdk_client is None:
            key = self._api_key or os.environ.get(self._env_var)
            # Say which env var is missing rather than letting the SDK's
            # generic "Missing credentials" surface — the provider tag is
            # what the user needs to act on.
            if not key:
                raise LlmKeyMissing(
                    f"{self.provider} is not configured — set {self._env_var} or "
                    f"[llm.{self.provider}].api_key in whygraph.toml"
                )
            self.__sdk_client = openai.OpenAI(api_key=key, base_url=self._base_url)
        return self.__sdk_client

    def list_models(self) -> tuple[ModelInfo, ...]:
        """List models via the OpenAI-compatible ``/models`` endpoint.

        All three providers on this adapter expose it. The payload carries
        no display name, so the id doubles as the label — OpenRouter ids
        (``anthropic/claude-opus-5``) are already readable, and OpenAI's
        are conventional enough.

        OpenRouter serves ``/models`` without authentication and returns
        several hundred entries, which is why the UI filters rather than
        rendering them all flat.
        """
        try:
            return tuple(
                ModelInfo(id=model.id, display_name=model.id)
                for model in self._client.models.list()
            )
        except openai.OpenAIError as exc:
            raise LlmError(f"{self.provider} model listing failed: {exc}") from exc

    def abort(self) -> None:
        """Wake the reader of the in-flight stream by shutting its socket down.

        See :meth:`whygraph.services.llm.chat.ChatClient.abort`. Never
        closes the stream from this thread and never raises.
        """
        shutdown_stream(self._active)

    def stream_turn(self, request: ChatRequest) -> Iterator[ChatStreamEvent]:
        """Stream one turn from the chat-completions endpoint.

        See :meth:`whygraph.services.llm.chat.ChatClient.stream_turn` for
        the contract. Usage arrives in a trailing chunk with an empty
        ``choices`` list (requested via ``stream_options``), which is why
        :class:`TurnDone` is emitted after the loop rather than at
        ``finish_reason``.
        """
        messages = _message_payload(request.messages, self.provider)
        if not messages:
            raise LlmError("OpenAIChatAdapter requires at least one message")

        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "timeout": request.timeout_sec or self._default_timeout,
        }
        extra_body: dict[str, Any] = {}
        thinking_disabled = False
        if request.tools:
            kwargs["tools"] = _tool_payload(request.tools)
            if request.tool_choice == "none":
                kwargs["tool_choice"] = "none"
            if self.provider == "deepseek" and _deepseek_needs_fallback(request):
                # An assistant message with no reasoning to pass back (a row
                # from before replay existed, an errored round, a session
                # switched from another provider) would fail the request in
                # thinking mode, so this request runs without thinking.
                extra_body["thinking"] = {"type": "disabled"}
                thinking_disabled = True
        if request.effort:
            if self.provider == "openrouter":
                extra_body["reasoning"] = {"effort": request.effort}
            elif not thinking_disabled:
                # `openai`, and `deepseek` unclamped (it maps unknown levels
                # itself); omitted when this request runs without thinking.
                kwargs["reasoning_effort"] = request.effort
        if extra_body:
            kwargs["extra_body"] = extra_body
        if request.max_tokens is not None:
            # Reasoning models on OpenAI reject `max_tokens`.
            cap = "max_completion_tokens" if self.provider == "openai" else "max_tokens"
            kwargs[cap] = request.max_tokens
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature

        accumulators: dict[int, _CallAccumulator] = {}
        finish_reason: str | None = None
        usage_fields: UsageFields = openai_usage_fields(None, provider=self.provider)
        served_model: str | None = None
        flushed = False
        reasoning_parts: list[str] = []
        reasoning_details: list[dict] = []

        try:
            stream = self._client.chat.completions.create(**kwargs)
            self._active = stream
            for chunk in stream:
                chunk_model = getattr(chunk, "model", None)
                if isinstance(chunk_model, str) and chunk_model:
                    served_model = chunk_model
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    # One `provider` branch covers the DeepSeek / OpenRouter
                    # extras (OpenRouter's cost and cache writes ride the
                    # SDK's untyped extra fields) - see `usage.py`.
                    usage_fields = openai_usage_fields(usage, provider=self.provider)

                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]

                delta = getattr(choice, "delta", None)
                if delta is not None:
                    if self.provider == "deepseek":
                        reasoning = getattr(delta, "reasoning_content", None)
                        if isinstance(reasoning, str) and reasoning:
                            reasoning_parts.append(reasoning)
                            yield ReasoningDelta(text=reasoning)
                    elif self.provider == "openrouter":
                        fragments = getattr(delta, "reasoning_details", None)
                        _merge_reasoning_details(reasoning_details, fragments)
                        for shown in _reasoning_text(fragments):
                            yield ReasoningDelta(text=shown)
                    text = getattr(delta, "content", None)
                    if text:
                        yield TextDelta(text=text)
                    for entry in getattr(delta, "tool_calls", None) or []:
                        index = getattr(entry, "index", 0) or 0
                        accumulators.setdefault(index, _CallAccumulator()).absorb(entry)

                reason = getattr(choice, "finish_reason", None)
                if reason is not None:
                    finish_reason = reason
                    # Flush at finish so parallel calls emit in index
                    # order — the order the model asked for them.
                    yield from _finished_calls(accumulators, reason)
                    flushed = True
        except LlmError:
            raise
        except openai.OpenAIError as exc:
            raise LlmError(
                f"openai API error: {exc}",
                request_id=getattr(exc, "request_id", None),
                code=_effort_code(exc, request),
            ) from exc
        except httpx.TransportError as exc:
            # Once a stream has started the SDK no longer wraps transport
            # errors: a read timeout or a dropped connection arrives raw.
            raise LlmError(
                f"{self.provider} stream interrupted: {type(exc).__name__}"
            ) from exc
        finally:
            self._active = None

        # Defensive: a stream that ends without a finish_reason (a
        # truncated connection) still owes its accumulated calls.
        if not flushed:
            yield from _finished_calls(accumulators, finish_reason)

        replay: dict[str, Any] = {}
        if reasoning_parts:
            replay["reasoning_content"] = "".join(reasoning_parts)
        if reasoning_details:
            replay["reasoning_details"] = reasoning_details
        yield TurnDone(
            finish_reason=finish_reason,
            model=served_model,
            replay=replay or None,
            **usage_fields,
        )


__all__ = ["OpenAIChatAdapter"]
