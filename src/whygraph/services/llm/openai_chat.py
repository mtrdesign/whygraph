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
contract is one event per *complete* call, never per fragment.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field

import openai

from .chat import (
    ChatClient,
    ChatMessage,
    ChatRequest,
    ChatStreamEvent,
    TextDelta,
    ToolCall,
    ToolCallMade,
    ToolSpec,
    TurnDone,
)
from .exceptions import LlmError


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
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }
        for spec in tools
    ]


def _message_payload(messages: tuple[ChatMessage, ...]) -> list[dict]:
    """Render :class:`ChatMessage`s into the chat-completions wire shape.

    Assistant tool calls become a ``tool_calls`` array with each
    argument object re-serialized to the JSON *string* the API expects;
    ``role="tool"`` messages carry their ``tool_call_id``.
    """
    payload: list[dict] = []
    for message in messages:
        entry: dict = {"role": message.role, "content": message.content}
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

    @property
    def _client(self) -> openai.OpenAI:
        if self._injected_client is not None:
            return self._injected_client
        if self.__sdk_client is None:
            key = self._api_key or os.environ.get(self._env_var)
            self.__sdk_client = openai.OpenAI(api_key=key, base_url=self._base_url)
        return self.__sdk_client

    def stream_turn(self, request: ChatRequest) -> Iterator[ChatStreamEvent]:
        """Stream one turn from the chat-completions endpoint.

        See :meth:`whygraph.services.llm.chat.ChatClient.stream_turn` for
        the contract. Usage arrives in a trailing chunk with an empty
        ``choices`` list (requested via ``stream_options``), which is why
        :class:`TurnDone` is emitted after the loop rather than at
        ``finish_reason``.
        """
        messages = _message_payload(request.messages)
        if not messages:
            raise LlmError("OpenAIChatAdapter requires at least one message")

        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "timeout": request.timeout_sec or self._default_timeout,
        }
        if request.tools:
            kwargs["tools"] = _tool_payload(request.tools)
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature

        accumulators: dict[int, _CallAccumulator] = {}
        finish_reason: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        flushed = False

        try:
            stream = self._client.chat.completions.create(**kwargs)
            for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    input_tokens = getattr(usage, "prompt_tokens", None)
                    output_tokens = getattr(usage, "completion_tokens", None)

                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]

                delta = getattr(choice, "delta", None)
                if delta is not None:
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
                    for index in sorted(accumulators):
                        yield ToolCallMade(call=accumulators[index].flush())
                    flushed = True
        except LlmError:
            raise
        except openai.APIError as exc:
            raise LlmError(f"openai API error: {exc}") from exc

        # Defensive: a stream that ends without a finish_reason (a
        # truncated connection) still owes its accumulated calls.
        if not flushed:
            for index in sorted(accumulators):
                yield ToolCallMade(call=accumulators[index].flush())

        yield TurnDone(
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


__all__ = ["OpenAIChatAdapter"]
