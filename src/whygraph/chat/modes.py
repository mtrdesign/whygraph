"""Chat modes: presets that change how the assistant answers, not what it can do.

A mode is a **preset** - a system-prompt block, the tools that block
recommends first (and the ones it keeps for explicit requests), and the
starter questions a new chat shows. It is never a tool subset: every mode
sends the full tool list on every request, because a request carrying tool
history must never drop a tool and a changed tool set invalidates replayed
thinking. The mode steers by prompt alone.

Modes are defined here, in code, with their prompt blocks packaged under
``prompts/modes/`` the same way ``prompts/system.md`` is. A repo can never
define one: a committed mode file would be a prompt-injection channel.

The block lands in the stable half of the system prompt, at ``{{MODE}}``
(see :func:`whygraph.chat.harness.build_system_prompt`), so it is cached
with the rest of the prompt and changes only when the session's mode does.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from types import MappingProxyType

from whygraph.analyze.prompt import render as render_prompt
from whygraph.core.config import MODE_IDS

__all__ = [
    "AREA_PLACEHOLDER",
    "DEFAULT_MODE",
    "MODES",
    "MODE_IDS",
    "Mode",
    "get_mode",
    "mode_block",
    "render_starters",
]

_log = logging.getLogger(__name__)

DEFAULT_MODE = "engineer"
"""The mode a session runs in when none is stored, or the stored one is gone."""

AREA_PLACEHOLDER = "<area>"
"""Stands in a starter question for the project's first configured area."""

_GENERIC_AREA = "the core of this project"
"""What :data:`AREA_PLACEHOLDER` becomes when the project has no areas."""

_PREFER_PLACEHOLDER = "{{PREFER}}"
_AVOID_PLACEHOLDER = "{{AVOID}}"


@dataclass(frozen=True, slots=True)
class Mode:
    """One chat mode.

    Attributes
    ----------
    id : str
        Stable identifier, one of :data:`MODE_IDS`. Stored on sessions and
        assistant rows and accepted in ``[chat].mode``.
    label : str
        Display name for the picker.
    description : str
        One line shown under the picker.
    starters : tuple[str, ...]
        Three starter questions for an empty chat. :data:`AREA_PLACEHOLDER`
        is filled in by :func:`render_starters`.
    prefer : tuple[str, ...]
        Tool names the block recommends first, rendered into its
        ``{{PREFER}}``. Every one is a chat tool (pinned by a test).
    avoid : tuple[str, ...]
        Tool names the block keeps for explicit requests, rendered into its
        ``{{AVOID}}``. The tools stay on the request either way.
    """

    id: str
    label: str
    description: str
    starters: tuple[str, ...]
    prefer: tuple[str, ...]
    avoid: tuple[str, ...]


MODES: Mapping[str, Mode] = MappingProxyType(
    {
        "engineer": Mode(
            id="engineer",
            label="Engineer",
            description=(
                "Code-first answers: symbols, files and lines, and why the "
                "code is that way."
            ),
            starters=(
                "What changed most in the last month?",
                "Explain how this project is structured",
                "Which areas have no rationale yet?",
            ),
            prefer=("get_area_outline", "get_symbol", "find_changes", "get_rationale"),
            avoid=(),
        ),
        "lead": Mode(
            id="lead",
            label="Lead",
            description=(
                "The state of the work: what shipped, what is in flight or "
                "stuck, and who knows each area."
            ),
            starters=(
                "What is in flight right now, and what looks stuck?",
                "How did the last two weeks go, by area?",
                f"Who knows {AREA_PLACEHOLDER}, and how thin is that knowledge?",
            ),
            prefer=(
                "list_recent_activity",
                "get_work_in_flight",
                "find_changes",
                "get_area_owners",
                "run_project_stats",
                "render_chart",
                "get_rationale",
            ),
            avoid=("get_symbol", "read_file"),
        ),
        "onboarding": Mode(
            id="onboarding",
            label="Onboarding",
            description=(
                "A top-down tour for someone new: the map, the pieces that "
                "matter, and where to start reading."
            ),
            starters=(
                "Give me a map of this codebase",
                f"Explain how {AREA_PLACEHOLDER} works end to end",
                f"What should I know before changing {AREA_PLACEHOLDER}?",
            ),
            prefer=("get_area_outline", "get_rationale", "read_file"),
            avoid=("run_project_stats", "find_changes"),
        ),
    }
)
"""Every mode, keyed by id, in :data:`MODE_IDS` order (pinned by a test)."""


def get_mode(mode_id: str | None) -> Mode:
    """Return the mode named ``mode_id``, falling back to the default.

    Lenient on purpose: a stored mode can go stale (a mode removed, a
    rollback), and a stale value must run as the default rather than fail
    the turn.

    Parameters
    ----------
    mode_id : str or None
        A mode id, or ``None`` for the default.

    Returns
    -------
    Mode
        ``MODES[mode_id]``, else ``MODES[DEFAULT_MODE]``. An unknown id
        logs a warning; ``None`` does not.
    """
    if mode_id is None:
        return MODES[DEFAULT_MODE]
    mode = MODES.get(mode_id)
    if mode is None:
        _log.warning("unknown chat mode %r; using %r", mode_id, DEFAULT_MODE)
        return MODES[DEFAULT_MODE]
    return mode


def _tool_list(names: tuple[str, ...]) -> str:
    """Render tool names as code spans: "`a`, `b` and `c`"."""
    spans = [f"`{name}`" for name in names]
    if len(spans) < 2:
        return "".join(spans)
    return ", ".join(spans[:-1]) + " and " + spans[-1]


def mode_block(mode: Mode) -> str:
    """Render a mode's packaged prompt block.

    Parameters
    ----------
    mode : Mode
        The mode to render.

    Returns
    -------
    str
        The text of ``prompts/modes/<id>.md`` with ``{{PREFER}}`` and
        ``{{AVOID}}`` replaced by the mode's tool names, stripped of
        surrounding whitespace. The same mode always renders the same bytes,
        which keeps the cached system prompt stable.
    """
    template = (
        resources.files("whygraph.chat") / "prompts" / "modes" / f"{mode.id}.md"
    ).read_text(encoding="utf-8")
    text = render_prompt(
        template, _tool_list(mode.prefer), placeholder=_PREFER_PLACEHOLDER
    )
    text = render_prompt(text, _tool_list(mode.avoid), placeholder=_AVOID_PLACEHOLDER)
    return text.strip()


def render_starters(mode: Mode, area: str | None) -> tuple[str, ...]:
    """Fill a mode's starter questions with a real area name.

    Parameters
    ----------
    mode : Mode
        The mode whose starters to render.
    area : str or None
        The project's first configured area name, or ``None`` when it has
        none.

    Returns
    -------
    tuple[str, ...]
        The starters with :data:`AREA_PLACEHOLDER` replaced by
        ``"the <area> area"``, or by a generic phrase when ``area`` is
        ``None`` or empty.
    """
    phrase = f"the {area} area" if area else _GENERIC_AREA
    return tuple(text.replace(AREA_PLACEHOLDER, phrase) for text in mode.starters)
