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
from contextlib import contextmanager
from enum import IntEnum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.logging import RichHandler

if TYPE_CHECKING:
    # Forward ref only — avoids a runtime cycle between core.config and
    # core.logger (config imports LogLevel from this module).
    from whygraph.core.config import LoggingConfig


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
_FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_configured = False
_file_configured = False


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


def configure_logging(
    level: LogLevel | str | int = LogLevel.INFO,
    *,
    file_config: "LoggingConfig | None" = None,
) -> logging.Logger:
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

    When ``file_config`` is provided and ``file_config.file`` is set, a
    :class:`~logging.handlers.RotatingFileHandler` is attached **in
    addition to** the Rich handler — both receive every record. The file
    handler is also idempotent (a tracked flag prevents a second one
    being attached on repeat calls).

    Parameters
    ----------
    level : LogLevel | str | int, optional
        Root verbosity for the logger (default :attr:`LogLevel.INFO`).
        Individual handlers may filter further; the file handler takes
        its own level from ``file_config.level`` when set.
    file_config : LoggingConfig, optional
        Settings for the rotating file handler. ``None`` (default) or a
        config whose ``file`` is ``None`` skips file logging entirely.

    Returns
    -------
    logging.Logger
        The configured ``whygraph`` logger.

    Examples
    --------
    >>> configure_logging("DEBUG")  # doctest: +SKIP
    """
    global _configured, _file_configured
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

    if (
        not _file_configured
        and file_config is not None
        and file_config.file is not None
    ):
        path = file_config.file
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=path,
            maxBytes=file_config.max_bytes,
            backupCount=file_config.backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
        if file_config.level is not None:
            file_handler.setLevel(_coerce_level(file_config.level))
            # The root logger filters records before handlers see them,
            # so if the file is *more* verbose than the root, lower the
            # root to match. The Rich handler stays at its own level.
            root_level = root.level
            file_level = _coerce_level(file_config.level)
            if file_level < root_level:
                root.setLevel(file_level)
                for h in root.handlers:
                    if h is not file_handler and h.level == logging.NOTSET:
                        h.setLevel(root_level)
        root.addHandler(file_handler)
        _file_configured = True
    return root


@contextmanager
def scan_log_redirect(log_path: Path):
    """Suppress console log output and redirect to *log_path* for the duration.

    Intended to wrap Rich ``Progress`` blocks where ``RichHandler`` output
    interleaves with progress-bar rendering, corrupting the terminal display.

    The file is opened in ``'w'`` mode so each scan run starts fresh.
    Any user-configured ``RotatingFileHandler`` (via ``[logging]`` in
    ``whygraph.toml``) is left in place and continues writing in parallel.

    The root logger level is temporarily lowered to ``DEBUG`` so the file
    captures everything; it is restored to the original level on exit
    regardless of whether an exception was raised.

    Parameters
    ----------
    log_path : Path
        Destination file; its parent directory is created if absent.

    Yields
    ------
    Path
        The resolved *log_path*, for callers that want to print it.
    """
    root = logging.getLogger(_ROOT_NAME)

    console_handlers = [h for h in root.handlers if isinstance(h, RichHandler)]
    for h in console_handlers:
        root.removeHandler(h)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    prev_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    try:
        yield log_path
    finally:
        root.setLevel(prev_level)
        root.removeHandler(file_handler)
        file_handler.close()
        for h in console_handlers:
            root.addHandler(h)


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
