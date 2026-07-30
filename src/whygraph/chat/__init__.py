"""The chat assistant: an in-house agentic harness over WhyGraph's tools.

A sibling of :mod:`whygraph.mcp` and :mod:`whygraph.serve`, and the third
adapter over the same core. Where the MCP server exposes WhyGraph's
capabilities to an *external* agent and the Explorer exposes them to a
*human*, this package drives a model against them directly — so a
developer can converse with the repo's accumulated knowledge.

Layout
------
* :mod:`.tools` — the twelve :class:`~whygraph.services.llm.chat.ToolSpec`
  definitions plus :class:`~whygraph.chat.tools.ToolRegistry`, which
  dispatches to the same plain functions the MCP tools call.
* :mod:`.files` — clamped read-only file access, the one genuinely new
  attack surface the assistant adds.
* :mod:`.harness` — :func:`~whygraph.chat.harness.run_turn`, the agentic
  loop, and :func:`~whygraph.chat.harness.build_window`, the context
  trimmer.
* ``prompts/system.md`` — the system-prompt template, packaged the same
  way :mod:`whygraph.analyze` packages its prompts.

The harness is deliberately **persistence-free**: it takes messages and
yields events. :mod:`whygraph.serve.chat` owns reading and writing rows,
which keeps the loop unit-testable against a scripted fake client.
"""

from __future__ import annotations

from .harness import (
    HarnessEvent,
    RoundLimit,
    ToolCallStarted,
    ToolResultReady,
    build_system_prompt,
    build_window,
    run_turn,
)
from .tools import ToolRegistry

__all__ = [
    "HarnessEvent",
    "RoundLimit",
    "ToolCallStarted",
    "ToolRegistry",
    "ToolResultReady",
    "build_system_prompt",
    "build_window",
    "run_turn",
]
