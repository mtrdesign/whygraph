"""Logging utilities for the WhyGraph package.

Provides a single configurable root logger (``whygraph``) backed by
``rich.logging.RichHandler`` so that all child loggers inherit colored,
timestamped terminal output.

Internal modules should obtain loggers via ``logging.getLogger(__name__)``
to get the natural module-path hierarchy (``whygraph.core.shell``,
``whygraph.scan.git``, etc.). The :func:`get_logger` helper exists for
external callers who want a logger under the WhyGraph root without
knowing the package layout.
"""

from __future__ import annotations

import logging
from enum import IntEnum

from rich.console import Console
from rich.logging import RichHandler


class LogLevel(IntEnum):
    """Integer log levels mirroring the standard ``logging`` module.

    Using an ``IntEnum`` means values can be passed directly to
    ``logger.setLevel`` and compared with stdlib constants without
    conversion.

    Attributes
    ----------
    DEBUG : int
        Detailed diagnostic output, including every shell invocation.
    INFO : int
        Routine progress messages.
    WARNING : int
        Recoverable surprises (e.g. unknown TOML keys).
    ERROR : int
        Failures that abort the current operation.
    CRITICAL : int
        Unrecoverable failures.
    """

    DEBUG = logging.DEBUG
    INFO = logging.INFO
    WARNING = logging.WARNING
    ERROR = logging.ERROR
    CRITICAL = logging.CRITICAL


_ROOT_NAME = "whygraph"
_configured = False


def _coerce_level(level: LogLevel | str | int) -> int:
    """Normalize a level value to the integer expected by ``logging``.

    Parameters
    ----------
    level : LogLevel | str | int
        Either a :class:`LogLevel` member, a case-insensitive level name
        (``"info"``, ``"DEBUG"``), or the raw integer.

    Returns
    -------
    int
        The integer level accepted by ``logger.setLevel``.

    Raises
    ------
    ValueError
        If ``level`` is a string that doesn't match a :class:`LogLevel`
        member name.
    """
    if isinstance(level, LogLevel):
        return level.value

    if isinstance(level, int):
        return level

    try:
        return LogLevel[level.upper()].value
    except KeyError as exc:
        raise ValueError(f"unknown log level: {level!r}") from exc


def configure_logging(level: LogLevel | str | int = LogLevel.INFO) -> logging.Logger:
    """Set up the ``whygraph`` root logger with a Rich handler.

    Idempotent — the handler is attached on the first call and skipped
    on subsequent ones, but the level is always re-applied so callers can
    change verbosity without re-creating the handler. ``propagate`` is
    disabled to prevent double-output when the consuming application has
    handlers attached to the Python root logger.

    Logs are routed to **stderr** so the package is safe to use inside an
    MCP stdio server (where stdout is reserved for the JSON-RPC
    protocol). In the CLI this also keeps diagnostic output cleanly
    separated from user-facing Click output.

    Parameters
    ----------
    level : LogLevel | str | int, optional
        Initial verbosity (default :attr:`LogLevel.INFO`).

    Returns
    -------
    logging.Logger
        The configured ``whygraph`` logger.

    Examples
    --------
    >>> configure_logging("DEBUG")  # doctest: +SKIP
    """
    global _configured
    root = logging.getLogger(_ROOT_NAME)
    if not _configured:
        handler = RichHandler(
            console=Console(stderr=True),
            rich_tracebacks=True,
            show_path=False,
            markup=False,
        )
        root.addHandler(handler)
        root.propagate = False
        _configured = True
    root.setLevel(_coerce_level(level))
    return root


def get_logger(name: str | None = None) -> logging.Logger:
    """Return the WhyGraph root logger or a named child.

    Prefer ``logging.getLogger(__name__)`` inside the package — this helper
    exists for external callers who want a logger under the WhyGraph
    hierarchy without knowing the package layout.

    Parameters
    ----------
    name : str, optional
        Suffix appended after the ``whygraph.`` root. If ``None``, the
        root logger itself is returned.

    Returns
    -------
    logging.Logger
        ``whygraph`` if ``name`` is ``None``, otherwise ``whygraph.<name>``.
    """
    return logging.getLogger(_ROOT_NAME if name is None else f"{_ROOT_NAME}.{name}")
