"""The ``whygraph_rationale_brief`` MCP tool.

Gathers the historical evidence for a code chunk (reusing
:func:`whygraph.mcp.evidence.collect_evidence`), optionally enriches it
with the target symbol's CodeGraph context, and asks the configured LLM to
synthesize a structured rationale card.
"""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

from whygraph.analyze import AnalyzeError, RationaleGenerator
from whygraph.core import get_config
from whygraph.core.context import current_project
from whygraph.core.remote import RemoteError, RemoteProject, platform_block
from whygraph.core.usage import BUDGET_EXCEEDED, budget_message, usage_blocked
from whygraph.services.codegraph import CodeGraph, CodeGraphError, SymbolContext
from whygraph.services.git import Repository
from whygraph.services.llm import LlmError

from whygraph.analyze import CommitEvidence, Rationale

from .errors import WhyGraphError, log_tool_errors
from .targets import Target, repo_root, resolve_target, target_dict
from .evidence import (
    backfill_evidence_descriptions,
    blame_target,
    collect_evidence,
    pushed_target,
    split_by_push_status,
)
from .rationale_cache import lookup_cached, store_cached

_TOOL_DESCRIPTION = (
    "Generate a structured rationale card (purpose / why / constraints / "
    "tradeoffs / risks) explaining why a chunk of code exists. Gathers "
    "historical evidence (commits, PRs, issues) for the target, optionally "
    "enriches it with CodeGraph symbol context, and asks the configured LLM "
    "to synthesize the card. Pass either (path, line_start, line_end) or a "
    "qualified_name. Calls the configured LLM provider — may take several "
    "seconds. Scan from the WhyGraph portal (or run `whygraph scan` outside it) "
    "first to populate the WhyGraph database."
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


def _format_response(
    target: Target,
    rationale: Rationale,
    evidence: list[CommitEvidence],
    cached_at: str,
) -> dict:
    """Shape the MCP response payload around a (fresh or cached) rationale."""
    return {
        "target": target_dict(target),
        "purpose": rationale.purpose,
        "why": rationale.why,
        "constraints": list(rationale.constraints),
        "tradeoffs": list(rationale.tradeoffs),
        "risks": list(rationale.risks),
        "model": rationale.model,
        "provider": rationale.provider,
        "cached_at": cached_at,
        "evidence_count": {
            "commits": len(evidence),
            "prs": sum(len(item.pull_requests) for item in evidence),
            "issues": sum(len(item.issues) for item in evidence),
        },
    }


class NoEvidenceError(WhyGraphError):
    """A rationale was asked for lines that map to no scanned commit."""


class RationaleGenerationError(WhyGraphError):
    """The LLM could not generate a card; ``__cause__`` is the ``AnalyzeError`` / ``LlmError``."""


_GENERATION_NOT_PERMITTED_MESSAGE = (
    "no rationale card is cached for this target, and your role on this "
    "project (viewer) cannot generate one; ask a contributor or admin"
)


class GenerationNotPermitted(WhyGraphError):
    """A card is not cached and the caller may not generate one.

    Raised by :func:`rationale_card` on a cache miss when the bound
    project context has ``llm_allowed`` false - a project viewer (M2f-1
    plan section 4.6) or an exhausted hard-stopped budget (M2f-2 plan
    section 4.7) - or when the bound usage sink reports a budget exhausted
    mid-request, before anything is spent or any budget is charged.

    Parameters
    ----------
    message : str, optional
        The message; each ``reason`` has its own default.
    reason : str, optional
        ``"role"`` (the default, a viewer) or ``"budget_exceeded"``.
    scope : str, optional
        With ``"budget_exceeded"``: ``"org"``, ``"project"`` or ``"member"``.

    Attributes
    ----------
    reason : str
        As passed.
    scope : str or None
        As passed.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        reason: str = "role",
        scope: str | None = None,
    ) -> None:
        if message is None:
            message = (
                "no rationale card is cached for this target, and "
                + budget_message(scope)
                if reason == BUDGET_EXCEEDED
                else _GENERATION_NOT_PERMITTED_MESSAGE
            )
        super().__init__(message)
        self.reason = reason
        self.scope = scope


_NO_EVIDENCE_MESSAGE = (
    "no historical evidence for this target — the lines map to no "
    "scanned commit. Scan from the WhyGraph portal (or run `whygraph scan` "
    "outside it) to populate the database."
)

_NOT_PUSHED_MESSAGE = (
    "no pushed history for this target — its lines are uncommitted or on "
    "commits no `origin/*` ref holds yet, and a linked project's rationale "
    "is generated on the platform. Push the commits, then ask again; "
    "`whygraph_evidence_for` meanwhile labels each line's push status."
)


def linked_rationale(remote: RemoteProject, target: Target) -> dict:
    """``whygraph_rationale_brief``'s answer for a project linked to a platform.

    The card is built, cached and paid for **on the platform** (M2e plan
    section 4.5), so every checkout of the repository shares one cache row:
    nothing here reads the local rationale cache or calls an LLM. Only the
    pushed hunks are sent, and with none there is no request at all.

    Parameters
    ----------
    remote : RemoteProject
        The project's platform.
    target : Target
        The locally resolved target.

    Returns
    -------
    dict
        The platform's card plus a ``platform`` block. Its ``target`` is
        the platform's own range for the symbol, which is what the card
        describes.

    Raises
    ------
    NoEvidenceError
        Nothing about this target may leave the machine yet.
    WhyGraphError
        The platform could not be reached, or refused.
    """
    repo = Repository(repo_root())
    split = split_by_push_status(repo, blame_target(repo, target))
    sendable = pushed_target(repo, target, split)
    if sendable is None:
        raise NoEvidenceError(_NOT_PUSHED_MESSAGE)
    try:
        card = remote.rationale(sendable, split.pushed)
    except RemoteError as exc:
        raise WhyGraphError(str(exc)) from exc
    return {**card, "platform": platform_block(remote)}


def rationale_card(
    target: Target,
    evidence: list[CommitEvidence],
    *,
    before_generate: Callable[[], None] | None = None,
    allow_description: Callable[[], bool] | None = None,
) -> dict:
    """The rationale card of ``target`` from ``evidence``: cached, or generated and cached.

    The half of :func:`whygraph_rationale_brief` after the target is
    resolved and its evidence collected, shared with the platform's
    ``POST /api/v1/projects/{slug}/rationale``.

    Parameters
    ----------
    target : Target
        The resolved code chunk (its path and range key the cache).
    evidence : list[CommitEvidence]
        Its evidence (the SHAs fingerprint the cache row).
    before_generate : callable, optional
        Called on a cache miss before anything is spent (the platform's
        org card budget raises from it).
    allow_description : callable, optional
        The description backfill's budget (see
        :func:`~whygraph.mcp.evidence.backfill_evidence_descriptions`).

    Returns
    -------
    dict
        The card (:func:`_format_response`).

    Raises
    ------
    NoEvidenceError
        ``evidence`` is empty.
    GenerationNotPermitted
        A cache miss while the bound project context has ``llm_allowed``
        false (``reason`` from its ``llm_block``) or while the bound usage
        sink reports an exhausted hard-stopped budget
        (``reason="budget_exceeded"``) - raised before ``before_generate``,
        so no budget is charged.
    RationaleGenerationError
        The generator failed.
    """
    if not evidence:
        raise NoEvidenceError(_NO_EVIDENCE_MESSAGE)

    config = get_config()
    provider, pinned_model = config.cache_identity("rationale")
    cached = lookup_cached(target, evidence, provider, pinned_model)
    if cached is not None:
        rationale, cached_at = cached
        return _format_response(target, rationale, evidence, cached_at)

    ctx = current_project()
    if ctx is not None and not ctx.llm_allowed:
        raise GenerationNotPermitted(
            reason=ctx.llm_block or "role", scope=ctx.llm_block_scope
        )
    # A budget exhausted during this request (a chat tool's card after the
    # turn's earlier rounds spent it): the bound sink knows, the context not.
    blocked = usage_blocked()
    if blocked is not None:
        raise GenerationNotPermitted(reason=BUDGET_EXCEEDED, scope=blocked)
    if before_generate is not None:
        before_generate()
    # Cache miss — lazily backfill any commit whose `llm_description` is
    # NULL (e.g. after `whygraph scan --skip-analyze`) so the
    # rationale prompt sees the richer per-commit summaries. Bulk commits
    # are described per-file against the target's path instead. The cache
    # fingerprint is sha256-over-sorted-SHAs, so backfilling here does
    # not affect cache keys.
    backfill_evidence_descriptions(
        evidence, target_path=target.path, allow=allow_description
    )

    try:
        generator = RationaleGenerator.from_config(config)
        rationale = generator.generate(
            evidence,
            symbol_context=_symbol_context(target),
            subject=target.qualified_name or target.path,
        )
    except (AnalyzeError, LlmError) as exc:
        raise RationaleGenerationError.wrap("rationale generation failed", exc)

    cached_at = store_cached(target, evidence, rationale, provider, pinned_model)
    return _format_response(target, rationale, evidence, cached_at)


def whygraph_rationale_brief(
    path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    qualified_name: str | None = None,
) -> dict:
    """MCP tool — a rationale card for a chunk of code.

    See :data:`_TOOL_DESCRIPTION` for the agent-facing summary.

    A previously generated card is returned from the SQLite-backed cache
    (see :mod:`whygraph.mcp.rationale_cache`) when the same target,
    provider, model, and evidence fingerprint are all unchanged.

    For a project linked to a WhyGraph platform the card comes from
    :func:`linked_rationale` instead, out of the platform's shared cache.
    """
    target = resolve_target(
        path=path,
        line_start=line_start,
        line_end=line_end,
        qualified_name=qualified_name,
    )
    ctx = current_project()
    if ctx is not None and ctx.remote is not None:
        return linked_rationale(ctx.remote, target)
    evidence = collect_evidence(target, limit=20)
    return rationale_card(target, evidence)


def register(mcp: FastMCP) -> None:
    """Attach the rationale tool to an MCP server."""
    mcp.tool(name="whygraph_rationale_brief", description=_TOOL_DESCRIPTION)(
        log_tool_errors(whygraph_rationale_brief)
    )
