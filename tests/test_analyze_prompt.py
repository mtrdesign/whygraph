"""Tests for :mod:`whygraph.analyze.prompt` — the markdown prompt resolver.

``resolve`` reads a ``system`` + ``task`` pair from
``prompts/<component>/<key>/`` and returns a :class:`Prompt`; the two
files resolve independently down a ``<model> -> <provider> -> default``
ladder. ``render`` interpolates the diff (or chunk descriptions) into the
task half.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whygraph.analyze import (
    AnalyzeError,
    PLACEHOLDER,
    Prompt,
    SYNTHESIS_PLACEHOLDER,
)
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


# ---- resolve: helper -----------------------------------------------------


def _write(
    prompts_dir: Path, component: str, key: str, filename: str, body: str
) -> None:
    """Create ``<prompts_dir>/<component>/<key>/<filename>`` with ``body``."""
    path = prompts_dir / component / key / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ---- resolve: the system + task pair -------------------------------------


def test_resolve_reads_system_and_task(tmp_path: Path) -> None:
    _write(tmp_path, "comp", "default", "system.md", "SYS")
    _write(tmp_path, "comp", "default", "task.md", "TASK {{DIFF}}")

    prompt = resolve("comp", "describe", "openai", "gpt-4o", prompts_dir=tmp_path)

    assert isinstance(prompt, Prompt)
    assert prompt.system == "SYS"
    assert prompt.task == "TASK {{DIFF}}"


# ---- resolve: the model -> provider -> default ladder --------------------


def test_resolve_prefers_model_then_provider_then_default(tmp_path: Path) -> None:
    for key in ("default", "anthropic", "claude-opus-4-7"):
        _write(tmp_path, "comp", key, "system.md", f"SYS:{key}")
        _write(tmp_path, "comp", key, "task.md", f"TASK:{key}")

    # The model folder wins outright.
    model = resolve(
        "comp", "describe", "anthropic", "claude-opus-4-7", prompts_dir=tmp_path
    )
    assert model.system == "SYS:claude-opus-4-7"
    assert model.task == "TASK:claude-opus-4-7"

    # A sibling model with no folder drops to the provider folder.
    provider = resolve(
        "comp", "describe", "anthropic", "claude-haiku-4-5", prompts_dir=tmp_path
    )
    assert provider.system == "SYS:anthropic"

    # An unknown provider drops to default.
    default = resolve("comp", "describe", "openai", "gpt-4o", prompts_dir=tmp_path)
    assert default.system == "SYS:default"


def test_resolve_mixes_rungs_per_file(tmp_path: Path) -> None:
    """system.md and task.md resolve independently: a model folder can
    override just the task and inherit the default system prompt."""
    _write(tmp_path, "comp", "default", "system.md", "SYS:default")
    _write(tmp_path, "comp", "default", "task.md", "TASK:default")
    # The model folder carries only task.md.
    _write(tmp_path, "comp", "claude-opus-4-7", "task.md", "TASK:model")

    prompt = resolve(
        "comp", "describe", "anthropic", "claude-opus-4-7", prompts_dir=tmp_path
    )

    assert prompt.task == "TASK:model"  # from the model folder
    assert prompt.system == "SYS:default"  # inherited from default/


# ---- resolve: the synthesis operation ------------------------------------


def test_resolve_synthesis_uses_prefixed_filenames(tmp_path: Path) -> None:
    _write(tmp_path, "comp", "default", "synthesis.system.md", "SYN-SYS")
    _write(
        tmp_path, "comp", "default", "synthesis.task.md", "SYN {{DESCRIPTIONS}}"
    )

    prompt = resolve("comp", "synthesis", "openai", "gpt-4o", prompts_dir=tmp_path)

    assert prompt.system == "SYN-SYS"
    assert prompt.task == "SYN {{DESCRIPTIONS}}"


def test_resolve_describe_and_synthesis_are_independent(tmp_path: Path) -> None:
    """A folder with only the describe files resolves describe but not
    synthesis — the prefixed filenames keep the operations apart."""
    _write(tmp_path, "comp", "default", "system.md", "SYS")
    _write(tmp_path, "comp", "default", "task.md", "TASK")

    assert resolve("comp", "describe", "openai", "gpt-4o", prompts_dir=tmp_path)
    with pytest.raises(AnalyzeError, match="synthesis"):
        resolve("comp", "synthesis", "openai", "gpt-4o", prompts_dir=tmp_path)


# ---- resolve: missing files ----------------------------------------------


def test_resolve_raises_when_system_file_missing(tmp_path: Path) -> None:
    _write(tmp_path, "comp", "default", "task.md", "TASK")  # no system.md

    with pytest.raises(AnalyzeError, match="system"):
        resolve("comp", "describe", "openai", "gpt-4o", prompts_dir=tmp_path)


def test_resolve_raises_when_task_file_missing(tmp_path: Path) -> None:
    _write(tmp_path, "comp", "default", "system.md", "SYS")  # no task.md

    with pytest.raises(AnalyzeError, match="task"):
        resolve("comp", "describe", "openai", "gpt-4o", prompts_dir=tmp_path)


def test_resolve_isolates_components(tmp_path: Path) -> None:
    """Each component resolves only its own subtree."""
    _write(tmp_path, "comp_a", "default", "system.md", "A-SYS")
    _write(tmp_path, "comp_a", "default", "task.md", "A-TASK")

    assert (
        resolve("comp_a", "describe", "p", "m", prompts_dir=tmp_path).system
        == "A-SYS"
    )
    with pytest.raises(AnalyzeError):
        resolve("comp_b", "describe", "p", "m", prompts_dir=tmp_path)


# ---- resolve: filename safety --------------------------------------------


def test_resolve_skips_unsafe_keys_without_traversal(tmp_path: Path) -> None:
    """A model id with path separators must not escape the component
    folder; resolution falls through to the next safe key."""
    _write(tmp_path, "comp", "default", "system.md", "DEFAULT")
    _write(tmp_path, "comp", "default", "task.md", "DEFAULT")
    # Plant files exactly where a `comp/../secrets/` traversal would land.
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "system.md").write_text("SECRET", encoding="utf-8")
    (secrets / "task.md").write_text("SECRET", encoding="utf-8")

    prompt = resolve(
        "comp", "describe", "anthropic", "../secrets", prompts_dir=tmp_path
    )

    assert prompt.system == "DEFAULT"


def test_resolve_unsafe_provider_falls_through_to_default(tmp_path: Path) -> None:
    _write(tmp_path, "comp", "default", "system.md", "DEFAULT")
    _write(tmp_path, "comp", "default", "task.md", "DEFAULT")

    prompt = resolve("comp", "describe", "../../etc", "x/y", prompts_dir=tmp_path)
    assert prompt.system == "DEFAULT"


# ---- resolve: the real packaged prompts ----------------------------------


def test_resolve_packaged_describe_prompt_is_shipped() -> None:
    """With no prompts_dir override, resolve() reads the packaged
    llm_descriptor describe prompt — the resolution backstop."""
    prompt = resolve("llm_descriptor", "describe", "no-such", "no-such")
    assert PLACEHOLDER in prompt.task
    assert prompt.system.strip()


def test_resolve_packaged_synthesis_prompt_is_shipped() -> None:
    prompt = resolve("llm_descriptor", "synthesis", "no-such", "no-such")
    assert SYNTHESIS_PLACEHOLDER in prompt.task
    assert prompt.system.strip()
