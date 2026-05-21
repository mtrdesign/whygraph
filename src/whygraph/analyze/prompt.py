"""Markdown-backed prompt templates for the LLM commit descriptor.

Prompts live as ``.md`` files under ``analyze/prompts/`` rather than as
Python string constants. This lets the prompt vary per model without
code changes, and keeps prose where prose belongs.

There are two prompt *kinds*: ``describe`` turns one diff (or diff
chunk) into a description, ``synthesis`` merges several chunk
descriptions into one. Each kind resolves independently.

Resolution is layered: for a given ``(provider, model)`` the first file
that exists wins. For ``describe`` the order is ``<model>.md`` →
``<provider>.md`` → ``default.md``; for ``synthesis`` it is
``<model>.synthesis.md`` → ``<provider>.synthesis.md`` →
``synthesis.md``. Only the two defaults ship; per-provider and
per-model files are dropped in by the user as needed.

Interpolation is a literal :meth:`str.replace` of a placeholder token —
*not* :meth:`str.format` — so braces in the markdown (code fences, JSON
examples) survive untouched.
"""

from __future__ import annotations

import re
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal

from .exceptions import AnalyzeError

PLACEHOLDER = "{{DIFF}}"
"""Placeholder token for the diff body in a ``describe`` prompt. Chosen
so it never collides with literal braces in prompt prose."""

SYNTHESIS_PLACEHOLDER = "{{DESCRIPTIONS}}"
"""Placeholder token for the joined chunk descriptions in a ``synthesis``
prompt."""

PromptKind = Literal["describe", "synthesis"]
"""The two prompt kinds — see the module docstring."""

_DEFAULT_NAME = "default.md"
_SYNTHESIS_NAME = "synthesis.md"

# A prompt key (provider tag or model id) is only used to build a
# filename when it is a plain path component — no separators, no `..`.
_SAFE_KEY = re.compile(r"^[A-Za-z0-9._-]+$")


def _packaged_prompts_dir() -> Traversable:
    """Return the packaged ``analyze/prompts/`` directory as a resource."""
    return resources.files("whygraph.analyze") / "prompts"


def _candidate_names(provider: str, model: str, kind: PromptKind) -> list[str]:
    """Ordered prompt filenames to try for ``(provider, model, kind)``.

    Most specific first. Keys that are not safe filename components are
    dropped silently — resolution falls through to the next candidate.
    The ``synthesis`` kind carries a ``.synthesis`` infix so its files
    sit alongside the ``describe`` ones without colliding.
    """
    if kind == "synthesis":
        infix, default_name = ".synthesis", _SYNTHESIS_NAME
    else:
        infix, default_name = "", _DEFAULT_NAME
    names: list[str] = []
    for key in (model, provider):
        if _SAFE_KEY.match(key):
            names.append(f"{key}{infix}.md")
    names.append(default_name)
    return names


def resolve(
    provider: str,
    model: str,
    *,
    kind: PromptKind = "describe",
    prompts_dir: Traversable | Path | None = None,
) -> str:
    """Return the prompt template for a ``(provider, model, kind)`` tuple.

    Resolution order — the first file that exists wins. For
    ``kind="describe"``::

        prompts/<model>.md  ->  prompts/<provider>.md  ->  prompts/default.md

    For ``kind="synthesis"``::

        <model>.synthesis.md -> <provider>.synthesis.md -> synthesis.md

    Parameters
    ----------
    provider : str
        Provider tag of the resolved :class:`~whygraph.services.llm.LlmClient`
        (e.g. ``"anthropic"``).
    model : str
        Model identifier bound to the client (e.g. ``"claude-opus-4-7"``).
    kind : {"describe", "synthesis"}, optional
        Which prompt to resolve. ``"describe"`` (default) turns a diff
        into a description; ``"synthesis"`` merges chunk descriptions.
    prompts_dir : Traversable or Path, optional
        Directory to resolve against. ``None`` (default) uses the
        packaged ``analyze/prompts/`` directory. Tests inject a tmp
        directory here.

    Returns
    -------
    str
        The raw template text, ready for :func:`render`.

    Raises
    ------
    AnalyzeError
        If not even the kind's default file is found — that means the
        package data is missing, a packaging bug rather than a user
        error.
    """
    directory = prompts_dir if prompts_dir is not None else _packaged_prompts_dir()
    names = _candidate_names(provider, model, kind)
    for name in names:
        candidate = directory / name
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise AnalyzeError(
        f"no {kind} prompt template found for provider={provider!r} "
        f"model={model!r}; {names[-1]} is missing from {directory}"
    )


def render(template: str, value: str, *, placeholder: str = PLACEHOLDER) -> str:
    """Substitute ``value`` into ``template`` at ``placeholder``.

    A literal :meth:`str.replace` of ``placeholder`` — no
    :meth:`str.format` — so any other braces in ``template`` are left
    exactly as written. A template without the placeholder is returned
    unchanged.

    Parameters
    ----------
    template : str
        A resolved prompt template (see :func:`resolve`).
    value : str
        Text to interpolate. For a ``describe`` prompt this is the raw
        diff (truncation is the caller's concern —
        :class:`whygraph.analyze.LlmDescriptor` clips before rendering);
        for a ``synthesis`` prompt it is the joined chunk descriptions.
    placeholder : str, optional
        The token to replace. Defaults to :data:`PLACEHOLDER` (the
        ``describe`` token); pass :data:`SYNTHESIS_PLACEHOLDER` for a
        ``synthesis`` prompt.

    Returns
    -------
    str
        The rendered prompt, ready as the body of a
        :class:`whygraph.services.llm.CompletionRequest`.
    """
    return template.replace(placeholder, value)
