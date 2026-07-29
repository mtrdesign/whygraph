"""LLM service: provider-agnostic completion clients.

Public API
----------
* :class:`LlmClient` — abstract port; all adapters implement
  :meth:`LlmClient.complete`.
* :class:`Message`, :class:`CompletionRequest`,
  :class:`CompletionResponse` — value objects exchanged across the port.
* :class:`AnthropicAdapter`, :class:`OpenAIAdapter`,
  :class:`DeepSeekAdapter`, :class:`OpenRouterAdapter`,
  :class:`OllamaAdapter`,
  :class:`ClaudeCliAdapter` — concrete adapters. Each has a typed
  ``from_config(<provider>Config)`` classmethod that maps the
  matching ``[llm.<provider>]`` TOML section onto the constructor.
* :class:`LlmClientFactory` — registry-backed factory for
  config-driven construction; supports runtime
  :meth:`~LlmClientFactory.register` of third-party adapters.
* :class:`LlmError` — single exception type for all provider failures.

The **chat port** is a second, parallel port for streaming tool-calling
conversation (:mod:`whygraph.services.llm.chat`): :class:`ChatClient` +
:func:`make_chat_client` over :class:`ChatMessage` / :class:`ChatRequest`,
yielding :class:`TextDelta` / :class:`ToolCallMade` / :class:`TurnDone`.
It exists beside :class:`LlmClient` rather than widening it — see that
module's docstring for why.

Examples
--------
Direct construction with explicit arguments::

    from whygraph.services.llm import AnthropicAdapter, CompletionRequest
    client = AnthropicAdapter(model="claude-opus-4-7")
    response = client.complete(CompletionRequest.of("Say hi.", system="Be terse."))
    print(response.text)

Config-driven construction via the factory (preferred for production wiring)::

    from whygraph.services.llm import LlmClientFactory, CompletionRequest
    factory = LlmClientFactory()                          # binds to get_config().llm
    client = factory.make("anthropic")                    # AnthropicAdapter
    print(client.complete(CompletionRequest.of("Hi.")).text)
"""

from .anthropic import AnthropicAdapter
from .anthropic_chat import AnthropicChatAdapter
from .chat import (
    CHAT_PROVIDERS,
    FALLBACK_MODELS,
    ChatClient,
    ChatMessage,
    ChatRequest,
    ChatRole,
    ChatStreamEvent,
    ModelInfo,
    TextDelta,
    ToolCall,
    ToolCallMade,
    ToolSpec,
    TurnDone,
    chat_provider_env_var,
    fallback_models,
    make_chat_client,
)
from .claude_cli import ClaudeCliAdapter
from .client import LlmClient
from .deepseek import DeepSeekAdapter
from .exceptions import LlmError
from .factory import LlmClientFactory
from .ollama import OllamaAdapter
from .openai import OpenAIAdapter
from .openai_chat import OpenAIChatAdapter
from .openrouter import OpenRouterAdapter
from .types import CompletionRequest, CompletionResponse, Message

__all__ = [
    "CHAT_PROVIDERS",
    "FALLBACK_MODELS",
    "AnthropicAdapter",
    "AnthropicChatAdapter",
    "ChatClient",
    "ChatMessage",
    "ChatRequest",
    "ChatRole",
    "ChatStreamEvent",
    "ClaudeCliAdapter",
    "CompletionRequest",
    "CompletionResponse",
    "DeepSeekAdapter",
    "LlmClient",
    "LlmClientFactory",
    "LlmError",
    "Message",
    "ModelInfo",
    "OllamaAdapter",
    "OpenAIAdapter",
    "OpenAIChatAdapter",
    "OpenRouterAdapter",
    "TextDelta",
    "ToolCall",
    "ToolCallMade",
    "ToolSpec",
    "TurnDone",
    "chat_provider_env_var",
    "fallback_models",
    "make_chat_client",
]
