from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from whygraph.config import load_config
from whygraph.prompts import Rationale
from whygraph.rationale import (
    AnthropicSdkClient,
    ClaudeCliClient,
    _extract_text_from_sdk_response,
    _sdk_usage,
    make_llm_client,
)

VALID_RATIONALE_JSON = json.dumps(
    {
        "purpose": "Validates JWT tokens.",
        "why": "Replaces legacy cookie validator after compliance audit.",
        "constraints": ["must be sync"],
        "tradeoffs": ["JWK lookup cached"],
        "risks": ["claim shape change breaks RoleResolver"],
    }
)


def _fake_response(
    text: str = VALID_RATIONALE_JSON, *, usage: dict | None = None
) -> SimpleNamespace:
    u = usage or {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 0,
    }
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(**u),
        stop_reason="end_turn",
    )


class _FakeMessages:
    def __init__(self, response: SimpleNamespace) -> None:
        self.response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class _FakeAnthropic:
    def __init__(self, response: SimpleNamespace) -> None:
        self.messages = _FakeMessages(response)
        self.api_key: str | None = None

    @classmethod
    def make(cls, response: SimpleNamespace):
        def factory(*, api_key: str):
            inst = cls(response)
            inst.api_key = api_key
            return inst

        return factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_extract_text_from_sdk_walks_blocks() -> None:
    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", text="..."),
            SimpleNamespace(type="text", text="hello"),
        ]
    )
    assert _extract_text_from_sdk_response(response) == "hello"


def test_extract_text_from_sdk_raises_when_no_text_block() -> None:
    response = SimpleNamespace(
        content=[SimpleNamespace(type="thinking", text="...")]
    )
    with pytest.raises(ValueError, match="no text content"):
        _extract_text_from_sdk_response(response)


def test_extract_text_from_sdk_raises_when_content_not_list() -> None:
    response = SimpleNamespace(content="oops")
    with pytest.raises(ValueError, match="not a list"):
        _extract_text_from_sdk_response(response)


def test_sdk_usage_returns_zeros_when_missing() -> None:
    response = SimpleNamespace()
    u = _sdk_usage(response)
    assert u.input_tokens == 0
    assert u.output_tokens == 0


def test_sdk_usage_extracts_known_fields() -> None:
    response = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=80,
            cache_creation_input_tokens=10,
        )
    )
    u = _sdk_usage(response)
    assert u.input_tokens == 100
    assert u.cache_read_input_tokens == 80
    assert u.cache_creation_input_tokens == 10


# ---------------------------------------------------------------------------
# AnthropicSdkClient.generate
# ---------------------------------------------------------------------------


def test_sdk_generate_returns_validated_rationale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_factory = _FakeAnthropic.make(_fake_response())
    monkeypatch.setattr("whygraph.rationale.anthropic.Anthropic", fake_factory)
    client = AnthropicSdkClient(model="claude-sonnet-4-6", api_key="sk-x")
    result = client.generate(system_prompt="sys", user_prompt="user")
    assert isinstance(result.rationale, Rationale)
    assert result.backend == "api"
    assert result.model == "claude-sonnet-4-6"


def test_sdk_generate_passes_cache_control_on_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class _Capture:
        def __init__(self) -> None:
            self.messages = self

        def create(self, **kwargs):
            captured.update(kwargs)
            return _fake_response()

    monkeypatch.setattr(
        "whygraph.rationale.anthropic.Anthropic",
        lambda *, api_key: _Capture(),
    )
    AnthropicSdkClient(model="m", api_key="k").generate(
        system_prompt="SYS", user_prompt="USER"
    )
    system = captured["system"]
    assert isinstance(system, list)
    assert system[0]["text"] == "SYS"
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert captured["messages"] == [{"role": "user", "content": "USER"}]
    assert captured["model"] == "m"
    assert captured["max_tokens"] == 2048


def test_sdk_generate_propagates_api_key_to_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def factory(*, api_key: str):
        captured["api_key"] = api_key
        inst = _FakeAnthropic(_fake_response())
        inst.api_key = api_key
        return inst

    monkeypatch.setattr("whygraph.rationale.anthropic.Anthropic", factory)
    AnthropicSdkClient(model="m", api_key="sk-test").generate(
        system_prompt="s", user_prompt="u"
    )
    assert captured["api_key"] == "sk-test"


def test_sdk_generate_raises_on_unparseable_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "whygraph.rationale.anthropic.Anthropic",
        _FakeAnthropic.make(_fake_response(text="not json at all")),
    )
    with pytest.raises(ValueError, match="could not parse"):
        AnthropicSdkClient(model="m", api_key="k").generate(
            system_prompt="s", user_prompt="u"
        )


def test_sdk_generate_raises_on_schema_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad_text = json.dumps({"purpose": "x"})  # missing required fields
    monkeypatch.setattr(
        "whygraph.rationale.anthropic.Anthropic",
        _FakeAnthropic.make(_fake_response(text=bad_text)),
    )
    with pytest.raises(ValueError, match="schema validation"):
        AnthropicSdkClient(model="m", api_key="k").generate(
            system_prompt="s", user_prompt="u"
        )


def test_sdk_generate_extracts_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "whygraph.rationale.anthropic.Anthropic",
        _FakeAnthropic.make(_fake_response()),
    )
    result = AnthropicSdkClient(model="m", api_key="k").generate(
        system_prompt="s", user_prompt="u"
    )
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 20
    assert result.usage.cache_read_input_tokens == 5


def test_sdk_generate_handles_fenced_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fenced = f"```json\n{VALID_RATIONALE_JSON}\n```"
    monkeypatch.setattr(
        "whygraph.rationale.anthropic.Anthropic",
        _FakeAnthropic.make(_fake_response(text=fenced)),
    )
    result = AnthropicSdkClient(model="m", api_key="k").generate(
        system_prompt="s", user_prompt="u"
    )
    assert isinstance(result.rationale, Rationale)


# ---------------------------------------------------------------------------
# make_llm_client factory
# ---------------------------------------------------------------------------


def test_factory_returns_cli_for_claude_cli_backend(tmp_path: Path) -> None:
    config = load_config(env={}, cwd=tmp_path)
    assert config.rationale_backend == "claude_cli"
    client = make_llm_client(config)
    assert isinstance(client, ClaudeCliClient)
    assert client.model == config.model


def test_factory_returns_sdk_for_api_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "whygraph.rationale.anthropic.Anthropic",
        lambda *, api_key: SimpleNamespace(messages=SimpleNamespace()),
    )
    config = load_config(
        env={
            "ANTHROPIC_API_KEY": "sk-x",
            "WHYGRAPH_RATIONALE_BACKEND": "api",
        },
        cwd=tmp_path,
    )
    assert config.rationale_backend == "api"
    client = make_llm_client(config)
    assert isinstance(client, AnthropicSdkClient)
    assert client.model == config.model


def test_factory_raises_when_api_backend_lacks_key(
    tmp_path: Path,
) -> None:
    config = load_config(
        env={"WHYGRAPH_RATIONALE_BACKEND": "api"}, cwd=tmp_path
    )
    assert config.rationale_backend == "api"
    assert config.anthropic_api_key is None
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is not set"):
        make_llm_client(config)
