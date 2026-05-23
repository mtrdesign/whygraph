"""Markdown-backed system + task prompts for the analyze package.

Prompts live as ``.md`` files under ``analyze/prompts/`` rather than as
Python string constants. This lets the prompt vary per model without
code changes, and keeps prose where prose belongs.

Layout — ``prompts/<component>/<key>/<file>``:

* ``component`` groups prompts by the analyze module that uses them
  (e.g. ``llm_descriptor``), so a future module drops its prompts into
  its own subtree without colliding.
* ``key`` is the resolution rung — a model id, a provider tag, or
  ``default``.
* Each LLM call needs two files: a ``system`` message (standing
  instructions) and a ``task`` message (the payload). A component may
  prompt for one or more *operations*, and the operation is a filename
  prefix mapped by :data:`_OPERATION_INFIX`: ``describe`` and
  ``rationale`` use ``system.md`` / ``task.md``, ``synthesis`` uses
  ``synthesis.system.md`` / ``synthesis.task.md``.

Resolution is layered and *per file*: for a given ``(provider, model)``
each file is looked up on its own, the first that exists winning, in the
order ``<model>/`` → ``<provider>/`` → ``default/``. An override folder
may therefore carry just one file (e.g. a model-specific ``task.md``)
and inherit the rest from ``default/``. Only the ``default/`` files
ship; per-provider and per-model folders are dropped in as needed.

Interpolation is a literal :meth:`str.replace` of a placeholder token —
*not* :meth:`str.format` — so braces in the markdown (code fences, JSON
examples) survive untouched. Only the ``task`` file carries a
placeholder; the ``system`` file is static.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal

from .exceptions import AnalyzeError

PLACEHOLDER = "{{DIFF}}"
"""Placeholder token for the diff body in a ``describe`` task prompt.
Chosen so it never collides with literal braces in prompt prose."""

SYNTHESIS_PLACEHOLDER = "{{DESCRIPTIONS}}"
"""Placeholder token for the joined chunk descriptions in a ``synthesis``
task prompt."""

RATIONALE_PLACEHOLDER = "{{EVIDENCE}}"
"""Placeholder token for the evidence bundle in a ``rationale`` task prompt."""

PromptOperation = Literal["describe", "synthesis", "rationale"]
"""The operations an analyze module prompts for — see the module docstring."""

PromptRole = Literal["system", "task"]
"""The two files a resolved prompt is made of."""

_OPERATION_INFIX: dict[PromptOperation, str] = {
    "describe": "",
    "synthesis": "synthesis.",
    "rationale": "",
}

_DEFAULT_KEY = "default"

# A prompt key (provider tag or model id) is only used to build a path
# component when it is plain — no separators, no `..`.
_SAFE_KEY = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True, slots=True)
class Prompt:
    """A resolved prompt — the ``system`` and ``task`` halves of one call.

    Attributes
    ----------
    system : str
        Standing instructions, sent as the ``"system"`` message. Static —
        carries no placeholder.
    task : str
        The payload framing, sent as the ``"user"`` message. Carries the
        operation's placeholder (:data:`PLACEHOLDER` or
        :data:`SYNTHESIS_PLACEHOLDER`); pass it through :func:`render`
        before use.
    """

    system: str
    task: str


def _packaged_prompts_dir() -> Traversable:
    """Return the packaged ``analyze/prompts/`` directory as a resource."""
    return resources.files("whygraph.analyze") / "prompts"


def _filename(operation: PromptOperation, role: PromptRole) -> str:
    """Build the ``.md`` filename for an ``(operation, role)`` pair."""
    return f"{_OPERATION_INFIX[operation]}{role}.md"


def _resolve_file(
    component: str,
    operation: PromptOperation,
    role: PromptRole,
    provider: str,
    model: str,
    *,
    directory: Traversable | Path,
) -> str:
    """Resolve one prompt file down the ``model -> provider -> default`` ladder.

    Returns the text of the first ``<component>/<key>/<filename>`` that
    exists. Keys that are not safe path components are skipped silently.

    Raises
    ------
    AnalyzeError
        If not even the ``default`` file is found — a packaging bug for
        the shipped files, otherwise a missing override.
    """
    filename = _filename(operation, role)
    for key in (model, provider, _DEFAULT_KEY):
        if not _SAFE_KEY.match(key):
            continue
        candidate = directory / component / key / filename
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise AnalyzeError(
        f"no {operation} {role} prompt found for component={component!r} "
        f"provider={provider!r} model={model!r}; "
        f"{component}/{_DEFAULT_KEY}/{filename} is missing from {directory}"
    )


def resolve(
    component: str,
    operation: PromptOperation,
    provider: str,
    model: str,
    *,
    prompts_dir: Traversable | Path | None = None,
) -> Prompt:
    """Resolve the ``system`` + ``task`` prompt for one operation.

    The two files are resolved independently — see the module docstring
    — so the returned :class:`Prompt` may mix rungs (e.g. a model-level
    ``task`` with the ``default`` ``system``).

    Parameters
    ----------
    component : str
        Analyze module the prompt belongs to (e.g. ``"llm_descriptor"``).
    operation : {"describe", "synthesis"}
        Which operation to resolve the prompt for.
    provider : str
        Provider tag of the resolved :class:`~whygraph.services.llm.LlmClient`
        (e.g. ``"anthropic"``).
    model : str
        Model identifier bound to the client (e.g. ``"claude-opus-4-7"``).
    prompts_dir : Traversable or Path, optional
        Directory to resolve against. ``None`` (default) uses the
        packaged ``analyze/prompts/`` directory. Tests inject a tmp
        directory here.

    Returns
    -------
    Prompt
        The ``system`` and ``task`` text, ready for :func:`render`.

    Raises
    ------
    AnalyzeError
        If either file cannot be resolved, even at the ``default`` rung.
    """
    directory = prompts_dir if prompts_dir is not None else _packaged_prompts_dir()
    return Prompt(
        system=_resolve_file(
            component, operation, "system", provider, model, directory=directory
        ),
        task=_resolve_file(
            component, operation, "task", provider, model, directory=directory
        ),
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
        The ``task`` half of a resolved :class:`Prompt`. The ``system``
        half is static and never rendered.
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
        The rendered task text, ready as the ``user`` message of a
        :class:`whygraph.services.llm.CompletionRequest`.
    """
    return template.replace(placeholder, value)
