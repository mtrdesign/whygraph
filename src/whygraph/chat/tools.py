"""The chat assistant's tool registry — specs plus dispatch.

Every tool here delegates to a function the MCP server or the Explorer
already calls. That is the whole point: the assistant's answers cannot
drift from the rest of WhyGraph, because there is one implementation per
capability and both surfaces are adapters over it. No MCP protocol
roundtrip is involved — these are plain in-process calls.

The twelve tools span **three sources**, and the system prompt tells the
model which to reach for:

* **CodeGraph** answers *what the code is* — structure, relationships.
* **WhyGraph** answers *why it is that way* — rationale, evidence, history.
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
  carries the turn-scoped rationale-generation budget. The specs
  themselves are module-level constants — they never change.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from whygraph.core import get_config
from whygraph.mcp.area_history import whygraph_area_history
from whygraph.mcp.errors import WhyGraphError
from whygraph.mcp.evidence import collect_evidence, whygraph_evidence_for
from whygraph.mcp.rationale import _format_response, whygraph_rationale_brief
from whygraph.mcp.rationale_cache import lookup_cached
from whygraph.mcp.resources import (
    _commit_resource,
    _issue_resource,
    _pr_resource,
    _recent_activity_resource,
    _repo_overview_resource,
)
from whygraph.mcp.targets import repo_root, resolve_target, target_dict
from whygraph.serve import graphdata
from whygraph.services.codegraph import CodeGraph, CodeGraphError
from whygraph.services.llm.chat import ToolSpec

from . import files

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
            "rationale generation."
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
        name="get_commit",
        description=(
            "Get one commit by SHA — message, stats, author, and its linked "
            "pull requests. Use it to follow up on a SHA another tool "
            "surfaced."
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
            "Counts and coverage only — for the actual recent work, use "
            "list_recent_activity."
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
            "marked with a trailing '/'. Use it to orient yourself before "
            "reading files."
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
        self._handlers: dict[str, Callable[..., dict]] = {
            "search_symbols": _search_symbols,
            "get_symbol": _get_symbol,
            "get_rationale": self._get_rationale,
            "get_evidence": _get_evidence,
            "get_area_history": _get_area_history,
            "get_commit": _get_commit,
            "get_pr": _get_pr,
            "get_issue": _get_issue,
            "get_repo_overview": _get_repo_overview,
            "list_recent_activity": _list_recent_activity,
            "read_file": files.read_file,
            "list_dir": files.list_dir,
        }

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """The tool specs to send with each :class:`ChatRequest`."""
        return TOOL_SPECS

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
