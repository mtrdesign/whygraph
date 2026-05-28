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
            raise
        except Exception:
            _log.exception("%s failed", func.__name__)
            raise

    return wrapper  # type: ignore[return-value]
