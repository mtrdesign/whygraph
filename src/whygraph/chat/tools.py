"""The chat assistant's tool registry — specs plus dispatch.

Every tool here delegates to a function the MCP server or the Explorer
already calls. That is the whole point: the assistant's answers cannot
drift from the rest of WhyGraph, because there is one implementation per
capability and both surfaces are adapters over it. No MCP protocol
roundtrip is involved — these are plain in-process calls.

The seventeen tools span **five sources**, and the system prompt tells the
model which to reach for:

* **CodeGraph** answers *what the code is* — structure, relationships, and
  (via ``get_area_outline``) what a whole directory contains.
* **WhyGraph** answers *why it is that way* and *what has happened* —
  rationale, evidence, path history, and content search over the
  diff-derived commit descriptions.
* **The statistics tools** answer *how much* — aggregate-only SQL, fenced so
  neither can become a record reader. There are **two**, because there are two
  databases: :mod:`.stats_sql` over commit history and :mod:`.graph_stats_sql`
  over CodeGraph's index. No SQL in the first can reach the second.
* **``render_chart``** (see :mod:`.charts`) draws any aggregate either statistics
  tool computed. The model passes the ``chart_ref`` that result carried and names
  **columns of it** — it never retypes a value into a chart.
* **The file tools** supply ground-truth source (see :mod:`.files`).

Registry rules
--------------
* Every result is a **JSON string**, truncated at
  :data:`MAX_RESULT_CHARS` with an explicit marker, so one fat tool result
  cannot swamp the context window.
* **Tool errors never end the turn.** ``WhyGraphError``, ``CodeGraphError``
  and argument-validation failures come back as ``{"error": "..."}``
  results, which the model can read and route around.
* A :class:`ToolRegistry` is instantiated **once per user turn** because it
  carries the turn-scoped rationale-generation budget **and the chart-ref
  map**. The specs themselves are module-level constants — they never change.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Callable

from sqlalchemy.exc import SQLAlchemyError

from whygraph.core import get_config
from whygraph.mcp.area_history import whygraph_area_history
from whygraph.mcp.errors import WhyGraphError
from whygraph.mcp.evidence import collect_evidence, whygraph_evidence_for
from whygraph.mcp.path_history import path_commit_counts
from whygraph.mcp.rationale import _format_response, whygraph_rationale_brief
from whygraph.mcp.rationale_cache import lookup_cached
from whygraph.mcp.resources import (
    _commit_resource,
    _find_changes_resource,
    _issue_resource,
    _pr_resource,
    _recent_activity_resource,
    _repo_overview_resource,
)
from whygraph.mcp.targets import repo_root, resolve_target, target_dict
from whygraph.serve import graphdata
from whygraph.services.codegraph import CodeGraph, CodeGraphError
from whygraph.services.llm.chat import ToolSpec

from . import charts, files, graph_stats_sql, stats_sql

_log = logging.getLogger(__name__)

MAX_RESULT_CHARS = 30_000
"""Per-result truncation. Bounds context growth per tool round."""

TRUNCATION_MARKER = "...[truncated]"

_NO_CODEGRAPH = "CodeGraph index unavailable — run `whygraph scan`"
"""Mirrors the Explorer's 503 message (``serve/routes.py``), but as tool
content: the WhyGraph and file tools still work without an index, so a
missing index degrades the conversation rather than ending it."""


# ---------------------------------------------------------------------------
# Tool specs
# ---------------------------------------------------------------------------
#
# Descriptions are the model's only guidance, so each one carries its
# behavioural caveats (case-sensitivity, cost, default limits). Where the
# MCP layer already has an agent-facing description, it is reused verbatim.

_QUALIFIED_NAME_DESC = (
    "The EXACT qualified_name from an earlier result — never construct one. "
    "CodeGraph names are bare for module-level symbols ('run_turn'), "
    "'Class::method' for methods ('ToolRegistry::dispatch'), and a "
    "repo-relative path for file nodes ('src/whygraph/serve/chat.py'). A "
    "dotted path such as 'whygraph.chat.harness.run_turn' is NOT valid and "
    "will not resolve — call search_symbols first if you don't have the name."
)
"""Shared by the three symbol-keyed tools.

One constant rather than three copies because the copies drifted: all three
used to say "Dotted symbol name", which is the one shape CodeGraph does not
use for symbols. The model followed the description, every lookup missed,
and it burned tool rounds guessing variants.
"""

_DESCRIPTION_AUTHORITY = (
    " Each commit carries `llm_description`, generated from the DIFF ALONE — "
    "the developer's commit message is never shown to that generator. Treat it "
    "as the authoritative account of what changed. `subject` and `body` are "
    "human-written and are often terse, stale, or simply wrong; cite them for "
    "intent, not for fact."
)
"""Appended to every history tool's description.

The generator's only input is a diff (``LlmDescriptor.describe(diff)``), so a
nonsense commit message *cannot* contaminate the description — that is
architectural, not incidental. Nothing used to tell the model this, so it read
``subject`` and ``llm_description`` as equally authoritative and anchored on
whichever arrived first. One constant rather than five copies, for the same
reason :data:`_QUALIFIED_NAME_DESC` is one: the copies drift.
"""

_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="search_symbols",
        description=(
            "Find code symbols (functions, classes, files, modules) by name "
            "in the CodeGraph index. Matching is a CASE-SENSITIVE substring "
            "match on the symbol name — try 'runTurn' and 'run_turn' "
            "separately if unsure. Start here when you know roughly what a "
            "thing is called but not where it lives."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Substring to match."},
                "limit": {
                    "type": "integer",
                    "description": "Max results. Default 20.",
                },
            },
            "required": ["query"],
        },
    ),
    ToolSpec(
        name="get_symbol",
        description=(
            "Get one symbol's identity plus all its typed relationships: "
            "callers, callees, imports, its container, and its children. "
            "Works on FILE nodes too — pass a file's path as the qualified "
            "name to get its outline (the classes and functions it defines). "
            "Every related symbol in the result is itself a valid "
            "qualified_name for this tool, so repeated calls walk the graph."
        ),
        parameters={
            "type": "object",
            "properties": {
                "qualified_name": {
                    "type": "string",
                    "description": _QUALIFIED_NAME_DESC,
                }
            },
            "required": ["qualified_name"],
        },
    ),
    ToolSpec(
        name="get_area_outline",
        description=(
            "Outline the symbols in a directory (or one file) — classes, "
            "functions, methods, routes — grouped by file, with line ranges. "
            "THE way to orient yourself in a subsystem: prefer it over "
            "list_dir for code, because it returns structure rather than "
            "filenames, and every qualified_name it returns is a valid input "
            "to get_symbol, get_evidence, and get_rationale. Each file also "
            "carries how many commits have touched it, so you can see where "
            "the churn is. Signatures are omitted to keep this compact — call "
            "get_symbol for one symbol's full signature and relationships. A "
            "very large directory returns detail='files' (a per-file map of "
            "symbol and commit counts) instead of every symbol; re-call on a "
            "subdirectory to drill in. Note: only code is indexed "
            "(.py/.ts/.tsx/.js) — for markdown, TOML, or any other file, use "
            "list_dir and read_file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Repo-relative directory, e.g. 'src/whygraph/chat'. "
                        "A file path works too."
                    ),
                }
            },
            "required": ["path"],
        },
    ),
    ToolSpec(
        name="get_rationale",
        description=(
            "Get the structured rationale card for a symbol — purpose, why, "
            "constraints, tradeoffs, risks — synthesized from its commit / PR "
            "/ issue history. Returns a cached card instantly when one "
            "exists. If none exists, this GENERATES one, which calls an LLM "
            "and can take tens of seconds; generation is BUDGETED to a small "
            "number of calls per user turn, after which it returns "
            "status='not_generated'. So prefer it for the one or two symbols "
            "that genuinely matter, and use get_evidence or get_area_history "
            "when you just need raw history."
        ),
        parameters={
            "type": "object",
            "properties": {
                "qualified_name": {
                    "type": "string",
                    "description": _QUALIFIED_NAME_DESC,
                }
            },
            "required": ["qualified_name"],
        },
    ),
    ToolSpec(
        name="get_evidence",
        description=(
            "Get the raw historical evidence behind a symbol's lines: the "
            "commits that authored them (via blame), plus every linked pull "
            "request and issue. This is line-precise and HEAD-anchored. Use "
            "it to answer 'what changed here and why' without paying for "
            "rationale generation." + _DESCRIPTION_AUTHORITY
        ),
        parameters={
            "type": "object",
            "properties": {
                "qualified_name": {
                    "type": "string",
                    "description": _QUALIFIED_NAME_DESC,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max commits, newest first. Default 10.",
                },
            },
            "required": ["qualified_name"],
        },
    ),
    ToolSpec(
        name="get_area_history",
        description=(
            "List commits that ever touched a FILE PATH, including its "
            "rename predecessors. Complements get_evidence: this reaches "
            "commits for code that has since been deleted, moved, or fully "
            "rewritten, which line-blame physically cannot. Use it for "
            "'what has been happening in this area lately' questions."
            + _DESCRIPTION_AUTHORITY
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repo-relative file path.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max commits, newest first. Default 10.",
                },
                "include_renames": {
                    "type": "boolean",
                    "description": "Walk the rename chain. Default true.",
                },
            },
            "required": ["path"],
        },
    ),
    ToolSpec(
        name="find_changes",
        description=(
            "Search the commit history by WHAT CHANGED — keywords and/or a "
            "path — rather than by an identifier you already know. THE "
            "debugging entry point: a defect is described in the vocabulary of "
            "behaviour ('sessions vanish after a refresh', 'the dropdown "
            "resets'), and the diff descriptions are the only text written in "
            "that vocabulary. search_symbols cannot find these — it matches "
            "symbol NAMES only, so a behaviour spread across three files, or a "
            "property inside an object literal, is invisible to it. Keywords "
            "are AND-ed, case-insensitive substrings, matched against each "
            "commit's description / subject / body and against the TITLES of "
            "its pull requests. `path` accepts a DIRECTORY as well as a file "
            "(unlike get_area_history) and follows rename chains. Pass at "
            "least one of the two." + _DESCRIPTION_AUTHORITY
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Space-separated keywords, all of which must appear. "
                        "Prefer behavioural words over symbol names — this "
                        "searches prose, not code."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Repo-relative file OR directory to restrict to, e.g. "
                        "'src/whygraph/chat'."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max commits, newest first. Default 15, max 50.",
                },
            },
        },
    ),
    ToolSpec(
        name="get_commit",
        description=(
            "Get one commit by SHA — message, stats, author, and its linked "
            "pull requests. Use it to follow up on a SHA another tool "
            "surfaced." + _DESCRIPTION_AUTHORITY
        ),
        parameters={
            "type": "object",
            "properties": {"sha": {"type": "string", "description": "Commit SHA."}},
            "required": ["sha"],
        },
    ),
    ToolSpec(
        name="get_pr",
        description=(
            "Get one pull request by number — title, body, labels, review "
            "comments, and the issues it closes. PR discussion is usually "
            "where design intent was actually argued out."
        ),
        parameters={
            "type": "object",
            "properties": {"number": {"type": "integer", "description": "PR number."}},
            "required": ["number"],
        },
    ),
    ToolSpec(
        name="get_issue",
        description=(
            "Get one issue by number — title, body, labels, and the PRs that "
            "closed it. Issues carry the original problem statement."
        ),
        parameters={
            "type": "object",
            "properties": {
                "number": {"type": "integer", "description": "Issue number."}
            },
            "required": ["number"],
        },
    ),
    ToolSpec(
        name="get_repo_overview",
        description=(
            "Get repository-wide totals: commit / PR / issue counts, the date "
            "range of scanned history, when the last scan ran, how much of "
            "the history has LLM descriptions, and the top contributors. "
            "These SIX FIXED NUMBERS and nothing else — it has no per-file, "
            "per-month, or per-PR breakdown, so for hotspot files, churn, "
            "velocity over time, or cycle time use run_project_stats. For the "
            "actual recent work, use list_recent_activity."
        ),
        parameters={"type": "object", "properties": {}},
    ),
    ToolSpec(
        name="list_recent_activity",
        description=(
            "List the most recent commits, pull requests, and issues in one "
            "call, newest first. THE place to start for 'what changed / "
            "shipped / was worked on lately', 'summarize recent progress', "
            "or 'what has the team been doing' — every other history tool "
            "needs an identifier you would have to already know (a SHA, a PR "
            "number, an exact file path). Returns a compact index — subject "
            "or title, author, date, a truncated description — then use "
            "get_commit / get_pr / get_issue on the entries that matter."
            + _DESCRIPTION_AUTHORITY
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max entries per category. Default 10.",
                }
            },
        },
    ),
    ToolSpec(
        name="run_project_stats",
        description=stats_sql._SCHEMA_DOC,
        parameters={
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": (
                        "One SELECT (or WITH ... SELECT) statement that "
                        "aggregates. No trailing semicolon needed."
                    ),
                }
            },
            "required": ["sql"],
        },
    ),
    ToolSpec(
        name="run_graph_stats",
        description=graph_stats_sql._GRAPH_SCHEMA_DOC,
        parameters={
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": (
                        "One SELECT (or WITH ... SELECT) statement that "
                        "aggregates. No trailing semicolon needed."
                    ),
                }
            },
            "required": ["sql"],
        },
    ),
    ToolSpec(
        name="render_chart",
        description=(
            "Draw a chart from an aggregate you have ALREADY computed. Pass the "
            "`chart_ref` that run_project_stats or run_graph_stats returned, and "
            "name COLUMNS OF THAT RESULT — never retype the values. "
            "`line` for ordered buckets over time; `bar` for ranked categories "
            "with short labels; `bar_h` when labels are long (file paths, "
            "emails). Use `bar_stacked` (or `bar_h_stacked` for long labels) to "
            "break each bar down by a category — 'commits per month BY author', "
            "'changes per month BY change type': pass that category column as "
            "`series` and GROUP BY both columns in your SQL. Max 6 series. "
            "ONE y column always: two MEASURES means two charts, and a "
            "breakdown is `series`, not a second y. Chart only a series worth "
            "seeing — 3+ ordered or ranked rows. A single number needs no "
            "chart; just say it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "chart_ref": {
                    "type": "string",
                    "description": (
                        "The `chart_ref` from a stats result in this turn. Refs "
                        "expire with the turn — re-run the query for a fresh one."
                    ),
                },
                "kind": {
                    "type": "string",
                    "enum": sorted(charts.CHART_KINDS),
                },
                "title": {
                    "type": "string",
                    "description": (
                        "Shown above the chart. Required: an unstacked chart has "
                        "no legend, so this is its only label."
                    ),
                },
                "x": {
                    "type": "string",
                    "description": "Column name for the category/time axis.",
                },
                "y": {
                    "type": "string",
                    "description": "Column name for the measure. One only.",
                },
                "series": {
                    "type": "string",
                    "description": (
                        "Stacked kinds ONLY, and required for them: the column "
                        "whose values become the stack segments. Max 6 distinct "
                        "values — fold the tail into an 'other' bucket in your "
                        "SQL if there are more."
                    ),
                },
                "y_label": {
                    "type": "string",
                    "description": "Optional axis caption, e.g. 'commits'.",
                },
            },
            "required": ["chart_ref", "kind", "title", "x", "y"],
        },
    ),
    ToolSpec(
        name="read_file",
        description=(
            "Read source from the repository, with line numbers. Returns at "
            f"most {files.MAX_LINES} lines per call, so page through a large "
            "file with successive ranges. Read-only. Paths outside the repo, "
            "WhyGraph's own config and databases, and .env files are refused."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repo-relative file path.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "First line, 1-based. Default 1.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Last line, 1-based. Default: start + 400.",
                },
            },
            "required": ["path"],
        },
    ),
    ToolSpec(
        name="list_dir",
        description=(
            "List one directory's contents, non-recursively. Directories are "
            "marked with a trailing '/'. Use it for FILENAMES — markdown, "
            "TOML, config, or finding out what non-code files exist. To see "
            "what a code directory CONTAINS or get an overview of a subsystem "
            "or its modules, call get_area_outline instead: it returns the "
            "actual classes and functions with line ranges, and filenames "
            "alone rarely answer the question you asked."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repo-relative directory. Default '.'.",
                }
            },
        },
    ),
)

TOOL_SPECS: tuple[ToolSpec, ...] = _SPECS
"""The tools offered to the model, in the order they are presented."""


# ---------------------------------------------------------------------------
# CodeGraph-backed handlers
# ---------------------------------------------------------------------------


def _open_graph() -> CodeGraph:
    """Open a per-dispatch CodeGraph handle.

    Cheap (a read-only SQLite connection), so it follows the serve layer's
    per-request pattern rather than being held open across a whole turn —
    a chat turn can run for minutes and a background ``whygraph scan``
    may replace the index underneath it.
    """
    return CodeGraph.for_repository(repo_root(), codegraph_db=get_config().codegraph_db)


def _search_symbols(query: str, limit: int = 20) -> dict:
    """Handler for ``search_symbols``."""
    if not query:
        return {"error": "query is required"}
    with _open_graph() as graph:
        hits = graph.search(query, limit=max(1, limit))
    return {
        "query": query,
        "count": len(hits),
        "symbols": [graphdata._symbol_dict(s) for s in hits],
    }


def _get_symbol(qualified_name: str) -> dict:
    """Handler for ``get_symbol``."""
    if not qualified_name:
        return {"error": "qualified_name is required"}
    with _open_graph() as graph:
        symbol = graph.symbol(qualified_name)
        if symbol is None:
            return {"error": f"{qualified_name!r} not found in CodeGraph"}
        return {
            "symbol": graphdata._symbol_dict(symbol),
            "relations": graphdata.node_relations(graph, symbol),
        }


_OUTLINE_SYMBOL_LIMIT = 150
"""Above this many symbols the outline returns a file-level map instead.

Calibrated against **serialized** cost, which is ~130 chars per symbol on this
repo (worst observed 161, in ``tests/``) — the six short field names repeat per
row and dominate the values they label. So 150 symbols lands near 19,500
typical / 24,150 worst case, against a :data:`MAX_RESULT_CHARS` of 30,000.

A values-only estimate suggests ~38 chars per symbol and a limit of 500; that
is wrong by 3.4x because it omits the repeated keys. Measured: 500 symbols is
~65,000 chars, and ``src/whygraph/services`` alone (241 symbols) serializes to
31,011 — already over the cap. Any future change to the emitted field set must
re-measure this, not scale it.
"""

_OUTLINE_MAP_HINT = (
    "Too large for a symbol outline — re-call on a subdirectory to drill in."
)
"""Load-bearing: a truncated list hides what was missed, whereas a map plus
this sentence is a complete answer at coarser resolution *and* tells the model
where to look next."""


_OUTLINE_DROPPED_FIELDS = ("id", "signature", "file_path")
"""Fields of :func:`graphdata._symbol_dict` an outline row omits.

``signature`` is ~75% of an outline's payload and :func:`_get_symbol` already
supplies it on demand. ``id`` is a CodeGraph node id the model never needs.
``file_path`` is the key of the ``files`` map the row already sits under, so
repeating it per symbol costs ~27% of the payload to say nothing new.
"""


def _outline_symbol_dict(symbol) -> dict:  # noqa: ANN001 -- codegraph Symbol
    """One outline row — :func:`graphdata._symbol_dict` minus the redundant fields.

    See :data:`_OUTLINE_DROPPED_FIELDS`. Between them they are what lets a whole
    package fit in one tool result.
    """
    row = graphdata._symbol_dict(symbol)
    for field in _OUTLINE_DROPPED_FIELDS:
        row.pop(field, None)
    return row


def _commit_counts(path: str) -> dict[str, int] | None:
    """Per-file commit counts, or ``None`` when WhyGraph history is unreachable.

    ``None`` and ``{}`` mean different things and the caller renders them
    differently: an empty mapping is "scanned, and these files have no commits"
    (each file reports ``0``), whereas ``None`` is "no WhyGraph DB" and the
    ``commit_count`` key is omitted entirely. The structural outline is useful
    on its own, so a missing DB must degrade rather than fail the call.
    """
    try:
        return path_commit_counts(path)
    except (WhyGraphError, SQLAlchemyError) as exc:
        _log.debug("area outline: commit counts unavailable for %r: %s", path, exc)
        return None


def _get_area_outline(path: str) -> dict:
    """Handler for ``get_area_outline``."""
    if not path or not path.strip():
        return {"error": "path is required"}
    with _open_graph() as graph:
        symbols = graph.area(path)
    if not symbols:
        return {
            "path": path,
            "detail": "symbols",
            "symbol_count": 0,
            "files": {},
            "note": (
                "Nothing indexed under this path. CodeGraph indexes code only "
                "(.py/.ts/.tsx/.js) — use list_dir for anything else."
            ),
        }

    counts = _commit_counts(path)

    def _file_entry(file_path: str) -> dict:
        """The per-file preamble both shapes share, counts-first."""
        if counts is None:
            return {}
        return {"commit_count": counts.get(file_path, 0)}

    if len(symbols) > _OUTLINE_SYMBOL_LIMIT:
        per_file: dict[str, int] = {}
        for symbol in symbols:
            per_file[symbol.file_path] = per_file.get(symbol.file_path, 0) + 1
        return {
            "path": path,
            "detail": "files",
            "symbol_count": len(symbols),
            "hint": _OUTLINE_MAP_HINT,
            "files": {
                file_path: {"symbol_count": count, **_file_entry(file_path)}
                for file_path, count in per_file.items()
            },
        }

    files: dict[str, dict] = {}
    # ``area()`` orders by (file_path, start_line), so grouping is one pass and
    # each file's symbols come out in source order.
    for symbol in symbols:
        entry = files.get(symbol.file_path)
        if entry is None:
            entry = {**_file_entry(symbol.file_path), "symbols": []}
            files[symbol.file_path] = entry
        entry["symbols"].append(_outline_symbol_dict(symbol))
    return {
        "path": path,
        "detail": "symbols",
        "symbol_count": len(symbols),
        "files": files,
    }


# ---------------------------------------------------------------------------
# WhyGraph-backed handlers
# ---------------------------------------------------------------------------


def _get_evidence(qualified_name: str, limit: int = 10) -> dict:
    """Handler for ``get_evidence``."""
    if not qualified_name:
        return {"error": "qualified_name is required"}
    return whygraph_evidence_for(qualified_name=qualified_name, limit=max(1, limit))


def _get_area_history(path: str, limit: int = 10, include_renames: bool = True) -> dict:
    """Handler for ``get_area_history``."""
    return whygraph_area_history(
        path=path, limit=max(1, limit), include_renames=include_renames
    )


def _find_changes(
    query: str | None = None, path: str | None = None, limit: int = 15
) -> dict:
    """Handler for ``find_changes``."""
    return _find_changes_resource(query=query, path=path, limit=limit)


def _get_commit(sha: str) -> dict:
    """Handler for ``get_commit``."""
    if not sha:
        return {"error": "sha is required"}
    return _commit_resource(sha)


def _get_pr(number: int) -> dict:
    """Handler for ``get_pr``."""
    return _pr_resource(int(number))


def _get_issue(number: int) -> dict:
    """Handler for ``get_issue``."""
    return _issue_resource(int(number))


def _get_repo_overview() -> dict:
    """Handler for ``get_repo_overview``."""
    return _repo_overview_resource()


def _list_recent_activity(limit: int = 10) -> dict:
    """Handler for ``list_recent_activity``."""
    return _recent_activity_resource(limit=int(limit))


def _run_project_stats(sql: str) -> dict:
    """Handler body for ``run_project_stats`` — no ref minting.

    Kept a plain function because charting is not this tool's concern: the
    registry mints the ``chart_ref`` (see
    :meth:`ToolRegistry._chartable_project_stats`), so a producer stays a
    producer and a fourth one is a one-line addition.
    """
    if not sql or not sql.strip():
        return {"error": "sql is required", "layer": "shape"}
    return stats_sql.run_stats_query(sql)


def _run_graph_stats(sql: str) -> dict:
    """Handler body for ``run_graph_stats``. See :func:`_run_project_stats`."""
    if not sql or not sql.strip():
        return {"error": "sql is required", "layer": "shape"}
    return graph_stats_sql.run_graph_query(sql)


class ToolRegistry:
    """One turn's tool dispatch, carrying that turn's generation budget.

    Parameters
    ----------
    max_rationale_generations : int, optional
        How many uncached ``get_rationale`` targets this turn may generate.
        ``None`` (default) reads ``[chat].max_rationale_generations``.
        ``0`` legitimately disables generation, making the tool cache-only.

    Attributes
    ----------
    specs : tuple[ToolSpec, ...]
        The tools to offer the model — always :data:`TOOL_SPECS`.
    generations_used : int
        How many rationale generations this instance has spent.

    Notes
    -----
    Instantiate one per **user turn**, not per tool round: the budget is
    what stops a single question fanning out into N nested LLM calls, so it
    must span the turn's whole tool loop.
    """

    def __init__(self, *, max_rationale_generations: int | None = None) -> None:
        if max_rationale_generations is None:
            max_rationale_generations = get_config().chat.max_rationale_generations
        self._generation_budget = max_rationale_generations
        self.generations_used = 0
        self._chartable: dict[str, dict] = {}
        self._handlers: dict[str, Callable[..., dict]] = {
            "search_symbols": _search_symbols,
            "get_symbol": _get_symbol,
            "get_area_outline": _get_area_outline,
            "get_rationale": self._get_rationale,
            "get_evidence": _get_evidence,
            "get_area_history": _get_area_history,
            "find_changes": _find_changes,
            "get_commit": _get_commit,
            "get_pr": _get_pr,
            "get_issue": _get_issue,
            "get_repo_overview": _get_repo_overview,
            "list_recent_activity": _list_recent_activity,
            "run_project_stats": self._chartable_project_stats,
            "run_graph_stats": self._chartable_graph_stats,
            "render_chart": self._render_chart,
            "read_file": files.read_file,
            "list_dir": files.list_dir,
        }

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """The tool specs to send with each :class:`ChatRequest`."""
        return TOOL_SPECS

    # -- charting -----------------------------------------------------------
    #
    # Any producer of an aggregate joins by returning `{columns, rows}` and
    # calling `_mint_chart_ref`. `render_chart`, `charts.py`, and the frontend
    # need no change for a new one — that is what makes charting a capability
    # rather than a parameter on one tool.

    def _mint_chart_ref(self, result: dict) -> dict:
        """Attach an opaque per-turn ref to a chartable aggregate result.

        Returns the same dict, mutated, so a producer is one line.

        Notes
        -----
        **No ref is minted** for a result that errored, was ``truncated``, or has
        fewer than :data:`charts.MIN_CHART_ROWS` rows. Withholding it beats
        refusing later: the model never sees an affordance it would be wrong to
        use, and a chart drawn from a capped result would be a confident lie —
        the tool cannot know the true total.

        The ref is ``secrets.token_hex``, not a counter and not the
        ``tool_call_id``: opaque per MCP's handle guidance, and unguessable, so
        it cannot address anything the model did not just compute.
        """
        if (
            "error" in result
            or result.get("truncated")
            or len(result.get("rows") or ()) < charts.MIN_CHART_ROWS
        ):
            return result
        ref = f"cr_{secrets.token_hex(4)}"
        self._chartable[ref] = {
            "columns": result["columns"],
            "rows": result["rows"],
        }
        result["chart_ref"] = ref
        result["chartable"] = "Pass chart_ref to render_chart to draw this."
        return result

    def _chartable_project_stats(self, sql: str) -> dict:
        """Handler for ``run_project_stats`` — the aggregate, plus a ref."""
        return self._mint_chart_ref(_run_project_stats(sql))

    def _chartable_graph_stats(self, sql: str) -> dict:
        """Handler for ``run_graph_stats`` — the aggregate, plus a ref."""
        return self._mint_chart_ref(_run_graph_stats(sql))

    def _render_chart(
        self,
        chart_ref: str,
        kind: str,
        title: str,
        x: str,
        y: str,
        series: str | None = None,
        y_label: str | None = None,
    ) -> dict:
        """Handler for ``render_chart`` — validate a directive against its rows.

        Returns
        -------
        dict
            ``{"chart_ref", "chart", "columns", "row_count"}`` on success.
            **The rows are not echoed**: the frontend already has them from the
            producer's result and correlates on ``chart_ref``, so this payload is
            a couple of hundred bytes rather than a second copy of 200 rows.

            On failure, ``{"error", "layer"}`` — ``layer="ref"`` for a stale or
            invented ref, ``layer="chart"`` for a directive the rows do not
            support. Either way the producer's numbers are already on screen and
            unaffected, so a bad chart degrades to correct numbers.
        """
        source = self._chartable.get(chart_ref)
        if source is None:
            return {
                "error": (
                    f"unknown chart_ref {chart_ref!r} — refs are valid only "
                    "within the turn that produced them. Re-run the query to get "
                    "a fresh one."
                ),
                "layer": "ref",
            }
        try:
            chart = charts.validate_chart(
                kind=kind,
                title=title,
                x=x,
                y=y,
                series=series,
                y_label=y_label,
                **source,
            )
        except charts.ChartNotAllowed as exc:
            return {"error": str(exc), "layer": "chart"}
        return {
            "chart_ref": chart_ref,
            "chart": chart,
            "columns": source["columns"],
            "row_count": len(source["rows"]),
        }

    def _get_rationale(self, qualified_name: str) -> dict:
        """Handler for ``get_rationale`` — cached read, budgeted generation.

        A cache hit is the same LLM-free flow the Explorer's Rationale tab
        uses. A miss runs :func:`whygraph_rationale_brief` **verbatim**
        (imported, not reimplemented) so the card and the cache row it
        writes are byte-identical to the MCP tool's — the Explorer then
        shows that same card as cached.

        Generation deliberately uses the ``[rationale]`` provider/model,
        not the chat session's: the cache key includes provider and model,
        so generating under the chat provider would write a row the
        Explorer and MCP would still see as missing.
        """
        if not qualified_name:
            return {"error": "qualified_name is required"}

        target = resolve_target(
            path=None, line_start=None, line_end=None, qualified_name=qualified_name
        )
        evidence = collect_evidence(target, limit=20)
        if not evidence:
            return {"status": "no_evidence", "target": target_dict(target)}

        config = get_config().rationale
        cached = lookup_cached(target, evidence, config.provider, config.model)
        if cached is not None:
            rationale, cached_at = cached
            return {
                "status": "cached",
                "generated": False,
                **_format_response(target, rationale, evidence, cached_at),
            }

        if self.generations_used >= self._generation_budget:
            return {
                "status": "not_generated",
                "note": "generation budget exhausted this turn",
                "target": target_dict(target),
            }

        self.generations_used += 1
        _log.info(
            "chat generating rationale for %s (%d/%d this turn)",
            qualified_name,
            self.generations_used,
            self._generation_budget,
        )
        card = whygraph_rationale_brief(qualified_name=qualified_name)
        return {"status": "cached", "generated": True, **card}

    def dispatch(self, name: str, arguments: dict) -> str:
        """Run one tool call and return its JSON-serialized result.

        Never raises for a *tool-level* problem: an unknown name, bad
        arguments, a WhyGraph error, or a missing CodeGraph index all come
        back as an ``{"error": ...}`` payload so the model can recover
        inside the same turn.

        Parameters
        ----------
        name : str
            Tool name. Must be in the registry — it is a closed allow-list.
        arguments : dict
            Parsed arguments, as the port produced them.

        Returns
        -------
        str
            JSON, truncated to :data:`MAX_RESULT_CHARS` with
            :data:`TRUNCATION_MARKER` appended when it was too long.
        """
        handler = self._handlers.get(name)
        if handler is None:
            return _encode({"error": f"unknown tool {name!r}"})

        try:
            result = handler(**arguments)
        except TypeError as exc:
            # Wrong / missing / unexpected argument names.
            result = {"error": f"invalid arguments for {name}: {exc}"}
        except CodeGraphError as exc:
            _log.debug("chat tool %s: no codegraph: %s", name, exc)
            result = {"error": f"{_NO_CODEGRAPH}: {exc}"}
        except WhyGraphError as exc:
            _log.debug("chat tool %s: %s", name, exc)
            result = {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 -- a tool must never kill the turn
            _log.exception("chat tool %s failed unexpectedly", name)
            result = {"error": f"{name} failed: {type(exc).__name__}: {exc}"}

        return _encode(result)


def _encode(result: object) -> str:
    """JSON-serialize ``result``, truncating past :data:`MAX_RESULT_CHARS`."""
    try:
        text = json.dumps(result, default=str)
    except (TypeError, ValueError) as exc:  # pragma: no cover -- default=str covers it
        text = json.dumps({"error": f"result not serializable: {exc}"})
    if len(text) > MAX_RESULT_CHARS:
        return text[:MAX_RESULT_CHARS] + TRUNCATION_MARKER
    return text


__all__ = [
    "MAX_RESULT_CHARS",
    "TOOL_SPECS",
    "TRUNCATION_MARKER",
    "ToolRegistry",
]
