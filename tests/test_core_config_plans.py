"""Tests for the ``[plans]`` section: config, ``check_plans_dir`` and policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from whygraph.analyze import project_purpose
from whygraph.core.config import (
    PLANS_DIR_DENIED_PREFIXES,
    PLANS_DIR_DENIED_SUFFIXES,
    PLANS_DIR_RE,
    Config,
    ConfigError,
    PlansConfig,
    check_plans_dir,
)
from whygraph.portal import policy
from whygraph.portal.policy import (
    DEFAULTS_ALLOWLIST,
    IMPORT_ALLOWLIST,
    LINKED_PUT_ALLOWLIST,
    filter_layer,
    invalid_values,
    preview_import,
    put_allowlist,
)
from whygraph.portal.transfer_import import TRANSFER_CONFIG_ALLOWLIST


@pytest.mark.parametrize(
    ("value", "expected"),
    [("docs/plans", "docs/plans"), ("plans", "plans"), ("", ""), ("a/b/", "a/b")],
)
def test_check_plans_dir_accepts(value: str, expected: str) -> None:
    assert check_plans_dir(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "..",
        "./x",
        "/abs",
        "-x",
        ".whygraph/plans",
        ".git",
        "a b",
        ".claude/commands",
        ".github/plans",
        "docs/secrets",
        "a/../b",
        "keys/server.pem",
    ],
)
def test_check_plans_dir_rejects(value: str) -> None:
    with pytest.raises(ConfigError):
        check_plans_dir(value)


def test_check_plans_dir_rejects_non_string() -> None:
    with pytest.raises(ConfigError):
        check_plans_dir(5)  # type: ignore[arg-type]


def test_copies_match_project_purpose() -> None:
    assert PLANS_DIR_RE.pattern == project_purpose._SAFE_REL.pattern
    assert PLANS_DIR_DENIED_PREFIXES == project_purpose._DENIED_PREFIXES
    assert PLANS_DIR_DENIED_SUFFIXES == project_purpose._DENIED_SUFFIXES


def test_from_dict_plans(tmp_path: Path) -> None:
    assert Config.from_dict({}, tmp_path).plans == PlansConfig(dir="docs/plans")
    assert Config.from_dict({"plans": {"dir": ""}}, tmp_path).plans.dir == ""
    assert (
        Config.from_dict({"plans": {"dir": None}}, tmp_path).plans.dir == "docs/plans"
    )
    assert Config.from_dict({"plans": {"dir": "x/"}}, tmp_path).plans.dir == "x"
    with pytest.raises(ConfigError):
        Config.from_dict({"plans": {"dir": "../x"}}, tmp_path)


def test_policy_allowlists() -> None:
    layer = {"plans": {"dir": "x"}}
    assert filter_layer(layer, IMPORT_ALLOWLIST) == (layer, [])
    assert filter_layer(layer, put_allowlist("production"))[0] == layer
    assert filter_layer(layer, put_allowlist("local"))[0] == layer
    assert filter_layer(layer, DEFAULTS_ALLOWLIST) == ({}, ["plans"])
    assert filter_layer(layer, LINKED_PUT_ALLOWLIST) == ({}, ["plans"])
    assert "plans" not in TRANSFER_CONFIG_ALLOWLIST
    assert filter_layer(layer, TRANSFER_CONFIG_ALLOWLIST)[0] == {}


def test_invalid_values() -> None:
    assert invalid_values({"plans": {"dir": "docs/plans"}}) == {}
    assert invalid_values({"plans": {"dir": ""}}) == {}
    assert "plans.dir" in invalid_values({"plans": {"dir": "../x"}})
    # An unchanged stored value is not re-checked.
    stored = {"plans": {"dir": "../x"}}
    assert invalid_values(stored, stored) == {}


def test_import_drops_invalid_plans_dir(tmp_path: Path) -> None:
    (tmp_path / "whygraph.toml").write_text('[plans]\ndir = "../x"\n')
    preview = preview_import(tmp_path)
    assert "plans" not in preview.layer
    dropped = {d["key"]: d["hint"] for d in preview.dropped}
    assert "plans.dir" in dropped
    assert policy._VALUE_HINT in dropped["plans.dir"]


def test_import_keeps_valid_plans_dir(tmp_path: Path) -> None:
    (tmp_path / "whygraph.toml").write_text('[plans]\ndir = "docs/rfcs"\n')
    preview = preview_import(tmp_path)
    assert preview.layer["plans"] == {"dir": "docs/rfcs"}
    assert not any(d["key"] == "plans.dir" for d in preview.dropped)
