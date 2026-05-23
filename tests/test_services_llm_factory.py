"""Tests for :class:`whygraph.services.llm.LlmClientFactory`.

Uses the ``ollama`` adapter as the subject — it constructs from an
:class:`OllamaConfig` with no API key and no network call, so building a
client is cheap and side-effect free.
"""

from __future__ import annotations

import pytest

from whygraph.core.config import LlmConfig
from whygraph.services.llm import LlmClientFactory, LlmError


def test_make_uses_the_providers_configured_model() -> None:
    client = LlmClientFactory(LlmConfig()).make("ollama")
    assert client.model == "llama3"  # OllamaConfig default


def test_make_applies_model_override() -> None:
    client = LlmClientFactory(LlmConfig()).make("ollama", model="qwen2.5-coder")
    assert client.model == "qwen2.5-coder"


def test_make_unknown_provider_raises() -> None:
    with pytest.raises(LlmError, match="unknown LLM provider"):
        LlmClientFactory(LlmConfig()).make("nonesuch")
