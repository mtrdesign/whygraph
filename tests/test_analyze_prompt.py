"""Tests for :mod:`whygraph.analyze.prompt` — the markdown prompt resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from whygraph.analyze import AnalyzeError, PLACEHOLDER, SYNTHESIS_PLACEHOLDER
from whygraph.analyze.prompt import render, resolve


# ---- render --------------------------------------------------------------


def test_render_substitutes_placeholder() -> None:
    assert render(f"before {PLACEHOLDER} after", "DIFF") == "before DIFF after"


def test_render_leaves_other_braces_literal() -> None:
    """Proves render() uses str.replace, not str.format: a bare ``{x}`` in
    the template must survive untouched."""
    rendered = render(f"see {{x}} and {PLACEHOLDER}", "D")
    assert rendered == "see {x} and D"


def test_render_template_without_placeholder_unchanged() -> None:
    assert render("no placeholder here", "D") == "no placeholder here"


def test_render_multiple_placeholders_all_replaced() -> None:
    assert render(f"{PLACEHOLDER}/{PLACEHOLDER}", "x") == "x/x"


# ---- resolve: layering ---------------------------------------------------


def _write(directory: Path, name: str, body: str) -> None:
    (directory / name).write_text(body, encoding="utf-8")


def test_resolve_falls_back_to_default(tmp_path: Path) -> None:
    _write(tmp_path, "default.md", "DEFAULT BODY")

    assert resolve("openai", "gpt-4o", prompts_dir=tmp_path) == "DEFAULT BODY"


def test_resolve_prefers_provider_over_default(tmp_path: Path) -> None:
    _write(tmp_path, "default.md", "DEFAULT")
    _write(tmp_path, "anthropic.md", "ANTHROPIC")

    assert resolve("anthropic", "any-model", prompts_dir=tmp_path) == "ANTHROPIC"
    # A provider with no file still falls through to default.
    assert resolve("openai", "any-model", prompts_dir=tmp_path) == "DEFAULT"


def test_resolve_prefers_model_over_provider(tmp_path: Path) -> None:
    _write(tmp_path, "default.md", "DEFAULT")
    _write(tmp_path, "anthropic.md", "ANTHROPIC")
    _write(tmp_path, "claude-opus-4-7.md", "MODEL")

    assert (
        resolve("anthropic", "claude-opus-4-7", prompts_dir=tmp_path) == "MODEL"
    )
    # A sibling model with no file drops to the provider file.
    assert (
        resolve("anthropic", "claude-haiku-4-5", prompts_dir=tmp_path)
        == "ANTHROPIC"
    )


def test_resolve_raises_when_default_missing(tmp_path: Path) -> None:
    """An empty prompts dir is a packaging bug — surface it clearly."""
    with pytest.raises(AnalyzeError, match="default.md"):
        resolve("anthropic", "claude-opus-4-7", prompts_dir=tmp_path)


# ---- resolve: filename safety -------------------------------------------


def test_resolve_skips_unsafe_keys_without_traversal(tmp_path: Path) -> None:
    """A model id with path separators must not escape the prompts dir;
    resolution simply falls through to the next safe candidate."""
    _write(tmp_path, "default.md", "DEFAULT")
    _write(tmp_path, "anthropic.md", "ANTHROPIC")
    # Plant a file one level up that a traversal could otherwise reach.
    (tmp_path.parent / "secrets.md").write_text("SECRET", encoding="utf-8")

    assert (
        resolve("anthropic", "../secrets", prompts_dir=tmp_path) == "ANTHROPIC"
    )


def test_resolve_unsafe_provider_falls_through_to_default(tmp_path: Path) -> None:
    _write(tmp_path, "default.md", "DEFAULT")

    assert resolve("../../etc/passwd", "x/y", prompts_dir=tmp_path) == "DEFAULT"


# ---- resolve: the real packaged prompts ---------------------------------


def test_resolve_packaged_default_is_shipped() -> None:
    """With no prompts_dir override, resolve() reads the packaged
    default.md — the resolution backstop must always be present."""
    template = resolve("no-such-provider", "no-such-model")
    assert PLACEHOLDER in template
    assert template.rstrip().endswith("Output only the description.")


# ---- render: synthesis placeholder --------------------------------------


def test_render_substitutes_synthesis_placeholder() -> None:
    rendered = render(
        f"before {SYNTHESIS_PLACEHOLDER} after",
        "DESCRIPTIONS",
        placeholder=SYNTHESIS_PLACEHOLDER,
    )
    assert rendered == "before DESCRIPTIONS after"


def test_render_default_placeholder_leaves_synthesis_token_untouched() -> None:
    """The default placeholder is the describe token; a synthesis token
    in the template survives unless it is explicitly requested."""
    rendered = render(f"{SYNTHESIS_PLACEHOLDER} and {PLACEHOLDER}", "D")
    assert rendered == f"{SYNTHESIS_PLACEHOLDER} and D"


# ---- resolve: the synthesis kind ----------------------------------------


def test_resolve_synthesis_falls_back_to_default(tmp_path: Path) -> None:
    _write(tmp_path, "synthesis.md", "SYNTH DEFAULT")

    assert (
        resolve("openai", "gpt-4o", kind="synthesis", prompts_dir=tmp_path)
        == "SYNTH DEFAULT"
    )


def test_resolve_synthesis_prefers_model_then_provider(tmp_path: Path) -> None:
    _write(tmp_path, "synthesis.md", "SYNTH")
    _write(tmp_path, "anthropic.synthesis.md", "SYNTH ANTHROPIC")
    _write(tmp_path, "claude-opus-4-7.synthesis.md", "SYNTH MODEL")

    assert (
        resolve(
            "anthropic", "claude-opus-4-7", kind="synthesis", prompts_dir=tmp_path
        )
        == "SYNTH MODEL"
    )
    # A sibling model with no file drops to the provider synthesis file.
    assert (
        resolve("anthropic", "other-model", kind="synthesis", prompts_dir=tmp_path)
        == "SYNTH ANTHROPIC"
    )
    # A provider with no file drops to the synthesis default.
    assert (
        resolve("openai", "gpt-4o", kind="synthesis", prompts_dir=tmp_path)
        == "SYNTH"
    )


def test_resolve_describe_and_synthesis_ladders_are_independent(
    tmp_path: Path,
) -> None:
    """A dir holding only describe prompts resolves describe but not
    synthesis — the two kinds never borrow each other's files."""
    _write(tmp_path, "default.md", "DESCRIBE")

    assert resolve("openai", "gpt-4o", prompts_dir=tmp_path) == "DESCRIBE"
    with pytest.raises(AnalyzeError, match="synthesis.md"):
        resolve("openai", "gpt-4o", kind="synthesis", prompts_dir=tmp_path)


def test_resolve_synthesis_raises_when_default_missing(tmp_path: Path) -> None:
    with pytest.raises(AnalyzeError, match="synthesis"):
        resolve(
            "anthropic", "claude-opus-4-7", kind="synthesis", prompts_dir=tmp_path
        )


def test_resolve_packaged_synthesis_is_shipped() -> None:
    """The synthesis backstop must ship alongside default.md."""
    template = resolve("no-such-provider", "no-such-model", kind="synthesis")
    assert SYNTHESIS_PLACEHOLDER in template
