"""OpenRouter via the ``openai`` SDK pointed at the OpenRouter endpoint.

OpenRouter implements the OpenAI chat-completions API at
``https://openrouter.ai/api/v1``, so the ``openai`` Python SDK works
unchanged once the ``base_url`` is overridden. This adapter pre-bakes
that URL and reads the conventional ``OPENROUTER_API_KEY`` env var
(rather than ``OPENAI_API_KEY``, which would point at the wrong
account).

Notes
-----
OpenRouter's optional ``HTTP-Referer`` / ``X-Title`` attribution headers
are deliberately not sent — the API works without them and adding them
would widen the shared ``openai.OpenAI(...)`` construction in
:class:`OpenAIAdapter` for no local-tool benefit.
"""

from __future__ import annotations

import os
from typing import Any

import openai

from whygraph.core.config import OpenRouterConfig

from .openai import OpenAIAdapter


class OpenRouterAdapter(OpenAIAdapter):
    """OpenAI-compatible adapter pre-configured for the OpenRouter endpoint.

    Parameters
    ----------
    model : str, optional
        OpenRouter model identifier. Default ``"openrouter/auto"`` — lets
        OpenRouter pick a model for the prompt.
    api_key : str, optional
        Explicit API key. ``None`` (default) reads
        ``OPENROUTER_API_KEY`` from the environment.
    timeout_sec : int, optional
        Default per-request timeout.
    client : openai.OpenAI, optional
        Inject a preconfigured SDK client (useful for tests).
    """

    provider = "openrouter"
    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        *,
        model: str = "openrouter/auto",
        api_key: str | None = None,
        timeout_sec: int = 60,
        client: openai.OpenAI | None = None,
    ) -> None:
        resolved_key = api_key or os.environ.get("OPENROUTER_API_KEY")
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
        config: OpenRouterConfig,
        **overrides: Any,
    ) -> "OpenRouterAdapter":
        """Build an adapter from a typed :class:`OpenRouterConfig` section.

        Recognized ``overrides``: ``client`` — inject a preconfigured
        ``openai.OpenAI`` (useful for tests).
        """
        return cls(
            model=config.model,
            api_key=config.api_key,
            timeout_sec=config.timeout_sec,
            **overrides,
        )
