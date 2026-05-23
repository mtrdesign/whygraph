"""Tests for the ``[logging]`` section of ``whygraph.toml``.

Exercises :class:`LoggingConfig` defaults, TOML parsing via
:meth:`Config.from_toml`, relative-path resolution, and the validators on
``level`` / ``max_bytes`` / ``backup_count``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from whygraph.core.config import Config, ConfigError, LoggingConfig


def _write(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


def test_logging_defaults_when_section_omitted(tmp_path: Path) -> None:
    config = _write(tmp_path / "whygraph.toml", "")
    cfg = Config.from_toml(config)

    assert cfg.logging == LoggingConfig()
    assert cfg.logging.file is None
    assert cfg.logging.level is None
    assert cfg.logging.max_bytes == 5_000_000
    assert cfg.logging.backup_count == 3


def test_logging_section_parsed(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "whygraph.toml",
        '[logging]\n'
        'file = "logs/whygraph.log"\n'
        'level = "DEBUG"\n'
        'max_bytes = 1024\n'
        'backup_count = 1\n',
    )
    cfg = Config.from_toml(config)

    assert cfg.logging.file == (tmp_path / "logs" / "whygraph.log").resolve()
    assert cfg.logging.level == "DEBUG"
    assert cfg.logging.max_bytes == 1024
    assert cfg.logging.backup_count == 1


def test_logging_file_relative_resolves_against_toml_dir(tmp_path: Path) -> None:
    """Mirrors the ``whygraph_db`` convention — file is relative to the TOML."""
    nested = tmp_path / "subdir"
    nested.mkdir()
    config = _write(
        nested / "whygraph.toml",
        '[logging]\nfile = ".whygraph/logs/whygraph.log"\n',
    )
    cfg = Config.from_toml(config)

    assert cfg.logging.file == (nested / ".whygraph/logs/whygraph.log").resolve()


def test_logging_file_absolute_path_preserved(tmp_path: Path) -> None:
    abs_path = tmp_path / "elsewhere" / "out.log"
    config = _write(
        tmp_path / "whygraph.toml",
        f'[logging]\nfile = "{abs_path}"\n',
    )
    cfg = Config.from_toml(config)

    assert cfg.logging.file == abs_path.resolve()


def test_logging_invalid_level_raises(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "whygraph.toml",
        '[logging]\nlevel = "VERBOSE"\n',
    )
    with pytest.raises(ConfigError, match="logging.level"):
        Config.from_toml(config)


def test_logging_invalid_max_bytes_raises(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "whygraph.toml",
        "[logging]\nmax_bytes = 0\n",
    )
    with pytest.raises(ConfigError, match="logging.max_bytes"):
        Config.from_toml(config)


def test_logging_negative_backup_count_raises(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "whygraph.toml",
        "[logging]\nbackup_count = -1\n",
    )
    with pytest.raises(ConfigError, match="logging.backup_count"):
        Config.from_toml(config)


def test_logging_unknown_key_warns_but_loads(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _write(
        tmp_path / "whygraph.toml",
        '[logging]\nfile = "x.log"\nbogus = true\n',
    )

    with caplog.at_level(logging.WARNING, logger="whygraph.core.config"):
        cfg = Config.from_toml(config)

    assert any("bogus" in r.message for r in caplog.records)
    assert cfg.logging.file == (tmp_path / "x.log").resolve()


def test_logging_config_is_frozen() -> None:
    cfg = LoggingConfig()
    with pytest.raises(Exception):  # FrozenInstanceError
        cfg.file = Path("/tmp/x")  # type: ignore[misc]
