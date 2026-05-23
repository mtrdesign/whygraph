"""Value object returned by :class:`whygraph.analyze.LlmDescriptor`."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Description:
    """One LLM-written description of a git diff.

    Attributes
    ----------
    text : str
        The model's description, stripped of surrounding whitespace.
        Suitable to write directly into ``commit.llm_description``.
    model : str
        Model identifier as reported by the provider — echoed from
        :attr:`CompletionResponse.model`.
    provider : str
        Provider tag (``"anthropic"``, ``"openai"``, …) from
        :attr:`CompletionResponse.provider`. Combined with :attr:`model`
        on persistence as ``f"{provider}:{model}"`` so downstream readers
        can distinguish "claude-opus-4-7 via Anthropic SDK" from "via
        the Claude CLI".
    input_tokens : int or None
        Prompt-token count when the provider reports it.
    output_tokens : int or None
        Completion-token count when the provider reports it.
    truncated : bool
        ``True`` when the input diff exceeded ``max_diff_chars`` and was
        clipped before prompting. Stored so callers can flag descriptions
        that may be lossy.
    """

    text: str
    model: str
    provider: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    truncated: bool = False
