"""LLM service: provider-agnostic completion clients.

Public API
----------
* :class:`LlmClient` — abstract port; all adapters implement
  :meth:`LlmClient.complete`.
* :class:`Message`, :class:`CompletionRequest`,
  :class:`CompletionResponse` — value objects exchanged across the port.
* :class:`AnthropicAdapter`, :class:`OpenAIAdapter`,
  :class:`DeepSeekAdapter`, :class:`OllamaAdapter`,
  :class:`ClaudeCliAdapter` — concrete adapters. Each has a typed
  ``from_config(<provider>Config)`` classmethod that maps the
  matching ``[llm.<provider>]`` TOML section onto the constructor.
* :class:`LlmClientFactory` — registry-backed factory for
  config-driven construction; supports runtime
  :meth:`~LlmClientFactory.register` of third-party adapters.
* :class:`LlmError` — single exception type for all provider failures.

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
from .claude_cli import ClaudeCliAdapter
from .client import LlmClient
from .deepseek import DeepSeekAdapter
from .exceptions import LlmError
from .factory import LlmClientFactory
from .ollama import OllamaAdapter
from .openai import OpenAIAdapter
from .types import CompletionRequest, CompletionResponse, Message

__all__ = [
    "AnthropicAdapter",
    "ClaudeCliAdapter",
    "CompletionRequest",
    "CompletionResponse",
    "DeepSeekAdapter",
    "LlmClient",
    "LlmClientFactory",
    "LlmError",
    "Message",
    "OllamaAdapter",
    "OpenAIAdapter",
]
