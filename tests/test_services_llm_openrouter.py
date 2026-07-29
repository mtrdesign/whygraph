"""Tests for :class:`whygraph.services.llm.OpenRouterAdapter`.

OpenRouter is an :class:`OpenAIAdapter` subclass that only pins a
``base_url`` and swaps the env var it reads, so the tests here cover
exactly those two behaviours plus the ``from_config`` mapping. No network
call is made — the SDK client is lazy (``openai.py:63-72``).
"""

from __future__ import annotations

from whygraph.core.config import LlmConfig, OpenRouterConfig
from whygraph.services.llm import LlmClientFactory, OpenAIAdapter, OpenRouterAdapter


def test_pins_the_openrouter_base_url_and_default_model() -> None:
    adapter = OpenRouterAdapter()
    assert isinstance(adapter, OpenAIAdapter)
    assert adapter.provider == "openrouter"
    assert adapter.model == "openrouter/auto"
    assert adapter._base_url == "https://openrouter.ai/api/v1"


def test_reads_openrouter_api_key_env_var(monkeypatch) -> None:
    """The key comes from OPENROUTER_API_KEY, not OPENAI_API_KEY."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-from-env")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-wrong-account")
    assert OpenRouterAdapter()._api_key == "sk-or-from-env"


def test_explicit_api_key_wins_over_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-from-env")
    assert OpenRouterAdapter(api_key="sk-or-explicit")._api_key == "sk-or-explicit"


def test_from_config_maps_the_section() -> None:
    cfg = OpenRouterConfig(
        model="anthropic/claude-sonnet-4",
        api_key="sk-or-cfg",
        timeout_sec=42,
    )
    adapter = OpenRouterAdapter.from_config(cfg)
    assert adapter.model == "anthropic/claude-sonnet-4"
    assert adapter._api_key == "sk-or-cfg"
    assert adapter._default_timeout == 42


def test_factory_builds_openrouter_from_llm_config() -> None:
    """The provider tag resolves through the factory registry (§3)."""
    config = LlmConfig(openrouter=OpenRouterConfig(model="openai/gpt-4o-mini"))
    client = LlmClientFactory(config).make("openrouter")
    assert isinstance(client, OpenRouterAdapter)
    assert client.model == "openai/gpt-4o-mini"
