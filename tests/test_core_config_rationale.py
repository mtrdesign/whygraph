"""Tests for the ``[rationale]`` section of ``whygraph.toml``.

Exercises :class:`RationaleConfig` defaults and TOML parsing via
:meth:`Config.from_toml`, and guards the removal of the superseded
top-level ``rationale_model`` field.
"""

from __future__ import annotations

import logging
from dataclasses import fields
from pathlib import Path

import pytest

from whygraph.core.config import Config, ConfigError, RationaleConfig


def _write(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


def test_rationale_defaults_when_section_omitted(tmp_path: Path) -> None:
    config = _write(tmp_path / "whygraph.toml", "")
    cfg = Config.from_toml(config)

    assert cfg.rationale == RationaleConfig()
    assert cfg.rationale.provider == "anthropic"
    assert cfg.rationale.model is None
    assert cfg.rationale.timeout_sec is None


def test_rationale_section_parsed(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "whygraph.toml",
        '[rationale]\nprovider = "openai"\nmodel = "gpt-4o"\ntimeout_sec = 90\n',
    )
    cfg = Config.from_toml(config)

    assert cfg.rationale == RationaleConfig(
        provider="openai", model="gpt-4o", timeout_sec=90
    )


def test_rationale_section_partial_overrides(tmp_path: Path) -> None:
    """Unspecified fields fall back to the dataclass defaults."""
    config = _write(
        tmp_path / "whygraph.toml",
        '[rationale]\nprovider = "ollama"\n',
    )
    cfg = Config.from_toml(config)

    assert cfg.rationale.provider == "ollama"
    assert cfg.rationale.model is None
    assert cfg.rationale.timeout_sec is None


def test_rationale_unknown_key_warns_but_loads(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _write(
        tmp_path / "whygraph.toml",
        '[rationale]\nprovider = "anthropic"\nbogus = true\n',
    )

    with caplog.at_level(logging.WARNING, logger="whygraph.core.config"):
        cfg = Config.from_toml(config)

    assert any("bogus" in r.message for r in caplog.records)
    assert cfg.rationale.provider == "anthropic"


def test_rationale_pr_render_cap_defaults(tmp_path: Path) -> None:
    config = _write(tmp_path / "whygraph.toml", "")
    cfg = Config.from_toml(config)

    assert cfg.rationale.pr_roster_max_commits == 30
    assert cfg.rationale.pr_discussion_max_comments == 20
    assert cfg.rationale.pr_comment_max_chars == 500


def test_rationale_pr_render_caps_parsed(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "whygraph.toml",
        "[rationale]\n"
        "pr_roster_max_commits = 5\n"
        "pr_discussion_max_comments = 3\n"
        "pr_comment_max_chars = 120\n",
    )
    cfg = Config.from_toml(config)

    assert cfg.rationale.pr_roster_max_commits == 5
    assert cfg.rationale.pr_discussion_max_comments == 3
    assert cfg.rationale.pr_comment_max_chars == 120


@pytest.mark.parametrize(
    "field_name",
    [
        "pr_roster_max_commits",
        "pr_discussion_max_comments",
        "pr_comment_max_chars",
    ],
)
def test_rationale_pr_render_cap_below_one_raises(
    tmp_path: Path, field_name: str
) -> None:
    config = _write(
        tmp_path / "whygraph.toml",
        f"[rationale]\n{field_name} = 0\n",
    )
    with pytest.raises(ConfigError, match=field_name):
        Config.from_toml(config)


def test_rationale_config_is_frozen() -> None:
    cfg = RationaleConfig()
    with pytest.raises(Exception):  # FrozenInstanceError
        cfg.provider = "openai"  # type: ignore[misc]


def test_config_no_longer_has_rationale_model() -> None:
    """The unused top-level ``rationale_model`` field was superseded by the
    ``[rationale]`` section — guard against a silent re-add."""
    field_names = {f.name for f in fields(Config)}
    assert "rationale_model" not in field_names
    assert "rationale" in field_names
