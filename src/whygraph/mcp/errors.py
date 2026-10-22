"""Error type for WhyGraph's MCP feature modules.

Holds :class:`WhyGraphError`, the single exception every MCP tool raises
when a request cannot be served. Kept in its own module so feature code
can import it without dragging in the rest of the MCP machinery, and so
new error types — if a second case ever appears — land here rather than
in whichever feature module happens to need them first.
"""

from __future__ import annotations

import functools
import logging
from typing import Callable, TypeVar

from mcp.server.fastmcp.exceptions import ToolError

_log = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., object])


class WhyGraphError(ToolError):
    """Raised by an MCP tool when a request cannot be served.

    Surfaces to the agent as the tool's error message — phrased for a
    reader who will act on it (e.g. "scan from the WhyGraph portal (or run
    ``whygraph scan`` outside it) first").

    Parameters
    ----------
    message : str, optional
        The message the agent (or the Explorer's "Show details") reads.
    code : str, optional
        A machine-readable code (``no_evidence``, ``blame_failed``, ...)
        that the portal's HTTP translation sends beside the message, so
        the playground can word the failure itself. The MCP surface
        ignores it.

    Attributes
    ----------
    code : str or None
        As passed.
    """

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code

    @classmethod
    def wrap(
        cls, message: str, cause: BaseException, *, code: str | None = None
    ) -> "WhyGraphError":
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
        code:
            The machine-readable code, as for the constructor.

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
        err = cls(f"{message}: {cause}", code=code)
        err.__cause__ = cause
        return err


def tool_error_text(exc: WhyGraphError) -> str:
    """Return the text an agent reads for a failed WhyGraph tool call.

    The shape is ``[WhyGraph] {message} (code: {code}[, scope: {scope}])``.
    The parenthetical is present only when the error carries a ``code``; the
    ``scope`` part only when it carries a scope too.

    Parameters
    ----------
    exc : WhyGraphError
        The error the tool raised.

    Returns
    -------
    str
        The message, prefixed and suffixed as above.

    Examples
    --------
    >>> tool_error_text(WhyGraphError("limit must be >= 1"))
    '[WhyGraph] limit must be >= 1'
    """
    text = f"[WhyGraph] {exc}"
    code = getattr(exc, "code", None)
    if code is None:
        return text
    scope = getattr(exc, "scope", None)
    detail = f"code: {code}" + (f", scope: {scope}" if scope else "")
    return f"{text} ({detail})"


def log_tool_errors(func: _F) -> _F:
    """Wrap an MCP tool so every failure is logged before it propagates.

    FastMCP catches a tool's exception and returns it to the client as an
    ``isError`` result, but logs nothing — so the WhyGraph file log shows
    the tool's entry line and then silence, making a rejected request look
    like a hang. This wrapper closes that gap: expected
    :class:`WhyGraphError` rejections (bad arguments, unscanned DB) log at
    WARNING with just their message; anything else logs a full traceback at
    ERROR. The exception is always re-raised unchanged so FastMCP still
    builds the ``isError`` response.

    A :class:`WhyGraphError` is re-raised as a plain ``ToolError`` whose text
    is :func:`tool_error_text`, so the agent always reads the same shape. Any
    other exception is re-raised unchanged.

    ``functools.wraps`` sets ``__wrapped__``, so ``inspect.signature`` still
    sees the original parameters — FastMCP's argument validation and the
    generated tool schema are unaffected by the wrapping.

    Parameters
    ----------
    func : callable
        The tool function to wrap.

    Returns
    -------
    callable
        ``func`` wrapped with log-and-reraise behavior.
    """

    @functools.wraps(func)
    def wrapper(*args: object, **kwargs: object) -> object:
        try:
            return func(*args, **kwargs)
        except WhyGraphError as exc:
            _log.warning("%s rejected request: %s", func.__name__, exc)
            raise ToolError(tool_error_text(exc)) from exc
        except Exception:
            _log.exception("%s failed", func.__name__)
            raise

    return wrapper  # type: ignore[return-value]
