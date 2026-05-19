"""OpenAI GPT family via the official ``openai`` Python SDK.

Also the base class for :class:`DeepSeekAdapter` and any other
OpenAI-compatible endpoint (Together, Groq, vLLM, …) that ships the
chat-completions API at a different ``base_url``.
"""

from __future__ import annotations

from typing import Any

import openai

from whygraph.core.config import OpenAIConfig

from .client import LlmClient
from .exceptions import LlmError
from .types import CompletionRequest, CompletionResponse


class OpenAIAdapter(LlmClient):
    """Adapter for the ``openai`` SDK.

    Parameters
    ----------
    model : str
        Model identifier (e.g. ``"gpt-4o"``).
    api_key : str, optional
        Explicit API key. ``None`` (default) lets the SDK read
        ``OPENAI_API_KEY`` from the environment.
    base_url : str, optional
        Override the API endpoint. ``None`` (default) uses
        :attr:`DEFAULT_BASE_URL`.
    timeout_sec : int, optional
        Default per-request timeout.
    client : openai.OpenAI, optional
        Inject a preconfigured SDK client (useful for tests).
    """

    provider = "openai"
    DEFAULT_BASE_URL = "https://api.openai.com/v1"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_sec: int = 60,
        client: openai.OpenAI | None = None,
    ) -> None:
        super().__init__(model=model)
        # The OpenAI SDK validates credentials at constructor time, so
        # we defer instantiation until the first request — keeps
        # `OpenAIAdapter(model="…")` safe to call when the caller is
        # only introspecting (tests, smoke checks, registry walks).
        self._injected_client = client
        self._api_key = api_key
        self._base_url = base_url or self.DEFAULT_BASE_URL
        self._default_timeout = timeout_sec
        self.__sdk_client: openai.OpenAI | None = None

    @property
    def _client(self) -> openai.OpenAI:
        if self._injected_client is not None:
            return self._injected_client
        if self.__sdk_client is None:
            self.__sdk_client = openai.OpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
            )
        return self.__sdk_client

    @classmethod
    def from_config(
        cls,
        config: OpenAIConfig,
        **overrides: Any,
    ) -> "OpenAIAdapter":
        """Build an adapter from a typed :class:`OpenAIConfig` section.

        Recognized ``overrides``: ``client`` — inject a preconfigured
        ``openai.OpenAI`` (useful for tests).
        """
        return cls(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
            timeout_sec=config.timeout_sec,
            **overrides,
        )

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        messages = [{"role": m.role, "content": m.content} for m in request.messages]
        if not messages:
            raise LlmError("OpenAIAdapter requires at least one message")
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "timeout": request.timeout_sec or self._default_timeout,
        }
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature

        try:
            result = self._client.chat.completions.create(**kwargs)
        except openai.APIError as exc:
            raise LlmError(f"openai API error: {exc}") from exc

        choice = result.choices[0]
        usage = result.usage
        return CompletionResponse(
            text=choice.message.content or "",
            model=getattr(result, "model", self.model),
            provider=self.provider,
            input_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            output_tokens=getattr(usage, "completion_tokens", None) if usage else None,
            finish_reason=getattr(choice, "finish_reason", None),
            raw=result.model_dump() if hasattr(result, "model_dump") else None,
        )
