"""Production's ``/api/v1`` data routes for connected portals (M2e plan section 4.5).

A connected local portal answers its agents' MCP calls for a linked
project from here. Every route is **production-only**
(:func:`~whygraph.portal.deps.require_production` runs first, so local
mode answers ``404``), lives on the org host under
``/api/v1/projects/{slug}``, authenticates with a connection token only
(:func:`~whygraph.portal.deps.v1_project_db_access`: the token's one
project, by id, at the user's current role, initialized) and runs under
that project's context. Every answer carries ``project`` - the
:class:`~whygraph.api_v1.StatusOut` - and every refusal is the
:class:`~whygraph.api_v1.ErrorOut` envelope:

* ``GET ""`` - the :class:`~whygraph.api_v1.StatusOut` itself.
* ``POST /evidence`` - :func:`~whygraph.mcp.evidence.evidence_from_hunks`
  over the request's (pushed, committed) hunks against the server clone,
  with a git budget of :data:`GIT_BUDGET` blame calls, then the lazy
  description backfill within the org's description budget.
* ``POST /rationale`` - when ``target.qualified_name`` is in the
  platform's own CodeGraph index (no freshness check, no re-sync), the
  card is built from **the platform's** range and its own
  :func:`~whygraph.mcp.evidence.collect_evidence` of it - exactly what the
  Explorer does - so every checkout shares one cache row and the hunks are
  ignored; otherwise from the hunks over the request's range. A cache miss
  needs a configured rationale key (``409 no_llm_key``) and spends the org's
  card budget first; a hit is served either way. The optional ``intent``,
  ``depth`` and ``cached_only`` choose the card's depth with the platform
  project's settings (plan 62 section 14.4); a ``cached_only`` miss answers
  ``{"status": "not_cached", "target"}``.
* ``GET /history``, ``/commits/{sha}``, ``/prs/{number}``,
  ``/issues/{number}``, ``/overview`` - the MCP resource bodies.
* ``POST /plans/for``, ``GET /plans/{stem}``, ``/plans/{stem}/sections/{n}``
  - committed plans (plan 61).
* ``POST /notes`` - the **one write route** (``project.chat``, plan 63): a
  note proposal by the token's user, ``source="mcp"``,
  ``origin="link:<connection id>"``, both set here and never by the client;
  a symbol resolves from the index only (no ``codegraph sync``); an anchor or
  ref the server clone lacks is retried as project-wide / dropped
  (``not_on_platform``). ``POST /notes/for``, ``GET /notes/project`` and
  ``GET /notes/{note_id}`` serve **link-trusted** confirmed notes only
  (:func:`~whygraph.mcp.notes.link_trusted`). Every note leaves through
  :class:`~whygraph.api_v1.NoteOut`, so no uid and no ``origin`` reach the
  wire.

Each of the fourteen data routes counts one agent call (``v1:evidence``,
``v1:rationale``, ``v1:history``, ``v1:commit``, ``v1:pr``, ``v1:issue``,
``v1:overview``, ``v1:plans_for``, ``v1:plan``, ``v1:plan_section``,
``v1:note_propose``, ``v1:notes_for``, ``v1:notes_project``, ``v1:note``)
once it passed its rate limit; the status route ``GET ""``, which a local
portal polls, is not agent activity and is not counted.

Limits (on :class:`~whygraph.portal.deps.PortalState`): ``v1_token`` per
token for the reads, ``v1_heavy`` per token for evidence and rationale,
``v1_in_flight`` per org for the heavy pair (``503 busy`` without waiting),
and the org's hourly ``agent_generations_per_hour`` /
``agent_descriptions_per_hour`` - read from the **org layer row** only,
never from a project's merged config.

Errors: ``404 no_evidence``, ``409 no_llm_key`` (generation failed and the
rationale provider has no key), ``503 llm_unavailable`` (any other
generation failure), ``422 bad_hunk`` (a ``GitError`` - git's stderr is
never returned), ``422 bad_request`` (anything else a request got wrong,
redacted), ``404 not_found`` (an unknown commit / PR / issue), ``413
body_too_large`` past :data:`MAX_BODY_BYTES`, ``429 throttled`` /
``generation_limited`` and ``403 generation_disabled``. The note routes map
the note codes first (``note_invalid`` / ``note_check_invalid`` ``400``,
``note_not_found`` ``404``, ``note_state`` ``409``, ``note_limit`` ``429``,
redacted messages) and a locked project DB to ``503 busy``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy.exc import OperationalError
from sqlmodel import col, select

from whygraph.analyze import AnalyzeError
from whygraph.api_v1 import (
    MAX_LIMIT,
    MAX_LINE,
    MAX_PATH,
    MAX_PLAN_SECTION,
    PLATFORM_BUDGET_MESSAGE,
    EvidenceIn,
    NoteOut,
    NoteProposeIn,
    NotesForIn,
    PlansForIn,
    RationaleIn,
    RepoPath,
    Sha,
    TargetIn,
    blame_hunks_from_in,
)
from whygraph.core import get_config
from whygraph.core.config import AnalyzeConfig, RationaleConfig, is_agent_limit
from whygraph.core.usage import BUDGET_EXCEEDED, count_agent_call
from whygraph.db import get_session
from whygraph.db.models.project_note import ProjectNote
from whygraph.mcp import notes
from whygraph.mcp.errors import WhyGraphError
from whygraph.mcp.evidence import (
    backfill_evidence_descriptions,
    collect_evidence,
    evidence_dicts,
    evidence_from_hunks,
    review_scope,
)
from whygraph.mcp.path_history import area_history_commits
from whygraph.mcp.plans import _plan_resource, _plan_section_resource, plans_for
from whygraph.mcp.rationale import (
    ORG_SUSPENDED,
    GenerationNotPermitted,
    NoEvidenceError,
    RationaleGenerationError,
    rationale_card,
)
from whygraph.mcp.rationale_depth import effective_ceiling, resolve_for_target
from whygraph.mcp.resources import (
    _DB_UNSCANNED_MESSAGE,
    _commit_resource,
    _issue_resource,
    _pr_resource,
    _repo_overview_resource,
)
from whygraph.mcp.targets import Target, indexed_symbol_target
from whygraph.services.git import GitError, Repository
from whygraph.services.git.credentials import redact_tokens
from whygraph.services.llm import LlmError

from .authz import Action
from .config_layers import load_layer
from .db import get_session as portal_session
from .deps import (
    ApiError,
    BoundProject,
    PortalState,
    portal_state,
    require_production,
    v1_project_db_access,
    v1_user,
)
from .models import Membership, ProjectGrant, User
from .notes_routes import NOTE_ERROR_STATUS
from .routes import _missing_key
from .security import Principal
from .v1_status import load_status

MAX_BODY_BYTES = 256 * 1024
"""Largest ``/api/v1`` request body (``413 body_too_large`` above it)."""

GIT_BUDGET = 50
"""Most ``git blame`` calls one evidence or rationale request may make."""

_M = TypeVar("_M", bound=BaseModel)

_SHA = TypeAdapter(Sha)
_PATH = TypeAdapter(RepoPath)

_PROJECT = v1_project_db_access(Action.PROJECT_READ)
_PROPOSE = v1_project_db_access(Action.PROJECT_CHAT)

v1_router = APIRouter(
    prefix="/api/v1/projects/{slug}", dependencies=[Depends(require_production)]
)
"""Every route of this module; included before the ``/api`` 404 catch-all."""


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def _rate(name: str) -> Callable[..., Awaitable[None]]:
    """The per-token rate limit dependency over ``PortalState.<name>``."""

    async def dependency(
        request: Request, principal: Principal = Depends(v1_user)
    ) -> None:
        throttle = getattr(portal_state(request), name)
        retry = throttle.hit(principal.token_id)
        if retry is not None:
            raise ApiError(
                429,
                "too many requests; try again later",
                code="throttled",
                headers={"Retry-After": str(retry)},
                retry_after=retry,
            )

    return dependency


_light = _rate("v1_token")
_heavy = _rate("v1_heavy")


def _counted(kind: str) -> Callable[[], Awaitable[None]]:
    """The dependency that counts a data call as agent activity (M2f-3).

    Declared after the route's throttle (and body) dependencies, which
    FastAPI resolves in signature order, so a refused (``429``) call is
    never counted. The usage sink the project binding put up carries the
    attribution (:meth:`~whygraph.portal.usage.PortalUsageSink.count_call`).
    """

    async def dependency() -> None:
        count_agent_call(kind)

    return dependency


@contextmanager
def _heavy_slot(state: PortalState, org_id: int) -> Iterator[None]:
    """Hold one of the org's in-flight heavy slots, or refuse (``503 busy``)."""
    if not state.v1_in_flight.try_acquire(org_id):
        raise ApiError(
            503,
            "too many requests in flight for this organization; retry shortly",
            code="busy",
            headers={"Retry-After": "1"},
            retry_after=1,
        )
    try:
        yield
    finally:
        state.v1_in_flight.release(org_id)


def org_limit(org_id: int, section: str, *keys: str) -> int | tuple[int, ...]:
    """``agent_*_per_hour`` limits from the org layer row (defaults when unset).

    The layer is read once for all ``keys``.

    Parameters
    ----------
    org_id : int
        The organization.
    section : str
        ``"rationale"`` or ``"analyze"``.
    *keys : str
        The limit keys of that section, e.g. ``"agent_generations_per_hour"``
        and ``"agent_generations_per_member_per_hour"``.

    Returns
    -------
    int or tuple of int
        One value per key (a bare ``int`` for a single key): the stored
        value, or the dataclass default when the org layer does not set a
        valid one. A project layer is never consulted.
    """
    with portal_session() as db:
        layer = load_layer(db, None, org_id=org_id)
    table = layer.get(section)
    table = table if isinstance(table, dict) else {}
    defaults = {"rationale": RationaleConfig(), "analyze": AnalyzeConfig()}[section]
    values = tuple(
        table[key] if is_agent_limit(table.get(key)) else getattr(defaults, key)
        for key in keys
    )
    return values[0] if len(keys) == 1 else values


def _no_llm_key(provider: str) -> ApiError:
    return ApiError(
        409,
        f"no API key is configured for this project's rationale provider "
        f"({provider}); only cached cards can be served",
        code="no_llm_key",
    )


def _before_generate(state: PortalState, org_id: int, user_id: int) -> None:
    """What a cache miss must pass before an LLM call is made.

    The key first (nothing is spent on a card that cannot be generated),
    then the org's and the member's hourly card budgets. A cache **hit**
    never runs this, so an org without a key still gets the cards it
    already paid for.

    Raises
    ------
    ApiError
        ``409 no_llm_key``, ``403 generation_disabled`` or
        ``429 generation_limited`` (the last two with ``scope``).
    """
    missing = _missing_key(get_config(), ("rationale",))
    if missing is not None:
        raise _no_llm_key(missing)
    _spend_card(state, org_id, user_id)


def _spend_card(state: PortalState, org_id: int, user_id: int) -> None:
    """Count one generated card against the org's and the member's budgets, or refuse."""
    org, member = org_limit(
        org_id,
        "rationale",
        "agent_generations_per_hour",
        "agent_generations_per_member_per_hour",
    )
    for scope, limit in (("org", org), ("member", member)):
        if limit == 0:
            who = "this organization does" if scope == "org" else "you do"
            raise ApiError(
                403,
                f"{who} not let agents generate rationale cards; "
                "only cached cards are served",
                code="generation_disabled",
                scope=scope,
            )
    org_key = f"card:{org_id}"
    retry = state.agent_budget.hit_all(
        [(org_key, org), (f"{org_key}:{user_id}", member)]
    )
    if retry is not None:
        scope = "org" if state.agent_budget.check(org_key, limit=org) else "member"
        which = "this organization's" if scope == "org" else "your"
        raise ApiError(
            429,
            f"{which} hourly limit of generated rationale cards is reached; "
            "cached cards are still served",
            code="generation_limited",
            scope=scope,
            headers={"Retry-After": str(retry)},
            retry_after=retry,
        )


def _description_budget(
    state: PortalState, org_id: int, user_id: int
) -> Callable[[], bool]:
    """The backfill's ``allow``: one org and one member event per described commit."""
    limits: tuple[int, int] | None = None

    def allow() -> bool:
        nonlocal limits
        if limits is None:
            limits = org_limit(
                org_id,
                "analyze",
                "agent_descriptions_per_hour",
                "agent_descriptions_per_member_per_hour",
            )  # type: ignore[assignment]
        org_key = f"desc:{org_id}"
        return (
            state.agent_budget.hit_all(
                [(org_key, limits[0]), (f"{org_key}:{user_id}", limits[1])]
            )
            is None
        )

    return allow


# ---------------------------------------------------------------------------
# Bodies, errors, the project
# ---------------------------------------------------------------------------


def _too_large() -> ApiError:
    return ApiError(
        413,
        f"the request body is larger than {MAX_BODY_BYTES} bytes",
        code="body_too_large",
    )


async def _read_body(request: Request) -> bytes:
    """The request body, refused (``413``) once it passes :data:`MAX_BODY_BYTES`."""
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isdigit():
            raise ApiError(400, "invalid Content-Length", code="bad_request")
        if int(declared) > MAX_BODY_BYTES:
            raise _too_large()
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise _too_large()
        chunks.append(chunk)
    return b"".join(chunks)


def _invalid(exc: ValidationError) -> ApiError:
    """``422 bad_request`` with where and what - never the offending input."""
    detail = [
        {
            "loc": list(err.get("loc", ())),
            "msg": err.get("msg"),
            "type": err.get("type"),
        }
        for err in exc.errors(include_url=False, include_input=False)
    ]
    return ApiError(422, "invalid request", code="bad_request", detail=detail)


def _json_body(model: type[_M]) -> Callable[..., Awaitable[_M]]:
    """The dependency reading and validating a capped JSON body as ``model``.

    Declared after the project and rate dependencies, so an unauthenticated
    or throttled request is refused before its body is read.
    """

    async def dependency(request: Request) -> _M:
        raw = await _read_body(request)
        try:
            return model.model_validate_json(raw)
        except ValidationError as exc:
            raise _invalid(exc) from None

    return dependency


def _checked(adapter: TypeAdapter, value: str) -> Any:
    try:
        return adapter.validate_python(value)
    except ValidationError as exc:
        raise _invalid(exc) from None


@contextmanager
def _mapped_errors() -> Iterator[None]:
    """Translate what the evidence / rationale code raises into ``ErrorOut``s."""
    try:
        yield
    except NoEvidenceError as exc:
        raise ApiError(404, str(exc), code="no_evidence") from None
    except GenerationNotPermitted as exc:
        # A viewer's cache miss (M2f-1 plan section 4.6) or an exhausted
        # hard-stopped budget (M2f-2 plan section 4.7): ahead of the generic
        # WhyGraphError below, which would answer 422. The budget message is
        # scope-neutral: a connected portal shows it as it is.
        if exc.reason == BUDGET_EXCEEDED:
            raise ApiError(
                403,
                PLATFORM_BUDGET_MESSAGE,
                code=BUDGET_EXCEEDED,
                scope=exc.scope,
            ) from None
        if exc.reason == ORG_SUSPENDED:
            # Defence in depth: bind_v1_project refuses a suspended org's
            # calls first (plan 59 section 4.5).
            raise ApiError(403, str(exc), code=ORG_SUSPENDED) from None
        raise ApiError(403, str(exc), code="generation_not_permitted") from None
    except (RationaleGenerationError, AnalyzeError, LlmError):
        missing = _missing_key(get_config(), ("rationale",))
        if missing is not None:
            raise _no_llm_key(missing) from None
        raise ApiError(
            503,
            "the LLM provider is unavailable; try again later",
            code="llm_unavailable",
        ) from None
    except GitError:
        raise _bad_hunk() from None
    except OperationalError:
        raise ApiError(422, _DB_UNSCANNED_MESSAGE, code="bad_request") from None
    except WhyGraphError as exc:
        if isinstance(exc.__cause__, GitError):
            raise _bad_hunk() from None
        raise ApiError(422, redact_tokens(str(exc)), code="bad_request") from None


def _bad_hunk() -> ApiError:
    return ApiError(
        422, "a hunk could not be blamed on this repository", code="bad_hunk"
    )


def _status(project: BoundProject, principal: Principal) -> dict:
    """The project's ``StatusOut`` for the token's user, as JSON."""
    with portal_session() as db:
        status = load_status(db, project.id, principal.user_id)
    if status is None:  # removed, or the membership ended, since the bind
        raise ApiError(404, "not found")
    out = status.model_dump(mode="json")
    # Plans (plan 61 section 4.7): the bound project's merged [plans].dir, which
    # v1_status (portal rows only) cannot see; "" when indexing is off.
    config = get_config()
    out["plans_dir"] = config.plans.dir
    # Depth (plan 62 section 14.5): the merged project setting and the
    # effective ceiling (the org's org_max_depth applied), for a linked
    # project's read-only view.
    out["rationale_depth"] = config.rationale.depth
    out["rationale_max_depth"] = effective_ceiling(config.rationale)
    return out


def _target(target: TargetIn, *, keep_name: bool = True) -> Target:
    return Target(
        path=target.path,
        line_start=target.line_start,
        line_end=target.line_end,
        qualified_name=target.qualified_name if keep_name else None,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@v1_router.get("")
def get_status(
    project: BoundProject = Depends(_PROJECT),
    principal: Principal = Depends(v1_user),
    _limit: None = Depends(_light),
) -> dict:
    """The linked project as this token sees it (``StatusOut``)."""
    return _status(project, principal)


@v1_router.post("/evidence")
def post_evidence(
    request: Request,
    project: BoundProject = Depends(_PROJECT),
    principal: Principal = Depends(v1_user),
    _limit: None = Depends(_heavy),
    body: EvidenceIn = Depends(_json_body(EvidenceIn)),
    _count: None = Depends(_counted("v1:evidence")),
) -> dict:
    """Evidence for a target's pushed hunks, from the server clone and its DB."""
    state = portal_state(request)
    status = _status(project, principal)
    target = _target(body.target)
    with _heavy_slot(state, project.org_id), _mapped_errors():
        result = evidence_from_hunks(
            Repository(project.root),
            target,
            blame_hunks_from_in(body.hunks),
            limit=body.limit,
            git_budget=GIT_BUDGET,
        )
        backfill_evidence_descriptions(
            result.evidence,
            target_path=target.path,
            allow=_description_budget(state, project.org_id, principal.user_id),
        )
        with get_session() as session:
            scope = review_scope(session, project.root, target.path)
    return {
        "evidence": evidence_dicts(result.evidence, scope),
        "unknown_shas": [hunk.sha for hunk in result.unknown],
        "project": status,
    }


@v1_router.post("/rationale")
def post_rationale(
    request: Request,
    project: BoundProject = Depends(_PROJECT),
    principal: Principal = Depends(v1_user),
    _limit: None = Depends(_heavy),
    body: RationaleIn = Depends(_json_body(RationaleIn)),
    _count: None = Depends(_counted("v1:rationale")),
) -> dict:
    """A rationale card: the platform's own symbol range when it knows the name."""
    state = portal_state(request)
    status = _status(project, principal)
    with _heavy_slot(state, project.org_id), _mapped_errors():
        name = body.target.qualified_name
        target = indexed_symbol_target(name) if name else None
        scope = None
        own_range = target is not None
        if own_range:
            # As the Explorer: the platform's range, its own blame of it, so
            # review threads are checked against the clone's lines.
            evidence = collect_evidence(target, limit=20)
            with get_session() as session:
                scope = review_scope(session, project.root, target.path)
        else:
            # The requester's working-tree numbers, not the clone's: no
            # review threads (slicing the clone could match a neighbour's).
            target = _target(body.target, keep_name=False)
            evidence = evidence_from_hunks(
                Repository(project.root),
                target,
                blame_hunks_from_in(body.hunks),
                limit=20,
                git_budget=GIT_BUDGET,
            ).evidence
        # The platform project's own depth settings (plan 62 section 14.4),
        # resolved before the cache lookup; the caller count only on the
        # clone's own range of the symbol.
        resolution = (
            resolve_for_target(
                target,
                evidence,
                intent=body.intent,
                depth=body.depth,
                cached_only=body.cached_only,
                count_callers=own_range,
            )
            if evidence
            else None
        )
        card = rationale_card(
            target,
            evidence,
            cached_only=resolution.cached_only if resolution is not None else False,
            resolution=resolution,
            before_generate=lambda: _before_generate(
                state, project.org_id, principal.user_id
            ),
            allow_description=_description_budget(
                state, project.org_id, principal.user_id
            ),
            review_scope=scope,
            target_range=(target.line_start, target.line_end),
        )
    return {**card, "project": status}


@v1_router.get("/history")
def get_history(
    request: Request,
    project: BoundProject = Depends(_PROJECT),
    principal: Principal = Depends(v1_user),
    _limit: None = Depends(_light),
    _count: None = Depends(_counted("v1:history")),
    path: str = Query(..., max_length=MAX_PATH),
    limit: int = Query(20, ge=1, le=MAX_LIMIT),
    include_renames: bool = Query(True),
) -> dict:
    """The area history of a path (the default-branch scope of the server clone)."""
    state = portal_state(request)
    status = _status(project, principal)
    path = _checked(_PATH, path)
    with _mapped_errors():
        items = area_history_commits(path, limit=limit, include_renames=include_renames)
        backfill_evidence_descriptions(
            items,
            target_path=path,
            allow=_description_budget(state, project.org_id, principal.user_id),
        )
        with get_session() as session:
            scope = review_scope(
                session, project.root, path, include_renames=include_renames
            )
    return {
        "path": path,
        "include_renames": include_renames,
        "evidence": evidence_dicts(items, scope),
        "project": status,
    }


def _resource(project: BoundProject, principal: Principal, read: Callable[[], dict]):
    status = _status(project, principal)
    with _mapped_errors():
        body = read()
    if body.get("error") == "not_found":
        raise ApiError(404, "not found in this project's history", code="not_found")
    return {**body, "project": status}


@v1_router.get("/commits/{sha}")
def get_commit(
    sha: str,
    project: BoundProject = Depends(_PROJECT),
    principal: Principal = Depends(v1_user),
    _limit: None = Depends(_light),
    _count: None = Depends(_counted("v1:commit")),
) -> dict:
    """A scanned commit and the PRs that contain it."""
    sha = _checked(_SHA, sha)
    return _resource(project, principal, lambda: _commit_resource(sha))


@v1_router.get("/prs/{number}")
def get_pr(
    number: int,
    project: BoundProject = Depends(_PROJECT),
    principal: Principal = Depends(v1_user),
    _limit: None = Depends(_light),
    _count: None = Depends(_counted("v1:pr")),
) -> dict:
    """A pull request and the issues it closes."""
    return _resource(project, principal, lambda: _pr_resource(number))


@v1_router.get("/issues/{number}")
def get_issue(
    number: int,
    project: BoundProject = Depends(_PROJECT),
    principal: Principal = Depends(v1_user),
    _limit: None = Depends(_light),
    _count: None = Depends(_counted("v1:issue")),
) -> dict:
    """An issue and the PRs that close it."""
    return _resource(project, principal, lambda: _issue_resource(number))


@v1_router.get("/overview")
def get_overview(
    project: BoundProject = Depends(_PROJECT),
    principal: Principal = Depends(v1_user),
    _limit: None = Depends(_light),
    _count: None = Depends(_counted("v1:overview")),
) -> dict:
    """Repository-level counts, freshness and top contributors."""
    return _resource(project, principal, _repo_overview_resource)


def _plans_target(body: PlansForIn) -> Target | None:
    """The symbol target of a ``plans/for`` request (scope ``symbol`` only).

    As ``/rationale``: the platform's own indexed range when the name
    resolves, else the sent range on the sent path (name dropped).
    """
    sent = body.target
    if body.scope != "symbol" or sent is None:
        return None
    if sent.qualified_name:
        found = indexed_symbol_target(sent.qualified_name)
        if found is not None:
            return found
    if sent.line_start is None or sent.line_end is None or not body.paths:
        return None
    return Target(
        path=body.paths[0], line_start=sent.line_start, line_end=sent.line_end
    )


@v1_router.post("/plans/for")
def post_plans_for(
    project: BoundProject = Depends(_PROJECT),
    principal: Principal = Depends(v1_user),
    _limit: None = Depends(_light),
    body: PlansForIn = Depends(_json_body(PlansForIn)),
    _count: None = Depends(_counted("v1:plans_for")),
) -> dict:
    """The plans that relate to a target's commits and paths (``PlansForOut``)."""
    status = _status(project, principal)
    with _mapped_errors():
        plans = plans_for(
            body.shas,
            body.paths,
            target=_plans_target(body),
            scope=body.scope,
            detail=body.detail,
        )
    return {"plans": plans, "project": status}


@v1_router.get("/plans/{stem}")
def get_plan(
    stem: str,
    project: BoundProject = Depends(_PROJECT),
    principal: Principal = Depends(v1_user),
    _limit: None = Depends(_light),
    _count: None = Depends(_counted("v1:plan")),
) -> dict:
    """A plan's digest, contents and links (never the whole plan)."""
    return _resource(project, principal, lambda: _plan_resource(stem))


@v1_router.get("/plans/{stem}/sections/{n}")
def get_plan_section(
    stem: str,
    n: int = Path(..., ge=0, le=MAX_PLAN_SECTION),
    project: BoundProject = Depends(_PROJECT),
    principal: Principal = Depends(v1_user),
    _limit: None = Depends(_light),
    _count: None = Depends(_counted("v1:plan_section")),
) -> dict:
    """One section of a plan, at most 8,000 characters."""
    return _resource(project, principal, lambda: _plan_section_resource(stem, str(n)))


# ---------------------------------------------------------------------------
# Notes (plan 63 section 4.2)
# ---------------------------------------------------------------------------

_FALLBACK_ANCHORS = ("path", "range", "symbol")
"""The anchor kinds a proposal may fall back from to ``project``."""


def _note_out(view: Any) -> dict:
    """One note as it may leave the platform (:class:`~whygraph.api_v1.NoteOut`).

    Every note a ``/notes`` route answers goes through here: the model drops
    ``proposed_by`` / ``confirmed_by`` / ``reconfirmed_by`` / ``retired_by``
    and ``origin``, and types everything a card line renders.
    """
    return NoteOut.model_validate(dict(view)).model_dump(mode="json", exclude_none=True)


def _admin_uids(project: BoundProject) -> frozenset[str]:
    """The uids whose **current** role on ``project`` can configure it.

    Org owners and admins (project admins everywhere) and members with an
    ``admin`` grant on this project, read from the portal DB on every call,
    so a demoted or removed admin's confirmations stop vouching at once. A
    disabled or soft-deleted account vouches for nothing.
    """
    live = (col(User.deleted_at).is_(None), col(User.disabled_at).is_(None))
    with portal_session() as db:
        org_admins = db.exec(
            select(User.uid)
            .join(Membership, col(Membership.user_id) == col(User.id))
            .where(
                col(Membership.org_id) == project.org_id,
                col(Membership.role).in_(("owner", "admin")),
                *live,
            )
        ).all()
        granted = db.exec(
            select(User.uid)
            .join(ProjectGrant, col(ProjectGrant.user_id) == col(User.id))
            .where(
                col(ProjectGrant.project_id) == project.id,
                col(ProjectGrant.org_id) == project.org_id,
                col(ProjectGrant.role) == "admin",
                *live,
            )
        ).all()
    return frozenset(org_admins) | frozenset(granted)


@contextmanager
def _note_errors() -> Iterator[None]:
    """:func:`_mapped_errors`, with the note codes and a busy DB mapped first.

    A note code keeps its status (:data:`NOTE_ERROR_STATUS`) and its code;
    its message is redacted. A locked project DB is ``503 busy``.
    """
    with _mapped_errors():
        try:
            yield
        except WhyGraphError as exc:
            status = NOTE_ERROR_STATUS.get(exc.code or "")
            if status is None:
                raise
            raise ApiError(status, redact_tokens(str(exc)), code=exc.code) from None
        except (sqlite3.OperationalError, OperationalError):
            raise ApiError(
                503,
                "the project database is busy; retry shortly",
                code="busy",
                headers={"Retry-After": "1"},
                retry_after=1,
            ) from None


@v1_router.post("/notes", status_code=201)
def post_note(
    response: Response,
    project: BoundProject = Depends(_PROPOSE),
    principal: Principal = Depends(v1_user),
    _limit: None = Depends(_light),
    body: NoteProposeIn = Depends(_json_body(NoteProposeIn)),
    _count: None = Depends(_counted("v1:note_propose")),
) -> dict:
    """Propose a note as the token's user (``NoteProposeOut``; ``201``, ``200`` on a duplicate).

    The proposer is the token's user and the origin its connection, both set
    here. An anchor the server clone lacks falls back to the project when
    ``fallback_to_project`` allows it, and a ref it has not scanned is
    dropped; each is reported as ``not_on_platform``.
    """
    status = _status(project, principal)
    anchor: dict[str, Any] = body.anchor.model_dump(exclude_none=True)
    refs = list(body.refs)
    dropped: list[dict[str, str]] = []
    downgraded: dict[str, str] | None = None
    # Each retry drops one ref or downgrades the anchor once, so at most
    # len(refs) + 1 retries; the bound is belt and braces.
    attempts = len(refs) + 2
    with _note_errors():
        # The local side refuses these too; a platform must not rely on it,
        # since any holder of a valid token can call this route.
        notes.refuse_secret_text(
            body.body,
            body.check.text if body.check else None,
            body.check.glob if body.check else None,
            anchor.get("area"),
        )
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                view = notes.propose(
                    body.body,
                    body.kind,
                    anchor,
                    source="mcp",
                    actor_uid=principal.uid,
                    origin=f"link:{principal.token_id}",
                    refs=refs,
                    check=body.check.model_dump() if body.check else None,
                    supersedes_id=body.supersedes_id,
                    symbol_sync=False,
                )
                break
            except notes.RefNotFound as exc:
                if last or not 0 <= exc.index < len(refs):
                    raise
                ref = refs.pop(exc.index)
                dropped.append({"ref": ref, "reason": "not_on_platform"})
            except notes.AnchorNotFound:
                kind = anchor["kind"]
                if (
                    last
                    or downgraded is not None
                    or not body.fallback_to_project
                    or kind not in _FALLBACK_ANCHORS
                ):
                    raise
                downgraded = {
                    "from": kind,
                    "to": "project",
                    "reason": "not_on_platform",
                }
                anchor = {"kind": "project"}
    result: dict[str, Any] = dict(notes.proposal_result(view, principal.uid))
    result.pop("review", None)
    if "note" in result:
        result["note"] = _note_out(result["note"])
    if downgraded is not None:
        result["anchor_downgraded"] = downgraded
    result["dropped_refs"] = dropped
    result["project"] = status
    response.status_code = 200 if result["duplicate"] else 201
    return result


@v1_router.post("/notes/for")
def post_notes_for(
    project: BoundProject = Depends(_PROJECT),
    principal: Principal = Depends(v1_user),
    _limit: None = Depends(_light),
    body: NotesForIn = Depends(_json_body(NotesForIn)),
    _count: None = Depends(_counted("v1:notes_for")),
) -> dict:
    """The link-trusted confirmed notes for a code target (``NotesForOut``)."""
    status = _status(project, principal)
    admins = _admin_uids(project)
    target = Target(
        path=body.path,
        line_start=body.line_start or 1,
        line_end=body.line_end or MAX_LINE,
        qualified_name=body.qualified_name,
    )
    with _note_errors():
        views = notes.notes_for_target(
            target, keep=lambda row: notes.link_trusted(row, admins)
        )
        out = [_note_out(view) for view in views]
    return {"notes": out, "project": status}


@v1_router.get("/notes/project")
def get_project_notes(
    project: BoundProject = Depends(_PROJECT),
    principal: Principal = Depends(v1_user),
    _limit: None = Depends(_light),
    _count: None = Depends(_counted("v1:notes_project")),
) -> dict:
    """The link-trusted confirmed project notes, purpose first, within budget."""
    status = _status(project, principal)
    admins = _admin_uids(project)
    with _note_errors():
        views = notes.project_notes(keep=lambda row: notes.link_trusted(row, admins))
        out = [_note_out(view) for view in views]
    return {"notes": out, "project": status}


def _trusted_note_resource(note_id: int, admins: frozenset[str]) -> dict:
    """``whygraph://note/{id}`` for a linked agent: confirmed and link-trusted only.

    Anything else - missing, proposed, retired, or self-confirmed by a
    non-admin - is ``{"error": "not_found"}``, never a different answer.
    """
    with _note_errors():
        with get_session() as session:
            row = session.get(ProjectNote, note_id)
            trusted = row is not None and notes.link_trusted(row, admins)
        if not trusted:
            return {"error": "not_found", "id": note_id}
        found = notes._note_resource(note_id)
        if found.get("error") == "not_found":
            return found
        return _note_out(found)


@v1_router.get("/notes/{note_id}")
def get_note(
    note_id: int = Path(..., ge=1),
    project: BoundProject = Depends(_PROJECT),
    principal: Principal = Depends(v1_user),
    _limit: None = Depends(_light),
    _count: None = Depends(_counted("v1:note")),
) -> dict:
    """One confirmed, link-trusted note (``NoteOut`` plus ``project``)."""
    admins = _admin_uids(project)
    return _resource(
        project, principal, lambda: _trusted_note_resource(note_id, admins)
    )


__all__ = ["GIT_BUDGET", "MAX_BODY_BYTES", "org_limit", "v1_router"]
