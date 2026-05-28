"""Tests for the ``whygraph.example.toml`` scaffold helpers.

Covers :func:`default_config_text` (the bundled template) and
:func:`write_example_config` (the always-refresh write used by
``whygraph init``).
"""

from __future__ import annotations

from pathlib import Path

from whygraph.core.config import (
    EXAMPLE_CONFIG_FILENAME,
    Config,
    default_config_text,
    write_example_config,
)


def test_default_config_text_is_valid_and_matches_defaults(tmp_path: Path) -> None:
    """The bundled template parses and yields the same Config as no file."""
    config = tmp_path / "whygraph.toml"
    config.write_text(default_config_text(), encoding="utf-8")

    cfg = Config.from_toml(config)

    # An unedited copy behaves exactly as if no config were present.
    assert cfg.log_level == "INFO"
    assert cfg.scan_max_workers == 2
    assert cfg.analyze.provider == "anthropic"
    assert cfg.rationale.provider == "anthropic"
    # `[logging]` is commented out in the template, so file logging is off.
    assert cfg.logging.file is None


def test_write_example_config_creates_example_file(tmp_path: Path) -> None:
    path = write_example_config(tmp_path)

    assert path == tmp_path / EXAMPLE_CONFIG_FILENAME
    assert path.read_text(encoding="utf-8") == default_config_text()
    # The real config is never written — that's the user's copy to make.
    assert not (tmp_path / "whygraph.toml").exists()


def test_write_example_config_refreshes_existing(tmp_path: Path) -> None:
    """A second call overwrites a stale example with the current template."""
    existing = tmp_path / EXAMPLE_CONFIG_FILENAME
    existing.write_text("stale = true\n", encoding="utf-8")

    path = write_example_config(tmp_path)

    assert path == existing
    assert existing.read_text(encoding="utf-8") == default_config_text()
