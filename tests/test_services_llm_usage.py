"""Usage capture in the completion adapters (M2f-2 plan §4.2, §6.1 #1).

Every adapter normalizes its provider's usage block to one meaning:
``input_tokens`` is every prompt token (cache reads and writes included),
``cache_*`` are subsets of it, ``output_tokens`` includes reasoning and
``reasoning_tokens`` is a subset. Only OpenRouter reports a cost.

The fixtures are **real SDK models** validated from response-shaped dicts
(``ChatCompletion.model_validate`` / ``Message.model_validate``), so the
fields the SDK does not type - OpenRouter's ``cost``, DeepSeek's
``prompt_cache_hit_tokens`` - arrive the way they do in production: in
pydantic's ``model_extra``, with nested extras as plain dicts.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from anthropic.types import Message as AnthropicMessage
from openai.types.chat import ChatCompletion

from whygraph.services.llm import (
    AnthropicAdapter,
    CompletionRequest,
    DeepSeekAdapter,
    OllamaAdapter,
    OpenAIAdapter,
    OpenRouterAdapter,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeOpenAI:
    """Stand-in for ``openai.OpenAI`` returning one canned completion."""

    def __init__(self, result) -> None:
        self._result = result
        self.last_kwargs: dict | None = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._result


class _FakeAnthropic:
    def __init__(self, result) -> None:
        self._result = result
        self.messages = SimpleNamespace(create=lambda **kwargs: self._result)


def _completion(usage: dict | None, *, model: str = "served-model") -> ChatCompletion:
    payload = {
        "id": "cmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "hi"},
            }
        ],
    }
    if usage is not None:
        payload["usage"] = usage
    return ChatCompletion.model_validate(payload)


def _request() -> CompletionRequest:
    return CompletionRequest.of("hello")


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def test_anthropic_input_includes_cache_reads_and_writes() -> None:
    message = AnthropicMessage.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-20261001",
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 12,
                "output_tokens": 40,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 88,
            },
        }
    )
    adapter = AnthropicAdapter(model="claude-opus-5", client=_FakeAnthropic(message))
    response = adapter.complete(_request())

    assert response.input_tokens == 12 + 900 + 88
    assert response.cache_read_tokens == 900
    assert response.cache_write_tokens == 88
    assert response.output_tokens == 40
    assert response.reasoning_tokens is None
    assert response.cost_usd is None
    assert response.model == "claude-opus-5-20261001"


def test_anthropic_without_cache_fields() -> None:
    message = AnthropicMessage.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 40},
        }
    )
    response = AnthropicAdapter(
        model="claude-haiku-4-5", client=_FakeAnthropic(message)
    ).complete(_request())
    assert (response.input_tokens, response.output_tokens) == (12, 40)
    assert response.cache_read_tokens is None
    assert response.cache_write_tokens is None


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


def test_openai_reads_cached_and_reasoning_details() -> None:
    result = _completion(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 300,
            "total_tokens": 1300,
            "prompt_tokens_details": {"cached_tokens": 600},
            "completion_tokens_details": {"reasoning_tokens": 120},
        },
        model="gpt-5-2026-08-01",
    )
    adapter = OpenAIAdapter(model="gpt-5", client=_FakeOpenAI(result))
    response = adapter.complete(_request())

    # prompt_tokens already includes the cached part: taken as is.
    assert response.input_tokens == 1000
    assert response.cache_read_tokens == 600
    assert response.cache_write_tokens is None
    assert response.output_tokens == 300
    assert response.reasoning_tokens == 120
    assert response.cost_usd is None
    assert response.model == "gpt-5-2026-08-01"


def test_openai_without_details() -> None:
    """Compatible endpoints often omit every ``*_details`` object."""
    result = _completion(
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "prompt_tokens_details": None,
            "completion_tokens_details": None,
        }
    )
    response = OpenAIAdapter(model="m", client=_FakeOpenAI(result)).complete(_request())
    assert (response.input_tokens, response.output_tokens) == (10, 5)
    assert response.cache_read_tokens is None
    assert response.reasoning_tokens is None


def test_openai_without_usage() -> None:
    response = OpenAIAdapter(model="m", client=_FakeOpenAI(_completion(None))).complete(
        _request()
    )
    assert response.input_tokens is None
    assert response.output_tokens is None
    assert response.cost_usd is None


def test_openai_ignores_a_cost_field_it_does_not_own() -> None:
    """Only OpenRouter's cost is trusted; a compatible endpoint's is not."""
    result = _completion(
        {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 9.9}
    )
    response = OpenAIAdapter(model="m", client=_FakeOpenAI(result)).complete(_request())
    assert response.cost_usd is None


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------


def test_openrouter_cost_byok_upstream_and_cache_write() -> None:
    result = _completion(
        {
            "prompt_tokens": 2000,
            "completion_tokens": 100,
            "total_tokens": 2100,
            "prompt_tokens_details": {"cached_tokens": 1500, "cache_write_tokens": 300},
            "completion_tokens_details": {"reasoning_tokens": 40},
            "cost": 0.0012,
            "is_byok": True,
            "cost_details": {"upstream_inference_cost": 0.0345},
        },
        model="anthropic/claude-sonnet-5",
    )
    fake = _FakeOpenAI(result)
    response = OpenRouterAdapter(model="openrouter/auto", client=fake).complete(
        _request()
    )

    assert response.provider == "openrouter"
    assert response.model == "anthropic/claude-sonnet-5"
    assert response.input_tokens == 2000
    assert response.cache_read_tokens == 1500
    assert response.cache_write_tokens == 300
    assert response.output_tokens == 100
    assert response.reasoning_tokens == 40
    # BYOK: `cost` is only OpenRouter's fee; the provider's charge is upstream.
    assert response.cost_usd == pytest.approx(0.0012 + 0.0345)
    # Usage is always returned; the deprecated `usage: {include: true}` is not sent.
    assert fake.last_kwargs is not None
    assert "extra_body" not in fake.last_kwargs
    assert "usage" not in fake.last_kwargs


def test_openrouter_cost_without_byok() -> None:
    result = _completion(
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "cost": 0.5,
            "is_byok": False,
            # Echoed on a non-BYOK request: never added on top.
            "cost_details": {"upstream_inference_cost": 0.5},
        }
    )
    response = OpenRouterAdapter(client=_FakeOpenAI(result)).complete(_request())
    assert response.cost_usd == pytest.approx(0.5)
    assert response.cache_write_tokens is None


def test_openrouter_without_cost() -> None:
    result = _completion(
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    )
    response = OpenRouterAdapter(client=_FakeOpenAI(result)).complete(_request())
    assert response.cost_usd is None


# ---------------------------------------------------------------------------
# DeepSeek
# ---------------------------------------------------------------------------


def test_deepseek_cache_read_from_prompt_tokens_details() -> None:
    result = _completion(
        {
            "prompt_tokens": 500,
            "completion_tokens": 50,
            "total_tokens": 550,
            "prompt_tokens_details": {"cached_tokens": 320},
            "prompt_cache_hit_tokens": 999,
        }
    )
    response = DeepSeekAdapter(client=_FakeOpenAI(result)).complete(_request())
    assert response.cache_read_tokens == 320
    assert response.input_tokens == 500
    assert response.cost_usd is None


def test_deepseek_cache_read_from_top_level_hit_tokens() -> None:
    result = _completion(
        {
            "prompt_tokens": 500,
            "completion_tokens": 50,
            "total_tokens": 550,
            "prompt_cache_hit_tokens": 320,
            "prompt_cache_miss_tokens": 180,
        }
    )
    response = DeepSeekAdapter(client=_FakeOpenAI(result)).complete(_request())
    assert response.cache_read_tokens == 320
    assert response.input_tokens == 500


def test_hit_tokens_fallback_is_deepseek_only() -> None:
    result = _completion(
        {
            "prompt_tokens": 500,
            "completion_tokens": 50,
            "total_tokens": 550,
            "prompt_cache_hit_tokens": 320,
        }
    )
    response = OpenAIAdapter(model="m", client=_FakeOpenAI(result)).complete(_request())
    assert response.cache_read_tokens is None


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


def test_ollama_reads_eval_counts_and_reports_no_cost() -> None:
    fake = SimpleNamespace(
        chat=lambda **kwargs: {
            "model": "llama3",
            "message": {"role": "assistant", "content": "hi"},
            "prompt_eval_count": 33,
            "eval_count": 11,
            "done_reason": "stop",
        }
    )
    response = OllamaAdapter(model="llama3", client=fake).complete(_request())
    assert (response.input_tokens, response.output_tokens) == (33, 11)
    assert response.cache_read_tokens is None
    assert response.cache_write_tokens is None
    assert response.reasoning_tokens is None
    assert response.cost_usd is None
