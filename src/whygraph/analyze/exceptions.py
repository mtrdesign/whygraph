"""Exception types for the :mod:`whygraph.analyze` package."""

from __future__ import annotations


class AnalyzeError(RuntimeError):
    """Base exception for the :mod:`whygraph.analyze` package.

    Raised when an analyze operation cannot complete — e.g. the diff
    descriptor cannot produce a description. Wraps lower-level
    :class:`~whygraph.services.git.GitError` and
    :class:`~whygraph.services.llm.LlmError` so callers can handle a single
    domain exception. The originating exception is preserved as
    ``__cause__``.
    """


class RationaleError(AnalyzeError):
    """Raised when the model's rationale output cannot be used.

    Signals that an LLM completion came back but its text could not be
    parsed as the expected rationale JSON, or failed schema validation
    (missing keys, wrong field types). Subclasses :class:`AnalyzeError`, so
    callers handling the package-wide exception still catch it.
    """
