"""Failure types for the codegraph service."""

from __future__ import annotations


class CodeGraphError(RuntimeError):
    """Raised when the CodeGraph database cannot be opened or queried.

    Covers a missing ``.codegraph/codegraph.db`` file and any
    :class:`sqlite3.Error` raised while reading it. When wrapping a
    lower-level failure the original exception is preserved as ``__cause__``.
    """


class CodeGraphBootstrapError(CodeGraphError):
    """Raised when bootstrapping CodeGraph via the vendored Docker image fails.

    Subtypes :class:`CodeGraphError` so callers that already handle the
    parent class — for example the MCP layer's lazy-open path — don't need
    to grow new ``except`` clauses. The distinct subclass exists so the CLI
    can present bootstrap-specific guidance (e.g. ``pass --no-codegraph``).
    """
