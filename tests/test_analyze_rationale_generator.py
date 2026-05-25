"""Tests for :class:`whygraph.analyze.RationaleGenerator`.

Uses a stub :class:`LlmClient` so no provider SDK is touched. The stub
records every :class:`CompletionRequest` it is handed and returns canned
JSON, which is enough to exercise evidence formatting, prompt rendering,
JSON parsing, and error wrapping.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from whygraph.analyze import (
    RATIONALE_PLACEHOLDER,
    AnalyzeError,
    CommitEvidence,
    Prompt,
    Rationale,
    RationaleError,
    RationaleGenerator,
)
from whygraph.analyze.rationale_generator import (
    _format_evidence,
    _format_symbol_context,
)
from whygraph.core.config import RationaleConfig
from whygraph.db.models import Commit, Issue, PullRequest
from whygraph.services.codegraph import Relation, Symbol, SymbolContext
from whygraph.services.llm import (
    CompletionRequest,
    CompletionResponse,
    LlmClient,
    LlmClientFactory,
    LlmError,
)

_VALID_RATIONALE = {
    "purpose": "Caches resolved prompts.",
    "why": "Added in #12 to cut repeated disk reads during a scan.",
    "constraints": ["cache key must stay content-addressable"],
    "tradeoffs": ["in-memory only, not shared across processes"],
    "risks": ["a stale cache survives a prompt edit"],
}
_VALID_JSON = json.dumps(_VALID_RATIONALE)


@dataclass
class _StubClient(LlmClient):
    """Test double for :class:`LlmClient`.

    Records every request it sees so tests can assert on the rendered
    prompt; returns a configurable :class:`CompletionResponse` (or raises
    :class:`LlmError` when ``raise_with`` is set).
    """

    provider = "stub"

    def __init__(
        self,
        *,
        text: str = _VALID_JSON,
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


def _commit(
    *,
    sha: str = "a1b2c3d4e5f6",
    subject: str = "add prompt cache",
    body: str = "Repeated disk reads slowed the scan.",
    llm_description: str | None = "Adds an in-memory cache keyed by name.",
    author_name: str = "Jane Dev",
) -> Commit:
    """A scanned :class:`Commit` row with sensible defaults for tests."""
    return Commit(
        sha=sha,
        parent_shas="[]",
        author_name=author_name,
        author_email="jane@example.com",
        authored_at="2026-05-01T12:00:00Z",
        committed_at="2026-05-01T12:00:00Z",
        subject=subject,
        body=body,
        files_changed=1,
        insertions=10,
        deletions=2,
        scanned_at="2026-05-02T00:00:00Z",
        llm_description=llm_description,
    )


def _pr(
    *, number: int = 12, title: str = "Cache prompts", body: str | None = "Speeds up scans."
) -> PullRequest:
    """A :class:`PullRequest` row with sensible defaults for tests."""
    return PullRequest(
        number=number,
        title=title,
        body=body,
        state="merged",
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-02T00:00:00Z",
        merged_at="2026-05-02T09:00:00Z",
        head_sha="deadbeef",
        base_ref="main",
        author="octocat",
        html_url="https://example.com/pr/12",
        labels='["perf"]',
        fetched_at="2026-05-02T00:00:00Z",
    )


def _issue(
    *,
    number: int = 7,
    title: str = "Scans are slow",
    body: str | None = "Profiling shows disk I/O.",
) -> Issue:
    """An :class:`Issue` row with sensible defaults for tests."""
    return Issue(
        number=number,
        title=title,
        body=body,
        state="closed",
        created_at="2026-04-01T00:00:00Z",
        updated_at="2026-05-02T00:00:00Z",
        author="reporter",
        html_url="https://example.com/issue/7",
        labels='["perf", "regression"]',
        fetched_at="2026-05-02T00:00:00Z",
    )


def _symbol(
    *,
    id: str = "n_target",
    kind: str = "method",
    name: str = "generate",
    qualified_name: str = "whygraph.analyze.RationaleGenerator.generate",
    file_path: str = "src/whygraph/analyze/rationale_generator.py",
    start_line: int = 301,
    end_line: int = 357,
    docstring: str | None = "Generate a rationale card for one evidence bundle.",
    signature: str | None = "def generate(self, evidence, *, symbol_context=None)",
) -> Symbol:
    """A CodeGraph :class:`Symbol` with sensible defaults for tests."""
    return Symbol(
        id=id,
        kind=kind,
        name=name,
        qualified_name=qualified_name,
        file_path=file_path,
        language="python",
        start_line=start_line,
        end_line=end_line,
        docstring=docstring,
        signature=signature,
    )


def _relation(
    *,
    qualified_name: str = "whygraph.cli.analyze",
    kind: str = "calls",
    line: int | None = 42,
) -> Relation:
    """A caller/callee :class:`Relation` with sensible defaults for tests."""
    return Relation(
        symbol=_symbol(
            id=f"n_{qualified_name}",
            kind="function",
            name=qualified_name.rsplit(".", 1)[-1],
            qualified_name=qualified_name,
            file_path="src/whygraph/cli.py",
            docstring=None,
            signature=None,
        ),
        kind=kind,
        line=line,
    )


# ---- generate: happy path ------------------------------------------------


def test_generate_returns_rationale_with_parsed_fields() -> None:
    client = _StubClient()
    generator = RationaleGenerator(client)

    rationale = generator.generate([CommitEvidence(_commit())])

    assert isinstance(rationale, Rationale)
    assert rationale.purpose == "Caches resolved prompts."
    assert rationale.why.startswith("Added in #12")
    assert rationale.constraints == ("cache key must stay content-addressable",)
    assert rationale.tradeoffs == ("in-memory only, not shared across processes",)
    assert rationale.risks == ("a stale cache survives a prompt edit",)
    assert isinstance(rationale.constraints, tuple)
    assert rationale.model == "stub-1"
    assert rationale.provider == "stub"
    assert rationale.input_tokens == 11
    assert rationale.output_tokens == 22


def test_generate_sends_system_then_user_with_evidence() -> None:
    client = _StubClient()
    generator = RationaleGenerator(client)

    generator.generate([CommitEvidence(_commit(subject="add prompt cache"))])

    assert len(client.requests) == 1
    messages = client.requests[0].messages
    assert [m.role for m in messages] == ["system", "user"]
    # The evidence bundle is interpolated into the user (task) message only.
    assert "add prompt cache" in messages[1].content
    assert "add prompt cache" not in messages[0].content
    assert messages[0].content.strip()  # system carries standing instructions


def test_generate_forwards_timeout_into_request() -> None:
    client = _StubClient()
    generator = RationaleGenerator(client, timeout_sec=42)

    generator.generate([CommitEvidence(_commit())])

    assert client.requests[0].timeout_sec == 42


def test_generate_uses_custom_rationale_prompt() -> None:
    """An explicit rationale_prompt skips resolution: its system goes out
    verbatim, its task is rendered with the evidence bundle."""
    client = _StubClient()
    generator = RationaleGenerator(
        client, rationale_prompt=Prompt(system="SYS", task="ONLY: {{EVIDENCE}}")
    )

    generator.generate([CommitEvidence(_commit(subject="renames X"))])

    messages = client.requests[0].messages
    assert messages[0].content == "SYS"
    assert messages[1].content.startswith("ONLY: ")
    assert "renames X" in messages[1].content


def test_generate_resolves_packaged_rationale_prompt_when_none_given() -> None:
    """With no rationale_prompt, the generator resolves the packaged
    rationale_generator markdown. The stub matches no override folder, so
    resolution lands on default/."""
    client = _StubClient()
    generator = RationaleGenerator(client)  # no rationale_prompt

    generator.generate([CommitEvidence(_commit(subject="cache prompts"))])

    messages = client.requests[0].messages
    # The system prompt enforces the raw-JSON output contract.
    assert "raw json only" in messages[0].content.lower()
    assert "cache prompts" in messages[1].content


# ---- generate: error paths ----------------------------------------------


def test_generate_rejects_empty_evidence_without_calling_client() -> None:
    client = _StubClient()
    generator = RationaleGenerator(client)

    with pytest.raises(AnalyzeError, match="empty evidence"):
        generator.generate([])

    assert client.requests == []


def test_generate_wraps_llm_error_as_analyze_error() -> None:
    cause = LlmError("provider down")
    client = _StubClient(raise_with=cause)
    generator = RationaleGenerator(client)

    with pytest.raises(AnalyzeError) as excinfo:
        generator.generate([CommitEvidence(_commit())])

    assert excinfo.value.__cause__ is cause
    assert "provider down" in str(excinfo.value)


# ---- generate: JSON parsing & validation --------------------------------


def test_generate_strips_json_code_fences() -> None:
    client = _StubClient(text=f"```json\n{_VALID_JSON}\n```")
    generator = RationaleGenerator(client)

    rationale = generator.generate([CommitEvidence(_commit())])

    assert rationale.purpose == "Caches resolved prompts."


def test_generate_raises_rationale_error_on_malformed_json() -> None:
    client = _StubClient(text="not json at all")
    generator = RationaleGenerator(client)

    with pytest.raises(RationaleError, match="not valid JSON"):
        generator.generate([CommitEvidence(_commit())])


def test_generate_raises_rationale_error_on_non_dict_json() -> None:
    client = _StubClient(text="[1, 2, 3]")
    generator = RationaleGenerator(client)

    with pytest.raises(RationaleError, match="must be a JSON object"):
        generator.generate([CommitEvidence(_commit())])


def test_generate_raises_rationale_error_on_missing_key() -> None:
    blob = json.dumps(
        {"purpose": "p", "why": "w", "constraints": [], "tradeoffs": []}
    )
    client = _StubClient(text=blob)
    generator = RationaleGenerator(client)

    with pytest.raises(RationaleError, match="risks"):
        generator.generate([CommitEvidence(_commit())])


def test_generate_raises_rationale_error_on_non_string_purpose() -> None:
    blob = json.dumps(
        {"purpose": 5, "why": "w", "constraints": [], "tradeoffs": [], "risks": []}
    )
    client = _StubClient(text=blob)
    generator = RationaleGenerator(client)

    with pytest.raises(RationaleError, match="purpose"):
        generator.generate([CommitEvidence(_commit())])


def test_generate_raises_rationale_error_on_non_list_field() -> None:
    blob = json.dumps(
        {
            "purpose": "p",
            "why": "w",
            "constraints": "nope",
            "tradeoffs": [],
            "risks": [],
        }
    )
    client = _StubClient(text=blob)
    generator = RationaleGenerator(client)

    with pytest.raises(RationaleError, match="constraints"):
        generator.generate([CommitEvidence(_commit())])


def test_generate_raises_rationale_error_on_non_string_list_element() -> None:
    blob = json.dumps(
        {
            "purpose": "p",
            "why": "w",
            "constraints": [],
            "tradeoffs": [],
            "risks": [1, 2],
        }
    )
    client = _StubClient(text=blob)
    generator = RationaleGenerator(client)

    with pytest.raises(RationaleError, match="risks"):
        generator.generate([CommitEvidence(_commit())])


def test_rationale_error_is_caught_as_analyze_error() -> None:
    """RationaleError subclasses AnalyzeError, so a caller handling the
    package-wide exception still catches malformed output."""
    client = _StubClient(text="not json")
    generator = RationaleGenerator(client)

    with pytest.raises(AnalyzeError):
        generator.generate([CommitEvidence(_commit())])


def test_generate_ignores_unknown_keys_in_output() -> None:
    blob = json.dumps(
        {
            "purpose": "p",
            "why": "w",
            "constraints": [],
            "tradeoffs": [],
            "risks": [],
            "confidence": 0.9,  # not part of the schema
        }
    )
    client = _StubClient(text=blob)
    generator = RationaleGenerator(client)

    rationale = generator.generate([CommitEvidence(_commit())])

    assert rationale.purpose == "p"


# ---- value objects -------------------------------------------------------


def test_rationale_is_frozen() -> None:
    rationale = RationaleGenerator(_StubClient()).generate(
        [CommitEvidence(_commit())]
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        rationale.purpose = "changed"  # type: ignore[misc]


def test_commit_evidence_is_frozen() -> None:
    evidence = CommitEvidence(_commit())
    with pytest.raises(Exception):  # FrozenInstanceError
        evidence.commit = _commit()  # type: ignore[misc]


# ---- _format_evidence ----------------------------------------------------


def test_format_evidence_renders_commit_pr_and_issue() -> None:
    bundle = _format_evidence(
        [
            CommitEvidence(
                _commit(subject="add cache", llm_description="adds a dict"),
                pull_requests=(_pr(number=12, title="Cache prompts"),),
                issues=(_issue(number=7, title="Scans are slow"),),
            )
        ]
    )

    assert "Evidence: 1 commit(s), 1 PR(s), 1 issue(s)." in bundle
    assert "Subject: add cache" in bundle
    assert "Summary: adds a dict" in bundle
    assert "PR #12" in bundle
    assert "Cache prompts" in bundle
    assert "Issue #7" in bundle
    assert "Scans are slow" in bundle


def test_format_evidence_decodes_json_labels() -> None:
    bundle = _format_evidence([CommitEvidence(_commit(), issues=(_issue(),))])

    assert "[perf, regression]" in bundle


def test_format_evidence_omits_absent_llm_description() -> None:
    bundle = _format_evidence([CommitEvidence(_commit(llm_description=None))])

    assert "Summary:" not in bundle


def test_format_evidence_renders_source_label_per_commit() -> None:
    bundle = _format_evidence(
        [
            CommitEvidence(_commit(subject="real edit"), source="blame"),
            CommitEvidence(_commit(subject="behind refactor"), source="blame-walked"),
            CommitEvidence(_commit(subject="pre-rename"), source="predecessor-blame"),
            CommitEvidence(_commit(subject="area touch"), source="area"),
        ]
    )

    assert "Source: line-blame\n" in bundle
    assert "skipped a refactor commit" in bundle
    assert "pre-rename predecessor" in bundle
    assert "area-history" in bundle


# ---- _format_symbol_context ---------------------------------------------


def test_format_symbol_context_renders_target_callers_and_callees() -> None:
    context = SymbolContext(
        target=_symbol(),
        callers=(_relation(qualified_name="whygraph.cli.analyze", line=42),),
        callees=(
            _relation(qualified_name="whygraph.analyze.prompt.render", line=120),
        ),
    )

    text = _format_symbol_context(context)

    assert "CODE GRAPH CONTEXT" in text
    assert (
        "Target: whygraph.analyze.RationaleGenerator.generate (method)" in text
    )
    assert "def generate(self, evidence, *, symbol_context=None)" in text
    assert "Called by (1 caller(s)" in text
    assert "whygraph.cli.analyze (function)" in text
    assert "Calls (1 callee(s)" in text
    assert "whygraph.analyze.prompt.render (function)" in text


def test_format_symbol_context_marks_empty_caller_and_callee_blocks() -> None:
    context = SymbolContext(target=_symbol(), callers=(), callees=())

    text = _format_symbol_context(context)

    assert text.count("(none recorded)") == 2


def test_format_symbol_context_falls_back_to_symbol_line_when_edge_lacks_one() -> (
    None
):
    context = SymbolContext(
        target=_symbol(),
        callers=(_relation(qualified_name="pkg.caller", line=None),),
        callees=(),
    )

    text = _format_symbol_context(context)

    # _relation defaults the caller symbol's start_line to 301.
    assert "src/whygraph/cli.py:301" in text


def test_generate_includes_symbol_context_in_user_message() -> None:
    client = _StubClient()
    generator = RationaleGenerator(client)
    context = SymbolContext(
        target=_symbol(qualified_name="pkg.thing"),
        callers=(_relation(qualified_name="pkg.caller"),),
        callees=(),
    )

    generator.generate([CommitEvidence(_commit())], symbol_context=context)

    user_message = client.requests[0].messages[1].content
    assert "CODE GRAPH CONTEXT" in user_message
    assert "pkg.thing" in user_message
    assert "pkg.caller" in user_message
    # The change-history evidence is still present alongside the new section.
    assert "COMMIT" in user_message


def test_generate_omits_graph_section_when_no_symbol_context() -> None:
    client = _StubClient()
    generator = RationaleGenerator(client)

    generator.generate([CommitEvidence(_commit())])

    assert "CODE GRAPH CONTEXT" not in client.requests[0].messages[1].content


# ---- from_config ---------------------------------------------------------


def test_from_config_resolves_provider_via_factory() -> None:
    factory = LlmClientFactory()
    factory.register("stub", _StubClient, config=object())  # config unused
    config = RationaleConfig(provider="stub", timeout_sec=7)

    generator = RationaleGenerator.from_config(config, factory=factory)
    rationale = generator.generate([CommitEvidence(_commit())])

    assert rationale.provider == "stub"
    assert generator._timeout_sec == 7


def test_from_config_propagates_unknown_provider_error() -> None:
    factory = LlmClientFactory()  # has no "stub" registered
    config = RationaleConfig(provider="stub")

    with pytest.raises(LlmError, match="unknown LLM provider"):
        RationaleGenerator.from_config(config, factory=factory)


def test_from_config_forwards_configured_model_to_factory() -> None:
    """``from_config`` threads ``RationaleConfig.model`` into ``factory.make``
    so the generator's client is bound to the configured model."""
    captured: dict[str, Any] = {}

    class _RecordingFactory:
        def make(self, provider: str, *, model: Any = None, **_: Any) -> _StubClient:
            captured["provider"] = provider
            captured["model"] = model
            return _StubClient()

    config = RationaleConfig(provider="anthropic", model="claude-haiku-4-5")
    RationaleGenerator.from_config(config, factory=_RecordingFactory())

    assert captured == {"provider": "anthropic", "model": "claude-haiku-4-5"}
