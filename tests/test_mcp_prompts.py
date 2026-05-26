"""Tests for the WhyGraph MCP prompts.

The prompt bodies are plain Python strings, so most tests call the
underlying functions directly and assert on substring content. One
test rounds through ``mcp.get_prompt`` to confirm the FastMCP pipeline
binds args, wraps the string as a single user message, and returns the
expected ``GetPromptResult`` shape.
"""

from __future__ import annotations

import asyncio

import pytest

from whygraph.mcp.prompts import (
    _pre_edit_brief,
    _triage_commit,
    _why_was_this_written,
)


# ---- registration / discoverability --------------------------------------


def test_prompts_registered() -> None:
    """All three prompts are listed with the expected arguments."""
    from whygraph.mcp.server import mcp

    listed = asyncio.run(mcp.list_prompts())
    by_name = {p.name: p for p in listed}
    assert set(by_name) == {
        "whygraph_pre_edit_brief",
        "whygraph_why_was_this_written",
        "whygraph_triage_commit",
    }

    # The targeted prompts surface four optional args; triage_commit has
    # a single required ``sha``.
    pre_edit_args = {a.name: a.required for a in by_name["whygraph_pre_edit_brief"].arguments or []}
    assert pre_edit_args == {
        "path": False,
        "line_start": False,
        "line_end": False,
        "qualified_name": False,
    }
    why_args = {a.name: a.required for a in by_name["whygraph_why_was_this_written"].arguments or []}
    assert why_args == pre_edit_args
    triage_args = {a.name: a.required for a in by_name["whygraph_triage_commit"].arguments or []}
    assert triage_args == {"sha": True}


def test_pre_edit_brief_via_get_prompt() -> None:
    """End-to-end via ``mcp.get_prompt`` — confirms FastMCP arg binding,
    single-string-to-UserMessage wrapping, and the returned shape."""
    from whygraph.mcp.server import mcp

    result = asyncio.run(
        mcp.get_prompt(
            "whygraph_pre_edit_brief",
            {"path": "src/foo.py", "line_start": 10, "line_end": 20},
        )
    )
    assert len(result.messages) == 1
    message = result.messages[0]
    assert message.role == "user"
    assert message.content.type == "text"
    assert "src/foo.py:10-20" in message.content.text
    assert "whygraph_rationale_brief" in message.content.text


# ---- pre_edit_brief ------------------------------------------------------


def test_pre_edit_brief_renders_line_target() -> None:
    body = _pre_edit_brief(path="src/foo.py", line_start=10, line_end=20)
    assert "src/foo.py:10-20" in body
    # Tool call args are embedded literally for direct copy-paste.
    assert 'path="src/foo.py"' in body
    assert "line_start=10" in body
    assert "line_end=20" in body
    # Both tools are referenced, with rationale leading.
    rationale_idx = body.index("whygraph_rationale_brief")
    evidence_idx = body.index("whygraph_evidence_for")
    assert rationale_idx < evidence_idx


def test_pre_edit_brief_renders_symbol_target() -> None:
    body = _pre_edit_brief(qualified_name="pkg.module.foo")
    assert "pkg.module.foo" in body
    assert 'qualified_name="pkg.module.foo"' in body
    # Line-range args must not leak in.
    assert "line_start" not in body
    assert "line_end" not in body


def test_pre_edit_brief_combines_path_and_qualified_name() -> None:
    """If both targeting modes are passed, the label includes both."""
    body = _pre_edit_brief(path="src/foo.py", qualified_name="pkg.foo")
    assert "src/foo.py" in body
    assert "pkg.foo" in body


def test_pre_edit_brief_rejects_no_target() -> None:
    with pytest.raises(ValueError, match="path"):
        _pre_edit_brief()


# ---- why_was_this_written ------------------------------------------------


def test_why_was_this_written_leads_with_evidence() -> None:
    """Framing distinction from pre_edit_brief: this prompt reaches for
    evidence first (the historical story) and rationale only as a synthesis
    aid."""
    body = _why_was_this_written(path="src/foo.py", line_start=10, line_end=20)
    assert "src/foo.py:10-20" in body
    rationale_idx = body.find("whygraph_rationale_brief")
    evidence_idx = body.find("whygraph_evidence_for")
    assert evidence_idx != -1
    assert rationale_idx != -1
    assert evidence_idx < rationale_idx


def test_why_was_this_written_renders_symbol_target() -> None:
    body = _why_was_this_written(qualified_name="pkg.foo")
    assert "pkg.foo" in body
    assert 'qualified_name="pkg.foo"' in body


def test_why_was_this_written_rejects_no_target() -> None:
    with pytest.raises(ValueError, match="path"):
        _why_was_this_written()


# ---- triage_commit -------------------------------------------------------


def test_triage_commit_body_references_resources() -> None:
    body = _triage_commit(sha="deadbeef")
    # Commit resource URI is interpolated; the PR resource URI is left
    # generic because the model fills the number from the linked PRs it sees.
    assert "whygraph://commit/deadbeef" in body
    assert "whygraph://pr/" in body
    # No tool calls — this prompt is resource-only.
    assert "whygraph_evidence_for" not in body
    assert "whygraph_rationale_brief" not in body


def test_triage_commit_requires_sha() -> None:
    """FastMCP enforces required args via Pydantic validation when called
    through ``get_prompt``. The direct Python call exposes the same contract
    via the missing-default positional arg."""
    from whygraph.mcp.server import mcp

    with pytest.raises(Exception):
        asyncio.run(mcp.get_prompt("whygraph_triage_commit", {}))
