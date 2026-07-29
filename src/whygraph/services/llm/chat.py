"""Tool-calling + streaming chat port, parallel to :class:`LlmClient`.

Why a second port rather than widening the first: the analyze / rationale
path needs exactly one thing — a synchronous ``complete()`` returning
text — and its value objects (:mod:`whygraph.services.llm.types`) are
frozen dataclasses with no notion of tool calls or incremental output.
Adding streaming and tool-calling to that port would ripple through all
five existing adapters for the benefit of one new consumer. So the chat
harness gets its own narrow port here; :class:`LlmClient` stays
untouched.

The port is deliberately **synchronous-generator** shaped rather than
async: the serve layer's documented contract is sync handlers running in
the threadpool (``serve/routes.py``), and both vendor SDKs expose sync
streaming (``openai`` returns an iterator for ``stream=True``;
``anthropic`` offers a ``messages.stream`` context manager). One
consumer, one thread, no event-loop coupling.

Public API
----------
* :class:`ChatMessage`, :class:`ToolSpec`, :class:`ToolCall`,
  :class:`ChatRequest` — value objects crossing the port.
* :class:`TextDelta`, :class:`ToolCallMade`, :class:`TurnDone` — the
  streamed event union. The chat harness layers its own event types on
  top of these (see :mod:`whygraph.chat.harness`).
* :class:`ChatClient` — the abstract port.
* :func:`make_chat_client` — config-driven construction by provider tag.

Notes
-----
Only four providers can chat: ``anthropic``, ``openai``, ``deepseek``,
``openrouter``. ``ollama`` is excluded because local models' tool-calling
reliability is unproven, and ``claude-cli`` because the CLI adapter
disables tools outright (``claude_cli.py`` passes ``--tools ""``). Both
raise :class:`LlmError` from :func:`make_chat_client` with a message
naming the supported set.
"""

from __future__ import annotations

import abc
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from whygraph.core import get_config
from whygraph.core.config import LlmConfig

from .exceptions import LlmError

ChatRole = Literal["system", "user", "assistant", "tool"]
"""Allowed values for :attr:`ChatMessage.role`.

Matches the OpenAI chat-completions vocabulary directly. The Anthropic
adapter translates: ``"system"`` becomes the SDK's separate ``system=``
parameter and ``"tool"`` messages become a ``user`` message carrying
``tool_result`` blocks.
"""

CHAT_PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "deepseek", "openrouter")
"""Provider tags :func:`make_chat_client` accepts, in picker order."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool offered to the model.

    Attributes
    ----------
    name : str
        Tool name the model calls. Must be unique within a request.
    description : str
        Natural-language description of what the tool does and when to
        reach for it. This is the model's only guidance, so it carries
        the usage caveats (cost, staleness, case-sensitivity, …).
    parameters : dict
        JSON Schema for the tool's arguments object. Passed verbatim as
        OpenAI's ``parameters`` / Anthropic's ``input_schema``.
    """

    name: str
    description: str
    parameters: dict


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool invocation the model requested.

    Attributes
    ----------
    id : str
        Provider-assigned call id. Echoed back on the matching
        ``role="tool"`` :class:`ChatMessage` so the provider can pair
        result to call.
    name : str
        Name of the tool to invoke.
    arguments : dict
        Parsed arguments object. Adapters parse the provider's
        JSON-string form once, at the port boundary, so callers never
        see raw JSON — a malformed payload raises :class:`LlmError`
        rather than reaching the dispatcher.
    """

    id: str
    name: str
    arguments: dict


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One message in a chat conversation.

    Note that :mod:`whygraph.db.models` also defines a ``ChatMessage``
    (the persisted row). Each is the natural name in its own layer; code
    holding both aliases the DB side as ``ChatMessageRow``.

    Attributes
    ----------
    role : ChatRole
        ``"system"``, ``"user"``, ``"assistant"``, or ``"tool"``.
    content : str
        Text body. For ``"tool"`` rows this is the JSON-serialized tool
        result; for assistant rows that only made tool calls it may be
        empty.
    tool_calls : tuple[ToolCall, ...]
        Tool invocations requested by an assistant turn. Empty for every
        other role.
    tool_call_id : str or None
        On a ``"tool"`` message, the :attr:`ToolCall.id` this result
        answers. ``None`` for every other role.
    """

    role: ChatRole
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """One streaming chat turn.

    Attributes
    ----------
    messages : tuple[ChatMessage, ...]
        Ordered conversation, already windowed by the caller (the port
        does no trimming — see :func:`whygraph.chat.harness.build_window`).
    tools : tuple[ToolSpec, ...]
        Tools the model may call this turn. Empty disables tool calling.
    max_tokens : int or None
        Per-call cap on output tokens. ``None`` lets the adapter pick a
        provider-appropriate default (Anthropic requires one, so its
        adapter substitutes 4096).
    temperature : float or None
        Sampling temperature; ``None`` uses the provider default.
    timeout_sec : int or None
        Per-call timeout; ``None`` uses the adapter's bound default.
    """

    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolSpec, ...] = ()
    max_tokens: int | None = None
    temperature: float | None = None
    timeout_sec: int | None = None


@dataclass(frozen=True, slots=True)
class TextDelta:
    """An incremental chunk of assistant text.

    Attributes
    ----------
    text : str
        The fragment. Concatenating every :class:`TextDelta` of a turn in
        order reproduces the assistant's full text.
    """

    text: str


@dataclass(frozen=True, slots=True)
class ToolCallMade:
    """A tool call finished accumulating and is ready to dispatch.

    Emitted once per call, after its arguments are complete — never per
    argument fragment. Parallel calls in one turn produce one event each,
    in the order the provider reported them.

    Attributes
    ----------
    call : ToolCall
        The fully-formed call.
    """

    call: ToolCall


@dataclass(frozen=True, slots=True)
class TurnDone:
    """The provider finished this turn. Exactly one, always last.

    Attributes
    ----------
    finish_reason : str or None
        Raw provider value (``"stop"``, ``"tool_calls"``, ``"end_turn"``,
        ``"tool_use"``, ``"max_tokens"``, …). Not normalized — callers
        decide whether more tool rounds follow by looking at whether any
        :class:`ToolCallMade` was emitted, not at this string.
    input_tokens : int or None
        Prompt-token usage, when the provider reports it.
    output_tokens : int or None
        Completion-token usage, when the provider reports it.
    """

    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


ChatStreamEvent = TextDelta | ToolCallMade | TurnDone
"""What :meth:`ChatClient.stream_turn` yields."""


class ChatClient(abc.ABC):
    """Abstract port for a streaming, tool-calling chat client.

    Attributes
    ----------
    provider : str
        Class-level provider tag, e.g. ``"anthropic"``.
    model : str
        Instance-bound model identifier.
    """

    provider: str

    def __init__(self, *, model: str) -> None:
        self.model = model

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(provider={self.provider!r}, model={self.model!r})"
        )

    @abc.abstractmethod
    def stream_turn(self, request: ChatRequest) -> Iterator[ChatStreamEvent]:
        """Stream one assistant turn.

        A synchronous generator. Yields :class:`TextDelta` and
        :class:`ToolCallMade` events as they arrive, then exactly one
        :class:`TurnDone` as its final event.

        Parameters
        ----------
        request : ChatRequest
            Messages, tools, and per-call overrides.

        Yields
        ------
        ChatStreamEvent
            Text fragments, completed tool calls, and finally one
            :class:`TurnDone`.

        Raises
        ------
        LlmError
            On any provider failure — auth, network, timeout, or a
            malformed tool-argument payload. Same contract as
            :meth:`whygraph.services.llm.LlmClient.complete`; the
            originating exception is preserved as ``__cause__``.
        """
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class _ProviderWiring:
    """How one provider tag maps onto a concrete chat adapter.

    Attributes
    ----------
    config_attr : str
        Attribute on :class:`whygraph.core.config.LlmConfig` holding the
        provider's typed section.
    env_var : str
        Environment variable the SDK / adapter falls back to for the key.
        Also what ``GET /api/chat/providers`` reports as the hint.
    base_url : str or None
        Endpoint override for the OpenAI-compatible adapter. ``None``
        means "the adapter's own default" (plain OpenAI, or Anthropic
        which does not take one).
    """

    config_attr: str
    env_var: str
    base_url: str | None = None


_CHAT_WIRING: dict[str, _ProviderWiring] = {
    "anthropic": _ProviderWiring(
        config_attr="anthropic",
        env_var="ANTHROPIC_API_KEY",
    ),
    "openai": _ProviderWiring(
        config_attr="openai",
        env_var="OPENAI_API_KEY",
    ),
    "deepseek": _ProviderWiring(
        config_attr="deepseek",
        env_var="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com/v1",
    ),
    "openrouter": _ProviderWiring(
        config_attr="openrouter",
        env_var="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
    ),
}


def chat_provider_env_var(provider: str) -> str | None:
    """Return the API-key env var for a chat ``provider``, or ``None``.

    Parameters
    ----------
    provider : str
        A provider tag; unknown tags yield ``None``.

    Returns
    -------
    str or None
        The environment variable name (e.g. ``"OPENROUTER_API_KEY"``).
    """
    wiring = _CHAT_WIRING.get(provider)
    return wiring.env_var if wiring is not None else None


def make_chat_client(
    provider: str,
    *,
    model: str | None = None,
    config: LlmConfig | None = None,
    **overrides,
) -> ChatClient:
    """Construct a :class:`ChatClient` for a provider tag.

    ``openai`` / ``deepseek`` / ``openrouter`` all resolve to
    :class:`~whygraph.services.llm.openai_chat.OpenAIChatAdapter` with the
    matching ``base_url`` + key; ``anthropic`` resolves to
    :class:`~whygraph.services.llm.anthropic_chat.AnthropicChatAdapter`.

    Parameters
    ----------
    provider : str
        One of :data:`CHAT_PROVIDERS`.
    model : str, optional
        Override the model from the provider's ``[llm.<provider>]``
        section. An empty string is treated as ``None`` (no override), so
        a blank UI field falls through to the configured default.
    config : LlmConfig, optional
        The LLM configuration to read keys / models from. ``None``
        (default) pulls the process-wide config.
    **overrides
        Forwarded to the adapter constructor — notably ``client=`` to
        inject a stub SDK in tests.

    Returns
    -------
    ChatClient
        A configured adapter ready for :meth:`ChatClient.stream_turn`.

    Raises
    ------
    LlmError
        If ``provider`` is not a chat provider. ``ollama`` and
        ``claude-cli`` are rejected here by design (see the module
        docstring), as is any unrecognized tag.
    """
    # Imported lazily: the adapters import this module for its types, so
    # a module-level import would be circular.
    from .anthropic_chat import AnthropicChatAdapter
    from .openai_chat import OpenAIChatAdapter

    wiring = _CHAT_WIRING.get(provider)
    if wiring is None:
        raise LlmError(
            f"{provider!r} is not a chat provider; "
            f"available: {CHAT_PROVIDERS}. "
            "(ollama and claude-cli support analyze/rationale but not "
            "tool-calling chat.)"
        )

    llm_config = config if config is not None else get_config().llm
    section = getattr(llm_config, wiring.config_attr)
    resolved_model = model or section.model

    if provider == "anthropic":
        return AnthropicChatAdapter(
            model=resolved_model,
            api_key=section.api_key,
            timeout_sec=section.timeout_sec,
            **overrides,
        )
    # OpenAI's own config carries a user-settable base_url; the two
    # compat providers pin theirs in the wiring table.
    base_url = wiring.base_url or getattr(section, "base_url", None)
    return OpenAIChatAdapter(
        provider=provider,
        model=resolved_model,
        api_key=section.api_key,
        base_url=base_url,
        env_var=wiring.env_var,
        timeout_sec=section.timeout_sec,
        **overrides,
    )


__all__ = [
    "CHAT_PROVIDERS",
    "ChatClient",
    "ChatMessage",
    "ChatRequest",
    "ChatRole",
    "ChatStreamEvent",
    "TextDelta",
    "ToolCall",
    "ToolCallMade",
    "ToolSpec",
    "TurnDone",
    "chat_provider_env_var",
    "make_chat_client",
]
