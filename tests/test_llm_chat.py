"""Tests for the chat port and its two streaming adapters (plan §4, §14).

Both adapters are driven by **fake SDK clients** injected through the
``client=`` seam, replaying recorded-shape chunk sequences. The point of
these tests is the normalization contract (risk #1 in the plan): two very
different wire dialects must produce one identical event stream, including
the awkward cases — parallel calls, argument JSON split across fragments,
and malformed argument payloads.

The fakes use ``SimpleNamespace`` rather than real SDK models because the
adapters read every field with ``getattr(..., default)``; duck-typing is
exactly what they consume.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import openai
import pytest

from whygraph.core.config import (
    AnthropicConfig,
    DeepSeekConfig,
    LlmConfig,
    OpenAIConfig,
    OpenRouterConfig,
)
from whygraph.services.llm import (
    CHAT_PROVIDERS,
    AnthropicChatAdapter,
    ChatMessage,
    ChatRequest,
    LlmError,
    OpenAIChatAdapter,
    TextDelta,
    ToolCall,
    ToolCallMade,
    ToolSpec,
    TurnDone,
    chat_provider_env_var,
    fallback_models,
    make_chat_client,
)

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

SEARCH_TOOL = ToolSpec(
    name="search_symbols",
    description="Find symbols by name.",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
)


def _request(*messages: ChatMessage, tools=(SEARCH_TOOL,)) -> ChatRequest:
    return ChatRequest(messages=messages or (user("hi"),), tools=tools)


def user(content: str) -> ChatMessage:
    return ChatMessage(role="user", content=content)


# ---------------------------------------------------------------------------
# OpenAI dialect: fake stream construction
# ---------------------------------------------------------------------------


def _oa_chunk(*, content=None, tool_calls=None, finish_reason=None, usage=None):
    """One chat-completions stream chunk.

    A chunk with ``usage`` and no choices is the trailing usage frame that
    ``stream_options={"include_usage": True}`` requests.
    """
    if usage is not None and content is None and tool_calls is None:
        return SimpleNamespace(choices=[], usage=usage)
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        usage=usage,
    )


def _oa_tool_delta(index, *, call_id=None, name=None, arguments=None):
    """One entry of a chunk's ``delta.tool_calls`` array."""
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _oa_usage(prompt=11, completion=22):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)


class _FakeOpenAI:
    """Minimal stand-in for ``openai.OpenAI`` that replays ``chunks``.

    Records the kwargs it was called with on :attr:`last_kwargs` so
    translation tests can inspect the wire payload.
    """

    def __init__(self, chunks, *, raise_on_create: Exception | None = None) -> None:
        self._chunks = chunks
        self._raise = raise_on_create
        self.last_kwargs: dict | None = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.last_kwargs = kwargs
        if self._raise is not None:
            raise self._raise
        return iter(self._chunks)


def _openai_adapter(chunks, **kwargs) -> tuple[OpenAIChatAdapter, _FakeOpenAI]:
    fake = _FakeOpenAI(chunks, **kwargs)
    return OpenAIChatAdapter(model="gpt-4o", client=fake), fake


# ---------------------------------------------------------------------------
# OpenAI dialect: event normalization
# ---------------------------------------------------------------------------


def test_openai_text_only_stream() -> None:
    adapter, _ = _openai_adapter(
        [
            _oa_chunk(content="Hel"),
            _oa_chunk(content="lo."),
            _oa_chunk(finish_reason="stop"),
            _oa_chunk(usage=_oa_usage()),
        ]
    )
    events = list(adapter.stream_turn(_request()))

    assert events[:2] == [TextDelta(text="Hel"), TextDelta(text="lo.")]
    assert events[-1] == TurnDone(
        finish_reason="stop", input_tokens=11, output_tokens=22
    )
    assert len(events) == 3


def test_openai_single_tool_call_with_fragmented_arguments() -> None:
    """Argument JSON split across chunks joins into one parsed dict."""
    adapter, _ = _openai_adapter(
        [
            _oa_chunk(content="Looking… "),
            _oa_chunk(
                tool_calls=[_oa_tool_delta(0, call_id="call_1", name="search_symbols")]
            ),
            _oa_chunk(tool_calls=[_oa_tool_delta(0, arguments='{"que')]),
            _oa_chunk(tool_calls=[_oa_tool_delta(0, arguments='ry": "run_')]),
            _oa_chunk(tool_calls=[_oa_tool_delta(0, arguments='turn"}')]),
            _oa_chunk(finish_reason="tool_calls"),
        ]
    )
    events = list(adapter.stream_turn(_request()))

    assert events[0] == TextDelta(text="Looking… ")
    assert events[1] == ToolCallMade(
        call=ToolCall(
            id="call_1", name="search_symbols", arguments={"query": "run_turn"}
        )
    )
    assert events[2] == TurnDone(finish_reason="tool_calls")


def test_openai_parallel_tool_calls_emit_in_index_order() -> None:
    """Two calls interleaved across chunks flush in index order."""
    adapter, _ = _openai_adapter(
        [
            _oa_chunk(
                tool_calls=[
                    _oa_tool_delta(0, call_id="call_a", name="search_symbols"),
                    _oa_tool_delta(1, call_id="call_b", name="get_symbol"),
                ]
            ),
            # Fragments arrive out of index order on the wire.
            _oa_chunk(tool_calls=[_oa_tool_delta(1, arguments='{"qualified_name":')]),
            _oa_chunk(tool_calls=[_oa_tool_delta(0, arguments='{"query": "x"}')]),
            _oa_chunk(tool_calls=[_oa_tool_delta(1, arguments=' "a.b"}')]),
            _oa_chunk(finish_reason="tool_calls"),
        ]
    )
    events = list(adapter.stream_turn(_request()))

    assert [e.call.id for e in events if isinstance(e, ToolCallMade)] == [
        "call_a",
        "call_b",
    ]
    assert events[1].call.arguments == {"qualified_name": "a.b"}


def test_openai_zero_argument_tool_call_yields_empty_dict() -> None:
    adapter, _ = _openai_adapter(
        [
            _oa_chunk(
                tool_calls=[_oa_tool_delta(0, call_id="c1", name="get_repo_overview")]
            ),
            _oa_chunk(finish_reason="tool_calls"),
        ]
    )
    events = list(adapter.stream_turn(_request()))
    assert events[0].call.arguments == {}


def test_openai_malformed_tool_arguments_raise_llm_error() -> None:
    adapter, _ = _openai_adapter(
        [
            _oa_chunk(
                tool_calls=[_oa_tool_delta(0, call_id="c1", name="search_symbols")]
            ),
            _oa_chunk(tool_calls=[_oa_tool_delta(0, arguments='{"query": "unclosed')]),
            _oa_chunk(finish_reason="tool_calls"),
        ]
    )
    with pytest.raises(LlmError, match="malformed arguments"):
        list(adapter.stream_turn(_request()))


def test_openai_non_object_tool_arguments_raise_llm_error() -> None:
    adapter, _ = _openai_adapter(
        [
            _oa_chunk(
                tool_calls=[_oa_tool_delta(0, call_id="c1", name="search_symbols")]
            ),
            _oa_chunk(
                tool_calls=[_oa_tool_delta(0, arguments='["not", "an", "object"]')]
            ),
            _oa_chunk(finish_reason="tool_calls"),
        ]
    )
    with pytest.raises(LlmError, match="must be a JSON object"):
        list(adapter.stream_turn(_request()))


def test_openai_truncated_stream_still_flushes_and_finishes() -> None:
    """A stream ending without finish_reason still yields its call + TurnDone."""
    adapter, _ = _openai_adapter(
        [
            _oa_chunk(
                tool_calls=[_oa_tool_delta(0, call_id="c1", name="search_symbols")]
            ),
            _oa_chunk(tool_calls=[_oa_tool_delta(0, arguments='{"query": "x"}')]),
        ]
    )
    events = list(adapter.stream_turn(_request()))
    assert isinstance(events[0], ToolCallMade)
    assert events[-1] == TurnDone(finish_reason=None)


def test_openai_api_error_becomes_llm_error() -> None:
    adapter, _ = _openai_adapter(
        [], raise_on_create=openai.APIError("boom", request=None, body=None)
    )
    with pytest.raises(LlmError, match="openai API error"):
        list(adapter.stream_turn(_request()))


def test_openai_empty_message_list_raises() -> None:
    adapter, _ = _openai_adapter([])
    with pytest.raises(LlmError, match="at least one message"):
        list(adapter.stream_turn(ChatRequest(messages=())))


# ---------------------------------------------------------------------------
# OpenAI dialect: request translation
# ---------------------------------------------------------------------------


def test_openai_translates_tool_round_trip_payload() -> None:
    """Assistant tool_calls + tool results render in the wire shape."""
    adapter, fake = _openai_adapter([_oa_chunk(finish_reason="stop")])
    call = ToolCall(id="call_1", name="search_symbols", arguments={"query": "x"})
    request = ChatRequest(
        messages=(
            ChatMessage(role="system", content="You are WhyGraph."),
            user("find x"),
            ChatMessage(role="assistant", content="", tool_calls=(call,)),
            ChatMessage(role="tool", content='{"hits": []}', tool_call_id="call_1"),
        ),
        tools=(SEARCH_TOOL,),
    )
    list(adapter.stream_turn(request))

    messages = fake.last_kwargs["messages"]
    assert messages[0] == {"role": "system", "content": "You are WhyGraph."}
    assistant = messages[2]
    # Arguments go over the wire as a JSON *string*, not an object.
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"query": "x"}'
    assert assistant["content"] is None  # empty content elided
    assert messages[3] == {
        "role": "tool",
        "content": '{"hits": []}',
        "tool_call_id": "call_1",
    }
    assert fake.last_kwargs["tools"][0]["function"]["name"] == "search_symbols"
    assert fake.last_kwargs["stream"] is True
    assert fake.last_kwargs["stream_options"] == {"include_usage": True}


def test_openai_omits_tools_key_when_no_tools_offered() -> None:
    adapter, fake = _openai_adapter([_oa_chunk(finish_reason="stop")])
    list(adapter.stream_turn(ChatRequest(messages=(user("hi"),), tools=())))
    assert "tools" not in fake.last_kwargs


# ---------------------------------------------------------------------------
# Anthropic dialect: fake stream construction
# ---------------------------------------------------------------------------


def _an_message_start(input_tokens=13):
    return SimpleNamespace(
        type="message_start",
        message=SimpleNamespace(usage=SimpleNamespace(input_tokens=input_tokens)),
    )


def _an_text_block_start(index=0):
    return SimpleNamespace(
        type="content_block_start",
        index=index,
        content_block=SimpleNamespace(type="text"),
    )


def _an_text_delta(text, index=0):
    return SimpleNamespace(
        type="content_block_delta",
        index=index,
        delta=SimpleNamespace(type="text_delta", text=text),
    )


def _an_tool_block_start(index, call_id, name):
    return SimpleNamespace(
        type="content_block_start",
        index=index,
        content_block=SimpleNamespace(type="tool_use", id=call_id, name=name),
    )


def _an_json_delta(index, partial_json):
    return SimpleNamespace(
        type="content_block_delta",
        index=index,
        delta=SimpleNamespace(type="input_json_delta", partial_json=partial_json),
    )


def _an_block_stop(index):
    return SimpleNamespace(type="content_block_stop", index=index)


def _an_message_delta(stop_reason="end_turn", output_tokens=44):
    return SimpleNamespace(
        type="message_delta",
        delta=SimpleNamespace(stop_reason=stop_reason),
        usage=SimpleNamespace(output_tokens=output_tokens),
    )


class _FakeAnthropicStream:
    def __init__(self, events) -> None:
        self._events = events

    def __enter__(self):
        return iter(self._events)

    def __exit__(self, *exc) -> bool:
        return False


class _FakeAnthropic:
    """Minimal stand-in for ``anthropic.Anthropic``."""

    def __init__(self, events, *, raise_on_stream: Exception | None = None) -> None:
        self._events = events
        self._raise = raise_on_stream
        self.last_kwargs: dict | None = None
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs):
        self.last_kwargs = kwargs
        if self._raise is not None:
            raise self._raise
        return _FakeAnthropicStream(self._events)


def _anthropic_adapter(events, **kwargs) -> tuple[AnthropicChatAdapter, _FakeAnthropic]:
    fake = _FakeAnthropic(events, **kwargs)
    return AnthropicChatAdapter(model="claude-opus-4-7", client=fake), fake


# ---------------------------------------------------------------------------
# Anthropic dialect: event normalization
# ---------------------------------------------------------------------------


def test_anthropic_text_only_stream() -> None:
    adapter, _ = _anthropic_adapter(
        [
            _an_message_start(),
            _an_text_block_start(),
            _an_text_delta("Hel"),
            _an_text_delta("lo."),
            _an_block_stop(0),
            _an_message_delta(),
        ]
    )
    events = list(adapter.stream_turn(_request()))

    assert events[:2] == [TextDelta(text="Hel"), TextDelta(text="lo.")]
    assert events[-1] == TurnDone(
        finish_reason="end_turn", input_tokens=13, output_tokens=44
    )
    assert len(events) == 3


def test_anthropic_tool_use_block_normalizes_to_tool_call_made() -> None:
    """Same event shape as the OpenAI dialect produces (zero drift)."""
    adapter, _ = _anthropic_adapter(
        [
            _an_message_start(),
            _an_text_block_start(0),
            _an_text_delta("Looking… ", 0),
            _an_block_stop(0),
            _an_tool_block_start(1, "toolu_1", "search_symbols"),
            _an_json_delta(1, '{"que'),
            _an_json_delta(1, 'ry": "run_turn"}'),
            _an_block_stop(1),
            _an_message_delta(stop_reason="tool_use"),
        ]
    )
    events = list(adapter.stream_turn(_request()))

    assert events[0] == TextDelta(text="Looking… ")
    assert events[1] == ToolCallMade(
        call=ToolCall(
            id="toolu_1", name="search_symbols", arguments={"query": "run_turn"}
        )
    )
    assert events[2].finish_reason == "tool_use"


def test_anthropic_parallel_tool_blocks_emit_at_their_own_stop() -> None:
    """Each block flushes when it closes, so order follows block order."""
    adapter, _ = _anthropic_adapter(
        [
            _an_message_start(),
            _an_tool_block_start(0, "toolu_a", "search_symbols"),
            _an_json_delta(0, '{"query": "x"}'),
            _an_block_stop(0),
            _an_tool_block_start(1, "toolu_b", "get_symbol"),
            _an_json_delta(1, '{"qualified_name": "a.b"}'),
            _an_block_stop(1),
            _an_message_delta(stop_reason="tool_use"),
        ]
    )
    events = list(adapter.stream_turn(_request()))
    assert [e.call.id for e in events if isinstance(e, ToolCallMade)] == [
        "toolu_a",
        "toolu_b",
    ]


def test_anthropic_zero_argument_tool_block_yields_empty_dict() -> None:
    adapter, _ = _anthropic_adapter(
        [
            _an_tool_block_start(0, "toolu_1", "get_repo_overview"),
            _an_block_stop(0),
            _an_message_delta(stop_reason="tool_use"),
        ]
    )
    events = list(adapter.stream_turn(_request()))
    assert events[0].call.arguments == {}


def test_anthropic_malformed_tool_input_raises_llm_error() -> None:
    adapter, _ = _anthropic_adapter(
        [
            _an_tool_block_start(0, "toolu_1", "search_symbols"),
            _an_json_delta(0, '{"query": "unclosed'),
            _an_block_stop(0),
        ]
    )
    with pytest.raises(LlmError, match="malformed input"):
        list(adapter.stream_turn(_request()))


def test_anthropic_truncated_stream_still_flushes_open_block() -> None:
    adapter, _ = _anthropic_adapter(
        [
            _an_tool_block_start(0, "toolu_1", "search_symbols"),
            _an_json_delta(0, '{"query": "x"}'),
            # no content_block_stop, no message_delta
        ]
    )
    events = list(adapter.stream_turn(_request()))
    assert isinstance(events[0], ToolCallMade)
    assert events[-1] == TurnDone(finish_reason=None)


def test_anthropic_api_error_becomes_llm_error() -> None:
    import anthropic

    adapter, _ = _anthropic_adapter(
        [], raise_on_stream=anthropic.APIError("boom", request=None, body=None)
    )
    with pytest.raises(LlmError, match="anthropic API error"):
        list(adapter.stream_turn(_request()))


def test_anthropic_system_only_request_raises() -> None:
    adapter, _ = _anthropic_adapter([])
    request = ChatRequest(messages=(ChatMessage(role="system", content="hi"),))
    with pytest.raises(LlmError, match="at least one non-system message"):
        list(adapter.stream_turn(request))


# ---------------------------------------------------------------------------
# Anthropic dialect: request translation
# ---------------------------------------------------------------------------


def test_anthropic_hoists_system_and_uses_input_schema() -> None:
    adapter, fake = _anthropic_adapter([_an_message_delta()])
    request = ChatRequest(
        messages=(
            ChatMessage(role="system", content="First."),
            ChatMessage(role="system", content="Second."),
            user("go"),
        ),
        tools=(SEARCH_TOOL,),
    )
    list(adapter.stream_turn(request))

    assert fake.last_kwargs["system"] == "First.\n\nSecond."
    assert [m["role"] for m in fake.last_kwargs["messages"]] == ["user"]
    tool = fake.last_kwargs["tools"][0]
    assert "input_schema" in tool and "parameters" not in tool
    assert fake.last_kwargs["max_tokens"] == 4096  # API requires one


def test_anthropic_encodes_tool_use_and_tool_result_blocks() -> None:
    """Assistant calls → tool_use blocks; tool results → a user message."""
    adapter, fake = _anthropic_adapter([_an_message_delta()])
    call = ToolCall(id="toolu_1", name="search_symbols", arguments={"query": "x"})
    request = ChatRequest(
        messages=(
            user("find x"),
            ChatMessage(role="assistant", content="Looking.", tool_calls=(call,)),
            ChatMessage(role="tool", content='{"hits": []}', tool_call_id="toolu_1"),
            user("thanks"),
        ),
    )
    list(adapter.stream_turn(request))

    messages = fake.last_kwargs["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "user"]
    assert messages[1]["content"] == [
        {"type": "text", "text": "Looking."},
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "search_symbols",
            "input": {"query": "x"},
        },
    ]
    assert messages[2]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": '{"hits": []}',
        }
    ]


def test_anthropic_merges_parallel_tool_results_into_one_user_message() -> None:
    """Anthropic requires sibling tool_results in a single user turn."""
    adapter, fake = _anthropic_adapter([_an_message_delta()])
    calls = (
        ToolCall(id="t1", name="search_symbols", arguments={"query": "a"}),
        ToolCall(id="t2", name="get_symbol", arguments={"qualified_name": "b"}),
    )
    request = ChatRequest(
        messages=(
            user("both please"),
            ChatMessage(role="assistant", content="", tool_calls=calls),
            ChatMessage(role="tool", content="{}", tool_call_id="t1"),
            ChatMessage(role="tool", content="{}", tool_call_id="t2"),
        ),
    )
    list(adapter.stream_turn(request))

    messages = fake.last_kwargs["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert [b["tool_use_id"] for b in messages[2]["content"]] == ["t1", "t2"]
    # An assistant turn with no prose emits only tool_use blocks.
    assert [b["type"] for b in messages[1]["content"]] == ["tool_use", "tool_use"]


# ---------------------------------------------------------------------------
# make_chat_client
# ---------------------------------------------------------------------------


def test_make_chat_client_routes_compat_providers_to_one_adapter() -> None:
    config = LlmConfig(
        openai=OpenAIConfig(model="gpt-4o"),
        deepseek=DeepSeekConfig(model="deepseek-chat"),
        openrouter=OpenRouterConfig(model="openrouter/auto"),
    )
    expected_urls = {
        "openai": None,  # SDK default
        "deepseek": "https://api.deepseek.com/v1",
        "openrouter": "https://openrouter.ai/api/v1",
    }
    for provider, url in expected_urls.items():
        client = make_chat_client(provider, config=config)
        assert isinstance(client, OpenAIChatAdapter)
        assert client.provider == provider
        assert client._base_url == url


def test_make_chat_client_routes_anthropic_to_its_own_adapter() -> None:
    client = make_chat_client(
        "anthropic",
        config=LlmConfig(anthropic=AnthropicConfig(model="claude-opus-4-7")),
    )
    assert isinstance(client, AnthropicChatAdapter)
    assert client.model == "claude-opus-4-7"


def test_make_chat_client_model_override_and_empty_string_fallthrough() -> None:
    config = LlmConfig(openai=OpenAIConfig(model="gpt-4o"))
    assert make_chat_client("openai", model="gpt-5", config=config).model == "gpt-5"
    # A blank UI field must not become the model name.
    assert make_chat_client("openai", model="", config=config).model == "gpt-4o"


@pytest.mark.parametrize("provider", ["ollama", "claude-cli", "nonesuch"])
def test_make_chat_client_rejects_non_chat_providers(provider: str) -> None:
    with pytest.raises(LlmError, match="is not a chat provider"):
        make_chat_client(provider, config=LlmConfig())


def test_chat_providers_and_env_var_map_agree() -> None:
    assert CHAT_PROVIDERS == ("anthropic", "openai", "deepseek", "openrouter")
    assert chat_provider_env_var("openrouter") == "OPENROUTER_API_KEY"
    assert chat_provider_env_var("ollama") is None


def test_openai_adapter_json_round_trip_is_lossless() -> None:
    """Arguments survive dict → wire string → parsed dict unchanged."""
    arguments = {"path": "src/a.py", "start_line": 1, "nested": {"x": [1, 2]}}
    adapter, fake = _openai_adapter([_oa_chunk(finish_reason="stop")])
    call = ToolCall(id="c1", name="read_file", arguments=arguments)
    list(
        adapter.stream_turn(
            ChatRequest(
                messages=(
                    user("go"),
                    ChatMessage(role="assistant", tool_calls=(call,)),
                )
            )
        )
    )
    wire = fake.last_kwargs["messages"][1]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(wire) == arguments


# ---------------------------------------------------------------------------
# SDK-base-exception handling
# ---------------------------------------------------------------------------
#
# The SDKs raise their *base* error class (openai.OpenAIError /
# anthropic.AnthropicError) for missing credentials at client-construction
# time — APIError is only for responses that came back. Catching APIError
# alone let that escape as an unhandled 500 from `GET /api/chat/models`, so
# both adapters catch the base class instead.


def test_openai_missing_credentials_names_the_env_var(monkeypatch) -> None:
    """An unconfigured provider is a clear LlmError, not an SDK error."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    adapter = OpenAIChatAdapter(model="gpt-4o")  # no client= → lazy real SDK

    for call in (adapter.list_models, lambda: list(adapter.stream_turn(_request()))):
        with pytest.raises(LlmError, match="openai is not configured"):
            call()


def test_deepseek_missing_credentials_names_its_own_env_var(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    adapter = OpenAIChatAdapter(
        provider="deepseek", model="deepseek-chat", env_var="DEEPSEEK_API_KEY"
    )
    with pytest.raises(LlmError, match="DEEPSEEK_API_KEY"):
        adapter.list_models()


def test_anthropic_missing_credentials_names_the_env_var(monkeypatch) -> None:
    """The SDK raises a bare TypeError here, so the adapter checks first."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    adapter = AnthropicChatAdapter(model="claude-opus-5")

    with pytest.raises(LlmError, match="anthropic is not configured"):
        adapter.list_models()
    with pytest.raises(LlmError, match="ANTHROPIC_API_KEY"):
        list(adapter.stream_turn(_request()))


def test_openai_list_models_maps_ids(monkeypatch) -> None:
    fake = _FakeOpenAI([])
    fake.models = SimpleNamespace(
        list=lambda: [SimpleNamespace(id="gpt-4o"), SimpleNamespace(id="gpt-4o-mini")]
    )
    adapter = OpenAIChatAdapter(model="gpt-4o", client=fake)
    listed = adapter.list_models()
    assert [m.id for m in listed] == ["gpt-4o", "gpt-4o-mini"]
    # No display name in the payload, so the id doubles as the label.
    assert listed[0].display_name == "gpt-4o"


def test_anthropic_list_models_uses_display_names() -> None:
    fake = _FakeAnthropic([])
    fake.models = SimpleNamespace(
        list=lambda: [
            SimpleNamespace(id="claude-opus-5", display_name="Claude Opus 5"),
            SimpleNamespace(id="claude-haiku-4-5", display_name=None),
        ]
    )
    adapter = AnthropicChatAdapter(model="claude-opus-5", client=fake)
    listed = adapter.list_models()
    assert (listed[0].id, listed[0].display_name) == ("claude-opus-5", "Claude Opus 5")
    # A null display_name falls back to the id rather than rendering "None".
    assert listed[1].display_name == "claude-haiku-4-5"


def test_fallback_models_cover_every_chat_provider() -> None:
    """The dropdown must never be empty for a provider the picker offers."""
    for provider in CHAT_PROVIDERS:
        entries = fallback_models(provider)
        assert entries, f"no fallback models for {provider}"
        assert all(m.id and m.display_name for m in entries)
    assert fallback_models("nonesuch") == ()
