"""Local Ollama models via the official ``ollama`` Python SDK.

No API key. Default host is ``http://localhost:11434`` — override via
:class:`whygraph.core.config.OllamaConfig` or the constructor for a
remote Ollama server.
"""

from __future__ import annotations

from typing import Any

import ollama

from whygraph.core.config import OllamaConfig

from .client import LlmClient
from .exceptions import LlmError
from .types import CompletionRequest, CompletionResponse


class OllamaAdapter(LlmClient):
    """Adapter for the local-or-remote Ollama server.

    Parameters
    ----------
    model : str
        Ollama model tag (e.g. ``"llama3"``, ``"deepseek-coder"``,
        ``"qwen2.5-coder:14b"``).
    host : str, optional
        Override the Ollama server URL. ``None`` (default) uses
        :attr:`DEFAULT_HOST`.
    timeout_sec : int, optional
        Default per-request timeout. Defaults to ``120`` — local models
        are slower than hosted ones.
    client : ollama.Client, optional
        Inject a preconfigured SDK client (useful for tests).
    """

    provider = "ollama"
    DEFAULT_HOST = "http://localhost:11434"

    def __init__(
        self,
        *,
        model: str,
        host: str | None = None,
        timeout_sec: int = 120,
        client: ollama.Client | None = None,
    ) -> None:
        super().__init__(model=model)
        self._client = client or ollama.Client(host=host or self.DEFAULT_HOST)
        self._default_timeout = timeout_sec

    @classmethod
    def from_config(
        cls,
        config: OllamaConfig,
        **overrides: Any,
    ) -> "OllamaAdapter":
        """Build an adapter from a typed :class:`OllamaConfig` section.

        Recognized ``overrides``: ``client`` — inject a preconfigured
        ``ollama.Client`` (useful for tests).
        """
        return cls(
            model=config.model,
            host=config.host,
            timeout_sec=config.timeout_sec,
            **overrides,
        )

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        messages = [{"role": m.role, "content": m.content} for m in request.messages]
        if not messages:
            raise LlmError("OllamaAdapter requires at least one message")
        options: dict = {}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.max_tokens is not None:
            options["num_predict"] = request.max_tokens

        try:
            result = self._client.chat(
                model=self.model,
                messages=messages,
                options=options or None,
            )
        except ollama.ResponseError as exc:
            raise LlmError(f"ollama error: {exc}") from exc

        message = result.get("message") or {}
        return CompletionResponse(
            text=message.get("content", ""),
            model=result.get("model", self.model),
            provider=self.provider,
            input_tokens=result.get("prompt_eval_count"),
            output_tokens=result.get("eval_count"),
            finish_reason=result.get("done_reason"),
            raw=dict(result),
        )
