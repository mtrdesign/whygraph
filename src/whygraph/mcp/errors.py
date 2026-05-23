"""Error type for WhyGraph's MCP feature modules.

Holds :class:`WhyGraphError`, the single exception every MCP tool raises
when a request cannot be served. Kept in its own module so feature code
can import it without dragging in the rest of the MCP machinery, and so
new error types — if a second case ever appears — land here rather than
in whichever feature module happens to need them first.
"""

from __future__ import annotations


class WhyGraphError(RuntimeError):
    """Raised by an MCP tool when a request cannot be served.

    Surfaces to the agent as the tool's error message — phrased for a
    reader who will act on it (e.g. "run ``whygraph scan`` first").
    """

    @classmethod
    def wrap(cls, message: str, cause: BaseException) -> "WhyGraphError":
        """Build a ``WhyGraphError`` that chains ``cause`` as ``__cause__``.

        Captures the recurring ``raise WhyGraphError(f"{prefix}: {exc}") from exc``
        idiom in one place. The resulting ``str(err)`` is ``"{message}: {cause}"`` —
        identical to the f-string form it replaces — and ``__cause__`` is
        set so tracebacks render the same as the manual ``raise … from exc``.

        Parameters
        ----------
        message:
            Human-readable prefix describing what failed (e.g.
            ``"git blame failed"``).
        cause:
            The underlying exception. Stringified into the message and
            attached as ``__cause__`` for traceback chaining.

        Returns
        -------
        WhyGraphError
            A new instance ready to ``raise``. Callers ``raise`` it
            directly (no ``from cause`` clause needed — ``__cause__`` is
            already set).

        Examples
        --------
        >>> raise WhyGraphError.wrap("git blame failed", exc)  # doctest: +SKIP
        """
        err = cls(f"{message}: {cause}")
        err.__cause__ = cause
        return err
