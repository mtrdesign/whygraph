"""The project-purpose card over REST: ``/api/projects/{slug}/purpose`` (plan 41 section 5.4).

Two routers, mounted by :mod:`whygraph.portal.app` beside the notes routers
with ``linked_guard("Project purpose")``:

- :data:`purpose_read_router` (``project.read``): ``GET`` - the card's state
  (the live purpose notes, the last run, staleness, the estimate);
- :data:`purpose_write_router` (``project.scan_full``, usage source
  ``explorer``): ``POST`` - one generation.

A generation runs **in the portal process** (D3), in the request's worker
thread, as one bounded, non-retrying model call
(:func:`whygraph.analyze.project_purpose.generate`). In order: the role and
linked guards (the mount), the throttle, the per-project single-flight
(:data:`_RUNNING`, project **ids**: a slug repeats across orgs) and the
portal-wide cap (:data:`_SLOTS`), the runner claim (a removal answers
``ProjectBusy`` while it is held), the budget (``llm_allowed``, then
:func:`~whygraph.core.usage.usage_blocked`), the key, the inputs (read from
the git object store at ``HEAD``), the too-little-input refusal, the
unchanged-input short-circuit, the call, the proposal
(:func:`whygraph.mcp.notes.propose_purpose`), the run row and the audit.
The runner claim comes before the budget check so that every write to the
project's database (a ``blocked`` run row too) happens under it.

A ``project_purpose_run`` row is written once per run that reached
``ok``, ``failed``, ``no_input`` or ``blocked``; ``unchanged``, ``409``
and guard refusals write nothing. Audit records carry ids, enums, the
model and token counts - never a body or any repository text.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import delete, func
from sqlmodel import col, select

from whygraph.analyze.exceptions import AnalyzeError
from whygraph.analyze.project_purpose import (
    PurposeInputs,
    PurposeInvalid,
    gather_inputs,
    generate,
)
from whygraph.core.config import ConfigError
from whygraph.core.usage import BUDGET_EXCEEDED, budget_message, usage_blocked
from whygraph.db.engine import get_session as project_session
from whygraph.db.models.project_note import ProjectNote
from whygraph.db.models.project_purpose_run import ProjectPurposeRun
from whygraph.mcp import notes
from whygraph.mcp.rationale import GenerationNotPermitted
from whygraph.serve.errors import ServeError, WhyGraphError
from whygraph.services.git import GitError, Repository
from whygraph.services.llm import LlmError

from .audit import audit
from .authz import Action, OrgAccess, project_allowed
from .deps import BoundProject, bound_project, current_org, portal_state
from .estimate import purpose_estimate
from .notes_routes import _mapped, _one, _uid
from .prices import cost, has_custom_endpoint, price_for
from .routes import _missing_key
from .runner import ProjectBusy

purpose_read_router = APIRouter()
"""``GET``; mounted under ``project_db_access(PROJECT_READ)``."""

purpose_write_router = APIRouter()
"""``POST``; mounted under ``project_db_access(PROJECT_SCAN_FULL, usage_source="explorer")``."""

MIN_INPUT_BYTES = 500
"""Below this many bytes of gathered input, no call is made (``422 purpose_no_input``)."""

RUNS_KEPT = 50
"""Newest ``project_purpose_run`` rows kept per project (the producing runs of live notes too)."""

MAX_CONCURRENT = 2
"""Generations at once, portal-wide: each holds a threadpool thread for up to a minute."""

SLOT_WAIT_SEC = 5.0
"""How long a generation waits for a free slot before ``409 busy``.

Short on purpose: a request never queues behind two slow calls for minutes
while holding a threadpool thread; the member presses Generate again.
"""

USER_LIMIT = 10
"""Generation requests per member and org per hour (``429 throttled`` past it)."""

PROJECT_LIMIT = 20
"""Generation requests per project per hour."""

STALE_CACHE_MAX = 512
"""Most ``(project id, HEAD, run id)`` staleness answers kept; cleared past it."""

_RUNNING: set[int] = set()
"""Project ids with a generation in flight (the single-flight set)."""

_RUNNING_LOCK = threading.Lock()

_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)
"""The portal-wide cap on generations in flight."""

_STALE: dict[tuple[int, str, int], bool] = {}
"""``(project id, HEAD sha, producing run id)`` -> stale."""

_STALE_LOCK = threading.Lock()


def _now() -> str:
    """The current UTC time, ISO-8601 to the second."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Single-flight
# ---------------------------------------------------------------------------


def _claim(project_id: int) -> bool:
    """Mark a generation in flight for the project; ``False`` if one already is."""
    with _RUNNING_LOCK:
        if project_id in _RUNNING:
            return False
        _RUNNING.add(project_id)
        return True


def _release(project_id: int) -> None:
    with _RUNNING_LOCK:
        _RUNNING.discard(project_id)


def is_running(project_id: int) -> bool:
    """Whether a generation is in flight for ``project_id`` (this process).

    Parameters
    ----------
    project_id : int
        ``projects.id``.

    Returns
    -------
    bool
        ``True`` while a ``POST`` for the project is past the single-flight
        check and has not returned.
    """
    with _RUNNING_LOCK:
        return project_id in _RUNNING


# ---------------------------------------------------------------------------
# Run rows
# ---------------------------------------------------------------------------


def _record(**values: Any) -> int:
    """Write one ``project_purpose_run`` row, prune, and return its id.

    Pruning keeps the newest :data:`RUNS_KEPT` rows plus the newest ``ok``
    run of each live (proposed or confirmed) purpose note - at most two
    more - so pruning never turns a live note's staleness into "unknown".
    """
    with project_session() as session:
        row = ProjectPurposeRun(finished_at=_now(), **values)
        session.add(row)
        session.commit()
        session.refresh(row)
        newest = (
            select(ProjectPurposeRun.id)
            .order_by(col(ProjectPurposeRun.id).desc())
            .limit(RUNS_KEPT)
        )
        live = select(ProjectNote.id).where(
            col(ProjectNote.kind) == notes.PURPOSE_KIND,
            col(ProjectNote.status) != "retired",
        )
        producing = (
            select(func.max(ProjectPurposeRun.id))
            .where(
                col(ProjectPurposeRun.status) == "ok",
                col(ProjectPurposeRun.note_id).in_(live),
            )
            .group_by(col(ProjectPurposeRun.note_id))
        )
        session.exec(  # type: ignore[call-overload]
            delete(ProjectPurposeRun).where(
                col(ProjectPurposeRun.id).not_in(newest),
                col(ProjectPurposeRun.id).not_in(producing),
            )
        )
        session.commit()
        return int(row.id or 0)


def _producing_run(note_id: int) -> ProjectPurposeRun | None:
    """The newest ``ok`` run that produced ``note_id``, if any is kept."""
    with project_session() as session:
        row = session.exec(
            select(ProjectPurposeRun)
            .where(
                col(ProjectPurposeRun.note_id) == note_id,
                col(ProjectPurposeRun.status) == "ok",
            )
            .order_by(col(ProjectPurposeRun.id).desc())
        ).first()
        if row is not None:
            session.expunge(row)
        return row


def _last_run() -> ProjectPurposeRun | None:
    with project_session() as session:
        row = session.exec(
            select(ProjectPurposeRun).order_by(col(ProjectPurposeRun.id).desc())
        ).first()
        if row is not None:
            session.expunge(row)
        return row


def _live_note(live: dict) -> dict | None:
    """The note the latest generation stands for: the proposal, else the confirmed one."""
    return live.get("proposed") or live.get("confirmed")


# ---------------------------------------------------------------------------
# The body
# ---------------------------------------------------------------------------


def _show_cost(project: BoundProject) -> bool:
    return project_allowed(project.role, Action.PROJECT_USAGE)


def _run_cost(
    request: Request, project: BoundProject, run: ProjectPurposeRun
) -> float | None:
    """The run's cost at today's price (never stored), or ``None`` when unpriced."""
    if run.tokens_in is None and run.tokens_out is None:
        return None
    priced = price_for(
        run.provider,
        run.model,
        portal_state(request).prices.for_org(project.org_id),
        custom_endpoint=has_custom_endpoint(project.ctx.config, run.provider),
    )
    if priced is None:
        return None
    usage = SimpleNamespace(input_tokens=run.tokens_in, output_tokens=run.tokens_out)
    return round(float(cost(priced[0], usage)), 6)


def _run_view(
    request: Request, project: BoundProject, run: ProjectPurposeRun | None
) -> dict | None:
    if run is None:
        return None
    try:
        sources = json.loads(run.sources) if run.sources else []
    except ValueError:
        sources = []
    return {
        "status": run.status,
        "model": run.model,
        "provider": run.provider,
        "tokens_in": run.tokens_in,
        "tokens_out": run.tokens_out,
        "cost_usd": _run_cost(request, project, run) if _show_cost(project) else None,
        "finished_at": run.finished_at,
        "sources": sources if isinstance(sources, list) else [],
        "error_code": run.error_code,
    }


def _current_inputs(project: BoundProject) -> PurposeInputs:
    """Gather the inputs now, from ``HEAD``'s objects (never the working tree)."""
    with project_session() as session:
        return gather_inputs(session, Repository(project.root))


def _stale(project: BoundProject, run: ProjectPurposeRun) -> bool | None:
    """Whether the inputs changed since ``run`` (D5), cached per ``HEAD``.

    The hash covers only what ``HEAD``'s tree holds, so one answer per
    ``(project id, HEAD sha, run id)`` stays right until a commit moves
    ``HEAD``. ``None`` when the run has no hash or git cannot answer.
    """
    if run.input_hash is None or run.id is None:
        return None
    try:
        head = Repository(project.root).head_commit()
    except GitError:
        return None
    if head is None:
        return None
    key = (project.id, head, run.id)
    with _STALE_LOCK:
        hit = _STALE.get(key)
    if hit is not None:
        return hit
    try:
        inputs = _current_inputs(project)
    except GitError:
        return None
    value = inputs.input_hash != run.input_hash
    with _STALE_LOCK:
        if len(_STALE) >= STALE_CACHE_MAX:
            _STALE.clear()
        _STALE[key] = value
    return value


def _estimate(request: Request, project: BoundProject) -> dict:
    """The ``scan-estimate``-shaped estimate of one generation, with ``missing_key``."""
    config = project.ctx.config
    body = purpose_estimate(
        config, overrides=portal_state(request).prices.for_org(project.org_id)
    )
    body["missing_key"] = _missing_key(config, ("analyze",))
    if not _show_cost(project):
        body.update(cost=None, tokens=None, cost_hidden=True)
    return body


def _body(request: Request, project: BoundProject) -> dict:
    """The section 7 body of ``GET`` (and of ``POST``, which adds ``unchanged``)."""
    with _mapped():
        live = notes.purpose_notes()
    confirmed, proposed = live["confirmed"], live["proposed"]
    target = _live_note(live)
    producing = _producing_run(int(target["id"])) if target is not None else None
    return {
        "confirmed": _one(confirmed) if confirmed is not None else None,
        "proposed": _one(proposed) if proposed is not None else None,
        "running": is_running(project.id),
        "last_run": _run_view(request, project, _last_run()),
        "stale": _stale(project, producing) if producing is not None else None,
        "can_generate": project_allowed(project.role, Action.PROJECT_SCAN_FULL)
        and project.ctx.llm_allowed,
        "estimate": _estimate(request, project),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@purpose_read_router.get("")
def get_purpose(request: Request) -> dict:
    """The project-purpose card's state (plan 41 section 7).

    Returns
    -------
    dict
        ``confirmed`` / ``proposed`` (notes with display names, or
        ``None``), ``running``, ``last_run`` (``None`` before any run;
        ``cost_usd`` is ``None`` without ``project.usage``), ``stale``
        (``None`` when the live note has no producing run), ``can_generate``
        and ``estimate`` (``missing_key``; ``cost`` / ``tokens`` ``None``
        and ``cost_hidden`` without ``project.usage``).
    """
    return _body(request, bound_project(request))


def _audit(
    event: str,
    request: Request,
    access: OrgAccess,
    project: BoundProject,
    **fields: Any,
) -> None:
    audit(
        event,
        request,
        uid=_uid(request),
        org_id=project.org_id,
        org=access.org_slug,
        project=project.slug,
        **fields,
    )


def _throttle(request: Request, project: BoundProject, access: OrgAccess) -> None:
    """Count one request for the member and the project, or ``429 throttled``."""
    retry = portal_state(request).purpose_generate.hit_all(
        [
            (("u", access.org_id, access.user_id), USER_LIMIT),
            (("p", project.id), PROJECT_LIMIT),
        ]
    )
    if retry is not None:
        raise ServeError(
            429,
            f"too many purpose generations; try again in {retry} seconds",
            code="throttled",
        )


def _check_budget(project: BoundProject, uid: str | None, started_at: str) -> None:
    """``llm_allowed``, then the bound sink's budget (``rationale_card``'s order).

    A refusal writes a ``blocked`` run row, then raises the
    :class:`~whygraph.mcp.rationale.GenerationNotPermitted` every spending
    surface shares (``403 budget_exceeded`` with its ``scope``).
    """
    ctx = project.ctx
    reason, scope = None, None
    if not ctx.llm_allowed:
        reason, scope = ctx.llm_block or "role", ctx.llm_block_scope
    else:
        blocked = usage_blocked()
        if blocked is not None:
            reason, scope = BUDGET_EXCEEDED, blocked
    if reason is None:
        return
    _record(
        requested_by=uid or "",
        started_at=started_at,
        status="blocked",
        error_code=reason,
    )
    raise GenerationNotPermitted(
        "no project purpose was generated: " + budget_message(scope)
        if reason == BUDGET_EXCEEDED
        else "you may not generate a project purpose here",
        reason=reason,
        scope=scope,
    )


@purpose_write_router.post("")
def generate_purpose(
    request: Request, access: OrgAccess = Depends(current_org)
) -> dict:
    """Generate the project purpose: one model call, one proposed note (plan 41 section 4).

    Returns
    -------
    dict
        The ``GET`` body plus ``unchanged``: ``True`` when the live note's
        producing run read the same inputs (no call, no run row, no usage).

    Raises
    ------
    ServeError
        ``429 throttled``; ``409 purpose_in_progress`` (one at a time per
        project); ``409 busy`` (two generations already running
        portal-wide for :data:`SLOT_WAIT_SEC`, or the project is being
        removed); ``409 no_llm_key``; ``422 purpose_no_input`` (fewer than
        :data:`MIN_INPUT_BYTES`, no call); ``502 purpose_invalid`` (the
        answer failed validation; not retried); ``502 provider_error``.
    GenerationNotPermitted
        ``403 budget_exceeded`` when a hard-stopped budget covers the call.
    """
    project = bound_project(request)
    state = portal_state(request)
    _throttle(request, project, access)
    if not _claim(project.id):
        raise ServeError(
            409,
            "a project purpose is already being generated for this project",
            code="purpose_in_progress",
        )
    try:
        if not _SLOTS.acquire(timeout=SLOT_WAIT_SEC):
            raise ServeError(
                409,
                "other project purposes are being generated; try again shortly",
                code="busy",
            )
        try:
            with state.runner.claim_project(project.id):
                unchanged = _generate(request, access, project)
        except ProjectBusy as exc:
            raise ServeError(409, str(exc), code="busy") from exc
        finally:
            _SLOTS.release()
    finally:
        _release(project.id)
    body = _body(request, project)
    body["unchanged"] = unchanged
    return body


def _generate(request: Request, access: OrgAccess, project: BoundProject) -> bool:
    """The guarded part of a generation; ``True`` when the inputs were unchanged."""
    uid = _uid(request)
    config = project.ctx.config
    _check_budget(project, uid, _now())
    missing = _missing_key(config, ("analyze",))
    if missing is not None:
        raise ServeError(
            409,
            f"no stored key for the analyze provider ({missing})",
            code="no_llm_key",
        )
    inputs = _current_inputs(project)
    run: dict[str, Any] = {
        "requested_by": uid or "",
        "input_hash": inputs.input_hash,
        "input_bytes": inputs.input_bytes,
        "sources": json.dumps(inputs.sources),
    }
    if inputs.input_bytes < MIN_INPUT_BYTES:
        _record(
            started_at=_now(), status="no_input", error_code="purpose_no_input", **run
        )
        raise ServeError(
            422,
            f"fewer than {MIN_INPUT_BYTES} bytes to describe the project from; "
            "add a README or a manifest",
            code="purpose_no_input",
        )
    with _mapped():
        live = _live_note(notes.purpose_notes())
    if live is not None:
        producing = _producing_run(int(live["id"]))
        if producing is not None and producing.input_hash == inputs.input_hash:
            return True
    try:
        # What was asked for; a served model replaces it on success.
        run["provider"], run["model"] = config.model_for("analyze")
    except ConfigError:
        pass
    started_at = _now()
    try:
        result = generate(inputs, config=config)
    except PurposeInvalid as exc:
        _failed(request, access, project, run, started_at, "purpose_invalid")
        raise ServeError(
            502,
            "the model's answer was not a usable project purpose; nothing was saved",
            code="purpose_invalid",
        ) from exc
    except (LlmError, AnalyzeError, ConfigError, TimeoutError, OSError) as exc:
        _failed(request, access, project, run, started_at, "provider_error")
        raise ServeError(
            502,
            f"the model provider call failed ({type(exc).__name__})",
            code="provider_error",
        ) from exc
    run.update(
        provider=result.provider,
        model=result.model,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
    )
    try:
        with _mapped():
            view, retired = notes.propose_purpose(result.body, actor_uid=uid)
    except WhyGraphError as exc:
        _failed(request, access, project, run, started_at, exc.code or "note_state")
        raise
    run_id = _record(started_at=started_at, status="ok", note_id=view["id"], **run)
    _audit(
        "purpose_generated",
        request,
        access,
        project,
        run_id=run_id,
        status="ok",
        model=result.model,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
    )
    for note_id in retired:
        _audit(
            "note_retired",
            request,
            access,
            project,
            note_id=note_id,
            source=notes.PURPOSE_SOURCE,
            anchor_kind="project",
            reason="superseded",
        )
    return False


def _failed(
    request: Request,
    access: OrgAccess,
    project: BoundProject,
    run: dict[str, Any],
    started_at: str,
    error_code: str,
) -> None:
    """Record and audit a run whose model call was made and failed."""
    run_id = _record(
        started_at=started_at, status="failed", error_code=error_code, **run
    )
    _audit(
        "purpose_generated",
        request,
        access,
        project,
        run_id=run_id,
        status="failed",
        model=run.get("model"),
        tokens_in=run.get("tokens_in"),
        tokens_out=run.get("tokens_out"),
    )


__all__ = [
    "MAX_CONCURRENT",
    "MIN_INPUT_BYTES",
    "RUNS_KEPT",
    "SLOT_WAIT_SEC",
    "is_running",
    "purpose_read_router",
    "purpose_write_router",
]
