"""Tests for ``[llm.claude_cli].config_dir`` parsing in ``whygraph.toml``.

Covers the default (``None`` — inherit the ambient ``CLAUDE_CONFIG_DIR``),
``~`` / ``$VAR`` expansion, relative-path resolution against the TOML
directory, and that both section spellings carry the key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whygraph.core.config import Config


def _write(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


def test_config_dir_defaults_to_none(tmp_path: Path) -> None:
    config = _write(tmp_path / "whygraph.toml", '[llm.claude_cli]\nmodel = "m"\n')
    assert Config.from_toml(config).llm.claude_cli.config_dir is None


def test_config_dir_expands_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = _write(
        tmp_path / "whygraph.toml", '[llm.claude_cli]\nconfig_dir = "~/.claude-work"\n'
    )
    assert Config.from_toml(config).llm.claude_cli.config_dir == (
        tmp_path / ".claude-work"
    )


def test_config_dir_expands_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROFILES", str(tmp_path / "profiles"))
    config = _write(
        tmp_path / "whygraph.toml", '[llm.claude-cli]\nconfig_dir = "$PROFILES/work"\n'
    )
    assert Config.from_toml(config).llm.claude_cli.config_dir == (
        tmp_path / "profiles" / "work"
    )


def test_config_dir_relative_resolves_against_toml_dir(tmp_path: Path) -> None:
    nested = tmp_path / "subdir"
    nested.mkdir()
    config = _write(
        nested / "whygraph.toml", '[llm.claude_cli]\nconfig_dir = ".claude-profile"\n'
    )
    assert Config.from_toml(config).llm.claude_cli.config_dir == (
        (nested / ".claude-profile").resolve()
    )
