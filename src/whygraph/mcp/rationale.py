"""The ``whygraph_rationale_brief`` MCP tool.

Gathers the historical evidence for a code chunk (reusing
:func:`whygraph.mcp.evidence.collect_evidence`), optionally enriches it
with the target symbol's CodeGraph context, and asks the configured LLM to
synthesize a structured rationale card.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from whygraph.analyze import AnalyzeError, RationaleGenerator
from whygraph.core import get_config
from whygraph.services.codegraph import CodeGraph, CodeGraphError, SymbolContext
from whygraph.services.llm import LlmError

from .errors import WhyGraphError
from .targets import Target, repo_root, resolve_target, target_dict
from .evidence import collect_evidence

_TOOL_DESCRIPTION = (
    "Generate a structured rationale card (purpose / why / constraints / "
    "tradeoffs / risks) explaining why a chunk of code exists. Gathers "
    "historical evidence (commits, PRs, issues) for the target, optionally "
    "enriches it with CodeGraph symbol context, and asks the configured LLM "
    "to synthesize the card. Pass either (path, line_start, line_end) or a "
    "qualified_name. Calls the configured LLM provider — may take several "
    "seconds. Run `whygraph scan` first to populate the WhyGraph database."
)


def _symbol_context(target: Target) -> SymbolContext | None:
    """CodeGraph context for ``target``, or ``None``.

    Only symbol-name targets carry graph context; a path/line target has no
    symbol to resolve. A missing or broken CodeGraph DB degrades to
    ``None`` rather than failing the whole rationale.
    """
    if target.qualified_name is None:
        return None
    try:
        with CodeGraph.for_repository(
            repo_root(), codegraph_db=get_config().codegraph_db
        ) as graph:
            return graph.context(target.qualified_name)
    except CodeGraphError:
        return None


def whygraph_rationale_brief(
    path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    qualified_name: str | None = None,
) -> dict:
    """MCP tool — a rationale card for a chunk of code.

    See :data:`_TOOL_DESCRIPTION` for the agent-facing summary.
    """
    target = resolve_target(
        path=path,
        line_start=line_start,
        line_end=line_end,
        qualified_name=qualified_name,
    )
    evidence = collect_evidence(target, limit=20)
    if not evidence:
        raise WhyGraphError(
            "no historical evidence for this target — the lines map to no "
            "scanned commit. Run `whygraph scan` to populate the database."
        )

    try:
        generator = RationaleGenerator.from_config(get_config().rationale)
        rationale = generator.generate(evidence, symbol_context=_symbol_context(target))
    except (AnalyzeError, LlmError) as exc:
        raise WhyGraphError.wrap("rationale generation failed", exc)

    return {
        "target": target_dict(target),
        "purpose": rationale.purpose,
        "why": rationale.why,
        "constraints": list(rationale.constraints),
        "tradeoffs": list(rationale.tradeoffs),
        "risks": list(rationale.risks),
        "model": rationale.model,
        "provider": rationale.provider,
        "evidence_count": {
            "commits": len(evidence),
            "prs": sum(len(item.pull_requests) for item in evidence),
            "issues": sum(len(item.issues) for item in evidence),
        },
    }


def register(mcp: FastMCP) -> None:
    """Attach the rationale tool to an MCP server."""
    mcp.tool(name="whygraph_rationale_brief", description=_TOOL_DESCRIPTION)(
        whygraph_rationale_brief
    )
