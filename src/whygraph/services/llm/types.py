"""Value objects exchanged across the :class:`LlmClient` port.

Frozen, slotted dataclasses — same convention as
:mod:`whygraph.services.git` and :mod:`whygraph.services.github`.
Adapters consume :class:`CompletionRequest` and return
:class:`CompletionResponse`; the shapes are deliberately small and
provider-agnostic, with ``raw`` as the escape hatch for
provider-specific detail.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Role = Literal["system", "user", "assistant"]
"""Allowed values for :attr:`Message.role`. Matches the OpenAI / Ollama
shape directly; the Anthropic adapter routes ``"system"`` messages to
the SDK's separate ``system=`` parameter."""


@dataclass(frozen=True, slots=True)
class Message:
    """One chat message.

    Attributes
    ----------
    role : Role
        ``"system"``, ``"user"``, or ``"assistant"``.
    content : str
        Plain-text body. Multi-part / image content is not modelled yet.
    """

    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    """One LLM completion request.

    Attributes
    ----------
    messages : tuple[Message, ...]
        Ordered conversation. The adapter is responsible for translating
        this into the provider's native message format.
    max_tokens : int or None
        Per-call cap on output tokens. ``None`` lets the adapter pick
        a provider-appropriate default.
    temperature : float or None
        Sampling temperature. ``None`` lets the adapter pick the
        provider default.
    timeout_sec : int or None
        Per-call timeout override. ``None`` uses the adapter's bound
        default timeout.
    """

    messages: tuple[Message, ...]
    max_tokens: int | None = None
    temperature: float | None = None
    timeout_sec: int | None = None

    @classmethod
    def of(
        cls,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_sec: int | None = None,
    ) -> "CompletionRequest":
        """Build a request for the common "one user message" shape.

        Parameters
        ----------
        prompt : str
            The user's message content.
        system : str, optional
            Prepended as a ``"system"`` :class:`Message`.
        max_tokens, temperature, timeout_sec :
            Forwarded to the :class:`CompletionRequest` constructor.

        Returns
        -------
        CompletionRequest
            A request with at most two messages: ``[system?, user]``.
        """
        msgs: list[Message] = []
        if system is not None:
            msgs.append(Message(role="system", content=system))
        msgs.append(Message(role="user", content=prompt))
        return cls(
            messages=tuple(msgs),
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_sec=timeout_sec,
        )


@dataclass(frozen=True, slots=True)
class CompletionResponse:
    """The result of one :meth:`LlmClient.complete` call.

    Attributes
    ----------
    text : str
        The assistant's text content (concatenated across text blocks
        when the provider returns multiple).
    model : str
        Model identifier reported by the provider, or echoed back from
        the request when the provider does not return it.
    provider : str
        Provider identifier (e.g. ``"anthropic"``, ``"openai"``,
        ``"claude-cli"``).
    input_tokens : int or None
        Prompt-token usage; ``None`` if the provider does not report it
        (e.g. the Claude CLI).
    output_tokens : int or None
        Completion-token usage; ``None`` if not reported.
    finish_reason : str or None
        Raw provider value (``"stop"``, ``"length"``, ``"tool_use"``,
        ``"end_turn"``, …). Not normalized across providers.
    raw : dict or None
        Provider-specific raw response, when available. Escape hatch
        for callers that need detail the normalized shape drops.
    """

    text: str
    model: str
    provider: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None
    raw: dict | None = None
