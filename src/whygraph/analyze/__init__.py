"""LLM-driven analysis of scanned commits — diff descriptions and rationale.

Public API
----------
* :class:`LlmDescriptor` — the raw service: takes a git diff string,
  returns a model-written :class:`Description`. Stateless aside from
  its bound :class:`~whygraph.services.llm.LlmClient` and per-call
  knobs.
* :class:`Description` — the value object returned by
  :meth:`LlmDescriptor.describe`.
* :class:`RationaleGenerator` — the raw service: takes a sequence of
  :class:`CommitEvidence`, returns a model-written :class:`Rationale`
  card explaining *why* the code exists. Like :class:`LlmDescriptor`,
  it owns only the prompt + LLM round-trip.
* :class:`CommitEvidence` — the input value object: one scanned commit
  with the pull requests and issues linked to it.
* :class:`Rationale` — the value object returned by
  :meth:`RationaleGenerator.generate`.
* :class:`Prompt`, :func:`resolve_prompt`, :func:`render_prompt`,
  :data:`PLACEHOLDER`, :data:`SYNTHESIS_PLACEHOLDER`,
  :data:`RATIONALE_PLACEHOLDER` — the resolved ``system`` + ``task``
  prompt pair, the markdown resolver, its renderer, and the placeholder
  tokens for the ``describe``, ``synthesis`` and ``rationale``
  operations. Prompts live as ``.md`` files under
  ``analyze/prompts/<component>/`` and resolve per ``(provider, model)``;
  exposed for callers (and tests) that need to inspect or override the
  wording.
* :class:`AnalyzeError` — the package's base domain exception. Wraps
  underlying ``LlmError`` / ``GitError`` so consumers handle one type.
* :class:`RationaleError` — subclass of :class:`AnalyzeError`, raised
  when the model's rationale output cannot be parsed or validated.

Examples
--------
End-to-end pairing with the git service::

    from pathlib import Path
    from whygraph.core import get_config
    from whygraph.services.git import Repository
    from whygraph.analyze import LlmDescriptor

    repo = Repository(Path.cwd())
    commit = next(iter(repo.commits))
    descriptor = LlmDescriptor.from_config(get_config().analyze)
    print(descriptor.describe(repo.diff(commit)).text)

Persistence (writing to ``commit.llm_description``) and concurrency
(``ThreadPoolExecutor`` over many commits) are intentionally out of
scope for this module — both live in the orchestrator that consumes
:class:`LlmDescriptor`.
"""

from .description import Description
from .exceptions import AnalyzeError, RationaleError
from .llm_descriptor import LlmDescriptor
from .prompt import (
    PLACEHOLDER,
    RATIONALE_PLACEHOLDER,
    SYNTHESIS_PLACEHOLDER,
    Prompt,
    render as render_prompt,
    resolve as resolve_prompt,
)
from .rationale import CommitEvidence, Rationale
from .rationale_generator import RationaleGenerator

__all__ = [
    "PLACEHOLDER",
    "RATIONALE_PLACEHOLDER",
    "SYNTHESIS_PLACEHOLDER",
    "AnalyzeError",
    "CommitEvidence",
    "Description",
    "LlmDescriptor",
    "Prompt",
    "Rationale",
    "RationaleError",
    "RationaleGenerator",
    "render_prompt",
    "resolve_prompt",
]
