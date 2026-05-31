"""Provider-agnostic plumbing tests for the LLM service.

Covers :class:`Message` / :class:`CompletionRequest` /
:class:`CompletionResponse` value objects, the
:meth:`CompletionRequest.of` builder, and :class:`LlmClientFactory`
(config-driven construction, the unknown-provider error path, and the
``register`` extensibility hook).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass

import pytest

from whygraph.core.config import AnthropicConfig, LlmConfig
from whygraph.services.llm import (
    AnthropicAdapter,
    ClaudeCliAdapter,
    CompletionRequest,
    CompletionResponse,
    DeepSeekAdapter,
    LlmClient,
    LlmClientFactory,
    LlmError,
    Message,
    OllamaAdapter,
    OpenAIAdapter,
)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


def test_completion_request_of_single_user_message() -> None:
    req = CompletionRequest.of("hello")
    assert len(req.messages) == 1
    assert req.messages[0].role == "user"
    assert req.messages[0].content == "hello"


def test_completion_request_of_with_system_prepends_system_message() -> None:
    req = CompletionRequest.of("user content", system="be terse")
    assert len(req.messages) == 2
    assert req.messages[0].role == "system"
    assert req.messages[0].content == "be terse"
    assert req.messages[1].role == "user"
    assert req.messages[1].content == "user content"


def test_completion_request_of_forwards_overrides() -> None:
    req = CompletionRequest.of("x", max_tokens=128, temperature=0.2, timeout_sec=15)
    assert req.max_tokens == 128
    assert req.temperature == 0.2
    assert req.timeout_sec == 15


def test_message_is_frozen() -> None:
    m = Message(role="user", content="x")
    with pytest.raises(FrozenInstanceError):
        m.role = "assistant"  # type: ignore[misc]


def test_completion_request_is_frozen() -> None:
    req = CompletionRequest(messages=())
    with pytest.raises(FrozenInstanceError):
        req.messages = (Message(role="user", content="y"),)  # type: ignore[misc]


def test_completion_response_is_frozen() -> None:
    resp = CompletionResponse(text="x", model="m", provider="p")
    with pytest.raises(FrozenInstanceError):
        resp.text = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# LlmClientFactory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DummyConfig:
    model: str = "dummy-m"


class _DummyAdapter(LlmClient):
    """Test-only adapter with a no-op ``complete``."""

    provider = "dummy"

    @classmethod
    def from_config(cls, config: _DummyConfig) -> "_DummyAdapter":
        return cls(model=config.model)

    def complete(self, request):  # type: ignore[override]
        return CompletionResponse(text="", model=self.model, provider=self.provider)


def test_factory_make_unknown_provider_raises() -> None:
    factory = LlmClientFactory()
    with pytest.raises(LlmError, match="unknown LLM provider"):
        factory.make("nope")


def test_factory_make_uses_bound_config() -> None:
    """The factory reads its bound LlmConfig, not the global one."""

    class _StubSdkClient:  # keeps anthropic.Anthropic() out of the test path
        pass

    custom = AnthropicConfig(model="claude-test", timeout_sec=33)
    factory = LlmClientFactory(config=LlmConfig(anthropic=custom))
    adapter = factory.make("anthropic", client=_StubSdkClient())

    assert isinstance(adapter, AnthropicAdapter)
    assert adapter.model == "claude-test"
    assert adapter._default_timeout == 33


def test_factory_lists_builtin_providers() -> None:
    """Every concrete adapter is reachable under its ``provider`` tag."""
    factory = LlmClientFactory()
    expected_tags = {
        AnthropicAdapter.provider,
        OpenAIAdapter.provider,
        DeepSeekAdapter.provider,
        OllamaAdapter.provider,
        ClaudeCliAdapter.provider,
    }
    assert set(factory.providers) == expected_tags
    assert set(LlmClientFactory.BUILTIN_PROVIDERS) == expected_tags


def test_factory_register_adds_provider() -> None:
    factory = LlmClientFactory()
    factory.register("dummy", _DummyAdapter, config=_DummyConfig(model="custom-m"))

    assert "dummy" in factory.providers
    client = factory.make("dummy")
    assert isinstance(client, _DummyAdapter)
    assert client.model == "custom-m"


def test_factory_register_overrides_builtin() -> None:
    """Re-registering an existing tag replaces the built-in entry."""
    factory = LlmClientFactory()
    factory.register("anthropic", _DummyAdapter, config=_DummyConfig(model="forked"))

    client = factory.make("anthropic")
    assert isinstance(client, _DummyAdapter)
    assert client.model == "forked"


def test_factory_instances_have_independent_registries() -> None:
    a = LlmClientFactory()
    b = LlmClientFactory()
    a.register("dummy", _DummyAdapter, config=_DummyConfig())

    assert "dummy" in a.providers
    assert "dummy" not in b.providers
