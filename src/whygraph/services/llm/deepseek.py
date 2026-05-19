"""DeepSeek via the ``openai`` SDK pointed at the DeepSeek endpoint.

DeepSeek implements the OpenAI chat-completions API at
``https://api.deepseek.com/v1``, so the ``openai`` Python SDK works
unchanged once the ``base_url`` is overridden. This adapter pre-bakes
that URL and reads the conventional ``DEEPSEEK_API_KEY`` env var
(rather than ``OPENAI_API_KEY``, which would point at the wrong
account).
"""

from __future__ import annotations

import os
from typing import Any

import openai

from whygraph.core.config import DeepSeekConfig

from .openai import OpenAIAdapter


class DeepSeekAdapter(OpenAIAdapter):
    """OpenAI-compatible adapter pre-configured for the DeepSeek endpoint.

    Parameters
    ----------
    model : str, optional
        DeepSeek model identifier. Default ``"deepseek-chat"``.
    api_key : str, optional
        Explicit API key. ``None`` (default) reads
        ``DEEPSEEK_API_KEY`` from the environment.
    timeout_sec : int, optional
        Default per-request timeout.
    client : openai.OpenAI, optional
        Inject a preconfigured SDK client (useful for tests).
    """

    provider = "deepseek"
    DEFAULT_BASE_URL = "https://api.deepseek.com/v1"

    def __init__(
        self,
        *,
        model: str = "deepseek-chat",
        api_key: str | None = None,
        timeout_sec: int = 60,
        client: openai.OpenAI | None = None,
    ) -> None:
        resolved_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        super().__init__(
            model=model,
            api_key=resolved_key,
            base_url=self.DEFAULT_BASE_URL,
            timeout_sec=timeout_sec,
            client=client,
        )

    @classmethod
    def from_config(
        cls,
        config: DeepSeekConfig,
        **overrides: Any,
    ) -> "DeepSeekAdapter":
        """Build an adapter from a typed :class:`DeepSeekConfig` section.

        Recognized ``overrides``: ``client`` — inject a preconfigured
        ``openai.OpenAI`` (useful for tests).
        """
        return cls(
            model=config.model,
            api_key=config.api_key,
            timeout_sec=config.timeout_sec,
            **overrides,
        )
