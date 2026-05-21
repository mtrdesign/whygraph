"""Failure type for the codegraph service."""

from __future__ import annotations


class CodeGraphError(RuntimeError):
    """Raised when the CodeGraph database cannot be opened or queried.

    Covers a missing ``.codegraph/codegraph.db`` file and any
    :class:`sqlite3.Error` raised while reading it. When wrapping a
    lower-level failure the original exception is preserved as ``__cause__``.
    """
