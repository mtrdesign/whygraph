"""Tests for the file-handler side of :func:`configure_logging`.

Exercises:
- Default behavior (no ``file_config``) attaches only the Rich handler.
- A ``LoggingConfig`` with ``file=...`` attaches a ``RotatingFileHandler``,
  creates the parent directory, and routes records into the file.
- Idempotency — a second call doesn't double-attach handlers.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from whygraph.core import logger as logger_module
from whygraph.core.config import LoggingConfig
from whygraph.core.logger import configure_logging


@pytest.fixture(autouse=True)
def _reset_root_logger() -> None:
    """Detach handlers and reset the module's idempotency flags.

    Without this, handlers attached in one test leak into the next and
    file paths from an earlier test stay open in a `RotatingFileHandler`.
    """
    root = logging.getLogger("whygraph")
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)
    root.setLevel(logging.NOTSET)
    logger_module._configured = False
    logger_module._file_configured = False
    yield
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)
    logger_module._configured = False
    logger_module._file_configured = False


def _file_handlers(root: logging.Logger) -> list[RotatingFileHandler]:
    return [h for h in root.handlers if isinstance(h, RotatingFileHandler)]


def test_no_file_handler_when_file_config_is_none() -> None:
    root = configure_logging("INFO")

    assert _file_handlers(root) == []
    assert len(root.handlers) == 1  # the Rich handler


def test_no_file_handler_when_file_path_is_none() -> None:
    root = configure_logging("INFO", file_config=LoggingConfig(file=None))

    assert _file_handlers(root) == []


def test_file_handler_attached_and_dir_created(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "whygraph.log"
    assert not log_path.parent.exists()

    root = configure_logging(
        "INFO", file_config=LoggingConfig(file=log_path)
    )

    handlers = _file_handlers(root)
    assert len(handlers) == 1
    assert log_path.parent.is_dir()


def test_records_land_in_file(tmp_path: Path) -> None:
    log_path = tmp_path / "whygraph.log"
    configure_logging("INFO", file_config=LoggingConfig(file=log_path))

    logging.getLogger("whygraph.test").info("hello-from-test")

    # Force the handler to flush so the file is readable from this process.
    for h in logging.getLogger("whygraph").handlers:
        h.flush()

    contents = log_path.read_text(encoding="utf-8")
    assert "hello-from-test" in contents
    assert "whygraph.test" in contents


def test_file_level_overrides_root_for_file_only(tmp_path: Path) -> None:
    """File at DEBUG while console (root) stays at INFO."""
    log_path = tmp_path / "whygraph.log"
    configure_logging(
        "INFO",
        file_config=LoggingConfig(file=log_path, level="DEBUG"),
    )

    logging.getLogger("whygraph.test").debug("debug-line")

    for h in logging.getLogger("whygraph").handlers:
        h.flush()

    contents = log_path.read_text(encoding="utf-8")
    assert "debug-line" in contents


def test_idempotent_no_duplicate_file_handler(tmp_path: Path) -> None:
    log_path = tmp_path / "whygraph.log"
    cfg = LoggingConfig(file=log_path)

    configure_logging("INFO", file_config=cfg)
    configure_logging("DEBUG", file_config=cfg)

    root = logging.getLogger("whygraph")
    assert len(_file_handlers(root)) == 1


def test_rotation_settings_applied(tmp_path: Path) -> None:
    log_path = tmp_path / "whygraph.log"
    cfg = LoggingConfig(file=log_path, max_bytes=1024, backup_count=2)

    configure_logging("INFO", file_config=cfg)

    [handler] = _file_handlers(logging.getLogger("whygraph"))
    assert handler.maxBytes == 1024
    assert handler.backupCount == 2
