"""Tests for :mod:`whygraph.analyze.prompt`."""

from __future__ import annotations

from whygraph.analyze import DEFAULT_PROMPT_TEMPLATE, render_prompt


def test_render_interpolates_diff() -> None:
    rendered = render_prompt("DIFF_BODY")
    assert "DIFF_BODY" in rendered


def test_render_uses_default_template() -> None:
    """Default template is the one exported as DEFAULT_PROMPT_TEMPLATE."""
    rendered = render_prompt("x")
    expected = DEFAULT_PROMPT_TEMPLATE.format(diff="x")
    assert rendered == expected


def test_render_template_override() -> None:
    rendered = render_prompt("payload", template="<<{diff}>>")
    assert rendered == "<<payload>>"


def test_default_template_ends_with_output_only_directive() -> None:
    """Guardrail: noticed drift in the prompt instruction tail."""
    assert DEFAULT_PROMPT_TEMPLATE.rstrip().endswith("Output only the description.")


def test_default_template_contains_diff_placeholder() -> None:
    assert "{diff}" in DEFAULT_PROMPT_TEMPLATE
