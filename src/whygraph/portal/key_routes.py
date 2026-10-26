"""The "Test key" and model-listing routes (M2f-3 plan section 4.13, plan 36).

Three ``POST`` routes test a **stored** key - no route accepts a key in its
body - and answer ``200 {ok, result, scope_tested, checked_at}``:

- ``POST /api/portal/defaults/keys/{provider}/test`` (:data:`key_router`,
  both modes, ``org.configure``): the org layer's key (locally the portal
  default); none stored -> ``409 key_missing``.
- ``POST /api/projects/{slug}/keys/{provider}/test`` (:data:`key_router`,
  both modes, ``project.configure``): the key the project would use
  (``ProjectContext.key_scopes``: its own or the org's);
  none -> ``409 key_missing``; a linked project -> ``403
  managed_on_platform``.
- ``POST /api/projects/{slug}/github-token/test`` (:data:`key_local_router`,
  local mode only - production answers ``404`` before ``current_user``):
  the token in effect against the project's own GitHub remote, through the
  repository probe the add-project form uses
  (:func:`whygraph.services.github.check_repo_access`); a remote not on
  GitHub -> ``422 not_github``, no token -> ``409 key_missing``.

An unknown provider is ``422 bad_provider``; Ollama, which has no key, is
``422 not_testable``. Every test that reaches a provider counts against one
:class:`~whygraph.portal.throttle.Throttle` (``state.key_test``): 10 per user
and 60 per org an hour, both or neither (``429 throttled`` with
``Retry-After``). A test spends no tokens and is not ledgered; production
audits it as ``key_tested`` (scope, provider, result - never key material).
The provider's or GitHub's answer is reduced to a result word
(:data:`~whygraph.portal.key_test.RESULTS`).

Two ``GET`` routes list a provider's model ids for Settings' model fields
(plan 36 section 4.1), with the same stored key a test would use - no route
accepts a key:

- ``GET /api/portal/defaults/models/{provider}`` (:data:`key_router`, both
  modes, ``org.configure``): the org layer's key and endpoint;
- ``GET /api/projects/{slug}/models/{provider}`` (:data:`key_router`, both
  modes, ``project.configure``): the key and endpoint the project would use;
  a linked project -> ``403 managed_on_platform``.

A provider that cannot be listed (Ollama included) is ``422 bad_provider``.
Otherwise the answer is always ``200 {provider, source, result, models}``:
``source`` is ``live`` with ``result`` ``ok`` and the provider's narrowed
models (:mod:`whygraph.portal.model_list`), or ``fallback`` with the
built-in list (:func:`~whygraph.portal.model_list.fallback_listing`) and the
reason - ``key_missing`` (no stored key: no outbound call, for every
provider, and nothing counted), ``throttled``, or the listing's own failure
word. Listings count against their own throttle (``state.model_list``): 30
per user and 120 per org an hour, both or neither. A listing is neither
audited nor ledgered.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request

from whygraph.services.github import GitHubError, RepoAccessError, check_repo_access

from .audit import audit
from .authz import Action, OrgAccess
from .config_layers import endpoint_of, load_layer
from .db import get_session
from .deps import (
    ApiError,
    BoundProject,
    PortalState,
    current_user,
    org_access,
    portal_state,
    project_access,
    require_local,
)
from .key_test import PROBES, KeyTestResult, probe_llm_key
from .model_list import LIST_PATHS, fallback_listing, list_llm_models
from .models import Project
from .routes import github_remote, github_token_scope, linked_guard
from .secrets import LLM_API_KEY, InvalidToken, read_secret
from .security import Principal

key_router = APIRouter(dependencies=[Depends(current_user)])
"""The LLM key tests (both modes); full paths, no prefix."""

key_local_router = APIRouter(
    dependencies=[Depends(require_local), Depends(current_user)]
)
"""Local mode's GitHub token test: ``404`` in production, before ``current_user``."""

USER_LIMIT = 10
"""Key tests per user per org an hour."""

ORG_LIMIT = 60
"""Key tests per org an hour."""

MODEL_LIST_USER_LIMIT = 30
"""Model listings per user per org an hour."""

MODEL_LIST_ORG_LIMIT = 120
"""Model listings per org an hour."""

_REPO_RESULTS = {
    "bad_token": "rejected",
    "no_access": "no_repo_access",
    "not_found": "no_repo_access",
}
"""The repository probe's refusal codes as result words (never errors)."""


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_provider(provider: str) -> None:
    """``422 not_testable`` for Ollama, ``422 bad_provider`` for an unknown tag."""
    if provider == "ollama":
        raise ApiError(422, "ollama has no key to test", code="not_testable")
    if provider not in PROBES:
        raise ApiError(422, f"unknown provider {provider!r}", code="bad_provider")


def _key_missing(what: str) -> ApiError:
    return ApiError(409, f"there is no stored {what} to test", code="key_missing")


def _throttle(state: PortalState, org_id: int, user_id: int) -> None:
    """Count one test for the user and the org, or refuse with ``429 throttled``."""
    retry = state.key_test.hit_all(
        [(("u", org_id, user_id), USER_LIMIT), (("o", org_id), ORG_LIMIT)]
    )
    if retry is not None:
        raise ApiError(
            429,
            "too many key tests; try again later",
            code="throttled",
            headers={"Retry-After": str(retry)},
        )


def _org_key(org_id: int, provider: str) -> tuple[str | None, str | None]:
    """The org layer's stored key for ``provider`` and its endpoint.

    Returns ``(key, base_url)``: the key is ``None`` when none is stored or
    the stored one is unreadable, the endpoint ``None`` when the org layer
    sets no ``[llm.<provider>].base_url``.
    """
    with get_session() as session:
        try:
            key = read_secret(
                session, kind=LLM_API_KEY, provider=provider, org_id=org_id
            )
        except InvalidToken:
            key = None  # unreadable: re-enter it, nothing to use
        layer = load_layer(session, None, org_id=org_id)
    return key, endpoint_of(layer, provider.replace("-", "_"))


def _project_key(
    project: BoundProject, provider: str
) -> tuple[str, str | None, str | None]:
    """The key ``project`` would use for ``provider``, its scope and endpoint.

    Returns ``(scope, key, base_url)`` from ``ProjectContext.key_scopes`` and
    the merged config: the key is ``None`` unless a stored project or org key
    is in effect.
    """
    scope = project.ctx.key_scopes.get(provider, "none")
    section = project.ctx.config.llm.section(provider)
    key: str | None = None
    if scope in ("project", "org"):
        key = getattr(section, "api_key", None)
    return scope, key, getattr(section, "base_url", None)


def _list_allowed(state: PortalState, org_id: int, user_id: int) -> bool:
    """Count one model listing for the user and the org; ``False`` when throttled."""
    retry = state.model_list.hit_all(
        [
            (("u", org_id, user_id), MODEL_LIST_USER_LIMIT),
            (("o", org_id), MODEL_LIST_ORG_LIMIT),
        ]
    )
    return retry is None


def _check_listable(provider: str) -> None:
    """``422 bad_provider`` for a provider whose models cannot be listed (Ollama too)."""
    if provider not in LIST_PATHS:
        raise ApiError(422, f"unknown provider {provider!r}", code="bad_provider")


def _model_listing(
    state: PortalState,
    provider: str,
    key: str | None,
    base_url: str | None,
    *,
    org_id: int,
    user_id: int,
) -> dict:
    """The always-``200`` listing body: live models, or the built-in ones."""
    if not key:
        result = "key_missing"  # no outbound call, no throttle hit
    elif not _list_allowed(state, org_id, user_id):
        result = "throttled"
    else:
        listing = list_llm_models(
            provider, key, base_url, transport=state.key_test_transport
        )
        if listing.result == "ok":
            return _listing_body(provider, "live", "ok", listing.models)
        result = listing.result
    return _listing_body(provider, "fallback", result, fallback_listing(provider))


def _listing_body(provider: str, source: str, result: str, models: tuple) -> dict:
    return {
        "provider": provider,
        "source": source,
        "result": result,
        "models": [{"id": m.id, "display_name": m.display_name} for m in models],
    }


def _answer(
    request: Request,
    state: PortalState,
    found: KeyTestResult,
    *,
    scope: str,
    provider: str,
    principal: Principal,
    org_id: int,
    project: str | None = None,
) -> dict:
    """The ``200`` body; in production the ``key_tested`` audit event first."""
    if state.mode == "production":
        audit(
            "key_tested",
            request,
            uid=principal.uid,
            org_id=org_id,
            org=request.scope.get("state", {}).get("org_slug"),
            project=project,
            scope=scope,
            provider=provider,
            result=found.result,
        )
    return {
        "ok": found.ok,
        "result": found.result,
        "scope_tested": scope,
        "checked_at": _checked_at(),
    }


@key_router.post("/api/portal/defaults/keys/{provider}/test")
def post_defaults_key_test(
    provider: str,
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_CONFIGURE)),
    principal: Principal = Depends(current_user),
) -> dict:
    """Test the org layer's stored key for ``provider`` (see the module docstring)."""
    state = portal_state(request)
    _check_provider(provider)
    key, base_url = _org_key(access.org_id, provider)
    if not key:
        raise _key_missing(f"{provider} key")
    _throttle(state, access.org_id, principal.user_id)
    found = probe_llm_key(provider, key, base_url, transport=state.key_test_transport)
    return _answer(
        request,
        state,
        found,
        scope="org",
        provider=provider,
        principal=principal,
        org_id=access.org_id,
    )


@key_router.post("/api/projects/{slug}/keys/{provider}/test")
def post_project_key_test(
    provider: str,
    request: Request,
    project: BoundProject = Depends(project_access(Action.PROJECT_CONFIGURE)),
    principal: Principal = Depends(current_user),
) -> dict:
    """Test the key ``project`` would use for ``provider`` (see the module docstring)."""
    state = portal_state(request)
    linked_guard("testing a key for this project")(project)
    _check_provider(provider)
    scope, key, base_url = _project_key(project, provider)
    if not key:
        raise _key_missing(f"{provider} key")
    _throttle(state, project.org_id, principal.user_id)
    found = probe_llm_key(provider, key, base_url, transport=state.key_test_transport)
    return _answer(
        request,
        state,
        found,
        scope=scope,
        provider=provider,
        principal=principal,
        org_id=project.org_id,
        project=project.slug,
    )


@key_router.get("/api/portal/defaults/models/{provider}")
def get_defaults_models(
    provider: str,
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_CONFIGURE)),
    principal: Principal = Depends(current_user),
) -> dict:
    """List ``provider``'s models with the org layer's stored key (module docstring)."""
    state = portal_state(request)
    _check_listable(provider)
    key, base_url = _org_key(access.org_id, provider)
    return _model_listing(
        state,
        provider,
        key,
        base_url,
        org_id=access.org_id,
        user_id=principal.user_id,
    )


@key_router.get("/api/projects/{slug}/models/{provider}")
def get_project_models(
    provider: str,
    request: Request,
    project: BoundProject = Depends(project_access(Action.PROJECT_CONFIGURE)),
    principal: Principal = Depends(current_user),
) -> dict:
    """List ``provider``'s models with the key ``project`` would use (module docstring)."""
    state = portal_state(request)
    linked_guard("listing models for this project")(project)
    _check_listable(provider)
    _scope, key, base_url = _project_key(project, provider)
    return _model_listing(
        state,
        provider,
        key,
        base_url,
        org_id=project.org_id,
        user_id=principal.user_id,
    )


def _probe_repo(owner: str, name: str, token: str) -> KeyTestResult:
    """The repository probe as a result word (:data:`_REPO_RESULTS`)."""
    try:
        check_repo_access(owner, name, token)
    except RepoAccessError as exc:
        return KeyTestResult.of(_REPO_RESULTS.get(exc.code, "unexpected"))
    except GitHubError:
        return KeyTestResult.of("unreachable")
    return KeyTestResult.of("ok")


@key_local_router.post("/api/projects/{slug}/github-token/test")
def post_github_token_test(
    request: Request,
    project: BoundProject = Depends(project_access(Action.PROJECT_CONFIGURE)),
    principal: Principal = Depends(current_user),
) -> dict:
    """Test the GitHub token in effect against the project's own GitHub remote."""
    state = portal_state(request)
    linked_guard("testing a token for this project")(project)
    with get_session() as session:
        row = session.get(Project, project.id)
        remote = github_remote(row, project.root) if row is not None else None
        scope = github_token_scope(session, project.id, project.org_id)
    if remote is None:
        raise ApiError(
            422,
            "this project's remote is not on GitHub",
            code="not_github",
        )
    token = project.ctx.config.scan_token
    if not token or scope == "none":
        raise _key_missing("GitHub token")
    owner, name = remote.split("/", 1)
    _throttle(state, project.org_id, principal.user_id)
    found = _probe_repo(owner, name, token)
    return _answer(
        request,
        state,
        found,
        scope=scope,
        provider="github",
        principal=principal,
        org_id=project.org_id,
        project=project.slug,
    )


__all__ = [
    "MODEL_LIST_ORG_LIMIT",
    "MODEL_LIST_USER_LIMIT",
    "ORG_LIMIT",
    "USER_LIMIT",
    "key_local_router",
    "key_router",
]
