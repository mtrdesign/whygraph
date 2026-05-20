"""Tests for :class:`whygraph.analyze.LlmDescriptor`.

Uses a stub :class:`LlmClient` so no provider SDK is touched. The stub
captures every :class:`CompletionRequest` it is handed and returns a
canned :class:`CompletionResponse`, which is enough to exercise the
descriptor's truncation, prompt-rendering, response-mapping, and
error-wrapping logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from whygraph.analyze import AnalyzeError, Description, LlmDescriptor
from whygraph.core.config import AnalyzeConfig
from whygraph.services.llm import (
    CompletionRequest,
    CompletionResponse,
    LlmClient,
    LlmClientFactory,
    LlmError,
)


@dataclass
class _StubClient(LlmClient):
    """Test double for :class:`LlmClient`.

    Records every request it sees so tests can assert on the rendered
    prompt; returns a configurable :class:`CompletionResponse` (or
    raises :class:`LlmError` when ``raise_with`` is set).
    """

    provider = "stub"

    def __init__(
        self,
        *,
        text: str = "model output",
        model: str = "stub-1",
        input_tokens: int | None = 11,
        output_tokens: int | None = 22,
        raise_with: LlmError | None = None,
    ) -> None:
        super().__init__(model=model)
        self.requests: list[CompletionRequest] = []
        self._text = text
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._raise_with = raise_with

    @classmethod
    def from_config(cls, config: Any, **overrides: Any) -> "_StubClient":
        return cls(**overrides)

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        if self._raise_with is not None:
            raise self._raise_with
        return CompletionResponse(
            text=self._text,
            model=self.model,
            provider=self.provider,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
        )


# ---- describe: happy path -----------------------------------------------


def test_describe_returns_description_with_response_metadata() -> None:
    client = _StubClient(text="  this commit renames X to Y.\n")
    descriptor = LlmDescriptor(client)

    desc = descriptor.describe("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-x\n+y\n")

    assert isinstance(desc, Description)
    assert desc.text == "this commit renames X to Y."  # stripped
    assert desc.model == "stub-1"
    assert desc.provider == "stub"
    assert desc.input_tokens == 11
    assert desc.output_tokens == 22
    assert desc.truncated is False


def test_describe_renders_prompt_with_diff_body() -> None:
    client = _StubClient()
    descriptor = LlmDescriptor(client)
    diff = "diff --git a/x b/x\nbody-here"

    descriptor.describe(diff)

    assert len(client.requests) == 1
    rendered = client.requests[0].messages[-1].content
    assert diff in rendered
    assert rendered.rstrip().endswith("Output only the description.")


def test_describe_forwards_timeout_into_request() -> None:
    client = _StubClient()
    descriptor = LlmDescriptor(client, timeout_sec=42)

    descriptor.describe("non-empty diff")

    assert client.requests[0].timeout_sec == 42


def test_describe_uses_custom_prompt_template() -> None:
    client = _StubClient()
    descriptor = LlmDescriptor(client, prompt_template="ONLY: {diff}")

    descriptor.describe("body")

    assert client.requests[0].messages[-1].content == "ONLY: body"


# ---- describe: truncation ------------------------------------------------


def test_describe_truncates_overlong_diff_and_flags_description() -> None:
    client = _StubClient()
    descriptor = LlmDescriptor(client, max_diff_chars=10)
    diff = "x" * 25  # 15 chars over the cap

    desc = descriptor.describe(diff)

    assert desc.truncated is True
    rendered = client.requests[0].messages[-1].content
    assert "x" * 10 in rendered
    assert "[truncated: 15 chars omitted]" in rendered
    # Untruncated tail must not leak through.
    assert "x" * 11 not in rendered


def test_describe_does_not_truncate_when_within_cap() -> None:
    client = _StubClient()
    descriptor = LlmDescriptor(client, max_diff_chars=100)
    diff = "y" * 100  # exactly at the cap

    desc = descriptor.describe(diff)

    assert desc.truncated is False
    assert "[truncated:" not in client.requests[0].messages[-1].content


# ---- describe: error paths ----------------------------------------------


def test_describe_rejects_empty_diff_without_calling_client() -> None:
    client = _StubClient()
    descriptor = LlmDescriptor(client)

    with pytest.raises(AnalyzeError, match="empty diff"):
        descriptor.describe("")

    assert client.requests == []


def test_describe_rejects_whitespace_only_diff() -> None:
    client = _StubClient()
    descriptor = LlmDescriptor(client)

    with pytest.raises(AnalyzeError, match="empty diff"):
        descriptor.describe("   \n\t\n  ")

    assert client.requests == []


def test_describe_wraps_llm_error_as_analyze_error() -> None:
    cause = LlmError("provider down")
    client = _StubClient(raise_with=cause)
    descriptor = LlmDescriptor(client)

    with pytest.raises(AnalyzeError) as excinfo:
        descriptor.describe("non-empty")

    assert excinfo.value.__cause__ is cause
    assert "provider down" in str(excinfo.value)


# ---- constructor validation ----------------------------------------------


def test_constructor_rejects_zero_max_diff_chars() -> None:
    with pytest.raises(ValueError, match="max_diff_chars"):
        LlmDescriptor(_StubClient(), max_diff_chars=0)


# ---- from_config ---------------------------------------------------------


def test_from_config_resolves_provider_via_factory() -> None:
    """An :class:`AnalyzeConfig` + a factory with the stub registered
    yields a descriptor bound to that stub."""
    factory = LlmClientFactory()
    factory.register("stub", _StubClient, config=object())  # config unused
    config = AnalyzeConfig(provider="stub", max_diff_chars=123, timeout_sec=7)

    descriptor = LlmDescriptor.from_config(config, factory=factory)
    desc = descriptor.describe("non-empty diff")

    assert desc.provider == "stub"
    assert descriptor._max_diff_chars == 123
    assert descriptor._timeout_sec == 7


def test_from_config_propagates_unknown_provider_error() -> None:
    factory = LlmClientFactory()  # has no "stub" registered
    config = AnalyzeConfig(provider="stub")

    with pytest.raises(LlmError, match="unknown LLM provider"):
        LlmDescriptor.from_config(config, factory=factory)
