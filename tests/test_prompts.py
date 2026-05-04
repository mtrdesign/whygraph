from __future__ import annotations

import json

from whygraph.backend import SymbolNode
from whygraph.cochange.types import CoChangeNeighbor, CoChangeReport, VolatilityReport
from whygraph.context import RationaleContext
from whygraph.evidence.types import EvidenceRecord
from whygraph.neighbors import RationaleNeighbors
from whygraph.prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    Rationale,
    _commit_time,
    _iso_day,
    _parse_date,
    build_user_prompt,
)


def _empty_cochange(target_file: str = "src/pkg/a.py") -> CoChangeReport:
    return CoChangeReport(
        target_file=target_file,
        head_sha="",
        commits_considered=0,
        neighbors=[],
        truncated=0,
    )


def _empty_volatility(target_file: str = "src/pkg/a.py") -> VolatilityReport:
    return VolatilityReport(
        target_file=target_file,
        head_sha="",
        commits_total=0,
        commits_90d=0,
        commits_180d=0,
        commits_365d=0,
        distinct_authors=0,
        days_since_last_change=None,
    )


def _ctx(
    *,
    callers: list[SymbolNode] | None = None,
    callees: list[SymbolNode] | None = None,
    truncated_callers: int = 0,
    truncated_callees: int = 0,
    cochange: CoChangeReport | None = None,
    volatility: VolatilityReport | None = None,
) -> RationaleContext:
    return RationaleContext(
        neighbors=RationaleNeighbors(
            callers=callers or [],
            callees=callees or [],
            truncated_callers=truncated_callers,
            truncated_callees=truncated_callees,
        ),
        cochange=cochange or _empty_cochange(),
        volatility=volatility or _empty_volatility(),
    )


_NO_CONTEXT = _ctx()


def _node(**overrides) -> SymbolNode:
    base = dict(
        id="n_a",
        kind="function",
        name="a",
        qualified_name="pkg.a",
        file_path="src/pkg/a.py",
        language="python",
        start_line=1,
        end_line=10,
        docstring=None,
        signature=None,
    )
    base.update(overrides)
    return SymbolNode(**base)


def _record(source: str, ref: str | None, payload: dict, **overrides) -> EvidenceRecord:
    base = dict(
        id=0,
        node_id="n_a",
        qualified_name="pkg.a",
        source=source,
        ref=ref,
        payload=payload,
        collected_at=0,
    )
    base.update(overrides)
    return EvidenceRecord(**base)


# ---------------------------------------------------------------------------
# Schema + version
# ---------------------------------------------------------------------------


def test_prompt_version_is_v5() -> None:
    assert PROMPT_VERSION == "v5"


def test_system_prompt_includes_inlined_json_schema() -> None:
    # v3's deviation from v2: schema embedded in the system prompt so the
    # CLI backend can produce the right shape without output_config.
    assert '"purpose"' in SYSTEM_PROMPT
    assert '"why"' in SYSTEM_PROMPT
    assert '"constraints"' in SYSTEM_PROMPT
    assert '"tradeoffs"' in SYSTEM_PROMPT
    assert '"risks"' in SYSTEM_PROMPT


def test_rationale_schema_round_trips_via_model_validate_json() -> None:
    raw = json.dumps(
        {
            "purpose": "Validates JWT tokens",
            "why": "Replaces legacy cookie validator after compliance audit (2025-Q4).",
            "constraints": ["must be sync"],
            "tradeoffs": ["JWK lookup cached"],
            "risks": ["claim shape changes break RoleResolver"],
        }
    )
    r = Rationale.model_validate_json(raw)
    assert r.purpose == "Validates JWT tokens"
    assert r.constraints == ["must be sync"]


def test_rationale_schema_rejects_missing_fields() -> None:
    raw = json.dumps({"purpose": "x", "why": "y"})
    try:
        Rationale.model_validate_json(raw)
    except Exception:
        return
    raise AssertionError("expected validation error for missing fields")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_parse_date_handles_iso_with_z() -> None:
    assert _parse_date("2026-01-01T00:00:00Z") == 1767225600


def test_parse_date_handles_offset() -> None:
    # 2026-01-01T01:00:00+01:00 → same instant as 2026-01-01T00:00:00Z
    assert _parse_date("2026-01-01T01:00:00+01:00") == 1767225600


def test_parse_date_returns_none_for_missing_or_bad() -> None:
    assert _parse_date(None) is None
    assert _parse_date("") is None
    assert _parse_date("garbage") is None
    assert _parse_date(123) is None


def test_iso_day_formats_known_seconds() -> None:
    assert _iso_day(1767225600) == "2026-01-01"


def test_iso_day_returns_placeholder_for_none_or_zero() -> None:
    assert _iso_day(None) == "????-??-??"
    assert _iso_day(0) == "????-??-??"


def test_commit_time_extracts_author_time() -> None:
    rec = _record("git_commit", "abc", {"author_time": 42})
    assert _commit_time(rec) == 42


def test_commit_time_returns_zero_for_missing() -> None:
    rec = _record("git_commit", "abc", {})
    assert _commit_time(rec) == 0


# ---------------------------------------------------------------------------
# build_user_prompt
# ---------------------------------------------------------------------------


def test_build_user_prompt_includes_symbol_header() -> None:
    text = build_user_prompt(_node(), [], _NO_CONTEXT)
    assert "Symbol: pkg.a" in text
    assert "Kind: function" in text
    assert "Location: src/pkg/a.py:1-10" in text
    assert "Language: python" in text


def test_build_user_prompt_includes_signature_when_present() -> None:
    text = build_user_prompt(_node(signature="def a() -> int"), [], _NO_CONTEXT)
    assert "Signature: def a() -> int" in text


def test_build_user_prompt_omits_signature_when_absent() -> None:
    text = build_user_prompt(_node(signature=None), [], _NO_CONTEXT)
    assert "Signature:" not in text


def test_build_user_prompt_includes_docstring_when_present() -> None:
    text = build_user_prompt(_node(docstring="Does the thing"), [], _NO_CONTEXT)
    assert "Docstring:" in text
    assert "Does the thing" in text


def test_build_user_prompt_evidence_count_line() -> None:
    evidence = [
        _record("pr", "1", {}),
        _record("issue", "2", {}),
        _record("git_commit", "abc", {}),
        _record("git_commit", "def", {}),
        _record("git_blame", "abc", {}),
    ]
    text = build_user_prompt(_node(), evidence, _NO_CONTEXT)
    assert (
        "Evidence: 5 item(s) — 1 PR(s), 1 issue(s), 2 commit(s), 1 blame entr(ies)."
        in text
    )


def test_build_user_prompt_orders_commits_newest_first() -> None:
    evidence = [
        _record("git_commit", "old", {"author_time": 100, "subject": "old work"}),
        _record("git_commit", "mid", {"author_time": 200, "subject": "mid work"}),
        _record("git_commit", "new", {"author_time": 300, "subject": "new work"}),
    ]
    text = build_user_prompt(_node(), evidence, _NO_CONTEXT)
    new_idx = text.index("new work")
    mid_idx = text.index("mid work")
    old_idx = text.index("old work")
    assert new_idx < mid_idx < old_idx


def test_build_user_prompt_pr_section() -> None:
    pr = _record(
        "pr",
        "42",
        {
            "title": "Compliance fix",
            "author": "alice",
            "merged_at": "2026-03-12T10:00:00Z",
            "closes_issues": [100, 101],
            "body": "Body text",
        },
    )
    text = build_user_prompt(_node(), [pr], _NO_CONTEXT)
    assert "PR #42" in text
    assert "merged 2026-03-12" in text
    assert "by alice" in text
    assert "Compliance fix" in text
    assert "Closes: #100, #101" in text
    assert "Body text" in text


def test_build_user_prompt_pr_section_handles_unmerged() -> None:
    pr = _record(
        "pr",
        "42",
        {"title": "WIP", "author": "alice", "merged_at": None, "body": ""},
    )
    text = build_user_prompt(_node(), [pr], _NO_CONTEXT)
    assert "merged ????-??-??" in text


def test_build_user_prompt_issue_section_with_labels() -> None:
    issue = _record(
        "issue",
        "802",
        {
            "title": "Legal review",
            "labels": ["compliance", "legal"],
            "body": "Token storage gap.",
        },
    )
    text = build_user_prompt(_node(), [issue], _NO_CONTEXT)
    assert "ISSUE #802  [compliance, legal]" in text
    assert "Legal review" in text
    assert "Token storage gap." in text


def test_build_user_prompt_blame_section() -> None:
    blame = _record(
        "git_blame",
        "abc1234567890",
        {"line_count": 3, "line_total": 10, "summary": "fix bug"},
    )
    text = build_user_prompt(_node(), [blame], _NO_CONTEXT)
    assert "Blame" in text
    assert "abc12345" in text
    assert "3/10 lines" in text
    assert "fix bug" in text


def test_build_user_prompt_omits_empty_sections() -> None:
    text = build_user_prompt(_node(), [], _NO_CONTEXT)
    assert "Pull requests" not in text
    assert "Linked issues" not in text
    assert "Commits" not in text
    assert "Blame" not in text


def test_build_user_prompt_truncates_commit_sha_to_8() -> None:
    rec = _record(
        "git_commit",
        "abcdef1234567890",
        {"author_time": 1, "subject": "x", "author": "a"},
    )
    text = build_user_prompt(_node(), [rec], _NO_CONTEXT)
    assert "COMMIT abcdef12  " in text


# ---------------------------------------------------------------------------
# Neighbors (callers / callees) — v4 prompt enrichment
# ---------------------------------------------------------------------------


def _neighbor_node(qname: str, *, signature: str | None = None, docstring: str | None = None) -> SymbolNode:
    return SymbolNode(
        id=f"id_{qname}",
        kind="function",
        name=qname.rsplit(".", 1)[-1],
        qualified_name=qname,
        file_path=f"src/{qname.replace('.', '/')}.py",
        language="python",
        start_line=1,
        end_line=2,
        docstring=docstring,
        signature=signature,
    )


def test_build_user_prompt_omits_neighbor_sections_when_empty() -> None:
    text = build_user_prompt(_node(), [], _NO_CONTEXT)
    assert "Callers" not in text
    assert "Callees" not in text


def test_build_user_prompt_renders_callers_section() -> None:
    text = build_user_prompt(
        _node(),
        [],
        _ctx(callers=[_neighbor_node("pkg.caller_one", signature="def caller_one()")]),
    )
    assert "Callers (1 — symbols that call pkg.a):" in text
    assert "pkg.caller_one" in text
    assert "def caller_one()" in text


def test_build_user_prompt_renders_callees_section() -> None:
    text = build_user_prompt(
        _node(),
        [],
        _ctx(callees=[_neighbor_node("pkg.callee_one", docstring="does the thing")]),
    )
    assert "Callees (1 — symbols called by pkg.a):" in text
    assert "pkg.callee_one" in text
    assert "does the thing" in text


def test_build_user_prompt_neighbor_truncation_suffix() -> None:
    text = build_user_prompt(
        _node(),
        [],
        _ctx(
            callers=[_neighbor_node("pkg.c1"), _neighbor_node("pkg.c2")],
            truncated_callers=10,
        ),
    )
    # Format is "Callers (N of TOTAL — ...)" when truncated.
    assert "Callers (2 of 12 — symbols that call pkg.a):" in text


def test_build_user_prompt_neighbor_section_omits_signature_when_absent() -> None:
    text = build_user_prompt(
        _node(),
        [],
        _ctx(callers=[_neighbor_node("pkg.bare", signature=None, docstring=None)]),
    )
    # Symbol line is present, but no extra indented body lines for it.
    assert "pkg.bare" in text
    block_start = text.index("pkg.bare")
    block_tail = text[block_start:]
    lines = block_tail.split("\n")
    assert lines[0].endswith("pkg.bare")
    if len(lines) > 1:
        assert lines[1].strip() == ""


def test_system_prompt_mentions_callers_callees_guideline() -> None:
    assert "Callers" in SYSTEM_PROMPT
    assert "Callees" in SYSTEM_PROMPT
    assert "structural context" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Co-change + volatility sections — v5 prompt enrichment
# ---------------------------------------------------------------------------


def _cochange_with(
    *,
    target_file: str = "src/pkg/a.py",
    commits_considered: int = 5,
    neighbors: list[CoChangeNeighbor] | None = None,
    truncated: int = 0,
) -> CoChangeReport:
    return CoChangeReport(
        target_file=target_file,
        head_sha="head1",
        commits_considered=commits_considered,
        neighbors=neighbors or [],
        truncated=truncated,
    )


def test_cochange_section_omitted_below_min_commits_threshold() -> None:
    """Even with neighbors present, suppress the section when the denominator
    is too small to support a percentage claim."""
    report = _cochange_with(
        commits_considered=2,  # below MIN_COMMITS_FOR_DISPLAY=3
        neighbors=[
            CoChangeNeighbor(
                file_path="src/other.py", cochange_count=2, target_commits_total=2
            )
        ],
    )
    text = build_user_prompt(_node(), [], _ctx(cochange=report))
    assert "Co-evolves with" not in text


def test_cochange_section_omitted_when_no_neighbors() -> None:
    text = build_user_prompt(
        _node(), [], _ctx(cochange=_cochange_with(commits_considered=10, neighbors=[]))
    )
    assert "Co-evolves with" not in text


def test_cochange_section_renders_neighbors_with_percent() -> None:
    report = _cochange_with(
        commits_considered=4,
        neighbors=[
            CoChangeNeighbor(
                file_path="src/wizard.tsx", cochange_count=3, target_commits_total=4
            ),
            CoChangeNeighbor(
                file_path="src/api.ts", cochange_count=2, target_commits_total=4
            ),
        ],
    )
    text = build_user_prompt(_node(), [], _ctx(cochange=report))
    assert "Co-evolves with (2 — files modified in the same commits as src/pkg/a.py):" in text
    assert "src/wizard.tsx" in text
    assert "(3/4)" in text
    assert "(2/4)" in text


def test_cochange_section_truncation_suffix() -> None:
    report = _cochange_with(
        commits_considered=10,
        neighbors=[
            CoChangeNeighbor(
                file_path="src/x.py", cochange_count=5, target_commits_total=10
            )
        ],
        truncated=7,
    )
    text = build_user_prompt(_node(), [], _ctx(cochange=report))
    assert "Co-evolves with (top 1 of 8 — files modified in the same commits as src/pkg/a.py):" in text


def test_volatility_section_omitted_when_no_history() -> None:
    text = build_user_prompt(_node(), [], _NO_CONTEXT)
    assert "Volatility" not in text


def test_volatility_section_renders_with_history() -> None:
    vol = VolatilityReport(
        target_file="src/pkg/a.py",
        head_sha="h",
        commits_total=12,
        commits_90d=4,
        commits_180d=8,
        commits_365d=12,
        distinct_authors=3,
        days_since_last_change=9,
    )
    text = build_user_prompt(_node(), [], _ctx(volatility=vol))
    assert "Volatility (this file, all-time):" in text
    assert "Last changed: 9 days ago" in text
    assert "12 commit(s) total" in text
    assert "3 distinct authors" in text
    assert "4 in last 90d" in text


def test_volatility_section_singular_author_phrasing() -> None:
    vol = VolatilityReport(
        target_file="src/pkg/a.py",
        head_sha="h",
        commits_total=1,
        commits_90d=1,
        commits_180d=1,
        commits_365d=1,
        distinct_authors=1,
        days_since_last_change=0,
    )
    text = build_user_prompt(_node(), [], _ctx(volatility=vol))
    assert "1 distinct author" in text  # not "authors"
    assert "Last changed: today" in text


def test_system_prompt_mentions_cochange_and_volatility_guidelines() -> None:
    assert "Co-evolves with" in SYSTEM_PROMPT
    assert "Volatility" in SYSTEM_PROMPT
