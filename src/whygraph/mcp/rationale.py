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
from typing import Any, TypedDict

import anyio.from_thread
from mcp.server.fastmcp import Context, FastMCP

from whygraph.analyze import AnalyzeError, RationaleGenerator
from whygraph.core import get_config
from whygraph.core.context import current_project
from whygraph.core.remote import RemoteError, RemoteProject, platform_block
from whygraph.core.usage import BUDGET_EXCEEDED, budget_message, usage_blocked
from whygraph.db import get_session
from whygraph.db.models import Commit
from whygraph.services.codegraph import CodeGraph, CodeGraphError, SymbolContext
from whygraph.services.git import Repository
from whygraph.services.llm import LlmError

from whygraph.analyze import CommitEvidence, Rationale

from .descriptions import (
    RATIONALE_BRIEF_ANNOTATIONS,
    RATIONALE_BRIEF_DESCRIPTION,
)
from .notes import NoteView, notes_for_target
from .structured import structured
from .errors import WhyGraphError, log_tool_errors
from .targets import Target, repo_root, resolve_target, target_dict
from .evidence import (
    _db_key,
    _spawn,
    _stop_event,
    backfill_evidence_descriptions,
    blame_target,
    collect_evidence,
    pushed_target,
    split_by_push_status,
)
from .rationale_cache import (
    CachedCard,
    lookup_cached,
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

_CompletionKey = tuple[Path, str, int, int, str, str | None]
"""``(db_key, path, line_start, line_end, provider, model)``: one card of one project."""

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


class _RationaleCardRequired(TypedDict):
    target: dict[str, Any]


class RationaleCard(_RationaleCardRequired, total=False):
    """The ``whygraph_rationale_brief`` result, for FastMCP's output schema.

    A card carries every key but ``status``; a ``cached_only`` miss is
    ``{"status": "not_cached", "target"}``; a ``cached_only`` card adds
    ``match`` (``"exact"`` / ``"overlap"``), ``stale`` and, for an
    overlap, ``card_range``; a project linked to a platform gets the
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
    ``/api/v1`` response never carry it.
    """

    status: str
    purpose: str
    why: str
    constraints: list[dict[str, Any]]
    tradeoffs: list[str]
    risks: list[dict[str, Any]]
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
) -> dict:
    """Shape the MCP response payload around a (fresh or cached) rationale.

    ``generated`` ``None`` (Explorer, Chat's cached read, the ``/api/v1``
    wire shape) leaves out ``evidence`` and ``generated``; the MCP tool
    passes a bool and gets both. ``partial``, ``describing`` and
    ``completing`` are always emitted (plan 49 section 4.3). Each
    constraint and risk is an object ``{"text", "basis", "refs"}`` plus
    ``"quote"`` when it has one (:meth:`whygraph.analyze.Item.as_dict`).
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
    if generated is not None:
        card["evidence"] = card_evidence_refs(evidence)
        card["generated"] = generated
    return card


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
        super().__init__(
            message,
            code=BUDGET_EXCEEDED
            if reason == BUDGET_EXCEEDED
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
        raise NoEvidenceError(_NOT_PUSHED_MESSAGE, code="no_evidence")
    try:
        card = remote.rationale(sendable, split.pushed)
    except RemoteError as exc:
        raise WhyGraphError(str(exc)) from exc
    return {**card, "platform": platform_block(remote)}


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


def _start_completion(
    target: Target,
    evidence: list[CommitEvidence],
    provider: str,
    model: str | None,
    allow_description: Callable[[], bool] | None,
    before_generate: Callable[[], None] | None,
) -> bool:
    """Start completing a partial card in the background, unless one may not start.

    The running set and the last-attempt map are keyed by
    ``(db_key, path, line_start, line_end, provider, model)``, so two
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


def _complete_partial(
    target: Target,
    evidence: list[CommitEvidence],
    provider: str,
    model: str | None,
    *,
    allow_description: Callable[[], bool] | None,
    before_generate: Callable[[], None] | None,
    stop: threading.Event,
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
    """
    try:
        if stop.is_set():
            return
        seen = lookup_exact(target, provider, model)
        if seen is None or not seen.deferred_shas or seen.is_stale(evidence):
            return
        deferred = frozenset(seen.deferred_shas.split())
        fresh = _fresh_evidence(evidence)
        if stop.is_set():
            return
        outcome = backfill_evidence_descriptions(
            fresh,
            target_path=target.path,
            allow=allow_description,
            max_commits=len(fresh),
            wall_sec=None,
            workers=1,
        )
        if stop.is_set():
            return
        if outcome.waiting:
            update_deferred(
                target,
                provider,
                model,
                seen=seen,
                deferred_shas=" ".join(sorted(outcome.waiting)),
            )
            return
        if not deferred - outcome.terminal:
            # Nothing gained: every deferred commit can never be described.
            update_deferred(target, provider, model, seen=seen, deferred_shas="")
            return
        if not _same_row(lookup_exact(target, provider, model), seen):
            return
        if usage_blocked() is not None:
            return
        if before_generate is not None:
            before_generate()
        rationale = RationaleGenerator.from_config(get_config()).generate(
            fresh,
            symbol_context=_symbol_context(target),
            subject=target.qualified_name or target.path,
        )
        if stop.is_set():
            return
        # The fingerprint is over SHAs only: the request's evidence is the row's.
        store_cached(
            target, evidence, rationale, provider, model, deferred_shas="", only_if=seen
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
) -> dict | None:
    """The looser lookup behind ``cached_only``: a stale or overlapping card.

    Reads only the cache and git - no LLM call, no backfill, no store.
    The exact key's row is returned whatever its fingerprint (``stale``
    when the lines got new commits since); without one, the best row of
    the same file whose range overlaps ``target``
    (:func:`~whygraph.mcp.rationale_cache.lookup_overlapping`), whose
    ``stale`` is checked against the evidence of its own range.

    Returns
    -------
    dict or None
        The card plus ``match``, ``stale`` and (overlap) ``card_range``;
        ``None`` when no row matches. ``partial`` / ``describing`` come
        from the row; nothing is started to complete it.
    """
    generated = False if mark_generated else None
    row = lookup_exact(target, provider, pinned_model)
    if row is not None:
        card = _format_response(
            target,
            row.rationale,
            evidence,
            row.cached_at,
            generated=generated,
            **_partial_fields(row),
        )
        return {**card, "match": "exact", "stale": row.is_stale(evidence)}
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
        card_evidence,
        row.cached_at,
        generated=generated,
        **_partial_fields(row),
    )
    return {
        **card,
        "match": "overlap",
        "stale": row.is_stale(card_evidence),
        "card_range": {"line_start": row.line_start, "line_end": row.line_end},
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
        still subject to the generation gates and ``before_generate``).
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

    Returns
    -------
    dict
        The card (:func:`_format_response`). A card generated while
        evidence commits still lacked a description is stored with them as
        its deferred SHAs and returned ``partial`` with ``describing`` set;
        a hit on such a card is served at once, and either case starts a
        background completion (``completing``) when the caller may spend
        (:func:`_start_completion`). A ``cached_only`` call starts nothing.

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
        raise NoEvidenceError(_NO_EVIDENCE_MESSAGE, code="no_evidence")

    config = get_config()
    provider, pinned_model = config.cache_identity("rationale")
    cached = (
        None
        if force_refresh
        else lookup_cached(target, evidence, provider, pinned_model)
    )
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
            )
        card = _format_response(
            target,
            cached.rationale,
            evidence,
            cached.cached_at,
            generated=False if mark_generated else None,
            completing=completing,
            **fields,
        )
        return {**card, "match": "exact", "stale": False} if cached_only else card
    if cached_only and not force_refresh:
        loose = _cached_only_card(
            target, evidence, provider, pinned_model, mark_generated=mark_generated
        )
        if note is not None:
            note(
                "rationale cache miss"
                if loose is None
                else f"rationale cache hit ({loose['match']}"
                + (", stale)" if loose["stale"] else ")")
            )
        return loose or {"status": "not_cached", "target": target_dict(target)}
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
    # Cache miss — lazily backfill any commit whose `llm_description` is
    # NULL (e.g. after a `--skip-analyze` scan) so the
    # rationale prompt sees the richer per-commit summaries. Bulk commits
    # are described per-file against the target's path instead. The cache
    # fingerprint is sha256-over-sorted-SHAs, so backfilling here does
    # not affect cache keys. A card may describe every evidence commit;
    # the wall still bounds the wait, and what it leaves waiting is stored
    # as the card's deferred SHAs and completed in the background.
    outcome = backfill_evidence_descriptions(
        evidence,
        target_path=target.path,
        allow=allow_description,
        max_commits=len(evidence),
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
        )
    except (AnalyzeError, LlmError) as exc:
        raise RationaleGenerationError.wrap(
            "rationale generation failed", exc, code="provider_error"
        )

    waiting = sorted(outcome.waiting)
    cached_at = store_cached(
        target,
        evidence,
        rationale,
        provider,
        pinned_model,
        deferred_shas=" ".join(waiting),
    )
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
        )
    )
    return _format_response(
        target,
        rationale,
        evidence,
        cached_at,
        generated=True if mark_generated else None,
        partial=bool(waiting),
        describing=len(waiting),
        completing=completing,
    )


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


def whygraph_rationale_brief(
    path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    qualified_name: str | None = None,
    force_refresh: bool = False,
    cached_only: bool = False,
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
    none (a linked project's card is the platform's, which ignores it).
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
        return linked_rationale(ctx.remote, target)
    return _brief(
        target,
        force_refresh=force_refresh,
        cached_only=cached_only,
        progress=progress,
        note=note,
    )


def _brief(
    target: Target,
    *,
    force_refresh: bool,
    cached_only: bool,
    progress: Callable[[int, str], None] | None,
    note: Callable[[str], None] | None,
) -> RationaleCard:
    """The non-linked half of :func:`whygraph_rationale_brief`.

    Parameters
    ----------
    target : Target
        The resolved target.
    force_refresh, cached_only : bool
        As for :func:`whygraph_rationale_brief`.
    progress, note : callable or None
        The client channel from :func:`_client_channel`.

    Returns
    -------
    RationaleCard
        The card, the ``cached_only`` card or the ``not_cached`` miss.

    Raises
    ------
    NoEvidenceError
        The target has no commits.
    """
    evidence = collect_evidence(target, limit=20)
    if progress is not None:
        progress(1, f"evidence collected ({len(evidence)} commits)")
    return rationale_card(
        target,
        evidence,
        force_refresh=force_refresh,
        cached_only=cached_only,
        mark_generated=True,
        progress=progress,
        note=note,
    )


def _rationale_brief_tool(
    path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    qualified_name: str | None = None,
    force_refresh: bool = False,
    cached_only: bool = False,
    ctx: Context | None = None,
) -> RationaleCard:
    """The registered ``whygraph_rationale_brief``: the card plus ``notes``.

    Same signature and behaviour as :func:`whygraph_rationale_brief`, then
    every non-linked outcome gains the target's confirmed notes (data the
    members wrote, never fed into the card). A linked project gets the
    platform's card unchanged. Code with no commits raises
    ``no_evidence`` as before, unless confirmed notes exist, in which case
    ``{"status": "no_evidence", "target", "notes"}`` is returned.
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
        return linked_rationale(project.remote, target)
    notes = notes_for_target(target)
    try:
        card = _brief(
            target,
            force_refresh=force_refresh,
            cached_only=cached_only,
            progress=progress,
            note=note,
        )
    except NoEvidenceError:
        if not notes:
            raise
        return {"status": "no_evidence", "target": target_dict(target), "notes": notes}
    if notes:
        card["notes"] = notes
    return card


def register(mcp: FastMCP) -> None:
    """Attach the rationale tool to an MCP server."""
    mcp.tool(
        name="whygraph_rationale_brief",
        title=RATIONALE_BRIEF_ANNOTATIONS.title,
        description=RATIONALE_BRIEF_DESCRIPTION,
        annotations=RATIONALE_BRIEF_ANNOTATIONS,
    )(log_tool_errors(structured(RationaleCard)(_rationale_brief_tool)))
