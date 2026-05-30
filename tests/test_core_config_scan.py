"""Tests for the ``[scan].provider`` and ``[scan].remote`` keys.

Exercises the defaults, TOML parsing / normalization via
:meth:`Config.from_toml`, and the ``scan_provider`` validator on
:meth:`Config.__post_init__`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whygraph.core.config import Config, ConfigError


def _write(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


def test_provider_and_remote_default_when_section_omitted(tmp_path: Path) -> None:
    cfg = Config.from_toml(_write(tmp_path / "whygraph.toml", ""))

    assert cfg.scan_provider == "off"
    assert cfg.scan_remote == "origin"


@pytest.mark.parametrize("value", ["off", "github", "auto"])
def test_provider_values_parse(tmp_path: Path, value: str) -> None:
    cfg = Config.from_toml(
        _write(tmp_path / "whygraph.toml", f'[scan]\nprovider = "{value}"\n')
    )

    assert cfg.scan_provider == value


def test_provider_mixed_case_normalizes(tmp_path: Path) -> None:
    cfg = Config.from_toml(
        _write(tmp_path / "whygraph.toml", '[scan]\nprovider = "GitHub"\n')
    )

    assert cfg.scan_provider == "github"


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_provider_normalizes_to_off(tmp_path: Path, value: str) -> None:
    cfg = Config.from_toml(
        _write(tmp_path / "whygraph.toml", f'[scan]\nprovider = "{value}"\n')
    )

    assert cfg.scan_provider == "off"


def test_unknown_provider_raises(tmp_path: Path) -> None:
    config = _write(tmp_path / "whygraph.toml", '[scan]\nprovider = "gitlab"\n')

    with pytest.raises(ConfigError, match="scan.provider"):
        Config.from_toml(config)


def test_remote_parses(tmp_path: Path) -> None:
    cfg = Config.from_toml(
        _write(tmp_path / "whygraph.toml", '[scan]\nremote = "upstream"\n')
    )

    assert cfg.scan_remote == "upstream"


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_remote_falls_back_to_origin(tmp_path: Path, value: str) -> None:
    cfg = Config.from_toml(
        _write(tmp_path / "whygraph.toml", f'[scan]\nremote = "{value}"\n')
    )

    assert cfg.scan_remote == "origin"


def test_provider_and_remote_coexist_with_max_workers(tmp_path: Path) -> None:
    cfg = Config.from_toml(
        _write(
            tmp_path / "whygraph.toml",
            '[scan]\nmax_workers = 4\nprovider = "github"\nremote = "upstream"\n',
        )
    )

    assert cfg.scan_max_workers == 4
    assert cfg.scan_provider == "github"
    assert cfg.scan_remote == "upstream"
