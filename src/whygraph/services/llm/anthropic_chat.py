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
4. **``max_tokens`` is required**, so a request that omits it gets 4096
   (matching :class:`~whygraph.services.llm.AnthropicAdapter`).

Unlike the OpenAI dialect, tool-call arguments arrive as
``input_json_delta`` fragments attached to a known block index, and the
SDK signals block completion explicitly (``content_block_stop``) — so
each call flushes as soon as its own block ends rather than waiting for
the whole turn.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import anthropic

from .chat import (
    ChatClient,
    ChatMessage,
    ChatRequest,
    ChatStreamEvent,
    ModelInfo,
    TextDelta,
    ToolCall,
    ToolCallMade,
    ToolSpec,
    TurnDone,
)
from .exceptions import LlmError

_ENV_VAR = "ANTHROPIC_API_KEY"


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
    """Render :class:`ToolSpec`s into Anthropic's ``tools`` array."""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.parameters,
        }
        for spec in tools
    ]


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
                raise LlmError(
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

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": request.max_tokens or 4096,
            "timeout": request.timeout_sec or self._default_timeout,
        }
        if system_parts:
            kwargs["system"] = "\n\n".join(system_parts)
        if request.tools:
            kwargs["tools"] = _tool_payload(request.tools)
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature

        blocks: dict[int, _BlockAccumulator] = {}
        finish_reason: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None

        try:
            with self._client.messages.stream(**kwargs) as stream:
                for event in stream:
                    event_type = getattr(event, "type", None)

                    if event_type == "content_block_start":
                        block = getattr(event, "content_block", None)
                        if getattr(block, "type", None) == "tool_use":
                            blocks[getattr(event, "index", 0) or 0] = _BlockAccumulator(
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
                            yield ToolCallMade(call=accumulator.flush())
                        continue

                    if event_type == "message_delta":
                        delta = getattr(event, "delta", None)
                        reason = getattr(delta, "stop_reason", None)
                        if reason is not None:
                            finish_reason = reason
                        usage = getattr(event, "usage", None)
                        if usage is not None:
                            output_tokens = getattr(usage, "output_tokens", None)
                        continue

                    if event_type == "message_start":
                        message = getattr(event, "message", None)
                        usage = getattr(message, "usage", None)
                        if usage is not None:
                            input_tokens = getattr(usage, "input_tokens", None)
                        continue
        except LlmError:
            raise
        except anthropic.AnthropicError as exc:
            raise LlmError(f"anthropic API error: {exc}") from exc

        # A stream cut short before content_block_stop still owes its
        # accumulated calls.
        for index in sorted(blocks):
            yield ToolCallMade(call=blocks[index].flush())

        yield TurnDone(
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


__all__ = ["AnthropicChatAdapter"]
