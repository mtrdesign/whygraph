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


def test_token_defaults_to_none_when_omitted(tmp_path: Path) -> None:
    cfg = Config.from_toml(_write(tmp_path / "whygraph.toml", ""))

    assert cfg.scan_token is None


def test_token_parses(tmp_path: Path) -> None:
    cfg = Config.from_toml(
        _write(tmp_path / "whygraph.toml", '[scan]\ntoken = "ghp_secret"\n')
    )

    assert cfg.scan_token == "ghp_secret"


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_token_normalizes_to_none(tmp_path: Path, value: str) -> None:
    cfg = Config.from_toml(
        _write(tmp_path / "whygraph.toml", f'[scan]\ntoken = "{value}"\n')
    )

    assert cfg.scan_token is None


# --- [scan].hooks and [scan].default_branch (plan §6) ------------------------


@pytest.mark.parametrize("value, expected", [("true", True), ("false", False)])
def test_hooks_bool_parses_verbatim(tmp_path: Path, value: str, expected: bool) -> None:
    """Case 18."""
    cfg = Config.from_toml(
        _write(tmp_path / "whygraph.toml", f"[scan]\nhooks = {value}\n")
    )

    assert cfg.scan_hooks is expected


def test_hooks_list_parses_to_tuple(tmp_path: Path) -> None:
    """Case 19 — a list becomes a tuple; an empty list collapses to False."""
    cfg = Config.from_toml(
        _write(tmp_path / "whygraph.toml", '[scan]\nhooks = ["post-commit"]\n')
    )
    assert cfg.scan_hooks == ("post-commit",)

    empty = Config.from_toml(_write(tmp_path / "empty.toml", "[scan]\nhooks = []\n"))
    assert empty.scan_hooks is False


@pytest.mark.parametrize("body", ["hooks = 5", 'hooks = ["post-commit", 7]'])
def test_hooks_wrong_shape_raises(tmp_path: Path, body: str) -> None:
    """Case 20 — a shape error is a hard failure, like an invalid provider."""
    with pytest.raises(ConfigError, match=r"\[scan\].hooks"):
        Config.from_toml(_write(tmp_path / "whygraph.toml", f"[scan]\n{body}\n"))


def test_hooks_names_are_not_validated_here(tmp_path: Path) -> None:
    """A typo'd name parses fine — `whygraph.hooks` owns name validation, so
    `core` keeps no dependency on it (§6)."""
    cfg = Config.from_toml(
        _write(tmp_path / "whygraph.toml", '[scan]\nhooks = ["post-comit"]\n')
    )

    assert cfg.scan_hooks == ("post-comit",)


def test_hooks_and_default_branch_defaults(tmp_path: Path) -> None:
    """Case 21."""
    cfg = Config.from_toml(_write(tmp_path / "whygraph.toml", ""))

    assert cfg.scan_hooks is True
    assert cfg.scan_default_branch is None


def test_default_branch_parses(tmp_path: Path) -> None:
    cfg = Config.from_toml(
        _write(tmp_path / "whygraph.toml", '[scan]\ndefault_branch = "develop"\n')
    )

    assert cfg.scan_default_branch == "develop"


@pytest.mark.parametrize("value", ['""', '"   "'])
def test_empty_default_branch_normalizes_to_none(tmp_path: Path, value: str) -> None:
    cfg = Config.from_toml(
        _write(tmp_path / "whygraph.toml", f"[scan]\ndefault_branch = {value}\n")
    )

    assert cfg.scan_default_branch is None


def test_unknown_scan_key_still_only_warns(tmp_path: Path) -> None:
    """Case 22 — the new `scan.pop("hooks")` must not break the warn loop."""
    cfg = Config.from_toml(
        _write(
            tmp_path / "whygraph.toml",
            '[scan]\nhooks = false\ndefault_branch = "main"\nnonsense = 1\n',
        )
    )

    assert cfg.scan_hooks is False
    assert cfg.scan_default_branch == "main"
