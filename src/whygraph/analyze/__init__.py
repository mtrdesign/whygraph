"""LLM-driven analysis of scanned commits.

Public API
----------
* :class:`LlmDescriptor` — the raw service: takes a git diff string,
  returns a model-written :class:`Description`. Stateless aside from
  its bound :class:`~whygraph.services.llm.LlmClient` and per-call
  knobs.
* :class:`Description` — the value object returned by
  :meth:`LlmDescriptor.describe`.
* :data:`DEFAULT_PROMPT_TEMPLATE`, :func:`render_prompt` — the prompt
  template and its renderer, exposed for callers (and tests) that need
  to inspect or override the wording.
* :class:`AnalyzeError` — single domain exception. Wraps underlying
  ``LlmError`` / ``GitError`` so consumers handle one type.

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
from .exceptions import AnalyzeError
from .llm_descriptor import LlmDescriptor
from .prompt import DEFAULT_PROMPT_TEMPLATE, render as render_prompt

__all__ = [
    "DEFAULT_PROMPT_TEMPLATE",
    "AnalyzeError",
    "Description",
    "LlmDescriptor",
    "render_prompt",
]
