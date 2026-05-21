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


@dataclass
class _ScriptedClient(LlmClient):
    """LlmClient stub that returns a scripted response per call.

    Each ``complete`` call consumes the next entry of ``script``: a
    ``str`` is returned as the response text, an :class:`LlmError` is
    raised. Lets a test give each chunk a distinct description and
    target the synthesis call specifically.
    """

    provider = "stub"

    def __init__(
        self,
        script: list[str | LlmError],
        *,
        model: str = "stub-1",
        input_tokens: int | None = 11,
        output_tokens: int | None = 22,
    ) -> None:
        super().__init__(model=model)
        self.requests: list[CompletionRequest] = []
        self._script = script
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens

    @classmethod
    def from_config(cls, config: Any, **overrides: Any) -> "_ScriptedClient":
        raise NotImplementedError("scripted client is built directly in tests")

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        item = self._script[len(self.requests) - 1]
        if isinstance(item, LlmError):
            raise item
        return CompletionResponse(
            text=item,
            model=self.model,
            provider=self.provider,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
        )


def _diff_file(name: str, size: int) -> str:
    """A unified-diff file section padded to exactly ``size`` chars.

    Newline-terminated so a following file's ``diff --git`` header lands
    at the start of a line, where the splitter's regex can see it.
    """
    header = f"diff --git a/{name} b/{name}\n"
    body_len = size - len(header) - 1
    assert body_len >= 0, f"size {size} too small for {name!r}"
    return header + "+" * body_len + "\n"


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
    descriptor = LlmDescriptor(client, prompt_template="ONLY: {{DIFF}}")

    descriptor.describe("body")

    assert client.requests[0].messages[-1].content == "ONLY: body"


def test_describe_resolves_packaged_default_when_no_template_given() -> None:
    """With no ``prompt_template``, the descriptor resolves a markdown
    prompt by the client's provider/model. The stub matches no per-model
    or per-provider file, so resolution lands on the packaged default."""
    client = _StubClient()
    descriptor = LlmDescriptor(client)  # no prompt_template

    descriptor.describe("the diff body")

    rendered = client.requests[0].messages[-1].content
    assert "the diff body" in rendered
    assert "writing a note to your future self" in rendered
    assert rendered.rstrip().endswith("Output only the description.")


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


def test_from_config_forwards_configured_model_to_factory() -> None:
    """``from_config`` threads ``AnalyzeConfig.model`` into ``factory.make``
    so the descriptor's client is bound to the analyzer's chosen model."""
    captured: dict[str, Any] = {}

    class _RecordingFactory:
        def make(self, provider: str, *, model: Any = None, **_: Any) -> _StubClient:
            captured["provider"] = provider
            captured["model"] = model
            return _StubClient()

    config = AnalyzeConfig(provider="anthropic", model="claude-haiku-4-5")
    LlmDescriptor.from_config(config, factory=_RecordingFactory())

    assert captured == {"provider": "anthropic", "model": "claude-haiku-4-5"}


# ---- describe: chunk splitting ------------------------------------------


def test_describe_small_diff_makes_a_single_request() -> None:
    """A diff within the cap takes the single-call path — no splitting."""
    client = _StubClient()
    descriptor = LlmDescriptor(client, max_diff_chars=10_000)

    descriptor.describe(_diff_file("a.py", 80))

    assert len(client.requests) == 1


def test_describe_splits_large_diff_into_chunk_calls_plus_synthesis() -> None:
    client = _StubClient()
    descriptor = LlmDescriptor(
        client,
        max_diff_chars=60,
        prompt_template="DESCRIBE {{DIFF}}",
        synthesis_template="SYNTH {{DESCRIPTIONS}}",
    )
    diff = _diff_file("a", 50) + _diff_file("b", 50) + _diff_file("c", 50)
    assert len(diff) > 60  # precondition: over the split threshold

    descriptor.describe(diff)

    # three per-chunk requests, then one synthesis request
    assert len(client.requests) == 4
    bodies = [r.messages[-1].content for r in client.requests]
    assert [b.startswith("DESCRIBE ") for b in bodies] == [True, True, True, False]
    assert bodies[-1].startswith("SYNTH ")


def test_describe_synthesis_request_carries_each_chunk_description() -> None:
    client = _ScriptedClient(["DESC-A", "DESC-B", "DESC-C", "MERGED"])
    descriptor = LlmDescriptor(
        client, max_diff_chars=60, synthesis_template="{{DESCRIPTIONS}}"
    )
    diff = _diff_file("a", 50) + _diff_file("b", 50) + _diff_file("c", 50)

    desc = descriptor.describe(diff)

    synthesis_body = client.requests[-1].messages[-1].content
    assert "DESC-A" in synthesis_body
    assert "DESC-B" in synthesis_body
    assert "DESC-C" in synthesis_body
    assert desc.text == "MERGED"


def test_describe_resolves_packaged_synthesis_when_no_template_given() -> None:
    """With no ``synthesis_template``, the descriptor resolves the
    packaged synthesis.md and uses it for the merge call."""
    client = _StubClient()
    descriptor = LlmDescriptor(client, max_diff_chars=60)  # no templates
    diff = _diff_file("a", 50) + _diff_file("b", 50) + _diff_file("c", 50)

    descriptor.describe(diff)

    synthesis_body = client.requests[-1].messages[-1].content
    assert synthesis_body.rstrip().endswith("Output only the merged description.")


def test_describe_single_oversized_file_truncates_without_synthesis() -> None:
    """One file bigger than the cap splits into a single chunk, so there
    is nothing to synthesise — it is truncated and described alone."""
    client = _StubClient()
    descriptor = LlmDescriptor(client, max_diff_chars=60)

    desc = descriptor.describe(_diff_file("big.py", 200))

    assert len(client.requests) == 1  # no synthesis call
    assert desc.truncated is True
    assert "[truncated:" in client.requests[0].messages[-1].content


def test_describe_split_sums_token_counts_across_calls() -> None:
    client = _StubClient(input_tokens=5, output_tokens=8)
    descriptor = LlmDescriptor(client, max_diff_chars=60)
    diff = _diff_file("a", 50) + _diff_file("b", 50) + _diff_file("c", 50)

    desc = descriptor.describe(diff)

    assert len(client.requests) == 4  # 3 chunks + synthesis
    assert desc.input_tokens == 5 * 4
    assert desc.output_tokens == 8 * 4


def test_describe_split_truncated_flag_set_when_a_chunk_is_clipped() -> None:
    client = _StubClient()
    descriptor = LlmDescriptor(client, max_diff_chars=60)
    # The first file alone exceeds the cap; the second forces a split.
    diff = _diff_file("big.py", 200) + _diff_file("small.py", 40)

    desc = descriptor.describe(diff)

    assert len(client.requests) == 3  # 2 chunks + synthesis
    assert desc.truncated is True


def test_describe_split_not_truncated_when_every_chunk_fits() -> None:
    client = _StubClient()
    descriptor = LlmDescriptor(client, max_diff_chars=60)
    diff = _diff_file("a", 50) + _diff_file("b", 50) + _diff_file("c", 50)

    desc = descriptor.describe(diff)

    assert desc.truncated is False


def test_describe_wraps_synthesis_llm_error_as_analyze_error() -> None:
    """A failure on the synthesis call is wrapped just like a chunk
    call, with the original error preserved as ``__cause__``."""
    cause = LlmError("synthesis provider down")
    client = _ScriptedClient(["DESC-A", "DESC-B", "DESC-C", cause])
    descriptor = LlmDescriptor(client, max_diff_chars=60)
    diff = _diff_file("a", 50) + _diff_file("b", 50) + _diff_file("c", 50)

    with pytest.raises(AnalyzeError, match="synthesis") as excinfo:
        descriptor.describe(diff)

    assert excinfo.value.__cause__ is cause
