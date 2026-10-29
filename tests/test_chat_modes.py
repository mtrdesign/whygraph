"""Tests for the chat modes (plan 38 §5.1, §10a.1).

A mode is a preset - a prompt block, preferred tools and starters - never a
tool subset, so these pin the presets themselves: every mode loads and
renders, its tool names are real chat tools, a stale id falls back to the
default, and the Lead and Product blocks carry their behavioural rules.
"""

from __future__ import annotations

import logging
from importlib import resources

import pytest

from whygraph.chat import modes
from whygraph.chat.modes import (
    AREA_PLACEHOLDER,
    DEFAULT_MODE,
    MILESTONE_PLACEHOLDER,
    MODES,
    get_mode,
    mode_block,
    render_starters,
)
from whygraph.chat.tools import TOOL_SPECS
from whygraph.core.config import MODE_IDS

_TOOL_NAMES = {spec.name for spec in TOOL_SPECS}


def _raw(mode_id: str) -> str:
    return (
        resources.files("whygraph.chat") / "prompts" / "modes" / f"{mode_id}.md"
    ).read_text(encoding="utf-8")


def test_modes_mirror_the_config_ids_in_order() -> None:
    """``[chat].mode`` validation and the chat modes name the same set."""
    assert tuple(MODES) == MODE_IDS
    assert modes.MODE_IDS is MODE_IDS
    assert DEFAULT_MODE in MODES
    for mode_id, mode in MODES.items():
        assert mode.id == mode_id


@pytest.mark.parametrize("mode_id", MODE_IDS)
def test_every_mode_is_complete(mode_id: str) -> None:
    mode = MODES[mode_id]
    assert mode.label and mode.description
    assert "\n" not in mode.description
    assert len(mode.starters) == 3
    assert mode.prefer


@pytest.mark.parametrize("mode_id", MODE_IDS)
def test_prefer_and_avoid_name_real_chat_tools(mode_id: str) -> None:
    mode = MODES[mode_id]
    assert set(mode.prefer) <= _TOOL_NAMES
    assert set(mode.avoid) <= _TOOL_NAMES
    assert not set(mode.prefer) & set(mode.avoid)


def test_get_mode_falls_back_to_the_default(caplog: pytest.LogCaptureFixture) -> None:
    assert get_mode(None) is MODES[DEFAULT_MODE]
    with caplog.at_level(logging.WARNING, logger="whygraph.chat.modes"):
        assert get_mode("gone") is MODES[DEFAULT_MODE]
    assert "gone" in caplog.text
    assert get_mode("lead") is MODES["lead"]


@pytest.mark.parametrize("mode_id", MODE_IDS)
def test_mode_files_hold_no_system_prompt_placeholders(mode_id: str) -> None:
    """A stray ``{{OVERVIEW}}`` would split the cached stable block."""
    raw = _raw(mode_id)
    for placeholder in ("{{REPO}}", "{{OVERVIEW}}", "{{MODE}}"):
        assert placeholder not in raw


@pytest.mark.parametrize("mode_id", MODE_IDS)
def test_mode_block_renders_every_placeholder_and_tool(mode_id: str) -> None:
    mode = MODES[mode_id]
    block = mode_block(mode)
    assert "{{" not in block and "}}" not in block
    assert block == block.strip()
    for name in mode.prefer + mode.avoid:
        assert f"`{name}`" in block
    assert mode_block(mode) == block


def test_the_engineer_block_stays_short() -> None:
    """The base prompt is already the Engineer voice; the block only names it."""
    assert len(mode_block(MODES["engineer"])) < 400


def test_mode_blocks_differ() -> None:
    blocks = {mode_block(mode) for mode in MODES.values()}
    assert len(blocks) == len(MODES)


def test_the_lead_block_carries_its_rules() -> None:
    """AC9: data date, not tracked, never rank (stats too), decision only if shown."""
    block = mode_block(MODES["lead"])
    assert "forge_data_as_of" in block
    assert "freshness_warning" in block
    assert "not_tracked" in block and '"not tracked"' in block
    assert "Never rank people" in block
    assert "run_project_stats" in block and "grouped by author" in block
    assert "productive" in block
    assert "only if the data shows one" in block
    assert "contributor_absence_factor" in block


def test_starters_take_the_first_area_or_a_generic_phrase() -> None:
    lead = MODES["lead"]
    assert any(AREA_PLACEHOLDER in text for text in lead.starters)
    named = render_starters(lead, "chat")
    assert "Who knows the chat area, and how thin is that knowledge?" in named
    generic = render_starters(lead, None)
    assert all(AREA_PLACEHOLDER not in text for text in generic)
    assert "Who knows the core of this project, and how thin is that knowledge?" in (
        generic
    )
    assert render_starters(lead, "") == generic


def test_engineer_starters_have_no_area() -> None:
    engineer = MODES["engineer"]
    assert render_starters(engineer, "chat") == engineer.starters


def test_the_product_block_carries_its_rules() -> None:
    """Plain language, data date, not tracked, dates both ways, never rank."""
    block = mode_block(MODES["product"])
    assert "product manager" in block
    assert "No\n  symbol names or file paths unless asked" in block
    assert "forge_data_as_of" in block and "freshness_warning" in block
    assert "not_tracked" in block and '"not tracked"' in block
    assert "Dates both ways" in block
    assert "Never rank people" in block
    assert "`compare_refs`" in block and "`get_milestones`" in block


def test_product_prefers_the_outcome_tools_and_avoids_code() -> None:
    product = MODES["product"]
    assert product.prefer[:4] == (
        "get_milestones",
        "compare_refs",
        "get_issue",
        "list_recent_activity",
    )
    assert {"get_symbol", "read_file"} <= set(product.avoid)


def test_product_starters_take_the_newest_milestone_or_a_generic_phrase() -> None:
    product = MODES["product"]
    assert any(MILESTONE_PLACEHOLDER in text for text in product.starters)
    named = render_starters(product, "chat", "v2.1")
    assert named == (
        "What shipped in the last release?",
        "How is the v2.1 milestone tracking?",
        "Which user-reported issues are still open?",
    )
    generic = render_starters(product, "chat", None)
    assert "How is the next milestone tracking?" in generic
    assert all(MILESTONE_PLACEHOLDER not in text for text in generic)
    assert render_starters(product, "chat", "  ") == generic
    assert render_starters(product, "chat") == generic


def test_no_starter_keeps_a_placeholder() -> None:
    for mode in MODES.values():
        for text in render_starters(mode, None, None) + render_starters(
            mode, "chat", "v2.1"
        ):
            assert AREA_PLACEHOLDER not in text
            assert MILESTONE_PLACEHOLDER not in text
