"""The ``whygraph_rationale_brief`` MCP tool.

Gathers the historical evidence for a code chunk (reusing
:func:`whygraph.mcp.evidence.collect_evidence`), optionally enriches it
with the target symbol's CodeGraph context, and asks the configured LLM to
synthesize a structured rationale card.
"""

from __future__ import annotations

import dataclasses
import functools
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

import anyio.from_thread
from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from whygraph.analyze import AnalyzeError, RationaleGenerator, card_bundle_for
from whygraph.analyze.card_bundle import (
    DEFAULT_LEVEL,
    LEVELS,
    LINE_LEVEL_SOURCES,
    UNVERIFIED_LEVEL,
    CardBundle,
    verifies,
)
from whygraph.analyze.rationale_generator import (
    verification_source,
    with_verification,
)
from whygraph.core import get_config
from whygraph.core.context import current_project
from whygraph.core.remote import RemoteError, RemoteProject, platform_block
from whygraph.core.usage import BUDGET_EXCEEDED, budget_message, usage_blocked
from whygraph.db import get_session
from whygraph.db.models import Commit, PullRequest
from whygraph.services.codegraph import CodeGraph, CodeGraphError, SymbolContext
from whygraph.services.git import Repository
from whygraph.services.llm import LlmError

from whygraph.analyze import CommitEvidence, Rationale

from .descriptions import (
    RATIONALE_BRIEF_ANNOTATIONS,
    RATIONALE_BRIEF_DESCRIPTION,
)
from .notes import NoteView, notes_for_target
from .plans import attach_linked_plans, card_plan_shas, guarded_plans_for
from .structured import structured
from .errors import WhyGraphError, log_tool_errors
from .targets import Target, repo_root, resolve_target, target_dict
from .evidence import (
    DescribeOutcome,
    ReviewScope,
    _db_key,
    _linked_issues,
    _linked_prs,
    _spawn,
    _stop_event,
    backfill_evidence_descriptions,
    blame_target,
    collect_evidence,
    pushed_target,
    review_scope,
    split_by_push_status,
)
from .rationale_depth import (
    SERVED_CACHED,
    SERVED_KEPT,
    SERVED_UNVERIFIED,
    DepthResolution,
    Intent,
    OfferedDepth,
    deeper,
    depth_block,
    effective_ceiling,
    resolve_for_target,
)
from .rationale_cache import (
    CachedCard,
    _now_iso,
    depth_of,
    level_of,
    lookup_card,
    lookup_exact,
    lookup_overlapping,
    store_cached,
    update_deferred,
)

_log = logging.getLogger(__name__)

PROGRESS_STEPS = 3
"""The rationale tool's progress steps: evidence, describing, generating."""

COMPLETIONS_MAX = 2
"""Background completions of partial cards running at once, process-wide."""

CARD_RETRY_COOLDOWN_SEC = 600
"""Seconds before a partial card's completion is attempted again (plan 49 I17)."""

_CompletionKey = tuple[Path, str, int, int, str, str | None, int | None]
"""``(db_key, path, line_start, line_end, provider, model, depth)``: one card of one project.

``depth`` is the **row's** (``None`` for a legacy row), never the requested one.
"""

_completions_lock = threading.Lock()
_completions_running: set[_CompletionKey] = set()
_completion_attempts: dict[_CompletionKey, float] = {}


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


CARD_EVIDENCE_REFS = 5
"""Commits a card cites under ``evidence`` (newest first)."""


class _RationaleItemRefRequired(TypedDict):
    kind: Literal["commit", "pr", "issue"]
    id: str


class RationaleItemRef(_RationaleItemRefRequired, total=False):
    """A source one card item names (:meth:`whygraph.analyze.ItemRef.as_dict`).

    ``kind`` is ``"commit"``, ``"pr"`` or ``"issue"``; ``id`` the commit's
    full SHA or the PR's or issue's number as a string; ``url`` the PR's or
    issue's ``html_url``, present only when known.
    """

    url: str


class RationaleItemCheck(TypedDict):
    """The verification pass's verdict on one item (a ``high`` card, plan 62 section 14.2).

    ``verdict`` ``"holds"``: the target's source shows the item is true of
    the code; ``"contradicted"``: it shows the item no longer is (the item
    is kept, marked); ``"cannot_tell"``: the source alone does not settle
    it. The verdict is as of the target's source when the card was built
    (``cached_at``): a later committed edit of the lines regenerates the
    card, an uncommitted one is not reflected.
    """

    verdict: Literal["holds", "contradicted", "cannot_tell"]
    reason: str


class _RationaleItemRequired(TypedDict):
    text: str
    basis: Literal["quoted", "cited", "inferred", "unknown"]
    refs: list[RationaleItemRef]


class RationaleItem(_RationaleItemRequired, total=False):
    """One ``constraints`` / ``risks`` item (:meth:`whygraph.analyze.Item.as_dict`).

    ``basis`` ``"quoted"``: a named source holds ``quote``; ``"cited"``: a
    named source is in the evidence but holds no quote; ``"inferred"``: no
    source is named, so the item is the model's reading of the diffs;
    ``"unknown"``: the card predates sources. ``quote`` is present only for
    ``"quoted"``. ``check`` (:class:`RationaleItemCheck`) is present on a
    card whose verification pass ran.
    """

    quote: str
    check: RationaleItemCheck


class RationaleDepth(TypedDict):
    """The card's ``depth`` (plan 62 section 14.3).

    ``requested`` is what the caller or the project asked for (``"auto"``
    or a level); ``resolved`` the level of the card returned; ``reason`` a
    short sentence saying why (:func:`~whygraph.mcp.rationale_depth.depth_block`).
    """

    requested: str
    resolved: str
    reason: str


class RationaleDeeper(TypedDict):
    """The card's ``deeper``: the next level and what it would add for this target.

    ``level`` is the next level's name; ``adds`` one sentence
    (:func:`~whygraph.mcp.rationale_depth.deeper`).
    """

    level: str
    adds: str


class _RationaleCardRequired(TypedDict):
    target: dict[str, Any]


class RationaleCard(_RationaleCardRequired, total=False):
    """The ``whygraph_rationale_brief`` result, for FastMCP's output schema.

    A card carries every key but ``status``; a ``cached_only`` miss is
    ``{"status": "not_cached", "target"}``; a ``cached_only`` card adds
    ``match`` (``"exact"`` / ``"overlap"``), ``stale`` (the target's own
    lines got new commits), ``context_stale`` and, for an overlap,
    ``card_range``; a project linked to a platform gets the
    platform's card (no ``evidence`` / ``generated``) plus ``platform``.
    Everything but ``target`` is therefore optional.

    ``partial`` says the card was built while ``describing`` evidence
    commits still had no description; ``completing`` that a background
    completion is running for it (plan 49 section 4.3). Every card carries
    all three, a cached one too; a linked card has them only when its
    platform sends them.

    ``status`` is therefore absent on a card, ``"not_cached"`` on a
    ``cached_only`` miss, or ``"no_evidence"`` on the MCP tool's answer for
    code with no commits but with confirmed notes. ``notes`` (confirmed
    project notes, member-written data) is attached by the registered MCP
    tool only, on every non-linked outcome; the Explorer, Chat and the
    ``/api/v1`` response never carry it. ``plans`` (plan refs, compact) is
    attached by the MCP tool and the Explorer's rationale routes, never
    cached and never fed to the card prompt.

    ``coverage`` (plan 62 section 13.4) says what the card's prompt left
    out - ``full_prs_demoted``, ``threads_lost``, ``trimmed``,
    ``descriptions_missing``, ``line_commits``, ``context_commits``,
    ``context_omitted`` - and is present on a card generated since the
    cache stores it (a legacy row has none). A card built at ``high`` or
    deeper adds ``verification`` (plan 62 section 14.2): ``"verified"``
    (every constraint and risk carries a ``check``, as of the target's
    source when the card was built), ``"pending"`` (a partial card, checked
    once its descriptions land), ``"failed"`` or ``"skipped"`` (not checked;
    the card was stored as ``medium``), ``"source_too_long"`` or
    ``"no_source"`` (not checked: the target's source is over the limit or
    could not be read). A card generated since plan 62 section 14.2a adds
    ``source``: ``"included"`` (the draft prompt held the target's source),
    ``"omitted"`` (longer than the draft's limit) or ``"unavailable"`` (its
    lines could not be read).

    ``context_stale`` (plan 62 section 13.5) says one or two commits touched
    the target's file (not its lines) since the card was written; three or
    more regenerate it. Every local card carries it.

    ``depth`` (:class:`RationaleDepth`, plan 62 section 14.4) says which
    level the card is and why; ``deeper`` (:class:`RationaleDeeper` or
    ``None`` at the ceiling, or when the next level would add nothing) what
    the next level would add. A card served by the MCP tool, Chat, the
    Explorer's Generate and ``/api/v1`` carries both (a linked card only
    when its platform sends them); a ``not_cached`` miss carries neither.
    """

    status: str
    purpose: str
    why: str
    constraints: list[RationaleItem]
    tradeoffs: list[str]
    risks: list[RationaleItem]
    model: str
    provider: str
    cached_at: str
    evidence_count: dict[str, int]
    evidence: list[dict[str, Any]]
    generated: bool
    partial: bool
    describing: int
    completing: bool
    match: str
    stale: bool
    card_range: dict[str, int]
    platform: dict[str, Any]
    notes: list[NoteView]
    plans: list[dict[str, Any]]
    coverage: dict[str, Any]
    context_stale: bool
    depth: RationaleDepth
    deeper: RationaleDeeper | None


def card_evidence_refs(evidence: list[CommitEvidence]) -> list[dict]:
    """The commits a card cites: SHA, subject and PR / issue links, newest first.

    Derived at response time from ``evidence``, so the cache row is
    unchanged. Lets a planner cite "PR #847" without an evidence call.

    Parameters
    ----------
    evidence : list[CommitEvidence]
        The target's evidence, newest first.

    Returns
    -------
    list[dict]
        At most :data:`CARD_EVIDENCE_REFS` items
        ``{"sha", "subject", "pull_requests": [{"number", "html_url"}],
        "issues": [{"number", "html_url"}]}``.
    """
    return [
        {
            "sha": item.commit.sha,
            "subject": item.commit.subject,
            "pull_requests": [
                {"number": pr.number, "html_url": pr.html_url}
                for pr in item.pull_requests
            ],
            "issues": [
                {"number": issue.number, "html_url": issue.html_url}
                for issue in item.issues
            ],
        }
        for item in evidence[:CARD_EVIDENCE_REFS]
    ]


def _format_response(
    target: Target,
    rationale: Rationale,
    evidence: list[CommitEvidence],
    cached_at: str,
    *,
    generated: bool | None = None,
    partial: bool = False,
    describing: int = 0,
    completing: bool = False,
    context_stale: bool | None = None,
) -> dict:
    """Shape the MCP response payload around a (fresh or cached) rationale.

    ``generated`` ``None`` (Explorer, Chat's cached read, the ``/api/v1``
    wire shape) leaves out ``evidence`` and ``generated``; the MCP tool
    passes a bool and gets both. ``partial``, ``describing`` and
    ``completing`` are always emitted (plan 49 section 4.3). Each
    constraint and risk is an object ``{"text", "basis", "refs"}`` plus
    ``"quote"`` when it has one (:meth:`whygraph.analyze.Item.as_dict`).
    ``coverage`` is added when the rationale carries one (a card generated
    now; see :class:`RationaleCard`). ``evidence`` is what the card
    includes - a card's bundle items (:func:`_card_items`, at the row's
    level for a cached card) - for the counts and refs. ``context_stale``
    is added when given.
    """
    card = {
        "target": target_dict(target),
        "purpose": rationale.purpose,
        "why": rationale.why,
        "constraints": [item.as_dict() for item in rationale.constraints],
        "tradeoffs": list(rationale.tradeoffs),
        "risks": [item.as_dict() for item in rationale.risks],
        "model": rationale.model,
        "provider": rationale.provider,
        "cached_at": cached_at,
        "evidence_count": {
            "commits": len(evidence),
            # One PR (and the issues it closes) spans several commits: count
            # each number once, not once per commit.
            "prs": len({pr.number for item in evidence for pr in item.pull_requests}),
            "issues": len({i.number for item in evidence for i in item.issues}),
        },
        "partial": partial,
        "describing": describing,
        "completing": completing,
    }
    coverage = getattr(rationale, "coverage", None)
    if coverage is not None:
        card["coverage"] = coverage.as_dict()
    if context_stale is not None:
        card["context_stale"] = context_stale
    if generated is not None:
        card["evidence"] = card_evidence_refs(evidence)
        card["generated"] = generated
    return card


def _card_items(
    evidence: list[CommitEvidence], level: str = DEFAULT_LEVEL
) -> list[CommitEvidence]:
    """The commits a card at ``level`` over ``evidence`` includes (Ring 0 then Ring 1)."""
    return list(card_bundle_for(evidence, get_config().rationale, level).items)


def _rendered_context(bundle: CardBundle, rationale: Rationale) -> str:
    """The space-joined Ring 1 SHAs the card's prompt rendered (its ``context_shas``)."""
    coverage = getattr(rationale, "coverage", None)
    omitted = coverage.omitted_shas if coverage is not None else frozenset()
    return " ".join(
        item.commit.sha for item in bundle.ring1 if item.commit.sha not in omitted
    )


def _completion_evidence(
    evidence: list[CommitEvidence], context_shas: str
) -> list[CommitEvidence]:
    """The current Ring 0 plus the row's stored context commits, newest first.

    A completion regenerates the card with the context it was written with
    (plan 62 section 13.5), so a Ring 1 change never shows up there. A
    stored context commit that has left the current evidence is rebuilt from
    the DB with its linked PRs and issues (as ``area``); one no longer in
    the DB is dropped.
    """
    stored = context_shas.split()
    ring0 = [i for i in evidence if i.source in LINE_LEVEL_SOURCES]
    present = {
        i.commit.sha: i
        for i in evidence
        if i.source not in LINE_LEVEL_SOURCES and i.commit.sha in stored
    }
    rebuilt: list[CommitEvidence] = []
    missing = [sha for sha in stored if sha not in present]
    if missing:
        with get_session() as session:
            for sha in missing:
                commit = session.get(Commit, sha)
                if commit is None:
                    continue
                prs: list[PullRequest] = _linked_prs(session, sha)
                rebuilt.append(
                    CommitEvidence(
                        commit,
                        tuple(prs),
                        tuple(_linked_issues(session, prs)),
                        source="area",
                    )
                )
            session.expunge_all()
    items = [*ring0, *present.values(), *rebuilt]
    items.sort(key=lambda item: item.commit.committed_at or "", reverse=True)
    return items


_TERMINAL_REASON = "this commit's diff cannot be described for this file"


def _missing_reasons(outcome: DescribeOutcome) -> dict[str, str]:
    """Why each undescribed bundle commit has no description (plan 62 R25)."""
    reasons = {sha: "not generated yet" for sha in outcome.waiting}
    reasons.update({sha: _TERMINAL_REASON for sha in outcome.terminal})
    return reasons


class NoEvidenceError(WhyGraphError):
    """A rationale was asked for lines that map to no scanned commit."""


class RationaleGenerationError(WhyGraphError):
    """The LLM could not generate a card; ``__cause__`` is the ``AnalyzeError`` / ``LlmError``."""


_GENERATION_NOT_PERMITTED_MESSAGE = (
    "no rationale card is cached for this target, and your role on this "
    "project (viewer) cannot generate one; ask a contributor or admin"
)

ORG_SUSPENDED = "org_suspended"
"""The ``llm_block`` (and error code) of a suspended org's project (plan 59 D6)."""

_ORG_SUSPENDED_MESSAGE = (
    "no rationale card is cached for this target, and none can be generated "
    "while this organization is suspended"
)


class GenerationNotPermitted(WhyGraphError):
    """A card is not cached and the caller may not generate one.

    Raised by :func:`rationale_card` on a cache miss when the bound
    project context has ``llm_allowed`` false - a project viewer (M2f-1
    plan section 4.6) or an exhausted hard-stopped budget (M2f-2 plan
    section 4.7) or a suspended org's project (plan 59 section 4.5) - or
    when the bound usage sink reports a budget exhausted mid-request,
    before anything is spent or any budget is charged.

    Parameters
    ----------
    message : str, optional
        The message; each ``reason`` has its own default.
    reason : str, optional
        ``"role"`` (the default, a viewer), ``"budget_exceeded"`` or
        ``"org_suspended"``; the last two are also the error's ``code``.
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
            if reason == BUDGET_EXCEEDED:
                message = (
                    "no rationale card is cached for this target, and "
                    + budget_message(scope)
                )
            elif reason == ORG_SUSPENDED:
                message = _ORG_SUSPENDED_MESSAGE
            else:
                message = _GENERATION_NOT_PERMITTED_MESSAGE
        super().__init__(
            message,
            code=reason
            if reason in (BUDGET_EXCEEDED, ORG_SUSPENDED)
            else "generation_not_permitted",
        )
        self.reason = reason
        self.scope = scope


_NO_EVIDENCE_MESSAGE = (
    "no historical evidence for this target — the lines map to no "
    "scanned commit. Scan from the WhyGraph portal to populate the "
    "database."
)

_NOT_PUSHED_MESSAGE = (
    "no pushed history for this target — its lines are uncommitted or on "
    "commits no `origin/*` ref holds yet, and a linked project's rationale "
    "is generated on the platform. Push the commits, then ask again; "
    "`whygraph_evidence_for` meanwhile labels each line's push status."
)


def linked_rationale(
    remote: RemoteProject,
    target: Target,
    *,
    intent: str | None = None,
    depth: str | None = None,
    cached_only: bool | None = None,
) -> dict:
    """``whygraph_rationale_brief``'s answer for a project linked to a platform.

    The card is built, cached and paid for **on the platform** (M2e plan
    section 4.5), so every checkout of the repository shares one cache row:
    nothing here reads the local rationale cache or calls an LLM. Only the
    pushed hunks are sent, and with none there is no request at all. The
    depth is resolved on the platform, with its settings (plan 62 section
    14.5).

    Parameters
    ----------
    remote : RemoteProject
        The project's platform.
    target : Target
        The locally resolved target.
    intent, depth, cached_only : optional
        Passed to the platform as they are; one that predates depth levels
        gets none of them (:meth:`RemoteProject.rationale`).

    Returns
    -------
    dict
        The platform's card plus a ``platform`` block. Its ``target`` is
        the platform's own range for the symbol, which is what the card
        describes. ``plans`` (symbol scope, from the platform) when any;
        ``platform.plans = "unsupported"`` on an older platform.

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
        raise NoEvidenceError(_NOT_PUSHED_MESSAGE, code="no_evidence")
    try:
        card = remote.rationale(
            sendable,
            split.pushed,
            intent=intent,
            depth=depth,
            cached_only=cached_only,
        )
    except RemoteError as exc:
        raise WhyGraphError(str(exc)) from exc
    result = {**card, "platform": platform_block(remote)}
    if card.get("status") == "not_cached":
        return result  # as the local tool: no plans on a cached_only miss
    # Plans beside the card (plan 61 section 4.8): only what this call just
    # sent - the pushed hunks' SHAs (first distinct, blame order) and the
    # sendable path; the target only when its path is the asked-for one,
    # since a substituted origin path's range belongs to another file.
    attach_linked_plans(
        result,
        remote,
        [hunk.sha for hunk in split.pushed],
        [sendable.path],
        scope="symbol",
        target=sendable if sendable.path == target.path else None,
    )
    return result


def _partial_fields(row: CachedCard) -> dict[str, Any]:
    """``partial`` and ``describing`` of a cache row (``deferred_shas`` empty is final)."""
    deferred = row.deferred_shas.split()
    return {"partial": bool(deferred), "describing": len(deferred)}


def _may_spend() -> bool:
    """Whether the bound caller may cause LLM spend now (role and hard-stopped budget)."""
    ctx = current_project()
    if ctx is not None and not ctx.llm_allowed:
        return False
    return usage_blocked() is None


def _reset_completions() -> None:
    """Forget every running completion and cooldown (the test seam, beside ``drain_describes``)."""
    with _completions_lock:
        _completions_running.clear()
        _completion_attempts.clear()


def _thread_inputs(
    scope: ReviewScope | None, target_range: tuple[int, int] | None
) -> dict[str, Any]:
    """The generator's review-thread arguments; all ``None`` without a scope (fail closed)."""
    if scope is None or target_range is None:
        return {"paths": None, "file_lines": None, "target_range": None}
    return {
        "paths": scope.paths,
        "file_lines": scope.current_lines,
        "target_range": target_range,
    }


def _local_review_scope(target: Target) -> ReviewScope:
    """The review-thread scope of a local target: its file under :func:`repo_root`."""
    with get_session() as session:
        return review_scope(session, repo_root(), target.path)


def _start_completion(
    target: Target,
    evidence: list[CommitEvidence],
    provider: str,
    model: str | None,
    allow_description: Callable[[], bool] | None,
    before_generate: Callable[[], None] | None,
    *,
    depth: int | None,
    review_scope: ReviewScope | None = None,
    target_range: tuple[int, int] | None = None,
) -> bool:
    """Start completing a partial card in the background, unless one may not start.

    The running set and the last-attempt map are keyed by
    ``(db_key, path, line_start, line_end, provider, model, depth)``, so two
    projects with the same path never share a completion or a cooldown.
    State is in memory: a restart drops a running completion and the card
    stays partial until a later hit.

    Parameters
    ----------
    target : Target
        The card's target (its path and range key the cache).
    evidence : list[CommitEvidence]
        The request's evidence; its SHAs fingerprint the row.
    provider : str
        The cache's provider tag.
    model : str or None
        The cache's pinned model.
    allow_description : callable or None
        The description budget, passed to the backfill.
    before_generate : callable or None
        The card budget, called before the card is generated again.
    depth : int or None
        The **row's** depth (``None`` for a legacy row): the completion
        regenerates the card at the row's level, never the requested one.
    review_scope : ReviewScope or None, optional
        The request's review-thread scope, reused by the completion (see
        :func:`rationale_card`); ``None`` keeps threads out of the card.
    target_range : tuple[int, int] or None, optional
        The target's line range in ``review_scope.current_lines``.

    Returns
    -------
    bool
        ``True`` when a completion is now running for this card (started
        here, or already running); ``False`` when :data:`COMPLETIONS_MAX`
        are running or the last attempt was under
        :data:`CARD_RETRY_COOLDOWN_SEC` ago.
    """
    db_key = _db_key()
    key: _CompletionKey = (
        db_key,
        target.path,
        target.line_start,
        target.line_end,
        provider,
        model,
        depth,
    )
    now = time.monotonic()
    with _completions_lock:
        if key in _completions_running:
            return True
        if len(_completions_running) >= COMPLETIONS_MAX:
            return False
        last = _completion_attempts.get(key)
        if last is not None and now - last < CARD_RETRY_COOLDOWN_SEC:
            return False
        for old, at in list(_completion_attempts.items()):
            if now - at >= CARD_RETRY_COOLDOWN_SEC:
                del _completion_attempts[old]
        _completion_attempts[key] = now
        _completions_running.add(key)
    # Taken now: once the project is removed its event is replaced, and the
    # completion must see the one that was set.
    stop = _stop_event(db_key)

    def run() -> None:
        try:
            _complete_partial(
                target,
                evidence,
                provider,
                model,
                allow_description=allow_description,
                before_generate=before_generate,
                stop=stop,
                depth=depth,
                review_scope=review_scope,
                target_range=target_range,
            )
        finally:
            with _completions_lock:
                _completions_running.discard(key)

    try:
        _spawn(run)
    except BaseException:
        with _completions_lock:
            _completions_running.discard(key)
        raise
    return True


def _fresh_evidence(evidence: list[CommitEvidence]) -> list[CommitEvidence]:
    """``evidence`` over fresh, detached ``Commit`` rows (same PRs, issues and source).

    The completion thread then shares no commit row with the response
    being serialized, and sees descriptions written since the request.
    """
    with get_session() as session:
        rows = {
            sha: session.get(Commit, sha) for sha in {i.commit.sha for i in evidence}
        }
        session.expunge_all()
    return [
        dataclasses.replace(item, commit=rows[item.commit.sha])
        for item in evidence
        if rows.get(item.commit.sha) is not None
    ]


def _same_row(row: CachedCard | None, seen: CachedCard) -> bool:
    """Whether ``row`` is still the row ``seen`` was read from."""
    return row is not None and (
        row.cached_at,
        row.evidence_fingerprint,
        row.deferred_shas,
    ) == (seen.cached_at, seen.evidence_fingerprint, seen.deferred_shas)


def _verified(
    generator: RationaleGenerator,
    rationale: Rationale,
    level: str,
    *,
    before_generate: Callable[[], None] | None,
    review_scope: ReviewScope | None,
    target_range: tuple[int, int] | None,
    subject: str,
) -> tuple[Rationale, str]:
    """The verification pass over a non-partial draft, and the level to store it at.

    Plan 62 section 14.2. Below ``high`` the draft is returned as is. With
    nothing to check (no constraints, no risks) it is ``verified`` without a
    call; without the target's source (none, or longer than
    :data:`~whygraph.analyze.rationale_generator.VERIFY_SOURCE_MAX_CHARS`)
    it keeps its level, marked ``no_source`` / ``source_too_long``, since
    regenerating could not change that. Otherwise the pass is charged like
    a card (``usage_blocked()``, then ``before_generate``) and run; a
    refusal (``skipped``) or a failed call or answer (``failed``) stores
    the draft at :data:`~whygraph.analyze.card_bundle.UNVERIFIED_LEVEL`, so
    a later ``high`` request regenerates it.

    Parameters
    ----------
    generator : RationaleGenerator
        The generator that wrote the draft.
    rationale : Rationale
        The draft.
    level : str
        The level the draft was built at.
    before_generate : callable or None
        The card budget; any exception it raises refuses the pass.
    review_scope : ReviewScope or None
        The request's scope: its ``current_lines`` hold the target's source.
    target_range : tuple[int, int] or None
        The target's line range in those lines.
    subject : str
        What the card is about, for the call's usage.

    Returns
    -------
    tuple[Rationale, str]
        The card (``coverage.verification`` set from ``high``) and the
        level to store it at.
    """
    if not verifies(level):
        return rationale, level
    if not rationale.constraints and not rationale.risks:
        return with_verification(rationale, "verified"), level
    file_lines = None if review_scope is None else review_scope.current_lines
    source, unavailable = verification_source(file_lines, target_range)
    if source is None or target_range is None:
        return with_verification(rationale, unavailable or "no_source"), level
    if usage_blocked() is not None:
        return with_verification(rationale, "skipped"), UNVERIFIED_LEVEL
    if before_generate is not None:
        try:
            before_generate()
        except Exception as exc:  # noqa: BLE001 - any refusal skips the pass
            _log.info("verification of the card of %s skipped: %s", subject, exc)
            return with_verification(rationale, "skipped"), UNVERIFIED_LEVEL
    try:
        checked = generator.verify(
            rationale, source, first_line=max(target_range[0], 1), subject=subject
        )
    except AnalyzeError as exc:
        _log.warning("verification of the card of %s failed: %s", subject, exc)
        return with_verification(rationale, "failed"), UNVERIFIED_LEVEL
    return checked, level


def _verification_pending(row: CachedCard) -> bool:
    """Whether ``row``'s card waits for its verification pass (``coverage.verification``)."""
    coverage = row.rationale.coverage
    return coverage is not None and coverage.verification == "pending"


def _complete_partial(
    target: Target,
    evidence: list[CommitEvidence],
    provider: str,
    model: str | None,
    *,
    allow_description: Callable[[], bool] | None,
    before_generate: Callable[[], None] | None,
    stop: threading.Event,
    depth: int | None = None,
    review_scope: ReviewScope | None = None,
    target_range: tuple[int, int] | None = None,
) -> None:
    """Describe a partial card's deferred commits, then regenerate it once (plan 49 section 4.3).

    Runs on its own daemon thread (:func:`_start_completion`). One
    describe worker and no wall: background work holds at most
    :data:`COMPLETIONS_MAX` of the process's describe slots. The card is
    generated again, and ``before_generate`` charged, only when a
    deferred commit gained a description and the row has not moved since
    it was read; a commit still waiting leaves the row partial with the
    commits that wait. Nothing is read or written once ``stop`` is set
    (the project was removed or the portal is stopping). Any failure is
    logged and leaves the row partial.

    The completion keeps the row's key and depth (plan 62 section 13.5): it
    regenerates at the row's level with the current Ring 0 plus the row's
    stored context commits (:func:`_completion_evidence`); a legacy row is
    regenerated from the whole evidence, as before, and stored as a
    ``medium`` row. At ``high`` and deeper the regenerated card - or, when
    no deferred commit could ever be described, the stored draft still
    ``pending`` - then gets its verification pass (:func:`_verified`), and
    a failed or skipped pass stores it as ``medium``.

    Parameters
    ----------
    target, evidence, provider, model
        As for :func:`_start_completion`.
    allow_description : callable or None
        The description budget.
    before_generate : callable or None
        The card budget; a refusal leaves the row partial.
    stop : threading.Event
        The project's stop event, taken when the completion started.
    depth : int or None, optional
        The row's depth when the completion started; a row whose depth has
        changed since is left alone.
    review_scope, target_range : optional
        As for :func:`_start_completion`: computed once at request time.
    """
    try:
        if stop.is_set():
            return
        seen = lookup_exact(target, provider, model)
        if (
            seen is None
            or not seen.deferred_shas
            or seen.depth != depth
            or seen.is_stale(evidence)
        ):
            return
        deferred = frozenset(seen.deferred_shas.split())
        basis = (
            evidence
            if seen.depth is None
            else _completion_evidence(evidence, seen.context_shas)
        )
        fresh = _fresh_evidence(basis)
        if stop.is_set():
            return
        config = get_config()
        level = seen.level
        bundle = card_bundle_for(fresh, config.rationale, level)
        items = list(bundle.items)
        outcome = backfill_evidence_descriptions(
            items,
            target_path=target.path,
            allow=allow_description,
            max_commits=len(items),
            wall_sec=None,
            workers=1,
        )
        if stop.is_set():
            return
        # Only the row's deferred commits count: a context commit the card
        # left out never holds it partial.
        waiting = outcome.waiting & deferred
        if waiting:
            update_deferred(
                target,
                provider,
                model,
                seen=seen,
                deferred_shas=" ".join(sorted(waiting)),
            )
            return
        subject = target.qualified_name or target.path
        if not deferred - outcome.terminal:
            # Nothing gained: every deferred commit can never be described.
            if not (verifies(level) and _verification_pending(seen)):
                update_deferred(target, provider, model, seen=seen, deferred_shas="")
                return
            # The draft stands as it is; only its verification pass is due.
            if not _same_row(lookup_exact(target, provider, model), seen):
                return
            checked, stored = _verified(
                RationaleGenerator.from_config(config),
                seen.rationale,
                level,
                before_generate=before_generate,
                review_scope=review_scope,
                target_range=target_range,
                subject=subject,
            )
            if stop.is_set():
                return
            store_cached(
                target,
                evidence,
                checked,
                provider,
                model,
                deferred_shas="",
                only_if=seen,
                seen=seen,
                depth=depth_of(stored),
                context_shas=seen.context_shas,
            )
            return
        if not _same_row(lookup_exact(target, provider, model), seen):
            return
        if usage_blocked() is not None:
            return
        if before_generate is not None:
            before_generate()
        generator = RationaleGenerator.from_config(config)
        rationale = generator.generate(
            fresh,
            symbol_context=_symbol_context(target),
            subject=subject,
            bundle=bundle,
            missing_descriptions=_missing_reasons(outcome),
            target_path=target.path,
            **_thread_inputs(review_scope, target_range),
        )
        rationale, stored = _verified(
            generator,
            rationale,
            level,
            before_generate=before_generate,
            review_scope=review_scope,
            target_range=target_range,
            subject=subject,
        )
        if stop.is_set():
            return
        # The fingerprint is over SHAs only: the request's evidence is the row's.
        # ``seen`` lets a failed verification store the card shallower than
        # the row it replaces; ``only_if`` still requires that exact row.
        store_cached(
            target,
            evidence,
            rationale,
            provider,
            model,
            deferred_shas="",
            only_if=seen,
            seen=seen,
            depth=depth_of(stored),
            context_shas=_rendered_context(bundle, rationale),
        )
    except Exception as exc:  # noqa: BLE001 - the row stays partial
        _log.warning(
            "completing the partial rationale card of %s:%d-%d failed: %s",
            target.path,
            target.line_start,
            target.line_end,
            exc,
        )


def _cached_only_card(
    target: Target,
    evidence: list[CommitEvidence],
    provider: str,
    pinned_model: str | None,
    *,
    mark_generated: bool,
) -> tuple[dict, str] | None:
    """The looser lookup behind ``cached_only``: a stale or overlapping card.

    Reads only the cache and git - no LLM call, no backfill, no store.
    The exact key's row is returned whatever its fingerprint (``stale``
    when the target's own lines got new commits since, the Ring 0 check;
    ``context_stale`` when other commits touched the file, counted apart);
    without one, the best row of the same file whose range overlaps
    ``target`` (:func:`~whygraph.mcp.rationale_cache.lookup_overlapping`),
    whose ``stale`` and ``context_stale`` are checked against the evidence
    of its own range.

    Returns
    -------
    tuple[dict, str] or None
        The card plus ``match``, ``stale``, ``context_stale`` and (overlap)
        ``card_range``, and the row's level; ``None`` when no row matches.
        ``partial`` / ``describing`` come from the row; nothing is started
        to complete it. Counts and refs are over the bundle at the row's
        level.
    """
    generated = False if mark_generated else None
    row = lookup_exact(target, provider, pinned_model)
    if row is not None:
        card = _format_response(
            target,
            row.rationale,
            _card_items(evidence, row.level),
            row.cached_at,
            generated=generated,
            context_stale=row.context_newer(evidence) > 0,
            **_partial_fields(row),
        )
        return {**card, "match": "exact", "stale": row.is_stale(evidence)}, row.level
    row = lookup_overlapping(target, provider, pinned_model)
    if row is None:
        return None
    card_target = Target(
        path=target.path,
        line_start=row.line_start,
        line_end=row.line_end,
        qualified_name=row.qualified_name,
    )
    try:
        card_evidence = collect_evidence(card_target, limit=20)
    except WhyGraphError:
        # The card's range no longer blames (the file shrank past it):
        # its lines certainly changed since.
        card_evidence = []
    card = _format_response(
        target,
        row.rationale,
        _card_items(card_evidence, row.level) if card_evidence else [],
        row.cached_at,
        generated=generated,
        context_stale=bool(card_evidence) and row.context_newer(card_evidence) > 0,
        **_partial_fields(row),
    )
    return {
        **card,
        "match": "overlap",
        "stale": row.is_stale(card_evidence),
        "card_range": {"line_start": row.line_start, "line_end": row.line_end},
    }, row.level


def _with_depth(
    card: dict,
    resolution: DepthResolution | None,
    level: str,
    evidence: list[CommitEvidence],
    served: str = SERVED_CACHED,
) -> dict:
    """``card`` plus ``depth`` and ``deeper`` (plan 62 section 14.4); as is without a resolution."""
    if resolution is None:
        return card
    ceiling = effective_ceiling(get_config().rationale)
    return {
        **card,
        "depth": depth_block(resolution, level, served),
        "deeper": deeper(level, ceiling, evidence),
    }


def rationale_card(
    target: Target,
    evidence: list[CommitEvidence],
    *,
    before_generate: Callable[[], None] | None = None,
    allow_description: Callable[[], bool] | None = None,
    force_refresh: bool = False,
    cached_only: bool = False,
    mark_generated: bool = False,
    progress: Callable[[int, str], None] | None = None,
    note: Callable[[str], None] | None = None,
    review_scope: ReviewScope | None = None,
    target_range: tuple[int, int] | None = None,
    level: str = DEFAULT_LEVEL,
    resolution: DepthResolution | None = None,
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
        org card budget raises from it), and by a background completion
        before it generates the card again.
    allow_description : callable, optional
        The description backfill's budget (see
        :func:`~whygraph.mcp.evidence.backfill_evidence_descriptions`).
    force_refresh : bool, default False
        Skip the cache lookup and generate a new card (still stored, and
        still subject to the generation gates and ``before_generate``). A
        deeper row of the same lines is regenerated at its own depth, never
        replaced by a shallower card.
    cached_only : bool, default False
        Never generate: a hit gets ``match: "exact"`` and ``stale: false``;
        on a miss, return the key's row even with a stale fingerprint
        (``stale: true``) or the best overlapping row of the same file
        (``match: "overlap"`` plus ``card_range``, see
        :func:`_cached_only_card`), and otherwise
        ``{"status": "not_cached", "target"}`` - no generation gate, no
        backfill, no LLM call. Ignored with ``force_refresh``.
    mark_generated : bool, default False
        Add ``evidence`` (:func:`card_evidence_refs`) and ``generated`` to
        the card (the MCP tool's shape; ``/api/v1`` leaves them out).
    progress : callable, optional
        Called as ``progress(step, message)`` for step 2 ("describing k
        commits", only when the backfill describes any) and step 3
        ("generating card") of :data:`PROGRESS_STEPS` on a cache miss.
    note : callable, optional
        Called with a one-line cache hit / miss message.
    review_scope : ReviewScope, optional
        The target file's review-thread scope (its rename aliases and
        current lines), computed once by the caller at request time and
        reused by a background completion. ``None`` (default) keeps review
        threads out of the card (fail closed).
    target_range : tuple[int, int], optional
        The target's line range in ``review_scope.current_lines``; ``None``
        keeps review threads out too.
    level : str, optional
        The depth level the card is wanted at
        (:data:`~whygraph.analyze.card_bundle.LEVELS`); default
        :data:`~whygraph.analyze.card_bundle.DEFAULT_LEVEL`. A cached card
        at least as deep is served; a generated card is built at it (or at a
        deeper row's level, see Notes), and from ``high`` it gets the
        verification pass (:func:`_verified`): a partial draft is stored
        ``pending`` and checked by its completion, a failed or skipped pass
        stores the card as ``medium``.
    resolution : DepthResolution, optional
        The resolver's answer
        (:func:`~whygraph.mcp.rationale_depth.resolve_for_target`, run by
        the caller before this lookup). When given it replaces ``level``
        with its ``resolved`` level, accepts a cached card of any depth for
        ``any_cached_depth``, and the card gains ``depth`` and ``deeper``
        (a ``not_cached`` miss does not). The caller passes its
        ``cached_only`` as ``cached_only``.

    Returns
    -------
    dict
        The card (:func:`_format_response`), with ``context_stale``. A card
        generated while evidence commits still lacked a description is
        stored with them as its deferred SHAs and returned ``partial`` with
        ``describing`` set; a hit on such a card is served at once, and
        either case starts a background completion (``completing``) when
        the caller may spend (:func:`_start_completion`). A ``cached_only``
        call starts nothing. A generated card that could not be stored
        (a deeper card of the same lines landed meanwhile,
        :func:`~whygraph.mcp.rationale_cache.store_cached`) is served as is
        and starts nothing.

    Notes
    -----
    The lookup (:func:`~whygraph.mcp.rationale_cache.lookup_card`) accepts
    a row at least as deep as the requested level and may move a row of the
    same symbol to the target's range. A miss on a deeper row whose Ring 0
    still matches (only its context went stale) regenerates at that row's
    depth. Counts and refs of a hit come from the bundle at the row's level.

    Raises
    ------
    ValueError
        ``level`` is not a known level.
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
        raise NoEvidenceError(_NO_EVIDENCE_MESSAGE, code="no_evidence")

    min_depth = level
    if resolution is not None:
        level = min_depth = resolution.resolved
        if resolution.any_cached_depth:
            min_depth = LEVELS[0]
    depth_of(level)  # an unknown level raises before anything is read
    config = get_config()
    provider, pinned_model = config.cache_identity("rationale")
    cached: CachedCard | None = None
    if force_refresh:
        # The row this card replaces: a deeper one of the same lines is
        # regenerated at its own depth below, never overwritten shallower.
        seen = lookup_exact(target, provider, pinned_model)
    else:
        found = lookup_card(
            target, evidence, provider, pinned_model, min_depth=min_depth
        )
        cached, seen = found.card, found.seen
    if cached is not None:
        if note is not None:
            note("rationale cache hit")
        fields = _partial_fields(cached)
        completing = False
        if fields["partial"] and not cached_only and _may_spend():
            # Served at once: a partial-card hit never waits for an LLM call.
            completing = _start_completion(
                target,
                evidence,
                provider,
                pinned_model,
                allow_description,
                before_generate,
                depth=cached.depth,
                review_scope=review_scope,
                target_range=target_range,
            )
        card = _format_response(
            target,
            cached.rationale,
            _card_items(evidence, cached.level),
            cached.cached_at,
            generated=False if mark_generated else None,
            completing=completing,
            context_stale=cached.context_stale,
            **fields,
        )
        card = _with_depth(card, resolution, cached.level, evidence)
        return {**card, "match": "exact", "stale": False} if cached_only else card
    if cached_only and not force_refresh:
        loose = _cached_only_card(
            target, evidence, provider, pinned_model, mark_generated=mark_generated
        )
        if note is not None:
            note(
                "rationale cache miss"
                if loose is None
                else f"rationale cache hit ({loose[0]['match']}"
                + (", stale)" if loose[0]["stale"] else ")")
            )
        if loose is None:
            return {"status": "not_cached", "target": target_dict(target)}
        return _with_depth(loose[0], resolution, loose[1], evidence)
    if note is not None:
        note("rationale cache " + ("bypassed" if force_refresh else "miss"))

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
    # A deeper row of the same lines (its context went stale) is rebuilt at
    # its own depth: max(requested, row depth), so it is never replaced by a
    # shallower card (plan 62 section 13.5).
    if (
        seen is not None
        and seen.depth is not None
        and seen.depth > depth_of(level)
        and not seen.is_stale(evidence)
    ):
        level = level_of(seen.depth)
    # What the card includes (plan 62 section 13.4), chosen before anything
    # is described: only these commits are described, rendered and counted.
    bundle = card_bundle_for(evidence, config.rationale, level)
    items = list(bundle.items)
    # Cache miss — lazily backfill any bundle commit whose `llm_description`
    # is NULL (e.g. after a `--skip-analyze` scan) so the
    # rationale prompt sees the richer per-commit summaries. Bulk commits
    # are described per-file against the target's path instead. The cache
    # fingerprint is sha256-over-sorted-SHAs, so backfilling here does
    # not affect cache keys. A card may describe every commit it includes
    # (never one it leaves out); the wall still bounds the wait, and what
    # it leaves waiting is stored as the card's deferred SHAs and completed
    # in the background.
    outcome = backfill_evidence_descriptions(
        items,
        target_path=target.path,
        allow=allow_description,
        max_commits=len(items),
        on_start=(
            None
            if progress is None
            else lambda k: progress(2, f"describing {k} commits")
        ),
    )

    if progress is not None:
        progress(3, "generating card")
    try:
        generator = RationaleGenerator.from_config(config)
        rationale = generator.generate(
            evidence,
            symbol_context=_symbol_context(target),
            subject=target.qualified_name or target.path,
            bundle=bundle,
            missing_descriptions=_missing_reasons(outcome),
            target_path=target.path,
            **_thread_inputs(review_scope, target_range),
        )
    except (AnalyzeError, LlmError) as exc:
        raise RationaleGenerationError.wrap(
            "rationale generation failed", exc, code="provider_error"
        )

    # A context commit the prompt left out (its budget, the prompt cap) is
    # not waited for (plan 62 section 13.4).
    coverage = getattr(rationale, "coverage", None)
    omitted = coverage.omitted_shas if coverage is not None else frozenset()
    waiting = sorted(outcome.waiting - omitted)
    # The verification pass (plan 62 section 14.2) runs on a non-partial
    # draft only; a partial one is checked by its completion.
    stored = level
    if waiting and verifies(level):
        rationale = with_verification(rationale, "pending")
    elif not waiting:
        rationale, stored = _verified(
            generator,
            rationale,
            level,
            before_generate=before_generate,
            review_scope=review_scope,
            target_range=target_range,
            subject=target.qualified_name or target.path,
        )
    cached_at = store_cached(
        target,
        evidence,
        rationale,
        provider,
        pinned_model,
        deferred_shas=" ".join(waiting),
        seen=seen,
        depth=depth_of(stored),
        context_shas=_rendered_context(bundle, rationale),
    )
    if cached_at is None:
        # A deeper card of the same lines landed meanwhile: it stays, this
        # request serves its own card and completes nothing.
        cached_at = _now_iso()
        completing = False
    else:
        completing = (
            bool(waiting)
            and _may_spend()
            and _start_completion(
                target,
                evidence,
                provider,
                pinned_model,
                allow_description,
                before_generate,
                depth=depth_of(stored),
                review_scope=review_scope,
                target_range=target_range,
            )
        )
    card = _format_response(
        target,
        rationale,
        items,
        cached_at,
        generated=True if mark_generated else None,
        partial=bool(waiting),
        describing=len(waiting),
        completing=completing,
        context_stale=False,
    )
    served = SERVED_UNVERIFIED if stored != level else SERVED_KEPT
    return _with_depth(card, resolution, stored, evidence, served)


def _client_channel(
    ctx: Context | None,
) -> tuple[Callable[[int, str], None] | None, Callable[[str], None] | None]:
    """The progress and log senders for one MCP request, from its worker thread.

    The tool body runs on a worker thread (:func:`~whygraph.mcp.server.offload`),
    so each send hops back to the event loop with
    :func:`anyio.from_thread.run`. Progress goes through
    ``session.send_progress_notification`` with ``related_request_id`` -
    never ``ctx.report_progress``, which omits it, so the stateless HTTP
    transport would route the notification to a GET stream that does not
    exist and drop it. A failed send is logged and never fails the call.

    Returns
    -------
    tuple
        ``(progress, note)``: ``progress`` is ``None`` without a context
        or without a client ``progressToken``; ``note`` (``ctx.info``) is
        ``None`` without a context.
    """
    if ctx is None:
        return None, None
    try:
        request = ctx.request_context
    except ValueError:
        return None, None
    request_id = ctx.request_id

    def _send(fn: Callable[[], object]) -> None:
        try:
            anyio.from_thread.run(fn)
        except Exception as exc:  # noqa: BLE001 - a lost notification is not an error
            _log.debug("MCP notification not sent: %s", exc)

    def note(message: str) -> None:
        _send(functools.partial(ctx.info, message))

    token = request.meta.progressToken if request.meta else None
    if token is None:
        return None, note

    def progress(step: int, message: str) -> None:
        _send(
            functools.partial(
                request.session.send_progress_notification,
                token,
                float(step),
                float(PROGRESS_STEPS),
                message,
                related_request_id=request_id,
            )
        )

    return progress, note


INTENT_HELP = (
    "The task the card is for. Under the project's auto depth it picks the "
    "level: explain or review -> low (the lines' own commits); plan or edit "
    "-> medium (plus nearby commits to the file); refactor or delete -> high "
    "(plus each claim checked against today's code). One level up for a "
    "symbol with many callers, one down near the budget limit. review also "
    "accepts a cached card of any depth and makes cached_only default to "
    "true. Omit when none fits."
)
"""The ``intent`` input's description (the tool's ``inputSchema``)."""

DEPTH_HELP = (
    "A level instead of auto: low, medium or high (see intent). Capped at "
    "the project's ceiling, never refused. Omit unless the card's `deeper` "
    "says the next level adds what the task needs."
)
"""The ``depth`` input's description (the tool's ``inputSchema``)."""

IntentInput = Annotated[Intent | None, Field(description=INTENT_HELP)]
"""The tool's optional ``intent`` (plan 62 section 14.4)."""

DepthInput = Annotated[OfferedDepth | None, Field(description=DEPTH_HELP)]
"""The tool's optional ``depth``: an offered level, so ``xhigh`` is refused."""


def whygraph_rationale_brief(
    path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    qualified_name: str | None = None,
    force_refresh: bool = False,
    cached_only: bool | None = None,
    intent: str | None = None,
    depth: str | None = None,
    ctx: Context | None = None,
) -> RationaleCard:
    """MCP tool — a rationale card for a chunk of code.

    See :data:`~whygraph.mcp.descriptions.RATIONALE_BRIEF_DESCRIPTION` for the agent-facing summary.

    A previously generated card is returned from the SQLite-backed cache
    (see :mod:`whygraph.mcp.rationale_cache`) when the same target,
    provider, model, and evidence fingerprint are all unchanged.
    ``force_refresh=True`` skips that lookup and generates (and stores) a
    new card. ``cached_only=True`` never generates: it also accepts the
    key's card with a stale fingerprint (``stale: true``) or the card of
    an overlapping range (``match: "overlap"``, ``card_range``), and
    returns ``{"status": "not_cached", "target"}`` only when there is
    none (a linked project sends it to its platform; one that predates
    depth levels ignores it). ``None`` (the default) means ``True`` for
    ``intent="review"`` and ``False`` otherwise.

    ``intent`` (one of :data:`~whygraph.mcp.rationale_depth.INTENTS`) and
    ``depth`` (a level) choose the card's depth through
    :func:`~whygraph.mcp.rationale_depth.resolve_for_target`, before the
    cache lookup (plan 62 section 14.3); the card reports it as ``depth``
    and what the next level would add as ``deeper``. A ``depth`` above the
    project's ceiling, or not offered yet (``xhigh``), is capped.

    The card carries the top
    :data:`CARD_EVIDENCE_REFS` commits as ``evidence`` and ``generated``
    (whether this call paid for it).

    For a project linked to a WhyGraph platform the card comes from
    :func:`linked_rationale` instead, out of the platform's shared cache.

    ``ctx`` is injected by FastMCP (never part of the input schema): with a
    client ``progressToken`` the call reports :data:`PROGRESS_STEPS` steps
    - "evidence collected (n commits)", "describing k commits" and
    "generating card" (the last two on a cache miss only) - and logs the
    cache hit or miss through ``ctx.info``.
    """
    progress, note = _client_channel(ctx)
    target = resolve_target(
        path=path,
        line_start=line_start,
        line_end=line_end,
        qualified_name=qualified_name,
    )
    ctx = current_project()
    if ctx is not None and ctx.remote is not None:
        return linked_rationale(
            ctx.remote, target, intent=intent, depth=depth, cached_only=cached_only
        )
    return _brief(
        target,
        force_refresh=force_refresh,
        cached_only=cached_only,
        intent=intent,
        depth=depth,
        progress=progress,
        note=note,
    )


def _brief(
    target: Target,
    *,
    force_refresh: bool,
    cached_only: bool | None,
    progress: Callable[[int, str], None] | None,
    note: Callable[[str], None] | None,
    evidence: list[CommitEvidence] | None = None,
    intent: str | None = None,
    depth: str | None = None,
) -> RationaleCard:
    """The non-linked half of :func:`whygraph_rationale_brief`.

    Parameters
    ----------
    target : Target
        The resolved target.
    force_refresh : bool
        As for :func:`whygraph_rationale_brief`.
    cached_only : bool or None
        As for :func:`whygraph_rationale_brief`.
    progress, note : callable or None
        The client channel from :func:`_client_channel`.
    evidence : list of CommitEvidence or None
        ``collect_evidence(target, limit=20)`` when the caller already
        has it (so plans and the card share one list); collected here
        otherwise.
    intent, depth : str or None, optional
        As for :func:`whygraph_rationale_brief`; resolved here, after the
        evidence and before the cache lookup.

    Returns
    -------
    RationaleCard
        The card, the ``cached_only`` card or the ``not_cached`` miss.

    Raises
    ------
    NoEvidenceError
        The target has no commits.
    """
    if evidence is None:
        evidence = collect_evidence(target, limit=20)
    if progress is not None:
        progress(1, f"evidence collected ({len(evidence)} commits)")
    # No evidence raises in rationale_card before anything is resolved.
    resolution = (
        resolve_for_target(
            target, evidence, intent=intent, depth=depth, cached_only=cached_only
        )
        if evidence
        else None
    )
    # Local blame reads the working tree, so the anchors are checked on it.
    return rationale_card(
        target,
        evidence,
        force_refresh=force_refresh,
        cached_only=resolution.cached_only if resolution is not None else False,
        resolution=resolution,
        mark_generated=True,
        progress=progress,
        note=note,
        review_scope=_local_review_scope(target),
        target_range=(target.line_start, target.line_end),
    )


def _rationale_brief_tool(
    path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    qualified_name: str | None = None,
    force_refresh: bool = False,
    cached_only: bool | None = None,
    intent: IntentInput = None,
    depth: DepthInput = None,
    ctx: Context | None = None,
) -> RationaleCard:
    """The registered ``whygraph_rationale_brief``: the card plus ``notes``.

    Same signature and behaviour as :func:`whygraph_rationale_brief` (its
    ``intent`` and ``depth`` carry their guidance in the input schema,
    :data:`INTENT_HELP` and :data:`DEPTH_HELP`; input validation refuses a
    level that is not offered), then
    every non-linked outcome gains the target's confirmed notes (data the
    members wrote, never fed into the card). A linked project gets the
    platform's card unchanged. Code with no commits raises
    ``no_evidence`` as before, unless confirmed notes exist, in which case
    ``{"status": "no_evidence", "target", "notes"}`` is returned. The same
    holds for ``plans`` (compact pointers to the committed plans that
    built the code, never fed to the card, omitted when empty or on a
    ``cached_only`` miss).
    """
    progress, note = _client_channel(ctx)
    target = resolve_target(
        path=path,
        line_start=line_start,
        line_end=line_end,
        qualified_name=qualified_name,
    )
    project = current_project()
    if project is not None and project.remote is not None:
        return linked_rationale(
            project.remote,
            target,
            intent=intent,
            depth=depth,
            cached_only=cached_only,
        )
    notes = notes_for_target(target)
    evidence = collect_evidence(target, limit=20)
    plans = guarded_plans_for(
        card_plan_shas(evidence), [target.path], target=target, scope="symbol"
    )
    try:
        card = _brief(
            target,
            force_refresh=force_refresh,
            cached_only=cached_only,
            intent=intent,
            depth=depth,
            progress=progress,
            note=note,
            evidence=evidence,
        )
    except NoEvidenceError:
        if not notes and not plans:
            raise
        reply: RationaleCard = {
            "status": "no_evidence",
            "target": target_dict(target),
        }
        if notes:
            reply["notes"] = notes
        if plans:
            reply["plans"] = plans
        return reply
    if notes:
        card["notes"] = notes
    if plans and card.get("status") != "not_cached":
        card["plans"] = plans
    return card


def register(mcp: FastMCP) -> None:
    """Attach the rationale tool to an MCP server."""
    mcp.tool(
        name="whygraph_rationale_brief",
        title=RATIONALE_BRIEF_ANNOTATIONS.title,
        description=RATIONALE_BRIEF_DESCRIPTION,
        annotations=RATIONALE_BRIEF_ANNOTATIONS,
    )(log_tool_errors(structured(RationaleCard)(_rationale_brief_tool)))
