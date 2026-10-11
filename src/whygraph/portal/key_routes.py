"""The "Test key" routes (M2f-3 plan section 4.13, decision Q6).

Three ``POST`` routes test a **stored** key - no route accepts a key in its
body - and answer ``200 {ok, result, scope_tested, checked_at}``:

- ``POST /api/portal/defaults/keys/{provider}/test`` (:data:`key_router`,
  both modes, ``org.configure``): the org layer's key (locally the portal
  default); none stored -> ``409 key_missing``.
- ``POST /api/projects/{slug}/keys/{provider}/test`` (:data:`key_router`,
  both modes, ``project.configure``): the key the project would use
  (``ProjectContext.key_scopes``: its own, the org's or the environment's);
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
"""

from __future__ import annotations

import os
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

_REPO_RESULTS = {
    "bad_token": "rejected",
    "no_access": "no_repo_access",
    "not_found": "no_repo_access",
}
"""The repository probe's refusal codes as result words (never errors)."""


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _env_var(provider: str) -> str:
    """The environment variable an adapter falls back to (``ANTHROPIC_API_KEY``)."""
    return f"{provider.replace('-', '_').upper()}_API_KEY"


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
    with get_session() as session:
        try:
            key = read_secret(
                session, kind=LLM_API_KEY, provider=provider, org_id=access.org_id
            )
        except InvalidToken:
            key = None  # unreadable: re-enter it, nothing to test
        layer = load_layer(session, None, org_id=access.org_id)
    if not key:
        raise _key_missing(f"{provider} key")
    base_url = endpoint_of(layer, provider.replace("-", "_"))
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
    scope = project.ctx.key_scopes.get(provider, "none")
    section = project.ctx.config.llm.section(provider)
    key: str | None = None
    if scope in ("project", "org"):
        key = getattr(section, "api_key", None)
    elif scope == "environment":
        key = os.environ.get(_env_var(provider))
    if not key:
        raise _key_missing(f"{provider} key")
    base_url = getattr(section, "base_url", None)
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


__all__ = ["ORG_LIMIT", "USER_LIMIT", "key_local_router", "key_router"]
