"""Exception types for the :mod:`whygraph.analyze` package."""

from __future__ import annotations


class AnalyzeError(RuntimeError):
    """Raised when the analyzer cannot produce a description.

    Wraps lower-level :class:`~whygraph.services.git.GitError` and
    :class:`~whygraph.services.llm.LlmError` so callers can handle a
    single domain exception. The originating exception is preserved as
    ``__cause__``.
    """
