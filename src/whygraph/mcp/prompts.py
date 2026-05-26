"""User-triggered MCP prompts for non-Claude-Code clients.

Three prompts are registered:

* ``whygraph_pre_edit_brief`` — "I'm about to edit this; ground me in its
  rationale and history first."
* ``whygraph_why_was_this_written`` — "Tell me the historical story
  behind this code."
* ``whygraph_triage_commit`` — "Summarize what this commit did and why."

Each prompt expands into a single ``UserMessage`` that instructs the host
model to call the relevant WhyGraph tools / read the relevant resources
in the right order, then synthesize the result.

Claude Code already has equivalent orchestration via the bundled slash
commands (``/whygraph-plan``, ``/rationale``) and the ``pre-edit`` skill,
which can spawn subagents. Prompts can't spawn subagents — they only
emit messages — so these prompts deliberately stay leaner than the
slash-command flows. The audience is Cursor, Claude Desktop, VS Code,
Codex, and any other MCP client that doesn't have local orchestration
hooks.
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

_log = logging.getLogger(__name__)


# ---- target rendering ----------------------------------------------------


def _target_label(
    path: str | None,
    line_start: int | None,
    line_end: int | None,
    qualified_name: str | None,
) -> str:
    """Human-readable label for the targeted code, used inside the prompt body.

    Raises
    ------
    ValueError
        If neither ``path`` nor ``qualified_name`` is provided — without
        one, there is nothing to address.
    """
    if qualified_name and path:
        return f"{path} (`{qualified_name}`)"
    if qualified_name:
        return f"`{qualified_name}`"
    if path and line_start is not None and line_end is not None:
        return f"{path}:{line_start}-{line_end}"
    if path:
        return path
    raise ValueError(
        "Provide either `path` (optionally with `line_start`/`line_end`) "
        "or `qualified_name`."
    )


def _target_args(
    path: str | None,
    line_start: int | None,
    line_end: int | None,
    qualified_name: str | None,
) -> str:
    """Render the targeting args as ``key=value, ...`` for direct embedding in
    prompt bodies — so the host model can copy the call verbatim instead of
    re-parsing the natural-language target."""
    parts: list[str] = []
    if path:
        parts.append(f'path="{path}"')
    if line_start is not None:
        parts.append(f"line_start={line_start}")
    if line_end is not None:
        parts.append(f"line_end={line_end}")
    if qualified_name:
        parts.append(f'qualified_name="{qualified_name}"')
    return ", ".join(parts)


# ---- prompt bodies -------------------------------------------------------


def _pre_edit_brief(
    path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    qualified_name: str | None = None,
) -> str:
    """Brief me on the code I'm about to edit, before I write any.

    Parameters
    ----------
    path : str, optional
        Source file path, relative to the repo root.
    line_start : int, optional
        First line (1-indexed, inclusive) of the chunk being edited.
    line_end : int, optional
        Last line (1-indexed, inclusive) of the chunk being edited.
    qualified_name : str, optional
        Fully-qualified symbol name (e.g. ``pkg.module.func``). Use
        instead of ``path``/``line_start``/``line_end`` when you know the
        symbol but not the lines.
    """
    target = _target_label(path, line_start, line_end, qualified_name)
    args = _target_args(path, line_start, line_end, qualified_name)
    return (
        f"I'm about to edit {target}. Ground me in its rationale and historical "
        f"context before I write any code.\n"
        f"\n"
        f"Steps:\n"
        f"1. Call `whygraph_rationale_brief` with: {args}.\n"
        f"2. If purpose / why / constraints / risks come back thin or empty, "
        f"also call `whygraph_evidence_for` with the same arguments for richer "
        f"historical context (commits, PRs, issues).\n"
        f"3. Summarize: purpose, constraints to preserve, and edit risks.\n"
        f"4. Ask me to describe my proposed change before writing any code."
    )


def _why_was_this_written(
    path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    qualified_name: str | None = None,
) -> str:
    """Recover the original intent behind a chunk of code.

    Parameters
    ----------
    path : str, optional
        Source file path, relative to the repo root.
    line_start : int, optional
        First line (1-indexed, inclusive) of the chunk.
    line_end : int, optional
        Last line (1-indexed, inclusive) of the chunk.
    qualified_name : str, optional
        Fully-qualified symbol name. Use instead of path / line_start /
        line_end when you know the symbol but not the lines.
    """
    target = _target_label(path, line_start, line_end, qualified_name)
    args = _target_args(path, line_start, line_end, qualified_name)
    return (
        f"Help me understand the intent behind {target}.\n"
        f"\n"
        f"Steps:\n"
        f"1. Call `whygraph_evidence_for` with: {args}. This returns the commits, "
        f"pull requests, and issues that own this code.\n"
        f"2. Read the PR and issue titles / bodies to recover the original "
        f"motivation.\n"
        f"3. If intent is still unclear, call `whygraph_rationale_brief` with the "
        f"same arguments to get a synthesized purpose/why card.\n"
        f"4. Tell the story: what was the original problem, how did the code "
        f"evolve to today's shape, and what context is no longer obvious from "
        f"reading the code alone."
    )


def _triage_commit(sha: str) -> str:
    """Summarize what a commit did and why.

    Parameters
    ----------
    sha : str
        Full or short commit SHA to triage. Must be a commit that
        ``whygraph scan`` has indexed.
    """
    return (
        f"Triage commit {sha}.\n"
        f"\n"
        f"Steps:\n"
        f"1. Read the resource `whygraph://commit/{sha}` for commit metadata "
        f"and the pull requests that contain it.\n"
        f"2. For each linked PR, read `whygraph://pr/<number>` to surface its "
        f"closing issues.\n"
        f"3. Summarize:\n"
        f"   - **What changed** — commit subject and `llm_description`.\n"
        f"   - **Who shipped it and why** — PR title and body.\n"
        f"   - **What it fixed** — closing-issue titles and bodies.\n"
        f"4. Flag concerns: missing PR link, missing issue link, or a PR / "
        f"issue body that looks unrelated to the diff."
    )


# ---- registration --------------------------------------------------------


def register(mcp: FastMCP) -> None:
    """Attach the three prompts to an MCP server."""
    mcp.prompt(
        name="whygraph_pre_edit_brief",
        title="Pre-edit brief",
        description=(
            "Before editing a chunk of code, gather its rationale and "
            "historical context so the planned edit can respect the "
            "constraints and avoid known risks."
        ),
    )(_pre_edit_brief)
    mcp.prompt(
        name="whygraph_why_was_this_written",
        title="Why was this written?",
        description=(
            "Recover the original intent behind a chunk of code from its "
            "commits, pull requests, and closing issues."
        ),
    )(_why_was_this_written)
    mcp.prompt(
        name="whygraph_triage_commit",
        title="Triage a commit",
        description=(
            "Summarize what a single commit did and why, using the linked "
            "pull request and closing issues."
        ),
    )(_triage_commit)
