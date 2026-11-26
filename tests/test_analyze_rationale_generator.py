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
    AnalyzeError,
    CommitEvidence,
    Item,
    ItemRef,
    Prompt,
    Rationale,
    RationaleError,
    RationaleGenerator,
)
from whygraph.analyze.rationale_generator import (
    _PrRenderCaps,
    _format_evidence,
    _format_pr,
    _format_symbol_context,
)
from whygraph.core.config import Config, RationaleConfig
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
    *,
    number: int = 12,
    title: str = "Cache prompts",
    body: str | None = "Speeds up scans.",
    commit_titles: str = "[]",
    comments: str = "[]",
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
        commit_titles=commit_titles,
        comments=comments,
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
    # Plain-string items (the old reply shape) parse, with no source.
    assert rationale.constraints == (
        Item("cache key must stay content-addressable", "inferred", (), ""),
    )
    assert rationale.tradeoffs == ("in-memory only, not shared across processes",)
    assert rationale.risks == (
        Item("a stale cache survives a prompt edit", "inferred", (), ""),
    )
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
    blob = json.dumps({"purpose": "p", "why": "w", "constraints": [], "tradeoffs": []})
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
    rationale = RationaleGenerator(_StubClient()).generate([CommitEvidence(_commit())])
    with pytest.raises(Exception):  # FrozenInstanceError
        rationale.purpose = "changed"  # type: ignore[misc]


def test_commit_evidence_is_frozen() -> None:
    evidence = CommitEvidence(_commit())
    with pytest.raises(Exception):  # FrozenInstanceError
        evidence.commit = _commit()  # type: ignore[misc]


# ---- sources: basis assigned by code (plan 54 section 5.4) ----------------

_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
_BODY = "The total number of attempts stays at 3:\nthe upstream locks a client out."


def _sourced_evidence() -> list[CommitEvidence]:
    """One commit with a PR and an issue that share the number 5."""
    return [
        CommitEvidence(
            _commit(
                sha=_SHA,
                subject="cap fetch retries",
                body=_BODY,
                llm_description="Lowers the retry count in fetch.",
            ),
            pull_requests=(_pr(number=5, title="Retry cap", body="alpha reason"),),
            issues=(_issue(number=5, title="Lockouts", body="beta report"),),
        )
    ]


def _with_items(constraints: list, risks: list | None = None) -> Rationale:
    """Generate a card whose reply carries ``constraints`` / ``risks`` items."""
    blob = json.dumps(
        {
            "purpose": "p",
            "why": "w",
            "constraints": constraints,
            "tradeoffs": ["a plain tradeoff"],
            "risks": risks or [],
        }
    )
    return RationaleGenerator(_StubClient(text=blob)).generate(_sourced_evidence())


_COMMIT_REF = ItemRef("commit", _SHA, None)
_PR_REF = ItemRef("pr", "5", "https://example.com/pr/12")
_ISSUE_REF = ItemRef("issue", "5", "https://example.com/issue/7")


def test_item_quoted_when_the_quote_is_in_a_cited_source() -> None:
    card = _with_items(
        [{"text": "at most 3 attempts", "refs": ["a1b2c3d4"], "quote": "stays at 3"}]
    )

    assert card.constraints == (
        Item("at most 3 attempts", "quoted", (_COMMIT_REF,), "stays at 3"),
    )


@pytest.mark.parametrize("quote", ["not in the commit", ""])
def test_item_cited_when_the_quote_is_absent_or_not_found(quote: str) -> None:
    card = _with_items(
        [{"text": "at most 3 attempts", "refs": ["a1b2c3d4"], "quote": quote}]
    )

    assert card.constraints == (
        Item("at most 3 attempts", "cited", (_COMMIT_REF,), ""),
    )


def test_item_inferred_when_no_ref_is_in_the_bundle() -> None:
    card = _with_items(
        [
            {
                "text": "at most 3 attempts",
                "refs": ["deadbeef", "PR #99", "Issue #6"],
                "quote": "stays at 3",
            }
        ]
    )

    assert card.constraints == (Item("at most 3 attempts", "inferred", (), ""),)


@pytest.mark.parametrize(
    "item",
    [
        "at most 3 attempts",
        {"text": "at most 3 attempts", "quote": "stays at 3"},
        {"text": "at most 3 attempts", "refs": ["a1b2c3d4"]},
        {"text": "at most 3 attempts", "refs": "a1b2c3d4", "quote": "stays at 3"},
        {"text": "at most 3 attempts", "refs": ["a1b2c3d4"], "quote": 3},
        {"text": "at most 3 attempts", "refs": None, "quote": None},
    ],
    ids=[
        "plain-string",
        "refs-missing",
        "quote-missing",
        "refs-not-a-list",
        "quote-not-a-string",
        "both-null",
    ],
)
def test_item_inferred_when_plain_or_refs_and_quote_are_malformed(item) -> None:
    card = _with_items([item], risks=[item])

    expected = (Item("at most 3 attempts", "inferred", (), ""),)
    assert card.constraints == expected
    assert card.risks == expected


def test_item_quote_matches_across_other_whitespace() -> None:
    card = _with_items(
        [
            {
                "text": "at most 3 attempts",
                "refs": ["a1b2c3d4"],
                "quote": "  attempts\tstays at 3: the   upstream locks ",
            }
        ]
    )

    (item,) = card.constraints
    assert item.basis == "quoted"
    assert item.quote == "attempts stays at 3: the upstream locks"


def test_item_quote_from_the_model_summary_is_not_a_source() -> None:
    """The diff summary is model output, so a quote from it is only cited."""
    card = _with_items(
        [
            {
                "text": "fewer retries",
                "refs": ["a1b2c3d4"],
                "quote": "Lowers the retry count",
            }
        ]
    )

    assert card.constraints[0].basis == "cited"


def test_item_pr_and_issue_of_the_same_number_are_different_sources() -> None:
    card = _with_items(
        [
            {"text": "pr", "refs": ["PR #5"], "quote": "alpha reason"},
            {"text": "issue", "refs": ["Issue #5"], "quote": "alpha reason"},
            {"text": "issue2", "refs": ["issue #5"], "quote": "beta report"},
        ]
    )

    assert card.constraints == (
        Item("pr", "quoted", (_PR_REF,), "alpha reason"),
        Item("issue", "cited", (_ISSUE_REF,), ""),
        Item("issue2", "quoted", (_ISSUE_REF,), "beta report"),
    )


def test_item_drops_refs_not_in_the_bundle_and_keeps_the_rest() -> None:
    card = _with_items(
        [
            {
                "text": "t",
                "refs": ["PR #99", 7, "a1b2c3d4", "Issue #5", "a1b2c3d4"],
                "quote": "beta report",
            }
        ]
    )

    assert card.constraints == (
        Item("t", "quoted", (_COMMIT_REF, _ISSUE_REF), "beta report"),
    )


def test_item_accepts_a_longer_sha_prefix() -> None:
    card = _with_items([{"text": "t", "refs": [_SHA], "quote": ""}])

    assert card.constraints[0].refs == (_COMMIT_REF,)


@pytest.mark.parametrize(
    "item",
    [
        {"refs": ["a1b2c3d4"], "quote": "stays at 3"},
        {"text": 5, "refs": [], "quote": ""},
    ],
    ids=["no-text", "text-not-a-string"],
)
def test_item_without_string_text_raises(item: dict) -> None:
    with pytest.raises(RationaleError, match="risks"):
        _with_items([], risks=[item])


def test_tradeoffs_stay_plain_strings() -> None:
    card = _with_items([])

    assert card.tradeoffs == ("a plain tradeoff",)


def test_item_is_frozen() -> None:
    item = Item("t", "inferred", (), "")
    with pytest.raises(Exception):  # FrozenInstanceError
        item.text = "changed"  # type: ignore[misc]


def test_rationale_prompt_asks_for_refs_and_quote() -> None:
    client = _StubClient()
    RationaleGenerator(client).generate([CommitEvidence(_commit())])

    system = client.requests[0].messages[0].content
    assert '"refs"' in system
    assert '"quote"' in system


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


# ---- _format_pr roster + discussion --------------------------------------


def _commit_titles(*headlines: str) -> str:
    """JSON-encode a commit_titles roster the way the github crawler stores it."""
    return json.dumps(
        [
            {"oid": f"{i:040x}", "headline": h, "author_name": "Jane"}
            for i, h in enumerate(headlines, start=1)
        ]
    )


def _comments(*pairs: tuple[str, str]) -> str:
    """JSON-encode a PR comments list ([{author, body, created_at}, ...])."""
    return json.dumps(
        [
            {"author": author, "body": body, "created_at": "2026-05-01T00:00:00Z"}
            for author, body in pairs
        ]
    )


def test_format_pr_renders_roster_and_discussion() -> None:
    lines = _format_pr(
        _pr(
            commit_titles=_commit_titles("first commit", "second commit"),
            comments=_comments(("alice", "looks good"), ("bob", "ship it")),
        )
    )
    text = "\n".join(lines)

    assert "Squashed commits:" in text
    assert "first commit" in text
    assert "second commit" in text
    assert "Discussion:" in text
    assert "[alice] looks good" in text
    assert "[bob] ship it" in text


def test_format_pr_empty_roster_and_discussion_add_no_lines() -> None:
    lines = _format_pr(_pr())  # default commit_titles="[]", comments="[]"
    text = "\n".join(lines)

    assert "Squashed commits:" not in text
    assert "Discussion:" not in text


def test_format_pr_respects_caps() -> None:
    caps = _PrRenderCaps(
        pr_roster_max_commits=1,
        pr_discussion_max_comments=1,
        pr_comment_max_chars=4,
    )
    lines = _format_pr(
        _pr(
            commit_titles=_commit_titles("kept commit", "dropped commit"),
            comments=_comments(("alice", "hello world"), ("bob", "dropped")),
        ),
        caps,
    )
    text = "\n".join(lines)

    assert "kept commit" in text
    assert "dropped commit" not in text
    assert "[alice] hell" in text  # clipped to 4 chars
    assert "hello world" not in text
    assert "[bob]" not in text  # second comment dropped by the cap


def test_format_pr_tolerates_malformed_json() -> None:
    lines = _format_pr(_pr(commit_titles="not json", comments="{}"))
    text = "\n".join(lines)

    assert "Squashed commits:" not in text
    assert "Discussion:" not in text


# ---- RationaleConfig PR-rendering caps -----------------------------------


def test_rationale_config_cap_defaults() -> None:
    cfg = RationaleConfig()

    assert cfg.pr_roster_max_commits == 30
    assert cfg.pr_discussion_max_comments == 20
    assert cfg.pr_comment_max_chars == 500


def test_pr_render_caps_from_config_projects_fields() -> None:
    caps = _PrRenderCaps.from_config(
        RationaleConfig(
            pr_roster_max_commits=3,
            pr_discussion_max_comments=2,
            pr_comment_max_chars=10,
        )
    )

    assert caps.pr_roster_max_commits == 3
    assert caps.pr_discussion_max_comments == 2
    assert caps.pr_comment_max_chars == 10


# ---- _format_symbol_context ---------------------------------------------


def test_format_symbol_context_renders_target_callers_and_callees() -> None:
    context = SymbolContext(
        target=_symbol(),
        callers=(_relation(qualified_name="whygraph.cli.analyze", line=42),),
        callees=(_relation(qualified_name="whygraph.analyze.prompt.render", line=120),),
    )

    text = _format_symbol_context(context)

    assert "CODE GRAPH CONTEXT" in text
    assert "Target: whygraph.analyze.RationaleGenerator.generate (method)" in text
    assert "def generate(self, evidence, *, symbol_context=None)" in text
    assert "Called by (1 caller(s)" in text
    assert "whygraph.cli.analyze (function)" in text
    assert "Calls (1 callee(s)" in text
    assert "whygraph.analyze.prompt.render (function)" in text


def test_format_symbol_context_marks_empty_caller_and_callee_blocks() -> None:
    context = SymbolContext(target=_symbol(), callers=(), callees=())

    text = _format_symbol_context(context)

    assert text.count("(none recorded)") == 2


def test_format_symbol_context_falls_back_to_symbol_line_when_edge_lacks_one() -> None:
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
    config = Config(rationale=RationaleConfig(provider="stub", timeout_sec=7))

    generator = RationaleGenerator.from_config(config, factory=factory)
    rationale = generator.generate([CommitEvidence(_commit())])

    assert rationale.provider == "stub"
    assert generator._timeout_sec == 7


def test_from_config_propagates_unknown_provider_error() -> None:
    factory = LlmClientFactory()  # has no "stub" registered
    config = Config(rationale=RationaleConfig(provider="stub"))

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

    config = Config(
        rationale=RationaleConfig(provider="anthropic", model="claude-haiku-4-5")
    )
    RationaleGenerator.from_config(config, factory=_RecordingFactory())

    assert captured == {"provider": "anthropic", "model": "claude-haiku-4-5"}


# ---- plan 62: today's prompt stays byte-identical (acceptance 6) ----------


def _byte_identity_evidence() -> list[CommitEvidence]:
    """A bundle with no repeated PR: blame + area items, roster, discussion, issue.

    The PR also stores review threads and summaries, which must not reach the
    prompt when ``generate`` is given no ``paths``.
    """
    thread = {
        "path": "src/retry.py",
        "original_line": 3,
        "anchor": {"lines": ["if attempts > MAX_ATTEMPTS:"], "added": True},
        "resolved": True,
        "outdated": False,
        "comments": [
            {
                "author": "octocat",
                "body": "Three attempts because the upstream locks a client out.",
                "created_at": "2026-05-01T01:00:00Z",
                "bot": False,
                "minimized": False,
            }
        ],
        "comments_total": 1,
    }
    pr = _pr(
        number=12,
        title="Cache prompts",
        body="Speeds up scans.\n\nSecond paragraph.",
        commit_titles=_commit_titles("first commit", "second commit"),
        comments=_comments(("alice", "looks good"), ("bob", "  ship it  ")),
    )
    pr.review_threads = json.dumps([thread])
    pr.review_summaries = json.dumps(
        [{"author": "alice", "state": "APPROVED", "body": "LGTM", "submitted_at": "x"}]
    )
    return [
        CommitEvidence(
            _commit(sha="a1b2c3d4e5f6a7b8", subject="add prompt cache"),
            pull_requests=(pr,),
            issues=(_issue(number=7),),
            source="blame",
            lines=((1, 4),),
        ),
        CommitEvidence(
            _commit(
                sha="0011223344556677",
                subject="touch the file",
                body="",
                llm_description=None,
                author_name="Bob",
            ),
            pull_requests=(_pr(number=13, title="Other", body=None),),
            source="area",
        ),
    ]


_BYTE_IDENTITY_PROMPT = (
    "Explain why the following code exists, based on this evidence.\n"
    "\n"
    "Evidence:\n"
    "CODE GRAPH CONTEXT\n"
    "\n"
    "Target: pkg.retry (method)\n"
    "  src/whygraph/analyze/rationale_generator.py:301-357\n"
    "  def generate(self, evidence, *, symbol_context=None)\n"
    "  Generate a rationale card for one evidence bundle.\n"
    "\n"
    "Called by (1 caller(s) — blast radius of a change):\n"
    "  - pkg.caller (function)  src/whygraph/cli.py:42\n"
    "\n"
    "Calls (0 callee(s) — what this depends on):\n"
    "  (none recorded)\n"
    "\n"
    "Evidence: 2 commit(s), 2 PR(s), 1 issue(s).\n"
    "\n"
    "COMMIT a1b2c3d4  2026-05-01T12:00:00Z  by Jane Dev\n"
    "  Source: line-blame\n"
    "  Summary: Adds an in-memory cache keyed by name.\n"
    "  Subject: add prompt cache\n"
    "  Body:\n"
    "    Repeated disk reads slowed the scan.\n"
    "\n"
    "  PR #12  by octocat  merged 2026-05-02T09:00:00Z  [perf]\n"
    "    Title: Cache prompts\n"
    "    Body:\n"
    "      Speeds up scans.\n"
    "      \n"
    "      Second paragraph.\n"
    "    Squashed commits:\n"
    "      - first commit  (000000000)\n"
    "      - second commit  (000000000)\n"
    "    Discussion:\n"
    "      [alice] looks good\n"
    "      [bob] ship it\n"
    "\n"
    "  Issue #7  closed  by reporter  [perf, regression]\n"
    "    Title: Scans are slow\n"
    "    Body:\n"
    "      Profiling shows disk I/O.\n"
    "\n"
    "COMMIT 00112233  2026-05-01T12:00:00Z  by Bob\n"
    "  Source: area-history (touched the file but not these lines)\n"
    "  Subject: touch the file\n"
    "\n"
    "  PR #13  by octocat  merged 2026-05-02T09:00:00Z  [perf]\n"
    "    Title: Other\n"
)


def test_prompt_without_paths_is_byte_identical_to_before_plan_62() -> None:
    client = _StubClient()
    context = SymbolContext(
        target=_symbol(qualified_name="pkg.retry"),
        callers=(_relation(qualified_name="pkg.caller"),),
        callees=(),
    )

    RationaleGenerator(client).generate(
        _byte_identity_evidence(), symbol_context=context
    )

    assert client.requests[0].messages[1].content == _BYTE_IDENTITY_PROMPT


# ---- plan 62: review threads in the card prompt (section 4.9, 4.9a) --------

# The target file today; the target is ``fetch`` (lines 1-6).
_FILE = (
    "def fetch(url):",
    "    attempts = 0",
    "    while attempts < MAX_ATTEMPTS:",
    "        attempts += 1",
    "        response = client.get(url, timeout=TIMEOUT)",
    "    return response",
    "",
    "def other():",
    "    return compute_the_other_value(42)",
)
_RANGE = (1, 6)
_PATHS = frozenset({"src/fetch.py", "src/old_fetch.py"})
_ANCHOR = {
    "lines": [
        "while attempts < MAX_ATTEMPTS:",
        "attempts += 1",
        "response = client.get(url, timeout=TIMEOUT)",
    ],
    "added": True,
}
_WHY = "Three attempts because the upstream locks a client out after the fourth."
_ASK = "Why three attempts and not five here?"


def _c(author: str, body: str, **flags: bool) -> dict:
    """One stored thread comment (flags only on the opener, as stored)."""
    return {"author": author, "body": body, "created_at": "2026-05-01T01:00:00Z"} | {
        k: v for k, v in flags.items()
    }


def _thread(
    *comments: dict,
    path: str = "src/fetch.py",
    anchor: dict | None = _ANCHOR,
    outdated: bool = False,
) -> dict:
    """A stored review thread (plan 62 section 5); the opener carries its flags."""
    stored = list(comments) or [_c("alice", _ASK), _c("octocat", _WHY)]
    stored[0] = {"bot": False, "minimized": False} | stored[0]
    return {
        "path": path,
        "original_line": 5,
        "anchor": anchor,
        "resolved": False,
        "outdated": outdated,
        "comments": stored,
        "comments_total": len(stored),
    }


def _thread_pr(
    *threads: dict,
    number: int = 12,
    author: str | None = "octocat",
    created_at: str = "2026-05-01T00:00:00Z",
    comments: str = "[]",
) -> PullRequest:
    """A PR (author ``octocat`` by default) that stores ``threads``."""
    pr = _pr(number=number, comments=comments)
    pr.author = author
    pr.created_at = created_at
    pr.review_threads = json.dumps(list(threads))
    return pr


def _blame(
    *prs: PullRequest,
    sha: str = "a1b2c3d4e5f6a7b8",
    lines: tuple[tuple[int, int], ...] | None = ((1, 6),),
    source: str = "blame",
    committed_at: str = "2026-05-01T12:00:00Z",
) -> CommitEvidence:
    """An evidence item owning ``lines`` and linked to ``prs``."""
    commit = _commit(sha=sha)
    commit.committed_at = committed_at
    return CommitEvidence(commit, pull_requests=prs, source=source, lines=lines)


def _prompt(
    evidence: list[CommitEvidence],
    *,
    paths: frozenset[str] | None = _PATHS,
    file_lines: tuple[str, ...] | None = _FILE,
    target_range: tuple[int, int] | None = _RANGE,
    caps: _PrRenderCaps | None = None,
    reply: str = _VALID_JSON,
) -> tuple[str, Rationale]:
    """The prompt and the card of one generation with the thread inputs."""
    client = _StubClient(text=reply)
    card = RationaleGenerator(client, caps=caps).generate(
        evidence, paths=paths, file_lines=file_lines, target_range=target_range
    )
    return client.requests[0].messages[1].content, card


def test_a_qualified_thread_renders_with_roles() -> None:
    prompt, _ = _prompt([_blame(_thread_pr(_thread()))])

    assert (
        "    Review threads on these lines (comments on a draft of this change):\n"
        f"      - [alice, reviewer] {_ASK}\n"
        f"        [octocat, PR author] {_WHY}"
    ) in prompt
    assert "omitted" not in prompt


def test_thread_block_follows_the_discussion() -> None:
    pr = _thread_pr(_thread(), comments=_comments(("bob", "ship it")))
    prompt, _ = _prompt([_blame(pr)])

    assert prompt.index("    Discussion:") < prompt.index("    Review threads on")


def test_thread_renders_the_opener_and_the_last_two_comments() -> None:
    thread = _thread(
        _c("alice", _ASK),
        _c("bob", "first middle comment"),
        _c("carol", "second middle comment"),
        _c("dave", "the next to last comment"),
        _c("octocat", _WHY),
    )
    prompt, _ = _prompt([_blame(_thread_pr(thread))])

    assert f"- [alice, reviewer] {_ASK}" in prompt
    assert "[dave, reviewer] the next to last comment" in prompt
    assert f"[octocat, PR author] {_WHY}" in prompt
    assert "middle comment" not in prompt


def test_each_rendered_comment_is_clipped_to_400_characters() -> None:
    long = "Because " + "x" * 600
    prompt, _ = _prompt([_blame(_thread_pr(_thread(_c("octocat", long))))])

    assert f"[octocat, PR author] {long[:400]}\n" in prompt + "\n"
    assert long[:401] not in prompt


def test_an_outdated_thread_is_marked() -> None:
    prompt, _ = _prompt([_blame(_thread_pr(_thread(outdated=True)))])

    assert (
        f"[octocat, PR author] {_WHY}\n        (code changed after this comment)"
    ) in prompt


def _renders(thread: dict, **pr: Any) -> bool:
    """Whether ``thread`` of a PR linked by a line-blame item reaches the prompt."""
    prompt, _ = _prompt([_blame(_thread_pr(thread, **pr))])
    return "Review threads on these lines" in prompt


@pytest.mark.parametrize(
    "opener",
    [
        _c("copilot-pull-request-reviewer", _ASK, bot=True, minimized=False),
        _c("alice", _ASK, bot=False, minimized=True),
        _c("cookie", "<!-- cookie-review --> please look at this"),
        _c("alice", "Written by the Claude Code agent: why three?"),
        _c("alice", "Agent-assisted reply: why three attempts?"),
        _c("alice", "**P1** Three attempts looks arbitrary here."),
        _c("alice", "[high] Three attempts looks arbitrary here."),
    ],
    ids=[
        "bot-opener",
        "minimized-human-opener",
        "machine-html-comment",
        "machine-written-by-agent",
        "machine-agent-assisted",
        "machine-severity-bold",
        "machine-severity-bracket",
    ],
)
def test_rule_1_a_flagged_or_machine_opener_never_renders(opener: dict) -> None:
    assert not _renders(_thread(opener, _c("octocat", _WHY)))


def test_rule_1_control_the_same_thread_with_a_human_opener_renders() -> None:
    assert _renders(_thread(_c("alice", _ASK), _c("octocat", _WHY)))


def test_a_machine_text_reply_is_dropped_from_the_rendering() -> None:
    thread = _thread(
        _c("alice", _ASK),
        _c("octocat", _WHY),
        _c("helper", "Agent-assisted reply: the agent agrees with the author."),
    )
    prompt, _ = _prompt([_blame(_thread_pr(thread))])

    assert f"[octocat, PR author] {_WHY}" in prompt
    assert "the agent agrees" not in prompt


def test_rule_2_needs_60_characters_from_the_pr_author() -> None:
    sixty = ("Because the lock-out " + "x" * 60)[:60]

    assert _renders(_thread(_c("alice", _ASK), _c("octocat", sixty)))
    assert not _renders(_thread(_c("alice", _ASK), _c("octocat", sixty[:59])))


def test_rule_2_counts_characters_after_cleaning() -> None:
    padded = "> " + "quoted " * 20 + "\nshort answer\n```python\nx = 1\n```"

    assert not _renders(_thread(_c("alice", _ASK), _c("octocat", padded)))


def test_rule_2_a_pr_author_comment_only_in_a_middle_position_does_not_qualify() -> (
    None
):
    thread = _thread(
        _c("alice", _ASK),
        _c("octocat", _WHY),
        _c("bob", "a later reviewer comment"),
        _c("carol", "the last reviewer comment"),
    )

    assert not _renders(thread)


def test_rule_2_compares_the_author_login_case_insensitively() -> None:
    thread = _thread(_c("alice", _ASK), _c("OctoCat", _WHY))
    prompt, _ = _prompt([_blame(_thread_pr(thread, author="octocat"))])

    assert f"[OctoCat, PR author] {_WHY}" in prompt


def test_rule_2_a_null_pr_author_gives_no_threads() -> None:
    assert not _renders(_thread(), author=None)


def test_rule_2_a_reviewer_only_thread_does_not_render() -> None:
    thread = _thread(_c("alice", _ASK), _c("bob", "Agreed, five would be safer here."))

    assert not _renders(thread)


def test_rule_2_a_suggestion_only_author_comment_does_not_qualify() -> None:
    suggestion = (
        "```suggestion\n    while attempts < 3 and not locked_out(client):\n```"
    )

    assert not _renders(_thread(_c("alice", _ASK), _c("octocat", suggestion)))


def test_cleaning_removes_suggestions_and_html_and_replaces_code() -> None:
    body = (
        f"{_WHY}\n"
        "```suggestion\nnew_code_here()\n```\n"
        "<!-- hidden marker -->\n"
        "```python\nretry(3)\n```\n"
        "> the reviewer's quoted words\n"
        "Done."
    )
    prompt, _ = _prompt(
        [_blame(_thread_pr(_thread(_c("alice", _ASK), _c("octocat", body))))]
    )

    assert f"[octocat, PR author] {_WHY} [code] Done." in prompt
    for gone in ("new_code_here", "hidden marker", "retry(3)", "quoted words"):
        assert gone not in prompt


def test_an_empty_after_cleaning_comment_is_dropped() -> None:
    thread = _thread(
        _c("alice", _ASK),
        _c("octocat", _WHY),
        _c("bob", "```suggestion\nx = 1\n```"),
    )
    prompt, _ = _prompt([_blame(_thread_pr(thread))])

    assert "[bob," not in prompt


def test_rule_3_a_pr_reached_only_through_area_gives_no_threads() -> None:
    pr = _thread_pr(_thread())
    area = _blame(pr, source="area", lines=None)
    prompt, _ = _prompt([area, _blame(sha="ffeeddccbbaa9988")])

    assert "PR #12" in prompt
    assert "Review threads" not in prompt


def test_rule_3_the_same_pr_also_reached_through_blame_renders() -> None:
    pr = _thread_pr(_thread())
    prompt, _ = _prompt(
        [_blame(pr, sha="ffeeddccbbaa9988"), _blame(pr, source="area", lines=None)]
    )

    assert prompt.count("Review threads on these lines") == 1


def test_a_thread_on_another_file_never_renders() -> None:
    assert not _renders(_thread(path="src/other.py"))


def test_a_thread_on_a_rename_alias_renders() -> None:
    assert _renders(_thread(path="src/old_fetch.py"))


@pytest.mark.parametrize(
    "anchor",
    [
        None,
        {"lines": ["this code was rewritten after the merge"], "added": True},
        {"lines": ["return compute_the_other_value(42)"], "added": True},
    ],
    ids=["no-anchor", "rewritten", "other-code-in-the-file"],
)
def test_rule_4_an_anchor_not_inside_the_target_never_renders(anchor) -> None:
    assert not _renders(_thread(anchor=anchor))


def test_rule_4_a_signature_line_comment_matches() -> None:
    anchor = {"lines": ["def fetch(url):"], "added": True}
    prompt, _ = _prompt(
        [_blame(_thread_pr(_thread(anchor=anchor)))], target_range=(1, 6)
    )

    assert "Review threads on these lines" in prompt


@pytest.mark.parametrize(
    "missing",
    [{"paths": None}, {"file_lines": None}, {"target_range": None}],
    ids=["paths", "file_lines", "target_range"],
)
def test_rule_4_fails_closed_without_thread_inputs(missing: dict) -> None:
    prompt, _ = _prompt([_blame(_thread_pr(_thread()))], **missing)

    assert "Review threads" not in prompt


def test_r22_an_added_anchor_owned_by_another_pr_does_not_render() -> None:
    pr = _thread_pr(_thread())
    other = _pr(number=99)
    evidence = [
        _blame(pr, sha="1111111111111111", lines=((1, 2),)),
        _blame(other, sha="2222222222222222", lines=((3, 6),)),
    ]
    prompt, _ = _prompt(evidence)

    assert "Review threads" not in prompt


def test_r22_an_added_anchor_owned_by_a_squash_commit_of_the_pr_renders() -> None:
    pr = _thread_pr(_thread())
    pr.merge_commit_sha = "5555555555555555"
    prompt, _ = _prompt([_blame(pr, sha="5555555555555555", lines=((3, 6),))])

    assert "Review threads on these lines" in prompt


def test_r22_a_rewritten_original_with_a_copy_elsewhere_does_not_render() -> None:
    copy_elsewhere = (
        "def fetch(url):",
        "    return cached(url)",
        "",
        "def legacy(url):",
        "    while attempts < MAX_ATTEMPTS:",
        "        attempts += 1",
        "        response = client.get(url, timeout=TIMEOUT)",
    )
    prompt, _ = _prompt(
        [_blame(_thread_pr(_thread()), lines=((1, 2),))],
        file_lines=copy_elsewhere,
        target_range=(1, 2),
    )

    assert "Review threads" not in prompt


def test_r22_an_owner_outside_the_bundle_does_not_render() -> None:
    prompt, _ = _prompt([_blame(_thread_pr(_thread()), lines=((1, 2),))])

    assert "Review threads" not in prompt


_CONTEXT = {
    "lines": ["attempts += 1", "response = client.get(url, timeout=TIMEOUT)"],
    "added": False,
}


def test_r22_a_unique_context_anchor_owned_by_an_older_commit_renders() -> None:
    pr = _thread_pr(_thread(anchor=_CONTEXT), created_at="2026-05-01T00:00:00Z")
    older = _blame(
        _pr(number=3), sha="0101010101010101", committed_at="2026-04-01T08:00:00+02:00"
    )
    prompt, _ = _prompt(
        [older, _blame(pr, sha="0202020202020202", lines=None, source="pr-origin")]
    )

    assert "Review threads on these lines" in prompt


def test_r22_a_context_anchor_owned_by_a_newer_commit_does_not_render() -> None:
    pr = _thread_pr(_thread(anchor=_CONTEXT), created_at="2026-05-01T00:00:00Z")
    newer = _blame(
        _pr(number=3), sha="0101010101010101", committed_at="2026-05-03T00:00:00Z"
    )
    prompt, _ = _prompt(
        [newer, _blame(pr, sha="0202020202020202", lines=None, source="pr-origin")]
    )

    assert "Review threads" not in prompt


def test_r22_a_duplicated_context_anchor_does_not_render() -> None:
    twice = _FILE + (
        "        attempts += 1",
        "        response = client.get(url, timeout=TIMEOUT)",
    )
    pr = _thread_pr(_thread(anchor=_CONTEXT), created_at="2026-05-01T00:00:00Z")
    older = _blame(
        _pr(number=3), sha="0101010101010101", committed_at="2026-04-01T00:00:00Z"
    )
    prompt, _ = _prompt(
        [older, _blame(pr, sha="0202020202020202", lines=None, source="pr-origin")],
        file_lines=twice,
    )

    assert "Review threads" not in prompt


def _author_thread(chars: int) -> dict:
    """A thread of one PR-author comment of exactly ``chars`` characters."""
    return _thread(_c("octocat", ("Because " + "x" * chars)[:chars]))


def test_the_per_pr_budget_stops_at_the_first_thread_that_does_not_fit() -> None:
    # Each thread spends 21 + 400 = 421 characters; 1,200 fit two.
    pr = _thread_pr(_author_thread(400), _author_thread(400), _author_thread(400))
    prompt, _ = _prompt([_blame(pr)])

    assert prompt.count("[octocat, PR author]") == 2
    assert prompt.endswith("    (1 more review threads omitted)") or (
        "    (1 more review threads omitted)\n" in prompt
    )


def test_the_card_budget_spans_prs_in_bundle_order() -> None:
    first = _thread_pr(_author_thread(300), number=12)
    second = _thread_pr(_author_thread(60), number=13)
    caps = _PrRenderCaps(review_threads_max_chars=400)
    prompt, _ = _prompt([_blame(first, second)], caps=caps)

    # 321 characters fit; the next thread (81) does not, nor any after it.
    assert prompt.count("[octocat, PR author]") == 1
    assert prompt.count("    (1 more review threads omitted)") == 1
    pr13 = prompt[prompt.index("PR #13") :]
    assert "(1 more review threads omitted)" in pr13


def test_the_card_budget_stops_every_later_thread() -> None:
    pr = _thread_pr(_author_thread(300), _author_thread(300), _author_thread(60))
    caps = _PrRenderCaps(review_threads_max_chars=400)
    prompt, _ = _prompt([_blame(pr)], caps=caps)

    assert prompt.count("[octocat, PR author]") == 1
    assert "    (2 more review threads omitted)" in prompt


def test_review_threads_max_chars_zero_keeps_threads_out() -> None:
    prompt, _ = _prompt(
        [_blame(_thread_pr(_thread()))], caps=_PrRenderCaps(review_threads_max_chars=0)
    )

    assert "Review threads" not in prompt
    assert "omitted" not in prompt


def test_pr_render_caps_from_config_copies_the_thread_budget() -> None:
    caps = _PrRenderCaps.from_config(RationaleConfig(review_threads_max_chars=7))

    assert caps.review_threads_max_chars == 7
    assert _PrRenderCaps().review_threads_max_chars == 3000


def test_review_summaries_and_issue_comments_are_not_rendered() -> None:
    pr = _thread_pr(_thread())
    pr.review_summaries = json.dumps(
        [
            {
                "author": "alice",
                "state": "APPROVED",
                "body": "summary words",
                "submitted_at": "x",
            }
        ]
    )
    issue = _issue()
    issue.comments = json.dumps([_c("reporter", "issue comment words")])
    prompt, _ = _prompt(
        [CommitEvidence(_blame(pr).commit, (pr,), (issue,), "blame", ((1, 6),))]
    )

    assert "summary words" not in prompt
    assert "issue comment words" not in prompt


# ---- once per bundle ------------------------------------------------------


def test_a_prs_discussion_renders_once_per_bundle() -> None:
    pr = _thread_pr(_thread(), comments=_comments(("bob", "ship it")))
    evidence = [
        _blame(pr, sha="1111111111111111"),
        _blame(pr, sha="2222222222222222", lines=((7, 9),)),
    ]
    prompt, _ = _prompt(evidence)

    assert prompt.count("    Title: Cache prompts") == 2
    assert prompt.count("    Discussion:") == 1
    assert prompt.count("Review threads on these lines") == 1
    assert prompt.count("    (discussion shown above)") == 1


def test_discussion_shown_above_only_when_something_was_shown() -> None:
    pr = _pr(number=12)
    bundle = _format_evidence(
        [CommitEvidence(_commit(), (pr,)), CommitEvidence(_commit(), (pr,))]
    )

    assert bundle.count("PR #12") == 2
    assert "discussion shown above" not in bundle


def test_discussion_shown_above_without_thread_inputs() -> None:
    pr = _pr(number=12, comments=_comments(("bob", "ship it")))
    bundle = _format_evidence(
        [CommitEvidence(_commit(), (pr,)), CommitEvidence(_commit(), (pr,))]
    )

    assert bundle.count("[bob] ship it") == 1
    assert bundle.count("    (discussion shown above)") == 1


# ---- R14: only the PR author's rendered thread words are quote sources -----


def _quote_basis(thread: dict, quote: str) -> str:
    """The basis a constraint quoting ``quote`` from PR #12 gets."""
    reply = json.dumps(
        {
            "purpose": "p",
            "why": "w",
            "constraints": [{"text": "t", "refs": ["PR #12"], "quote": quote}],
            "tradeoffs": [],
            "risks": [],
        }
    )
    _, card = _prompt([_blame(_thread_pr(thread))], reply=reply)
    return card.constraints[0].basis


def test_r14_a_quote_from_the_pr_authors_rendered_reply_is_quoted() -> None:
    assert _quote_basis(_thread(), "the upstream locks a client out") == "quoted"


def test_r14_a_quote_from_a_reviewers_thread_comment_is_cited() -> None:
    assert _quote_basis(_thread(), "and not five here") == "cited"


def test_r14_a_quote_from_an_unrendered_comment_is_cited() -> None:
    thread = _thread(
        _c("alice", _ASK),
        _c("octocat", "An earlier author note: the retry loop must stay bounded."),
        _c("bob", "a later reviewer comment"),
        _c("octocat", _WHY),
    )

    assert _quote_basis(thread, "the retry loop must stay bounded") == "cited"


def test_r14_a_quote_from_a_thread_that_did_not_qualify_is_cited() -> None:
    thread = _thread(
        anchor={"lines": ["return compute_the_other_value(42)"], "added": True}
    )

    assert _quote_basis(thread, "the upstream locks a client out") == "cited"


def test_r14_reviewer_words_quoted_inside_the_authors_reply_are_cited() -> None:
    thread = _thread(
        _c("alice", "Five attempts would ride out a slow deploy."),
        _c("octocat", f"> five attempts would ride out a slow deploy\n{_WHY}"),
    )

    assert _quote_basis(thread, "would ride out a slow deploy") == "cited"
    assert _quote_basis(thread, "the upstream locks a client out") == "quoted"


def test_system_prompt_labels_reviewer_comments_as_opinions() -> None:
    client = _StubClient()
    RationaleGenerator(client).generate([CommitEvidence(_commit())])

    system = client.requests[0].messages[0].content
    assert "Review-thread comments labelled `PR author`" in system
    assert (
        "never base a constraint, risk or tradeoff on a reviewer comment alone"
        in system
    )
