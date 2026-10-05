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
  card budget first; a hit is served either way.
* ``GET /history``, ``/commits/{sha}``, ``/prs/{number}``,
  ``/issues/{number}``, ``/overview`` - the MCP resource bodies.

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
``generation_limited`` and ``403 generation_disabled``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy.exc import OperationalError

from whygraph.analyze import AnalyzeError
from whygraph.api_v1 import (
    MAX_LIMIT,
    MAX_PATH,
    EvidenceIn,
    RationaleIn,
    RepoPath,
    Sha,
    TargetIn,
    blame_hunks_from_in,
)
from whygraph.core import get_config
from whygraph.core.config import AnalyzeConfig, RationaleConfig, is_agent_limit
from whygraph.mcp.errors import WhyGraphError
from whygraph.mcp.evidence import (
    _evidence_dict,
    backfill_evidence_descriptions,
    collect_evidence,
    evidence_from_hunks,
)
from whygraph.mcp.path_history import area_history_commits
from whygraph.mcp.rationale import (
    NoEvidenceError,
    RationaleGenerationError,
    rationale_card,
)
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


def org_limit(org_id: int, section: str, key: str) -> int:
    """An ``agent_*_per_hour`` limit from the org layer row (its default when unset).

    Parameters
    ----------
    org_id : int
        The organization.
    section, key : str
        ``"rationale"`` / ``"agent_generations_per_hour"`` or ``"analyze"`` /
        ``"agent_descriptions_per_hour"``.

    Returns
    -------
    int
        The stored value, or the dataclass default when the org layer does
        not set a valid one. A project layer is never consulted.
    """
    with portal_session() as db:
        layer = load_layer(db, None, org_id=org_id)
    table = layer.get(section)
    value = table.get(key) if isinstance(table, dict) else None
    if is_agent_limit(value):
        return value  # type: ignore[return-value] -- checked by is_agent_limit
    defaults = {"rationale": RationaleConfig(), "analyze": AnalyzeConfig()}
    return getattr(defaults[section], key)


def _no_llm_key(provider: str) -> ApiError:
    return ApiError(
        409,
        f"no API key is configured for this project's rationale provider "
        f"({provider}); only cached cards can be served",
        code="no_llm_key",
    )


def _before_generate(state: PortalState, org_id: int) -> None:
    """What a cache miss must pass before an LLM call is made.

    The key first (nothing is spent on a card that cannot be generated),
    then the org's hourly card budget. A cache **hit** never runs this, so
    an org without a key still gets the cards it already paid for.

    Raises
    ------
    ApiError
        ``409 no_llm_key``, ``403 generation_disabled`` or
        ``429 generation_limited``.
    """
    missing = _missing_key(get_config(), ("rationale",))
    if missing is not None:
        raise _no_llm_key(missing)
    _spend_card(state, org_id)


def _spend_card(state: PortalState, org_id: int) -> None:
    """Count one generated card against the org's budget, or refuse."""
    limit = org_limit(org_id, "rationale", "agent_generations_per_hour")
    if limit == 0:
        raise ApiError(
            403,
            "this organization does not let agents generate rationale cards; "
            "only cached cards are served",
            code="generation_disabled",
        )
    retry = state.agent_budget.hit(f"card:{org_id}", limit=limit)
    if retry is not None:
        raise ApiError(
            429,
            "this organization's hourly limit of generated rationale cards is "
            "reached; cached cards are still served",
            code="generation_limited",
            headers={"Retry-After": str(retry)},
            retry_after=retry,
        )


def _description_budget(state: PortalState, org_id: int) -> Callable[[], bool]:
    """The backfill's ``allow``: one ``desc:<org_id>`` event per described commit."""
    limit: int | None = None

    def allow() -> bool:
        nonlocal limit
        if limit is None:
            limit = org_limit(org_id, "analyze", "agent_descriptions_per_hour")
        return state.agent_budget.hit(f"desc:{org_id}", limit=limit) is None

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
    return status.model_dump(mode="json")


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
            allow=_description_budget(state, project.org_id),
        )
    return {
        "evidence": [_evidence_dict(item) for item in result.evidence],
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
) -> dict:
    """A rationale card: the platform's own symbol range when it knows the name."""
    state = portal_state(request)
    status = _status(project, principal)
    with _heavy_slot(state, project.org_id), _mapped_errors():
        name = body.target.qualified_name
        target = indexed_symbol_target(name) if name else None
        if target is not None:
            # As the Explorer: the platform's range, its own blame of it.
            evidence = collect_evidence(target, limit=20)
        else:
            target = _target(body.target, keep_name=False)
            evidence = evidence_from_hunks(
                Repository(project.root),
                target,
                blame_hunks_from_in(body.hunks),
                limit=20,
                git_budget=GIT_BUDGET,
            ).evidence
        card = rationale_card(
            target,
            evidence,
            before_generate=lambda: _before_generate(state, project.org_id),
            allow_description=_description_budget(state, project.org_id),
        )
    return {**card, "project": status}


@v1_router.get("/history")
def get_history(
    request: Request,
    project: BoundProject = Depends(_PROJECT),
    principal: Principal = Depends(v1_user),
    _limit: None = Depends(_light),
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
            items, target_path=path, allow=_description_budget(state, project.org_id)
        )
    return {
        "path": path,
        "include_renames": include_renames,
        "evidence": [_evidence_dict(item) for item in items],
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
) -> dict:
    """A pull request and the issues it closes."""
    return _resource(project, principal, lambda: _pr_resource(number))


@v1_router.get("/issues/{number}")
def get_issue(
    number: int,
    project: BoundProject = Depends(_PROJECT),
    principal: Principal = Depends(v1_user),
    _limit: None = Depends(_light),
) -> dict:
    """An issue and the PRs that close it."""
    return _resource(project, principal, lambda: _issue_resource(number))


@v1_router.get("/overview")
def get_overview(
    project: BoundProject = Depends(_PROJECT),
    principal: Principal = Depends(v1_user),
    _limit: None = Depends(_light),
) -> dict:
    """Repository-level counts, freshness and top contributors."""
    return _resource(project, principal, _repo_overview_resource)


__all__ = ["GIT_BUDGET", "MAX_BODY_BYTES", "org_limit", "v1_router"]
