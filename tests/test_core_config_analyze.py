"""Tests for the ``[analyze]`` section of ``whygraph.toml``.

Exercises :class:`AnalyzeConfig` defaults, TOML parsing via
:meth:`Config.from_toml`, and the validator on
:attr:`AnalyzeConfig.max_diff_chars`.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from whygraph.core.config import AnalyzeConfig, Config, ConfigError


def _write(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


def test_analyze_defaults_when_section_omitted(tmp_path: Path) -> None:
    config = _write(tmp_path / "whygraph.toml", "")
    cfg = Config.from_toml(config)

    assert cfg.analyze == AnalyzeConfig()
    assert cfg.analyze.provider == "anthropic"
    assert cfg.analyze.max_diff_chars == 50_000
    assert cfg.analyze.large_commit_file_count == 30
    assert cfg.analyze.timeout_sec is None


def test_analyze_section_parsed(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "whygraph.toml",
        '[analyze]\nprovider = "openai"\nmax_diff_chars = 1234\ntimeout_sec = 90\n',
    )
    cfg = Config.from_toml(config)

    assert cfg.analyze == AnalyzeConfig(
        provider="openai", max_diff_chars=1234, timeout_sec=90
    )


def test_analyze_section_partial_overrides(tmp_path: Path) -> None:
    """Unspecified fields fall back to the dataclass defaults."""
    config = _write(
        tmp_path / "whygraph.toml",
        "[analyze]\nmax_diff_chars = 10\n",
    )
    cfg = Config.from_toml(config)

    assert cfg.analyze.max_diff_chars == 10
    assert cfg.analyze.provider == "anthropic"
    assert cfg.analyze.timeout_sec is None


def test_analyze_unknown_key_warns_but_loads(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _write(
        tmp_path / "whygraph.toml",
        '[analyze]\nprovider = "anthropic"\nbogus = true\n',
    )

    with caplog.at_level(logging.WARNING, logger="whygraph.core.config"):
        cfg = Config.from_toml(config)

    assert any("bogus" in r.message for r in caplog.records)
    assert cfg.analyze.provider == "anthropic"


def test_analyze_invalid_max_diff_chars_raises(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "whygraph.toml",
        "[analyze]\nmax_diff_chars = 0\n",
    )
    with pytest.raises(ConfigError, match="max_diff_chars"):
        Config.from_toml(config)


def test_analyze_large_commit_file_count_parsed(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "whygraph.toml",
        "[analyze]\nlarge_commit_file_count = 75\n",
    )
    cfg = Config.from_toml(config)

    assert cfg.analyze.large_commit_file_count == 75


def test_analyze_invalid_large_commit_file_count_raises(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "whygraph.toml",
        "[analyze]\nlarge_commit_file_count = 0\n",
    )
    with pytest.raises(ConfigError, match="large_commit_file_count"):
        Config.from_toml(config)


def test_analyze_config_is_frozen() -> None:
    cfg = AnalyzeConfig()
    with pytest.raises(Exception):  # FrozenInstanceError
        cfg.provider = "openai"  # type: ignore[misc]


def test_analyze_model_defaults_to_none(tmp_path: Path) -> None:
    config = _write(tmp_path / "whygraph.toml", "")
    cfg = Config.from_toml(config)

    assert cfg.analyze.model is None


def test_analyze_model_parsed(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "whygraph.toml",
        '[analyze]\nmodel = "claude-haiku-4-5"\n',
    )
    cfg = Config.from_toml(config)

    assert cfg.analyze.model == "claude-haiku-4-5"
