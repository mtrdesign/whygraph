"""Anthropic Claude via the official ``anthropic`` Python SDK.

For API-billed access. Use :class:`whygraph.services.llm.ClaudeCliAdapter`
if you'd rather bill against a Claude Code subscription.
"""

from __future__ import annotations

from typing import Any

import anthropic

from whygraph.core.config import AnthropicConfig

from .client import LlmClient
from .exceptions import LlmError
from .types import CompletionRequest, CompletionResponse


class AnthropicAdapter(LlmClient):
    """Adapter for the ``anthropic`` SDK.

    Parameters
    ----------
    model : str
        Anthropic model identifier (e.g. ``"claude-opus-4-7"``).
    api_key : str, optional
        Explicit API key. ``None`` (default) lets the SDK read
        ``ANTHROPIC_API_KEY`` from the environment.
    timeout_sec : int, optional
        Default per-request timeout. Per-call overrides go on the
        :class:`CompletionRequest`.
    client : anthropic.Anthropic, optional
        Inject a preconfigured SDK client (useful for tests).
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
        self._client = client or (
            anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        )
        self._default_timeout = timeout_sec

    @classmethod
    def from_config(
        cls,
        config: AnthropicConfig,
        **overrides: Any,
    ) -> "AnthropicAdapter":
        """Build an adapter from a typed :class:`AnthropicConfig` section.

        Recognized ``overrides``: ``client`` — inject a preconfigured
        ``anthropic.Anthropic`` (useful for tests).
        """
        return cls(
            model=config.model,
            api_key=config.api_key,
            timeout_sec=config.timeout_sec,
            **overrides,
        )

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        system_parts = [m.content for m in request.messages if m.role == "system"]
        chat_messages = [
            {"role": m.role, "content": m.content}
            for m in request.messages
            if m.role != "system"
        ]
        if not chat_messages:
            raise LlmError("AnthropicAdapter requires at least one non-system message")
        kwargs: dict = {
            "model": self.model,
            "messages": chat_messages,
            "max_tokens": request.max_tokens or 4096,
            "timeout": request.timeout_sec or self._default_timeout,
        }
        if system_parts:
            kwargs["system"] = "\n\n".join(system_parts)
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature

        try:
            result = self._client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            raise LlmError(f"anthropic API error: {exc}") from exc

        text_blocks = [
            b.text for b in result.content if getattr(b, "type", None) == "text"
        ]
        return CompletionResponse(
            text="".join(text_blocks),
            model=getattr(result, "model", self.model),
            provider=self.provider,
            input_tokens=getattr(result.usage, "input_tokens", None)
            if result.usage
            else None,
            output_tokens=getattr(result.usage, "output_tokens", None)
            if result.usage
            else None,
            finish_reason=getattr(result, "stop_reason", None),
            raw=result.model_dump() if hasattr(result, "model_dump") else None,
        )
