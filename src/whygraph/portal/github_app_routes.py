"""Production's GitHub App routes: authorize / install, the callback, the listings.

M2d-2 plan sections 4.4 and 4.5. Every route is **production-only** - the
router's :func:`~whygraph.portal.deps.require_production` runs first, so
local mode answers ``404`` - and answers ``503 github_app_not_configured``
if the portal holds no GitHub App client (a guard only: production refuses
to start without the ``WHYGRAPH_GITHUB_APP_*`` variables):

* ``POST /api/github/app/authorize`` (org host, ``org.add_project``) starts
  a user authorization (PKCE) or an install of the app, bound to this
  browser by the ``whygraph_ghapp`` cookie and to this session, user and
  org by the server-held ``state``;
* ``POST /api/github/app/callback`` (base host, ``user.self``) finishes it:
  the SPA page at ``/auth/github-app`` posts GitHub's redirect query here.
  The resulting user token is kept in memory for the session
  (:class:`~whygraph.portal.github_app.UserTokens`), never in the DB or a
  response;
* ``GET /api/github/installations`` and
  ``GET /api/github/installations/{installation_id}/repos`` (org host,
  ``org.add_project``) list what the import page offers, through that
  user token.

The import itself is the production branch of ``POST /api/projects``
(:mod:`whygraph.portal.routes`), which uses :func:`user_token` and
:func:`require_github_app` from here.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from collections.abc import Iterable
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import col, select

from .audit import audit
from .authz import Action, OrgAccess, authorize
from .db import get_session
from .deps import (
    ApiError,
    PortalState,
    current_user,
    load_org_access,
    org_access,
    portal_state,
    require_production,
    user_access,
)
from .github_app import (
    GitHubApp,
    GitHubNotFound,
    GitHubTokenRejected,
    UserToken,
)
from .github_auth import GitHubAuthFailed, GitHubUnavailable, pkce_challenge
from .hosts import BaseUrl
from .models import Project, User
from .security import Principal

logger = logging.getLogger(__name__)

GHAPP_COOKIE = "whygraph_ghapp"
"""The cookie binding a started authorization or install to this browser."""

GHAPP_COOKIE_PATH = "/api/github/app/callback"
"""The only path the browser sends :data:`GHAPP_COOKIE` to."""

GHAPP_COOKIE_MAX_AGE = 600
"""Seconds: the pending authorization's own lifetime."""

github_app_router = APIRouter(dependencies=[Depends(require_production)])
"""Every route of this module; included before the ``/api`` 404 catch-all."""


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthorizeBody(_Strict):
    """``POST /api/github/app/authorize``: ``install`` opens the install page."""

    install: bool = False


class AppCallbackBody(_Strict):
    """``POST /api/github/app/callback``: GitHub's redirect query, as the SPA got it."""

    code: str | None = Field(default=None, min_length=1, max_length=512)
    state: str | None = Field(default=None, max_length=512)
    iss: str | None = Field(default=None, max_length=512)
    installation_id: int | None = None
    setup_action: Literal["install", "update", "request"] | None = None


# ---------------------------------------------------------------------------
# Helpers (shared with the import in routes.py)
# ---------------------------------------------------------------------------


def require_github_app(state: PortalState) -> GitHubApp:
    """The GitHub App client, or ``503 github_app_not_configured``."""
    if state.github_app is None:
        raise ApiError(
            503,
            "this portal has no GitHub App configured: set the "
            "WHYGRAPH_GITHUB_APP_* variables",
            code="github_app_not_configured",
        )
    return state.github_app


def github_id_of(principal: Principal) -> int:
    """The signed-in user's GitHub id, or ``403 github_required`` (a password account)."""
    with get_session() as db:
        github_id = db.exec(
            select(User.github_id).where(col(User.id) == principal.user_id)
        ).first()
    if github_id is None:
        raise ApiError(
            403,
            "importing from GitHub needs an account that signs in with GitHub",
            code="github_required",
        )
    return github_id


def _authorization_required() -> ApiError:
    return ApiError(
        401,
        "connect GitHub first: the import page needs your GitHub authorization",
        code="github_authorization_required",
    )


def user_token(state: PortalState, principal: Principal) -> str:
    """This session's live GitHub App user token, or ``401 github_authorization_required``."""
    entry = (
        None
        if principal.session_id is None
        else state.user_tokens.get(principal.session_id, principal.user_id)
    )
    if entry is None:
        raise _authorization_required()
    return entry.token


def token_rejected(state: PortalState, principal: Principal) -> ApiError:
    """Forget a token GitHub refused (``401``) and say ``401 github_authorization_required``."""
    if principal.session_id is not None:
        state.user_tokens.drop(principal.session_id)
    return _authorization_required()


def github_unavailable() -> ApiError:
    """The ``502 github_unavailable`` answer."""
    return ApiError(
        502,
        "GitHub could not be reached: try again in a moment",
        code="github_unavailable",
    )


def revoke_user_tokens(state: PortalState, entries: Iterable[UserToken]) -> None:
    """Revoke dropped user tokens at GitHub (best-effort, never raises)."""
    app = state.github_app
    if app is None:
        return
    for entry in entries:
        if not app.revoke(entry.token):
            logger.info("a dropped GitHub App user token was not revoked")


def end_user_tokens(
    state: PortalState, *, session_id: int | None = None, user_id: int | None = None
) -> None:
    """Drop (and revoke) the user tokens of an ended session or of all of a user's.

    Called after sign-out, a session replaced by a new sign-in, a password
    change or reset and disabling. Correctness never depends on it - a
    token is only handed out to its own live session (M2d-2 plan section
    4.3) - it frees memory and kills the token at GitHub early.

    Parameters
    ----------
    state : PortalState
        The portal state.
    session_id : int, optional
        One session that ended.
    user_id : int, optional
        A user all of whose sessions ended.
    """
    dropped: list[UserToken] = []
    if session_id is not None and (entry := state.user_tokens.drop(session_id)):
        dropped.append(entry)
    if user_id is not None:
        dropped += state.user_tokens.drop_user(user_id)
    revoke_user_tokens(state, dropped)


def _base(state: PortalState) -> BaseUrl:
    assert state.base_url is not None  # production startup set it
    return state.base_url


def _cookie_args(base: BaseUrl) -> dict:
    return {
        "path": GHAPP_COOKIE_PATH,
        "domain": base.host,
        "secure": base.scheme == "https",
        "httponly": True,
        "samesite": "lax",
    }


def _clearing_header(base: BaseUrl) -> str:
    """The ``Set-Cookie`` value that clears :data:`GHAPP_COOKIE`."""
    response = Response()
    response.delete_cookie(GHAPP_COOKIE, **_cookie_args(base))
    return response.headers["set-cookie"]


def _ghapp_cookies(request: Request) -> list[str]:
    """Every :data:`GHAPP_COOKIE` value the request carried (duplicates kept)."""
    found = []
    for header in request.headers.getlist("cookie"):
        for chunk in header.split(";"):
            name, sep, value = chunk.partition("=")
            if sep and name.strip() == GHAPP_COOKIE:
                found.append(value.strip().strip('"'))
    return found


# ---------------------------------------------------------------------------
# Authorize / install (plan section 4.4)
# ---------------------------------------------------------------------------


@github_app_router.post("/api/github/app/authorize")
def post_authorize(
    body: AuthorizeBody,
    request: Request,
    response: Response,
    access: OrgAccess = Depends(org_access(Action.ORG_ADD_PROJECT)),
    principal: Principal = Depends(current_user),
) -> dict:
    """Start a GitHub App user authorization (or, with ``install``, an install).

    ``503 github_app_not_configured``; ``403 github_required`` for a
    password account. Keeps ``state`` with a PKCE verifier, this session,
    this user and this org server-side, and sets the ``whygraph_ghapp``
    cookie (``Domain=<base host>``, ``Path=/api/github/app/callback``,
    ``HttpOnly``, ``SameSite=Lax``, ``Secure`` on https, ten minutes) to
    the same ``state``.

    Returns
    -------
    dict
        ``{"url": ...}``: the install page, or the authorize URL with the
        PKCE challenge.
    """
    state = portal_state(request)
    app = require_github_app(state)
    github_id_of(principal)
    if principal.session_id is None:  # production always has one
        raise _authorization_required()
    oauth_state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    app.pending.put(
        oauth_state,
        verifier=verifier,
        session_id=principal.session_id,
        user_id=principal.user_id,
        org_slug=access.org_slug,
    )
    response.set_cookie(
        GHAPP_COOKIE,
        oauth_state,
        max_age=GHAPP_COOKIE_MAX_AGE,
        **_cookie_args(_base(state)),
    )
    if body.install:
        return {"url": app.install_url(oauth_state)}
    return {"url": app.authorize_url(oauth_state, pkce_challenge(verifier))}


@github_app_router.post("/api/github/app/callback")
def post_callback(
    body: AppCallbackBody,
    request: Request,
    response: Response,
    principal: Principal = Depends(user_access("base")),
) -> dict:
    """Finish an authorization or install started by :func:`post_authorize`.

    Two arrivals (plan section 4.4): *authorize* (``code``, ``state``,
    ``iss``; exchanged with the PKCE verifier) and *install* (``code``,
    ``state``, ``installation_id``, ``setup_action``; exchanged without).
    Refusals: ``400 oauth_state`` (no, several or a mismatched
    ``whygraph_ghapp`` cookie, an unknown, expired or used ``state``, or one
    started by another session or user), ``409 start_from_portal`` (an
    install arrival without a known ``state`` or cookie), ``400
    github_auth_failed`` (an ``iss`` other than GitHub's, a refused code),
    ``502 github_unavailable``, ``403 github_required`` (a password
    account), ``403 github_account_mismatch`` (the token belongs to another
    GitHub account; it is revoked), ``403 forbidden`` (no longer allowed to
    add projects in the org). A ``setup_action=request`` (an org member
    asked the owners to install) exchanges nothing. The cookie is cleared
    on every outcome.

    Returns
    -------
    dict
        ``{"return_to": "<org origin>/projects/new"}``, built by the
        server; for a request, ``{"requested": true, "return_to": ...}``
        (``null`` when the portal did not start it).
    """
    state = portal_state(request)
    base = _base(state)
    try:
        result = _callback(state, base, body, request, principal)
    except ApiError as exc:
        exc.headers = {**(exc.headers or {}), "set-cookie": _clearing_header(base)}
        raise
    response.delete_cookie(GHAPP_COOKIE, **_cookie_args(base))
    return result


def _callback(
    state: PortalState,
    base: BaseUrl,
    body: AppCallbackBody,
    request: Request,
    principal: Principal,
) -> dict:
    """The callback's steps; every refusal is an :class:`ApiError`."""
    app = require_github_app(state)
    install = body.installation_id is not None or body.setup_action is not None

    # The cookie must be the browser's one and only and equal the posted
    # state; only then is the state spent (single use).
    cookies = _ghapp_cookies(request)
    pending = None
    if (
        body.state
        and len(cookies) == 1
        and hmac.compare_digest(cookies[0].encode("utf-8"), body.state.encode("utf-8"))
    ):
        pending = app.pending.pop(body.state)
    if pending is not None and (
        pending.session_id != principal.session_id
        or pending.user_id != principal.user_id
    ):
        pending = None
    return_to = (
        None if pending is None else f"{base.org_origin(pending.org_slug)}/projects/new"
    )

    if body.setup_action == "request":
        return {"requested": True, "return_to": return_to}
    if pending is None:
        if install:
            raise ApiError(
                409,
                "open your organization in WhyGraph and choose Import from GitHub",
                code="start_from_portal",
            )
        raise ApiError(
            400,
            "this authorization expired or was started in another browser: try again",
            code="oauth_state",
        )
    assert return_to is not None
    github_id = github_id_of(principal)
    failed = ApiError(
        400,
        "GitHub did not confirm this authorization: try again",
        code="github_auth_failed",
    )
    if body.iss is not None and body.iss != f"{app.config.web_url}/login/oauth":
        raise failed
    if body.code is None:
        raise failed

    try:
        auth = app.exchange(body.code, None if install else pending.verifier)
    except GitHubAuthFailed:
        raise failed from None
    except GitHubUnavailable:
        raise github_unavailable() from None
    keep = False
    try:
        try:
            user = app.user(auth.token)
        except GitHubAuthFailed:
            raise failed from None
        except GitHubUnavailable:
            raise github_unavailable() from None
        if user.id != github_id:
            audit(
                "github_account_mismatch",
                request,
                uid=principal.uid,
                github_login=user.login,
            )
            raise ApiError(
                403,
                "you authorized a different GitHub account than the one you "
                "signed in with: switch accounts on GitHub and try again",
                code="github_account_mismatch",
            )
        access = load_org_access(principal.user_id, pending.org_slug)
        if access is None:
            raise ApiError(
                403,
                "you are no longer a member of that organization",
                code="forbidden",
            )
        authorize(access, Action.ORG_ADD_PROJECT)
        assert principal.session_id is not None
        state.user_tokens.sweep()
        state.user_tokens.put(
            principal.session_id,
            token=auth.token,
            user_id=principal.user_id,
            github_id=user.id,
            expires=auth.expires_at,
        )
        keep = True
    finally:
        if not keep:
            app.revoke(auth.token)
    audit(
        "github_app_authorized",
        request,
        uid=principal.uid,
        org=pending.org_slug,
        github_login=user.login,
        arrival="install" if install else "authorize",
    )
    return {"return_to": return_to}


# ---------------------------------------------------------------------------
# Listing (plan section 4.5)
# ---------------------------------------------------------------------------


@github_app_router.get("/api/github/installations")
def get_installations(
    request: Request,
    access: OrgAccess = Depends(org_access(Action.ORG_ADD_PROJECT)),
    principal: Principal = Depends(current_user),
) -> dict:
    """The app's installations the signed-in user can see on GitHub.

    ``503 github_app_not_configured``, ``403 github_required``, ``401
    github_authorization_required`` (no usable user token in this session:
    the page offers "Connect GitHub"), ``502 github_unavailable``.

    Returns
    -------
    dict
        ``{"installations": [{id, account_login, account_type, avatar_url,
        repository_selection}]}``.
    """
    state = portal_state(request)
    app = require_github_app(state)
    github_id_of(principal)
    token = user_token(state, principal)
    try:
        found = app.installations(token)
    except (GitHubTokenRejected, GitHubNotFound):
        raise token_rejected(state, principal) from None
    except GitHubUnavailable:
        raise github_unavailable() from None
    return {
        "installations": [
            {
                "id": i.id,
                "account_login": i.account_login,
                "account_type": i.account_type,
                "avatar_url": i.avatar_url,
                "repository_selection": i.repository_selection,
            }
            for i in found
        ]
    }


@github_app_router.get("/api/github/installations/{installation_id}/repos")
def get_installation_repos(
    installation_id: int,
    request: Request,
    page: int = Query(1, ge=1, le=10_000),
    access: OrgAccess = Depends(org_access(Action.ORG_ADD_PROJECT)),
    principal: Principal = Depends(current_user),
) -> dict:
    """One page (100) of an installation's repositories the user can see.

    ``imported`` marks a repository that is already a project **in this
    org** (by GitHub's repository id). ``404 no_access`` when the user
    cannot see the installation; otherwise as
    :func:`get_installations`.

    Returns
    -------
    dict
        ``{"repos": [{id, full_name, private, default_branch, imported}],
        "total_count": n, "page": page}``.
    """
    state = portal_state(request)
    app = require_github_app(state)
    github_id_of(principal)
    token = user_token(state, principal)
    try:
        listed = app.installation_repos(token, installation_id, page)
    except GitHubTokenRejected:
        raise token_rejected(state, principal) from None
    except GitHubNotFound:
        raise ApiError(
            404, "you cannot see that installation on GitHub", code="no_access"
        ) from None
    except GitHubUnavailable:
        raise github_unavailable() from None
    ids = [r.id for r in listed.repos]
    with get_session() as db:
        imported = (
            set(
                db.exec(
                    select(Project.github_repo_id).where(
                        Project.org_id == access.org_id,
                        col(Project.github_repo_id).in_(ids),
                    )
                ).all()
            )
            if ids
            else set()
        )
    return {
        "repos": [
            {
                "id": r.id,
                "full_name": r.full_name,
                "private": r.private,
                "default_branch": r.default_branch,
                "imported": r.id in imported,
            }
            for r in listed.repos
        ],
        "total_count": listed.total_count,
        "page": page,
    }


__all__ = [
    "GHAPP_COOKIE",
    "GHAPP_COOKIE_PATH",
    "AppCallbackBody",
    "AuthorizeBody",
    "end_user_tokens",
    "github_app_router",
    "github_id_of",
    "github_unavailable",
    "require_github_app",
    "revoke_user_tokens",
    "token_rejected",
    "user_token",
]
