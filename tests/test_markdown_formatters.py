from __future__ import annotations

from whygraph.backend import SymbolNode
from whygraph.cochange.types import CoChangeReport, VolatilityReport
from whygraph.context import RationaleContext
from whygraph.evidence.types import CollectionResult, EvidenceRecord
from whygraph.mcp_server import format_evidence_markdown, format_rationale_markdown
from whygraph.neighbors import RationaleNeighbors
from whygraph.prompts import PROMPT_VERSION
from whygraph.rationale import RationaleRecord, cache_key


def _empty_context(target_file: str = "src/pkg/a.py") -> RationaleContext:
    return RationaleContext(
        neighbors=RationaleNeighbors(
            callers=[], callees=[], truncated_callers=0, truncated_callees=0
        ),
        cochange=CoChangeReport(
            target_file=target_file,
            head_sha="",
            commits_considered=0,
            neighbors=[],
            truncated=0,
        ),
        volatility=VolatilityReport(
            target_file=target_file,
            head_sha="",
            commits_total=0,
            commits_90d=0,
            commits_180d=0,
            commits_365d=0,
            distinct_authors=0,
            days_since_last_change=None,
        ),
    )


_NO_CONTEXT = _empty_context()


def _node() -> SymbolNode:
    return SymbolNode(
        id="n_a",
        kind="function",
        name="a",
        qualified_name="pkg.a",
        file_path="src/pkg/a.py",
        language="python",
        start_line=10,
        end_line=20,
        docstring=None,
        signature=None,
    )


def _bundle_hash(short: str = "deadbeef") -> str:
    # 64-char hex string with a known prefix for assertions.
    return short.ljust(64, "0")


def _collection(
    *,
    evidence: list[EvidenceRecord] | None = None,
    bundle_hash: str | None = None,
    head: str | None = None,
    source: str = "collected",
) -> CollectionResult:
    return CollectionResult(
        evidence=evidence or [],
        bundle_hash=bundle_hash or _bundle_hash(),
        source=source,
        collected_at=0,
        head_at_collection=head,
    )


def _record(
    *,
    purpose: str = "Validates JWT.",
    why: str = "Replaces legacy cookie validator.",
    constraints: list[str] | None = None,
    tradeoffs: list[str] | None = None,
    risks: list[str] | None = None,
    bundle_hash: str | None = None,
) -> RationaleRecord:
    bh = bundle_hash or _bundle_hash()
    return RationaleRecord(
        node_id="n_a",
        bundle_hash=bh,
        prompt_version=PROMPT_VERSION,
        model="m",
        purpose=purpose,
        why=why,
        constraints=constraints or [],
        tradeoffs=tradeoffs or [],
        risks=risks or [],
        generated_at=0,
        cache_key=cache_key("pkg.a", "src/pkg/a.py", PROMPT_VERSION, "m", bh),
    )


# ---------------------------------------------------------------------------
# Evidence markdown
# ---------------------------------------------------------------------------


def test_evidence_markdown_renders_empty_block() -> None:
    text = format_evidence_markdown(_node(), _collection())
    assert "_(no evidence)_" in text
    assert "**Items**: 0" in text


def test_evidence_markdown_truncates_bundle_hash_to_12() -> None:
    bh = _bundle_hash("abcdef0123456789feedface")
    text = format_evidence_markdown(_node(), _collection(bundle_hash=bh))
    assert "bundle abcdef012345" in text
    assert bh not in text  # full 64-char form should not appear


def test_evidence_markdown_renders_head_at_collection_truncated() -> None:
    head = "1234567890abcdef" * 4  # 64 chars
    text = format_evidence_markdown(_node(), _collection(head=head))
    assert "**HEAD at collection**: 1234567890ab" in text


def test_evidence_markdown_renders_head_none() -> None:
    text = format_evidence_markdown(_node(), _collection(head=None))
    assert "**HEAD at collection**: (none)" in text


def test_evidence_markdown_renders_source_label() -> None:
    text = format_evidence_markdown(_node(), _collection(source="cache"))
    assert "**Source**: cache" in text


def test_evidence_markdown_lists_items_with_short_ref() -> None:
    rec = EvidenceRecord(
        id=1,
        node_id="n_a",
        qualified_name="pkg.a",
        source="git_commit",
        ref="abcdef1234567890",
        payload={"subject": "fix bug"},
        collected_at=0,
    )
    text = format_evidence_markdown(_node(), _collection(evidence=[rec]))
    assert "- **git_commit** `abcdef123456` — fix bug" in text


def test_evidence_markdown_handles_missing_ref() -> None:
    rec = EvidenceRecord(
        id=1,
        node_id="n_a",
        qualified_name="pkg.a",
        source="docstring",
        ref=None,
        payload={"summary": "module docstring"},
        collected_at=0,
    )
    text = format_evidence_markdown(_node(), _collection(evidence=[rec]))
    assert "- **docstring** `-` — module docstring" in text


# ---------------------------------------------------------------------------
# Rationale markdown
# ---------------------------------------------------------------------------


def test_rationale_markdown_renders_all_sections() -> None:
    text = format_rationale_markdown(
        _node(),
        _collection(source="cache"),
        _record(
            constraints=["must be sync"],
            tradeoffs=["JWK lookup cached"],
            risks=["claim shape change"],
        ),
        "cached",
        _NO_CONTEXT,
    )
    for header in ("## Purpose", "## Why", "## Constraints", "## Tradeoffs", "## Risks"):
        assert header in text
    assert "Validates JWT." in text
    assert "must be sync" in text


def test_rationale_markdown_renders_empty_lists_as_none() -> None:
    text = format_rationale_markdown(
        _node(), _collection(), _record(), "generated", _NO_CONTEXT
    )
    # All five sections fall back to _(none)_ — three empty lists plus
    # purpose/why fall back when empty (tested separately below).
    assert text.count("_(none)_") == 3


def test_rationale_markdown_renders_empty_purpose_and_why_as_none() -> None:
    text = format_rationale_markdown(
        _node(), _collection(), _record(purpose="", why=""), "generated", _NO_CONTEXT
    )
    assert text.count("_(none)_") == 5


def test_rationale_markdown_truncates_bundle_hash_to_12() -> None:
    bh = _bundle_hash("feedface00000000")
    text = format_rationale_markdown(
        _node(), _collection(bundle_hash=bh), _record(bundle_hash=bh), "cached", _NO_CONTEXT
    )
    assert "bundle feedface0000" in text
    assert bh not in text


def test_rationale_markdown_omits_confidence() -> None:
    text = format_rationale_markdown(
        _node(), _collection(), _record(), "generated", _NO_CONTEXT
    )
    assert "Confidence" not in text
    assert "confidence" not in text


def test_rationale_markdown_includes_source_and_evidence_source() -> None:
    text = format_rationale_markdown(
        _node(), _collection(source="cache"), _record(), "generated", _NO_CONTEXT
    )
    assert "**Rationale**: generated · **Evidence**: cache" in text


def test_rationale_markdown_includes_model_and_prompt_version() -> None:
    text = format_rationale_markdown(
        _node(), _collection(), _record(), "generated", _NO_CONTEXT
    )
    assert f"**Model**: m (prompt {PROMPT_VERSION})" in text


def test_rationale_markdown_renders_constraints_as_bullets() -> None:
    text = format_rationale_markdown(
        _node(),
        _collection(),
        _record(constraints=["one", "two", "three"]),
        "generated",
        _NO_CONTEXT,
    )
    assert "- one" in text
    assert "- two" in text
    assert "- three" in text


def test_rationale_markdown_context_line_zero_neighbors() -> None:
    text = format_rationale_markdown(
        _node(), _collection(), _record(), "generated", _NO_CONTEXT
    )
    assert "**Context**: (no callers, callees, or co-change signal)" in text


def test_rationale_markdown_context_line_with_neighbors() -> None:
    callers = [
        SymbolNode(
            id="c1",
            kind="function",
            name="caller_one",
            qualified_name="pkg.caller_one",
            file_path="src/c1.py",
            language="python",
            start_line=1,
            end_line=2,
            docstring=None,
            signature=None,
        )
    ]
    neighbors = RationaleNeighbors(
        callers=callers, callees=[], truncated_callers=2, truncated_callees=0
    )
    context = RationaleContext(
        neighbors=neighbors,
        cochange=_NO_CONTEXT.cochange,
        volatility=_NO_CONTEXT.volatility,
    )
    text = format_rationale_markdown(
        _node(), _collection(), _record(), "generated", context
    )
    # Total caller count = 1 shown + 2 truncated.
    assert "**Context**: 3 caller(s), 0 callee(s), 0 co-change peer(s)" in text


def test_rationale_markdown_volatility_line_no_history() -> None:
    text = format_rationale_markdown(
        _node(), _collection(), _record(), "generated", _NO_CONTEXT
    )
    assert "**Volatility**: (no git history)" in text


def test_rationale_markdown_volatility_line_with_history() -> None:
    vol = VolatilityReport(
        target_file="src/pkg/a.py",
        head_sha="h",
        commits_total=8,
        commits_90d=3,
        commits_180d=5,
        commits_365d=8,
        distinct_authors=2,
        days_since_last_change=14,
    )
    context = RationaleContext(
        neighbors=_NO_CONTEXT.neighbors,
        cochange=_NO_CONTEXT.cochange,
        volatility=vol,
    )
    text = format_rationale_markdown(
        _node(), _collection(), _record(), "generated", context
    )
    assert "**Volatility**: 8 commit(s), 3 in last 90d, last touched 14d ago" in text
