"""Prompt template for the LLM commit descriptor.

The template is kept as a module-level constant rather than a class so
it stays trivially diff-able: a single grep finds every place the prompt
is referenced, and tests can assert against the exact wording.
"""

from __future__ import annotations

DEFAULT_PROMPT_TEMPLATE = """\
You are writing a note to your future self.

The diff below describes a code change. Your future readers are LLM agents — most often you — pulling this back as evidence for downstream features: rationale generation, code review, change attribution, dependency analysis, search. No human reads this directly.

Two anchors:
- Token efficiency. Every word costs your future self's context budget. Don't pad. Don't restate the diff verbatim. Don't moralize.
- No ambiguity. Your future self will not have the diff. They must be able to reason about this change from your note alone — paraphrases that erase identity are a failure mode.

You choose the shape, density, and notation. There is no required schema. Decide what's worth keeping and how to write it.

Diff:
{diff}

Output only the description.
"""
"""Default prompt template. Must contain the literal ``{diff}`` placeholder."""


def render(diff: str, *, template: str | None = None) -> str:
    """Render a prompt by interpolating ``diff`` into the template.

    Parameters
    ----------
    diff : str
        Raw diff text. The caller is responsible for any truncation —
        :class:`whygraph.analyze.LlmDescriptor` does this before
        calling :func:`render`.
    template : str, optional
        Override the template. Must contain ``{diff}``. Defaults to
        :data:`DEFAULT_PROMPT_TEMPLATE`. Mostly used in tests.

    Returns
    -------
    str
        The rendered prompt, ready to ship as the body of a
        :class:`whygraph.services.llm.CompletionRequest`.
    """
    return (template or DEFAULT_PROMPT_TEMPLATE).format(diff=diff)
