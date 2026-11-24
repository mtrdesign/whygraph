"""Tests for the pure plan builders - :mod:`whygraph.mcp.plans`.

Plan 61 section 9.1 cases 3, 4, 4a, 4b, 4d, 4e and the pure half of 5.
The harness fixtures are real harness-written plans:
``tests/fixtures/plans/harness.md`` (``plans/58-eval/replay/plan.md``,
tagged quotes), ``harness-untagged.md`` (a 47-eval plan, no basis tags)
and ``c17.md`` (a ui-demo gate plan).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from whygraph.analyze.project_purpose import redact_purpose_text
from whygraph.mcp import plans
from whygraph.mcp.plans import (
    DIGEST_MAX_CHARS,
    LINE_CUT_NOTICE,
    OUTLINE_MAX_ENTRIES,
    SECTION_MAX_CHARS,
    build_digest,
    build_outline,
    build_why_index,
    echo_spans,
    parse_header,
    path_mentions,
    plan_trailers,
)

FIXTURES = Path(__file__).parent / "fixtures" / "plans"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


HARNESS = _fixture("harness.md")
UNTAGGED = _fixture("harness-untagged.md")
C17 = _fixture("c17.md")


def _parts(body: str) -> tuple[list, list[dict]]:
    spans = echo_spans(body)
    return spans, build_outline(body, spans)


def _digest(body: str, stem: str = "p") -> str:
    spans, outline = _parts(body)
    return build_digest(body, outline, spans, stem=stem)


def _why(body: str) -> dict:
    spans, outline = _parts(body)
    return build_why_index(body, outline, spans)


def _mentions(body: str) -> dict[str, plans.PathMention]:
    spans, outline = _parts(body)
    return {m.path: m for m in path_mentions(body, outline, spans)}


def _echo_text(body: str) -> list[tuple[str, str]]:
    return [(kind, body[a:b]) for a, b, kind in echo_spans(body)]


def _own_text(body: str, entry: dict) -> str:
    return body[entry["start"] : entry["end"]]


# --- case 3: trailers ----------------------------------------------------------------


def test_trailer_key_is_case_insensitive_and_values_reduce_to_stems() -> None:
    body = (
        "Fix it\n\n"
        "WhyGraph-Plan: add-notes-filter\n"
        "whygraph-plan: docs/plans/other-plan.md\n"
        "WHYGRAPH-PLAN: third.md\n"
    )
    assert plan_trailers(body) == ["add-notes-filter", "other-plan", "third"]


def test_trailer_ignores_mid_sentence_invalid_and_caps_at_five() -> None:
    body = "We follow WhyGraph-Plan: not-a-trailer here\nWhyGraph-Plan: Bad_Stem\n"
    body += "".join(f"WhyGraph-Plan: plan-{i}\n" for i in range(8))
    assert plan_trailers(body) == [f"plan-{i}" for i in range(5)]


def test_trailer_matches_crlf_and_cr_bodies() -> None:
    assert plan_trailers("Title\r\n\r\nWhyGraph-Plan: crlf-plan\r\nmore\r\n") == [
        "crlf-plan"
    ]
    assert plan_trailers("Title\r\rWhyGraph-Plan: cr-plan\r") == ["cr-plan"]
    assert plan_trailers(None) == []
    assert plan_trailers("WhyGraph-Plan: a-b\nWhyGraph-Plan: a-b\n") == ["a-b"]


# --- case 4: header ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("# Plan: Add X\n\n**Objective:** Do X.\n", ("Add X", "Do X.")),
        ("# Plain title\n\ntext\n", ("Plain title", None)),
        ("# Plan: CRLF\r\n**Objective:** Win.\r\n", ("CRLF", "Win.")),
        ("\ufeff# Plan: BOM\n**Objective:** Yes.\n", ("BOM", "Yes.")),
        ("no heading at all\n", (None, None)),
        ("```\n# not a title\n```\n# Real\n", ("Real", None)),
    ],
)
def test_parse_header(body: str, expected: tuple) -> None:
    assert parse_header(body) == expected


# --- case 4b: outline ----------------------------------------------------------------------


def test_outline_numbers_entries_in_order_with_a_header() -> None:
    body = "# T\nintro\n## A\na\n### A1\nx\n## B\nb\n"
    outline = build_outline(body, [])
    assert [(e["n"], e["level"], e["title"], e["parent"]) for e in outline] == [
        (0, 1, "Header", None),
        (1, 2, "A", None),
        (2, 3, "A1", 1),
        (3, 2, "B", None),
    ]
    # A ## with ### children owns only its intro.
    assert _own_text(body, outline[1]) == "## A\na\n"


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_outline_ignores_headings_inside_fences(fence: str) -> None:
    body = f"# T\n## A\n{fence}\n## not a heading\n### nor this\n{fence}\n## B\n"
    titles = [e["title"] for e in build_outline(body, [])]
    assert titles == ["Header", "A", "B"]


def test_outline_splits_a_long_leaf_into_continued_parts() -> None:
    line = "x" * 99 + "\n"
    body = "# T\n## Long\n" + line * 180  # about 18,000 characters
    outline = build_outline(body, [])
    long_parts = [e for e in outline if e["title"].startswith("Long")]
    assert len(long_parts) == 3
    assert [e["title"] for e in long_parts] == [
        "Long",
        "Long (continued 2/3)",
        "Long (continued 3/3)",
    ]
    assert [e["part"] for e in long_parts] == [[1, 3], [2, 3], [3, 3]]
    for entry in long_parts:
        text = _own_text(body, entry)
        assert len(text) <= SECTION_MAX_CHARS
        assert text.endswith("\n")
    assert "".join(_own_text(body, e) for e in outline) == body


def test_outline_cuts_a_single_overlong_line() -> None:
    body = "# T\n## Wide\n" + "y" * 10_000 + "\nend\n"
    outline = build_outline(body, [])
    wide = [e for e in outline if e["title"].startswith("Wide")]
    assert len(wide) == 2
    assert wide[0]["cut"] is True and wide[1]["cut"] is False
    for entry in wide:
        served = _own_text(body, entry) + (LINE_CUT_NOTICE if entry["cut"] else "")
        assert len(served) <= SECTION_MAX_CHARS
    assert "".join(_own_text(body, e) for e in outline) == body


def test_outline_of_a_plan_without_sections_and_of_an_empty_body() -> None:
    body = "# Only a title\n" + ("z" * 79 + "\n") * 150  # 12,000 characters
    outline = build_outline(body, [])
    assert [e["title"] for e in outline] == ["Header", "Header (continued 2/2)"]
    assert "".join(_own_text(body, e) for e in outline) == body
    empty = build_outline("", [])
    assert empty == [
        {
            "n": 0,
            "level": 1,
            "title": "Header",
            "start": 0,
            "end": 0,
            "parent": None,
            "part": None,
            "echo": False,
            "cut": False,
        }
    ]


def test_outline_caps_the_entry_count_and_leaves_the_rest_out() -> None:
    body = "# T\n" + "".join(
        f"## S{i}\ntext {i}\n" for i in range(OUTLINE_MAX_ENTRIES + 50)
    )
    outline = build_outline(body, [])
    assert len(outline) == OUTLINE_MAX_ENTRIES
    # The last kept entry absorbs the headings after it (one part fits it all).
    assert outline[-1]["title"] == f"S{OUTLINE_MAX_ENTRIES - 2}"
    assert outline[-1]["end"] == len(body)
    assert "".join(_own_text(body, e) for e in outline) == body


def test_outline_cap_reports_characters_left_out() -> None:
    big = "w" * 7_000 + "\n"
    body = "# T\n" + "".join(f"## S{i}\n{big}" for i in range(OUTLINE_MAX_ENTRIES + 10))
    outline = build_outline(body, [])
    assert len(outline) == OUTLINE_MAX_ENTRIES
    assert outline[-1]["end"] < len(body)
    joined = "".join(_own_text(body, e) for e in outline)
    assert body.startswith(joined)


def test_outline_titles_are_one_line_and_clipped() -> None:
    title = "word " * 40
    body = f"# T\n## {title}\nx\n"
    outline = build_outline(body, [])
    assert len(outline[1]["title"]) <= 120
    assert "\n" not in outline[1]["title"]


def test_outline_of_a_crlf_plan_after_normalising() -> None:
    lf = "# T\n## A\na\n### B\nb\n"
    crlf = lf.replace("\n", "\r\n")
    assert build_outline(plans.normalise_newlines(crlf), []) == build_outline(lf, [])


def test_outline_harness_steps_are_entries() -> None:
    spans, outline = _parts(HARNESS)
    titles = {e["n"]: e["title"] for e in outline}
    assert titles[4] == "Steps"
    assert titles[8] == "Step 4: Notes page: add the Kind dropdown"
    assert outline[8]["parent"] == 4 and outline[8]["level"] == 3
    assert titles[12] == "Resolved decisions"
    step4 = _own_text(HARNESS, outline[8])
    assert len(step4) <= 3000
    assert step4.startswith("4. **Notes page")
    assert "5. **Docs" not in step4 and "3. **Client" not in step4


@pytest.mark.parametrize("name", ["harness.md", "harness-untagged.md", "c17.md"])
def test_outline_join_invariant_and_echo_never_moves_offsets(name: str) -> None:
    body = _fixture(name)
    spans = echo_spans(body)
    with_echo = build_outline(body, spans)
    without = build_outline(body, [])
    assert "".join(_own_text(body, e) for e in with_echo) == body
    assert [(e["start"], e["end"]) for e in with_echo] == [
        (e["start"], e["end"]) for e in without
    ]
    assert any(e["echo"] for e in with_echo) == bool(spans)
    assert not any(e["echo"] for e in without)


# --- case 4e: echo --------------------------------------------------------------------------


def test_echo_tagged_quote_with_escaped_quotes_runs_to_the_tag() -> None:
    line = next(line for line in HARNESS.splitlines() if line.startswith('- "Relaxing'))
    start = HARNESS.index(line)
    quote = line[2 : line.index(" (quoted: 3c3f8020)")]
    assert quote.endswith('"') and '\\"' in quote
    assert ("b", quote) in _echo_text(HARNESS)
    spans = echo_spans(HARNESS)
    assert any(a <= start + 2 and b >= start + 2 + len(quote) for a, b, _ in spans)


def test_echo_covers_step_constraints_and_risks_fields() -> None:
    kinds = _echo_text(HARNESS)
    a_texts = [text for kind, text in kinds if kind == "a"]
    assert any(
        text.startswith('"`whygraph_propose_note` must call') for text in a_texts
    )
    assert "none recorded" in a_texts


def test_echo_changes_on_purpose_keeps_the_reason() -> None:
    spans = echo_spans(HARNESS)
    reason = "the task itself changes the notes query key and filter handling"
    pos = HARNESS.index(reason)
    assert not any(a <= pos < b for a, b, _ in spans)


def test_echo_untagged_quotes_and_prior_art() -> None:
    kinds = _echo_text(UNTAGGED)
    risks = [text for kind, text in kinds if kind == "e"]
    assert any(text.startswith('"The gate order') for text in risks)
    prior = [text for kind, text in kinds if kind == "g"]
    assert len(prior) == 1 and prior[0].startswith("## Prior art")
    # Step 1's untagged constraints are kind (a).
    assert any(
        kind == "a" and text.startswith('"`agent_generations_per_hour`')
        for kind, text in kinds
    )


def test_echo_constraint_checks_blockers_and_external() -> None:
    body = (
        "# Plan: E\n\n"
        "## Steps\n\n"
        "1. **One**\n"
        "   - **Files:** src/a.py:1-2\n"
        '   - **Test first:** "copied text"\n\n'
        "## Constraint checks\n- anything here\n\n"
        "## Blockers\n- **Constraint at risk:** something copied\n- own blocker\n\n"
        "## Notes\nUse a queue (external)\nOwn line.\n"
    )
    kinds = {kind for _, _, kind in echo_spans(body)}
    assert kinds == {"c", "d", "e", "f"}
    texts = _echo_text(body)
    assert ("e", '"copied text"') in texts
    assert ("f", "Use a queue (external)") in texts
    assert ("d", "- **Constraint at risk:** something copied") in texts
    assert any(kind == "c" and "anything here" in text for kind, text in texts)
    assert not any("own blocker" in text for _, text in texts)


def test_echo_all_seven_kinds_on_the_fixtures() -> None:
    found = {kind for _, _, kind in echo_spans(HARNESS)}
    found |= {kind for _, _, kind in echo_spans(UNTAGGED)}
    assert {"a", "b", "e", "g"} <= found


def test_echo_names_make_no_decision_name_and_no_link() -> None:
    why = _why(HARNESS)
    names = {name for row in why["decisions"] for name in row["names"]}
    assert "whygraph_propose_note" not in names
    # A path that appears only inside echo text makes no mention.
    body = (
        "# Plan: P\n\n## Steps\n\n1. **One**\n   - **Files:** src/a.py:1-3\n"
        '   - **Constraints to preserve:** "keep src/only_echo.py stable" (cited: abc)\n'
    )
    assert "src/only_echo.py" not in _mentions(body)


def test_echo_prior_art_path_has_no_section_and_reads_as_copies() -> None:
    spans, outline = _parts(UNTAGGED)
    prior = next(e for e in outline if e["title"] == "Prior art")
    assert prior["echo"] is True
    mention = _mentions(UNTAGGED)["src/whygraph/portal/policy.py"]
    assert prior["n"] not in mention.sections
    assert mention.sections == (11,)  # step 7's own Files line


# --- case 5 (pure): path mentions -------------------------------------------------------------


def test_path_mention_shapes() -> None:
    body = (
        "# T\n\n## Notes\n"
        "Edit src/a.py:12-30 and ./src/b.py, then see src/c.py.\n"
        "Also src/d.py:7; and (src/e.py) here.\n"
        "Not a path: https://x/y and http://example.com/a/b\n"
    )
    mentions = _mentions(body)
    for path in ("src/a.py", "src/b.py", "src/c.py", "src/d.py", "src/e.py"):
        assert path in mentions, path
    assert "x/y" not in mentions
    assert not any(path.startswith(("example.com", "a/b")) for path in mentions)


def test_path_mentions_declared_only_with_a_range_or_point() -> None:
    body = (
        "# Plan: P\n\n## Steps\n\n"
        "1. **One**\n"
        "   - **Files:** src/a.py:1-3, src/b.py:9, src/c.py\n"
        "   - **Change:** also touch src/d.py:4-5.\n"
    )
    mentions = _mentions(body)
    assert mentions["src/a.py"].declared is True
    assert mentions["src/b.py"].declared is True
    assert mentions["src/c.py"].declared is False
    assert mentions["src/d.py"].declared is False


def test_path_mentions_skip_negative_mentions_in_the_harness_fixture() -> None:
    mentions = _mentions(HARNESS)
    assert "src/playground/src/components/NotesTab.tsx" not in mentions
    assert "src/playground/src/router.tsx" not in mentions
    assert "src/whygraph/chat/tools.py" not in mentions
    assert mentions["src/playground/src/pages/NotesPage.tsx"].sections == (8,)
    assert mentions["src/playground/src/pages/NotesPage.tsx"].declared is True


def test_path_mentions_amendments_link_only_the_new_side() -> None:
    body = (
        "# Plan: P\n\n## Steps\n\n1. **One**\n   - **Files:** src/new.py:1-2\n\n"
        "## Amendments\n"
        "- Step 1: Files - src/old.py:1-2 -> src/new.py:1-2 (2026-11-16T10:00:00Z)\n"
    )
    mentions = _mentions(body)
    assert "src/old.py" not in mentions
    assert "src/new.py" in mentions


def test_path_mentions_sections_skip_header_and_working_set() -> None:
    body = (
        "# Plan: P\n**Read first:** src/head.py\n\n"
        "## Working set\n| `f` | src/ws.py:1-2 | x |\n\n"
        "## A\nsrc/both.py\n\n## B\ntext\n\n## C\nsrc/both.py again\n"
    )
    mentions = _mentions(body)
    assert mentions["src/head.py"].sections == ()
    assert mentions["src/ws.py"].sections == ()
    assert mentions["src/both.py"].sections == (2, 4)


def test_path_mentions_skip_no_change_needed_sentences() -> None:
    body = (
        "# T\n\n## Notes\n"
        "No change needed in src/skip.py:3 (`f`): it is fine. But src/keep.py changes.\n"
    )
    mentions = _mentions(body)
    assert "src/skip.py" not in mentions
    assert "src/keep.py" in mentions


# --- case 4d: why index --------------------------------------------------------------------------


def test_why_index_harness_fixture_matches_the_plan_example() -> None:
    why = _why(HARNESS)
    step4 = next(step for step in why["steps"] if step["k"] == 4)
    assert step4 == {
        "k": 4,
        "name": "Notes page: add the Kind dropdown",
        "n": 8,
        "files": [
            {
                "path": "src/playground/src/pages/NotesPage.tsx",
                "ranges": [[32, 112]],
                "points": [],
            },
            {
                "path": "src/playground/src/test/notes.test.tsx",
                "ranges": [],
                "points": [104, 197],
            },
        ],
        "not": ["ProjectPurposeCard", "rejectCount"],
        "on_purpose": True,
        "done": True,
    }
    assert why["non_goals"] == ["NOTE_FILTERS", "find_notes"]
    assert why["decisions"][0] == {
        "id": "Q1",
        "title": "What should the dropdown filter by?",
        "n": 12,
        "names": [],
    }
    step3 = next(step for step in why["steps"] if step["k"] == 3)
    assert step3["not"] == ["NOTE_FILTERS"]
    step1 = next(step for step in why["steps"] if step["k"] == 1)
    assert step1["files"][1] == {
        "path": "tests/test_mcp_notes.py",
        "ranges": [],
        "points": [1052],
    }
    assert step1["not"] == ["_refuse_linked"]


def test_why_index_c17_point() -> None:
    why = _why(C17)
    assert why["steps"][0]["files"] == [
        {"path": "src/main.jsx", "ranges": [], "points": [8]}
    ]
    assert why["steps"][0]["done"] is True


def _harness_plan(log: str | None, rows: str = "") -> str:
    body = (
        "# Plan: P\n\n**Status:** PROPOSAL\n**Objective:** O.\n**Non-goals:** `Foo` stays.\n\n"
        "## Steps\n\n"
        "1. **First**\n   - **Files:** src/a.py:1-3\n   - **Boundaries:** `Bar.baz()`\n\n"
        "2. **Second**\n   - **Files:** src/b.py\n\n"
    )
    if rows:
        body += rows
    if log is not None:
        body += "## Implementation log\n\n" + log
    return body


def test_why_index_done_reads_the_last_log_line_and_null_without_log() -> None:
    logged = _harness_plan(
        "- [x] Step 1: First (verified)\n- [ ] Step 1: First (failed)\n"
    )
    steps = _why(logged)["steps"]
    assert steps[0]["done"] is False
    assert steps[1]["done"] is None
    assert steps[0]["not"] == ["Bar::baz"]
    assert _why(_harness_plan(None))["steps"][0]["done"] is None
    assert _why(_harness_plan(None))["non_goals"] == ["Foo"]


def test_why_index_decision_rows_ids_titles_and_names() -> None:
    rows = (
        "## Resolved decisions\n\n"
        "| # | Question | Decision |\n|---|---|---|\n"
        "| R4 (was Q1) | Where? | Use `Foo.bar()` and `path/like.py` |\n"
        "| free text | Why? | words only |\n\n"
        "### What changed in rev 2\n\n| Id | Change |\n|---|---|\n| S1 | `Nope` |\n\n"
        "### Risks\n\n| # | Risk |\n|---|---|\n| 1 | `Risky` |\n\n"
        "```\n| R9 | in a fence | `Fenced` |\n```\n\n"
    )
    why = _why(_harness_plan(None, rows))
    assert [
        (row["id"], row.get("title"), row["names"]) for row in why["decisions"]
    ] == [
        ("R4", "Where?", ["Foo::bar"]),
        ("row 2", "Why?", []),
    ]


def test_why_index_plan25_shape_and_plain_decisions_heading() -> None:
    plan25 = (
        "# Plan: M\n\n## 0. Resolved decisions\n\n### 0.1 Decided together\n\n"
        "| Decision | What it means |\n|---|---|\n"
        "| **End users sign in with GitHub only** | No email accounts. |\n"
    )
    why = _why(plan25)
    assert why["decisions"] == [{"id": "row 1", "n": 2, "names": []}]
    assert why["steps"] == []
    plan38 = (
        "# Plan: C\n\n## 0. Decisions\n\n| # | Finding | Fix |\n|---|---|---|\n"
        "| F1 | `Thing` broke | fixed |\n"
    )
    assert _why(plan38)["decisions"] == []


def test_why_index_caps_steps_and_rows() -> None:
    steps = "".join(
        f"{k}. **Step {k}**\n   - **Files:** src/f{k}.py:1-2\n\n" for k in range(1, 70)
    )
    rows = "".join(f"| Q{k} | q{k}? | a |\n" for k in range(1, 70))
    body = (
        "# Plan: Many\n\n## Steps\n\n" + steps + "## Resolved decisions\n\n"
        "| Q | Question | Answer |\n|---|---|---|\n" + rows
    )
    why = _why(body)
    assert len(why["steps"]) == 60
    assert len(why["decisions"]) == 60


def test_why_index_is_json_serialisable() -> None:
    for body in (HARNESS, UNTAGGED, C17):
        json.dumps(_why(body))


# --- case 4a: digest -------------------------------------------------------------------------------


def test_digest_harness_format_order_and_content() -> None:
    digest = _digest(HARNESS, stem="add-notes-kind-filter")
    lines = digest.splitlines()
    assert lines[0] == "# Add a filter dropdown to the notes list"
    assert lines[1].startswith("**Objective:** A member narrows")
    assert lines[2].startswith("**Non-goals:** The four status tabs")
    assert lines[3] == (
        "Implementation log: acceptance passed (2026-11-16); 5 of 5 steps done."
    )
    order = [
        digest.index("| Q1 |"),
        digest.index("Step 4 (Notes page: add the Kind dropdown) changes a recorded"),
        digest.index("Q3: Should the dropdown offer"),
        digest.index("- The page pages server-side"),
        digest.index("… digest of a 125-line plan"),
    ]
    assert order == sorted(order)
    assert "**Status:**" not in digest and "PROPOSAL" not in digest
    assert "Read first" not in digest
    assert "Constraints to preserve" not in digest
    assert "Working set" not in digest and "list_notes` (domain)" not in digest
    assert (
        "Step 4 (Notes page: add the Kind dropdown) changes a recorded constraint "
        "(cited: 58e1281d): the task itself changes the notes query key"
    ) in digest
    assert "The existing notes query" not in digest
    assert (
        'Q3: Should the dropdown offer "Project purpose"? Default: Yes, all five kinds'
        in digest
    )
    # Quoted (echo) risks are out; the plan's own one is in.
    assert "A validator that builds its result" not in digest
    assert "Relaxing the" not in digest
    assert digest.endswith(
        "read one section at a time: whygraph://plan/add-notes-kind-filter/section/<n>"
    )
    assert len(digest) <= DIGEST_MAX_CHARS


def test_digest_log_line_absent_without_a_log_and_counts_distinct_done_steps() -> None:
    assert "Implementation log" not in _digest(_harness_plan(None))
    steps = "".join(
        f"{k}. **S{k}**\n   - **Files:** src/f{k}.py:1-2\n\n" for k in range(1, 6)
    )
    log = (
        "- [ ] Step 1: S1 (failed)\n- Acceptance: failed: 1 (2026-11-15T10:00:00Z)\n"
        "- [x] Step 1: S1\n- [x] Step 1: S1\n- [x] Step 2: S2\n- [x] Step 3: S3\n"
        "- [x] Step 4: S4\n- Acceptance: passed (2026-11-16T18:20:14Z)\n"
    )
    body = "# Plan: P\n\n## Steps\n\n" + steps + "## Implementation log\n\n" + log
    assert (
        "Implementation log: acceptance passed (2026-11-16); 4 of 5 steps done."
        in _digest(body)
    )
    no_acceptance = (
        "# Plan: P\n\n## Steps\n\n"
        + steps
        + "## Implementation log\n\n- [x] Step 2: S2\n"
    )
    assert "Implementation log: 1 of 5 steps done." in _digest(no_acceptance)


def test_digest_untagged_quotes_and_external_risks_are_left_out() -> None:
    digest = _digest(UNTAGGED)
    for quoted in (
        "The gate order (cached_only",
        "The serve handler (`whygraph_error_handler`)",
        "Any path that opens a local DB",
    ):
        assert quoted not in digest
    assert "- Uncovered by any existing test" in digest
    external = UNTAGGED.replace(
        "- Uncovered by any existing test",
        "- Use a circuit breaker for the provider (external)\n- Uncovered by any existing test",
    )
    assert "circuit breaker" not in _digest(external)
    assert "Prior art" not in digest and "PR #61" not in digest


def test_digest_skips_superseded_rows_and_drops_the_earliest_on_overflow() -> None:
    rows = "| R0 | Old? | **Superseded** by R9 |\n"
    rows += "".join(
        f"| R{k} | Question {k}? | {'d' * 150} {k} |\n" for k in range(1, 40)
    )
    body = (
        "# Plan: Long\n\n**Objective:** Win.\n\n## 0. Resolved decisions\n\n"
        "| # | Question | Decision |\n|---|---|---|\n" + rows
    )
    digest = _digest(body)
    assert len(digest) <= DIGEST_MAX_CHARS
    assert "| R0 |" not in digest
    assert "| R39 |" in digest
    assert "| R1 |" not in digest
    kept = re.findall(r"^\| R(\d+) \|", digest, re.M)
    assert kept == sorted(kept, key=int)
    match = re.search(r"left out: (\d+) decision rows \(section (\d+)\)", digest)
    assert match is not None
    assert int(match.group(1)) == 39 - len(kept)
    assert int(match.group(2)) == 1
    assert digest.splitlines()[-1].startswith("… digest of a ")


def test_digest_other_format_lines_and_decisions() -> None:
    body = (
        "# House plan\n\n**Status:** Draft\n**Goal:** Ship it.\n**Objective:** Win.\n"
        "**Non-goals:** Losing.\n\n## Resolved decisions (rev 2)\n\n"
        "| # | Question | Decision |\n|---|---|---|\n| R1 | Q? | Yes. |\n"
        "\n## Detail\nlots of text\n"
    )
    digest = _digest(body)
    assert digest.splitlines()[:4] == [
        "# House plan",
        "**Objective:** Win.",
        "**Goal:** Ship it.",
        "**Non-goals:** Losing.",
    ]
    assert "| R1 | Q? | Yes. |" in digest
    assert "Draft" not in digest and "lots of text" not in digest
    assert "left out" not in digest


def test_digest_fallback_without_objective_or_decisions() -> None:
    body = "# Notes\n**Status:** Old\n\n" + "Some prose line here.\n" * 200
    digest = _digest(body)
    assert "Old" not in digest
    assert "Some prose line here." in digest
    body_part = digest.split("\n\n")[1]
    assert len(body_part) <= 1500
    assert len(digest) <= DIGEST_MAX_CHARS


def test_digest_drops_items_in_reverse_order() -> None:
    risks = "".join(f"- Own risk {k} {'r' * 300}\n" for k in range(3))
    questions = "".join(
        f"- **Q{k}.** Question {k} {'q' * 150}? **Default:** yes\n" for k in range(1, 4)
    )
    rows = "".join(f"| R{k} | T{k}? | {'d' * 300} |\n" for k in range(1, 6))
    body = (
        "# Plan: Big\n\n**Objective:** O.\n\n## Steps\n\n1. **S**\n"
        "   - **Files:** src/a.py:1-2\n\n## Risks called out across the change\n"
        + risks
        + "\n## Resolved decisions\n| # | Question | Decision |\n|---|---|---|\n"
        + rows
        + "\n## Open questions\n"
        + questions
    )
    digest = _digest(body)
    assert len(digest) <= DIGEST_MAX_CHARS
    assert "| R1 |" in digest  # decision rows are dropped last
    assert "Own risk 2" not in digest  # risks go first
    assert "risk" in digest.splitlines()[-1]


def test_digest_two_thousand_line_plan_stays_within_the_cap() -> None:
    rows = "".join(f"| R{k} | Topic {k} | {'x' * 120} |\n" for k in range(1, 200))
    filler = "".join(f"line {i}\n" for i in range(1800))
    body = (
        "# Plan: Huge\n\n**Objective:** O.\n\n## Resolved decisions\n\n"
        "| # | Topic | Decision |\n|---|---|---|\n" + rows + "\n## Detail\n" + filler
    )
    digest = _digest(body, stem="huge")
    assert len(digest) <= DIGEST_MAX_CHARS
    last = digest.splitlines()[-1]
    assert last.startswith(
        f"… digest of a {plans.line_count(body)}-line plan; left out: "
    )
    assert "decision rows (section 1)" in last
    for line in digest.splitlines():
        if line.startswith("| R"):
            assert line.endswith("|")  # never cut mid-row


def test_digest_section_heading_forms() -> None:
    for heading in ("## 0. Resolved decisions", "## Resolved decisions (rev 2)"):
        body = f"# Plan: H\n\n{heading}\n\n| # | Question | Decision |\n|---|---|---|\n| R1 | Q? | A |\n"
        assert "| R1 | Q? | A |" in _digest(body)


def test_digest_empty_body_and_redacted_text() -> None:
    assert _digest("", stem="e") == (
        "# e\n\n… digest of a 0-line plan; read one section at a time: "
        "whygraph://plan/e/section/<n>"
    )
    raw = "# Plan: S\n\n**Objective:** Use AKIAABCDEFGHIJKLMNOP carefully.\n"
    body = redact_purpose_text(raw)
    digest = _digest(body)
    assert "AKIAABCDEFGHIJKLMNOP" not in digest
    assert "AKIA***" in digest


def test_digest_c17_fixture() -> None:
    digest = _digest(C17, stem="c17")
    assert digest.splitlines()[:4] == [
        "# C17 flags off",
        "**Objective:** C17 flags off.",
        "**Non-goals:** Everything else.",
        "Implementation log: acceptance passed (2026-11-08); 1 of 1 steps done.",
    ]


# --- hostile input ----------------------------------------------------------------------------------


def test_builders_stay_linear_on_hostile_lines() -> None:
    """Plan text is untrusted: a crafted 200 KB line must not make a scan quadratic."""
    import time

    size = 200_000
    steps = "# Plan: H\n\n## Steps\n\n1. **S**\n   - **Files:** src/a.py:1-2\n"
    bodies = [
        steps + "- " + '"x" (cited: ' * (size // 12) + "\n",
        "# T\n## " + " #" * (size // 2) + "x\n",
        "# T\n## A\n" + "No change needed in src/a.py " * (size // 30) + "\n",
        steps + "   - **Changes on purpose:** " + '" - ' * (size // 4) + "\n",
        "# T\n"
        + "## Resolved decisions\n| # | Question | D |\n|---|---|---|\n| R1 | q | `A.b` |\n"
        * (size // 80),
        steps + "## Open questions\n- " + "**Default:** **A" * (size // 16) + "\n",
    ]
    started = time.monotonic()
    for body in bodies:
        spans, outline = _parts(body)
        assert len(build_digest(body, outline, spans)) <= DIGEST_MAX_CHARS
        build_why_index(body, outline, spans)
        path_mentions(body, outline, spans)
    assert time.monotonic() - started < 20
