"""Failure types for the codegraph service."""

from __future__ import annotations

from collections.abc import Sequence

SHOWN_PATHS = 5
"""How many offending paths a :class:`CodeGraphUnsafeSymlinkError` message names."""


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


class CodeGraphUnsafeSymlinkError(CodeGraphBootstrapError):
    """Raised instead of indexing a repository with a link that leaves it.

    CodeGraph follows symbolic links out of the repository on purpose, so
    a link to a file elsewhere on the machine would put that file's
    symbols (and, through them, its text) into the index. WhyGraph refuses
    to run CodeGraph at all while such a link exists; it never excludes the
    link and indexes the rest.

    Parameters
    ----------
    paths : Sequence[str]
        The offending links, relative to the repository root.

    Attributes
    ----------
    paths : tuple[str, ...]
        As given. The message names the first :data:`SHOWN_PATHS`.
    """

    def __init__(self, paths: Sequence[str]) -> None:
        self.paths = tuple(paths)
        shown = ", ".join(self.paths[:SHOWN_PATHS])
        more = len(self.paths) - SHOWN_PATHS
        if more > 0:
            shown = f"{shown} and {more} more"
        noun = "link leads" if len(self.paths) == 1 else "links lead"
        super().__init__(
            f"refusing to index: {len(self.paths)} symbolic {noun} out of the"
            f" repository ({shown}). WhyGraph never indexes a file through a link"
            " that leaves the repository; replace each with a regular file or"
            " remove it, then scan again"
        )
