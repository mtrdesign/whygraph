"""Production identity routes: bootstrap, sign-in, account, orgs and admin.

Every route here is **production-only** - local mode answers ``404`` - and
none names an organization, so none is org-scoped (M2c plan section 4.7):

* Public routes (bootstrap, GitHub sign-in, the admin sign-ins, logout,
  reset) declare no action. Their gate
  (:func:`~whygraph.portal.deps.require_mode_and_host`) runs as a route
  dependency, so a wrong mode or host is a ``404`` before the body is even
  validated. The credential routes are served on the base host only; logout
  on any host. End users sign in with GitHub (M2d-1 plan section 4.4). The
  admin panel has its own session (plan 59 section 4.2): a GitHub admin
  signs in to it through the same OAuth App with ``purpose="admin"``, and
  email + password is kept for password accounts only - in practice the
  bootstrap instance admin, break-glass (D4) - and gives an admin session
  only. No route creates another password account.
* ``user.self`` routes (:func:`~whygraph.portal.deps.user_access`): the
  caller's own account, and org creation (base host).
* ``instance.admin`` routes (:func:`~whygraph.portal.deps.instance_access`):
  the admin panel's data and its own account (base host, admin session).

Handlers are sync ``def`` (the threadpool runs them; argon2 work is bounded
by :mod:`whygraph.portal.passwords`' own semaphore). Session cookies are set
and cleared only through :mod:`whygraph.portal.sessions`, and every
security event of plan section 0.2 goes through
:func:`whygraph.portal.audit.audit` - never with a token, secret or password.
"""

from __future__ import annotations

import enum
import hmac
import json
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import anyio.to_thread
from alembic.util import CommandError as AlembicCommandError
from fastapi import APIRouter, Body, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, literal_column, or_, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, func, select

from whygraph.core.config import ConfigError
from whygraph.core.context import ProjectContext, use_project
from whygraph.core.releases import format_version, running_version
from whygraph.core.safe_paths import UnsafePathError
from whygraph.db import get_session as project_db_session
from whygraph.db.models import ChatSession
from whygraph.mcp.evidence import stop_describes
from whygraph.serve.chat import delete_session_rows

from . import audit_store, connections, updates
from .audit import audit, truncate_email
from .audit import current_writer as current_audit_writer
from .authz import Action, ProjectRole, Role
from .budgets import reload_org_budgets
from .context import ProjectNotFound
from .db import get_session
from .deps import (
    ApiError,
    BoundProject,
    PortalState,
    bound_from,
    instance_access,
    portal_state,
    require_mode_and_host,
    user_access,
)
from .github_app_routes import end_user_tokens
from .github_auth import (
    GitHubAuthFailed,
    GitHubOAuth,
    GitHubUnavailable,
    GitHubUser,
    PendingLogin,
)
from .hosts import BaseUrl, safe_admin_redirect, safe_redirect
from .member_routes import (
    Redeemed,
    _checked_role,
    _last_owner,
    _lock_owners,
    _locked_target,
    _member_row,
    redeem_invitations,
)
from .models import (
    AgentCallDay,
    AuditEvent,
    ConnectionToken,
    Invitation,
    Membership,
    Organization,
    PasswordReset,
    Project,
    ProjectGrant,
    ReaderGrant,
    RetiredOrgSlug,
    ScanRun,
    UsageEvent,
    User,
    UserSession,
)
from .orgs import (
    RESERVED_ORG_SLUGS,
    OrgSlugTaken,
    add_member,
    create_org,
    is_valid_org_slug,
    validate_org_slug,
)
from .org_routes import (
    _checked_name,
    not_deleted_error,
    org_deleted_error,
    purge_org,
    soft_delete_org,
)
from .paths import project_db_path
from .people import DELETED_USER, user_label
from .passwords import (
    hash_password,
    normalize_email,
    validate_password,
    verify_password,
)
from .routes import (
    DeleteProjectBody,
    _remove_project,
    soft_delete_project,
    soft_delete_result,
)
from .runner import ProjectBusy, ProjectGone
from .security import Principal
from .storage import latest_project_bytes
from .sessions import (
    admin_cookie_name,
    clear_admin_cookie,
    clear_cookie,
    create_session,
    end_paired_admin,
    hash_token,
    repair_admin,
    revoke,
    revoke_user,
    revoke_user_kind,
    rotate,
    session_cookies,
    set_admin_cookie,
    set_cookie,
)
from .throttle import ip_key
from .usage_routes import account_usage, usd

RESET_LINK_LIFETIME = timedelta(hours=24)
"""How long an admin-issued reset link works."""

READER_GRANT_TTL = timedelta(minutes=60)
"""How long a platform administrator's reader grant lasts (plan 59 D17)."""

_PUBLIC_BASE = [Depends(require_mode_and_host("base"))]
_PUBLIC_ANY = [Depends(require_mode_and_host("any"))]

auth_router = APIRouter()
"""Every route of this module; included before the ``/api`` 404 catch-all."""


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


_PASSWORD = Field(max_length=4096)  # the length rule itself is weak_password


class BootstrapBody(_Strict):
    """``POST /api/auth/bootstrap``."""

    secret: str = Field(max_length=200)
    email: str = Field(max_length=1024)
    display_name: str = Field(min_length=1, max_length=100)
    password: str = _PASSWORD


class GitHubStartBody(_Strict):
    """``POST /api/auth/github/start``."""

    next: str | None = Field(default=None, max_length=4096)


class GitHubCallbackBody(_Strict):
    """``POST /api/auth/github/callback``."""

    code: str = Field(min_length=1, max_length=512)
    state: str = Field(min_length=1, max_length=512)


class LoginBody(_Strict):
    """``POST /api/auth/admin/login``."""

    email: str = Field(max_length=1024)
    password: str = _PASSWORD
    next: str | None = Field(default=None, max_length=4096)


class ResetBody(_Strict):
    """``POST /api/auth/reset``."""

    token: str = Field(max_length=200)
    password: str = _PASSWORD


class AccountBody(_Strict):
    """``PATCH /api/account``."""

    display_name: str = Field(min_length=1, max_length=100)


class PasswordBody(_Strict):
    """``POST /api/admin/me/password``."""

    current: str = _PASSWORD
    new: str = _PASSWORD


class OrgBody(_Strict):
    """``POST /api/orgs``."""

    slug: str = Field(max_length=100)
    name: str = Field(min_length=1, max_length=200)


class AdminOrgBody(_Strict):
    """``POST /api/admin/orgs``: the new org and the account that owns it."""

    slug: str = Field(max_length=100)
    name: str = Field(min_length=1, max_length=200)
    owner_uid: str = Field(min_length=1, max_length=100)


class AdminOrgPatchBody(_Strict):
    """``PATCH /api/admin/orgs/{slug}``."""

    name: str = Field(min_length=1, max_length=1000)


class AdminMemberBody(_Strict):
    """``POST /api/admin/orgs/{slug}/members``: an existing account and its role."""

    uid: str = Field(min_length=1, max_length=100)
    role: str = Field(max_length=20)


class AdminMemberRoleBody(_Strict):
    """``PATCH /api/admin/orgs/{slug}/members/{uid}``."""

    role: str = Field(max_length=20)


class SuspendBody(_Strict):
    """``POST /api/admin/orgs/{slug}/suspend``: the reason members are shown."""

    reason: str = Field(min_length=1, max_length=500)


class AdminUserBody(_Strict):
    """``PATCH /api/admin/users/{uid}``: either field, or both (at least one)."""

    is_instance_admin: bool | None = None
    disabled: bool | None = None


class AdminSessionsBody(_Strict):
    """``DELETE /api/admin/users/{uid}/sessions``: which kind to end (default both)."""

    kind: Literal["user", "admin"] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _base(state: PortalState) -> BaseUrl:
    assert state.base_url is not None  # production startup set it
    return state.base_url


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _display_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise ApiError(422, "display_name must not be blank")
    return name


def _throttled(retry_after: int | None) -> None:
    """Raise ``429`` with ``Retry-After`` when a throttle said to wait."""
    if retry_after is not None:
        raise ApiError(
            429,
            "too many attempts; try again later",
            code="throttled",
            headers={"Retry-After": str(retry_after)},
        )


def _require_claimed(state: PortalState) -> None:
    """``409 bootstrap_required`` while the instance is unclaimed."""
    if state.bootstrap_secret is not None:
        raise ApiError(
            409,
            "this portal has not been set up yet: claim it with the bootstrap "
            "secret first",
            code="bootstrap_required",
        )


def _login_throttles(state: PortalState, email: str, ip: str) -> list:
    """The three sign-in failure rules and their keys (plan section 4.8)."""
    return [
        (state.login_pair, (email, ip)),
        (state.login_email, email),
        (state.login_ip, ip),
    ]


def _check_login_throttles(rules: list) -> None:
    waits = [w for rule, key in rules if (w := rule.check(key)) is not None]
    _throttled(max(waits) if waits else None)


def _invalidate_reset_links(db: Session, user_id: int) -> None:
    """Mark every unused reset link of a user used."""
    db.exec(
        update(PasswordReset)
        .where(
            col(PasswordReset.user_id) == user_id,
            col(PasswordReset.used_at).is_(None),
        )
        .values(used_at=_iso(_now()))
    )


def _revoke_reader_grants(db: Session, user_id: int) -> list[tuple[int, str]]:
    """Revoke ``user_id``'s live reader grants; return each grant's org.

    Returns
    -------
    list of (int, str)
        ``(org_id, org_slug)`` of every grant that was live, for the
        ``reader_grant_revoked`` events the caller writes after its commit.
    """
    now = _iso(_now())
    org_ids = db.exec(
        update(ReaderGrant)
        .where(
            col(ReaderGrant.user_id) == user_id,
            col(ReaderGrant.revoked_at).is_(None),
            col(ReaderGrant.expires_at) > now,
        )
        .values(revoked_at=now)
        .returning(col(ReaderGrant.org_id))
    ).all()
    if not org_ids:
        return []
    slugs = dict(
        db.exec(
            select(Organization.id, Organization.slug).where(
                col(Organization.id).in_({org_id for (org_id,) in org_ids})
            )
        ).all()
    )
    return [(org_id, slugs[org_id]) for (org_id,) in org_ids]


def _email_taken() -> ApiError:
    return ApiError(
        409, "an account with that email already exists", code="email_taken"
    )


def _account_disabled() -> ApiError:
    return ApiError(403, "this account is disabled", code="account_disabled")


def user_deleted_error() -> ApiError:
    """``409 user_deleted``: the account was soft-deleted (plan 59 section 4.3.1)."""
    return ApiError(
        409, "this account was deleted: restore it first", code="user_deleted"
    )


def _erased_error() -> ApiError:
    return ApiError(409, "this account was erased", code="erased")


def _account_deleted() -> ApiError:
    return ApiError(403, "this account was deleted", code="account_deleted")


def _no_password() -> ApiError:
    return ApiError(
        409,
        "this account signs in with GitHub and has no password",
        code="no_password",
    )


def _reset_not_an_admin() -> ApiError:
    """``409 not_an_admin``: a password account that is no admin gets no reset (D28)."""
    return ApiError(
        409,
        "this password account is not an instance admin, so it gets no reset link",
        code="not_an_admin",
    )


# ---------------------------------------------------------------------------
# The admin session's sign-in rules (plan 59 section 4.2)
# ---------------------------------------------------------------------------


def _principal(request: Request) -> Principal | None:
    """The request's resolved principal (on ``/api/auth`` always an everyday one)."""
    return request.scope.get("state", {}).get("principal")


def _end_admin_cookie_sessions(
    db: Session, request: Request, base: BaseUrl
) -> list[int]:
    """Delete the admin sessions this browser's admin cookie names (rule 1).

    The admin cookie is read **directly**: no route here is under
    ``/api/admin``, so the request's principal is the everyday session and
    never the admin one. Run it in the same transaction as the new admin
    session, so an admin sign-in can never leave a fixated one behind.

    Returns
    -------
    list of int
        ``users.id`` of each admin row deleted.
    """
    values = session_cookies(
        request.headers.getlist("cookie"), name=admin_cookie_name(base)
    )
    ended: list[int] = []
    for value in values:
        ended += db.exec(
            delete(UserSession)
            .where(
                col(UserSession.token_hash) == hash_token(value),
                col(UserSession.kind) == "admin",
            )
            .returning(col(UserSession.user_id))
        ).scalars()
    return ended


def _paired_session(request: Request, user_id: int) -> int | None:
    """The everyday session a new admin session of ``user_id`` pairs with (D51).

    The request's everyday session when it belongs to the same user; else
    ``None`` (no everyday session, as for a password account, or another
    user's), and the admin session is unpaired.
    """
    principal = _principal(request)
    if (
        principal is None
        or principal.token_id is not None
        or principal.session_kind != "user"
        or principal.user_id != user_id
    ):
        return None
    return principal.session_id


def _start_admin_session(
    db: Session, request: Request, base: BaseUrl, user_id: int
) -> str:
    """Rules 1 and 3: end the cookie's admin sessions, start a paired new one."""
    _end_admin_cookie_sessions(db, request, base)
    return create_session(
        db,
        user_id,
        _user_agent(request),
        kind="admin",
        paired_session_id=_paired_session(request, user_id),
    )


def _admin_surface(exc: ApiError) -> ApiError:
    """Mark a refusal of an admin sign-in (rule 4), so the SPA links to ``/admin/login``."""
    exc.extra.setdefault("surface", "admin")
    return exc


# ---------------------------------------------------------------------------
# Bootstrap (plan section 4.3)
# ---------------------------------------------------------------------------


@auth_router.get("/api/auth/bootstrap", dependencies=_PUBLIC_BASE)
def get_bootstrap(request: Request) -> dict:
    """Whether the instance still waits for its first admin."""
    return {"required": portal_state(request).bootstrap_secret is not None}


@auth_router.post("/api/auth/bootstrap", dependencies=_PUBLIC_BASE)
def post_bootstrap(body: BootstrapBody, request: Request, response: Response) -> dict:
    """Claim a fresh instance with the logged secret: the first instance admin.

    ``404`` once no bootstrap is pending (the route is inert afterwards),
    ``429`` past 10 attempts per ``ip_key`` in 15 minutes, ``403
    bad_secret``, the email and password rules (``422``), ``409 email_taken``.
    The password is hashed before ``setup_lock`` is taken; under it the
    pending secret and the absence of an admin are re-checked, so a double
    submit makes one admin. The new admin gets an **admin** session (the
    admin cookie; a password account has no everyday session, plan 59 D4)
    and lands on ``<base>/admin``.
    """
    state = portal_state(request)
    base = _base(state)
    expected = state.bootstrap_secret
    if expected is None:
        raise ApiError(404, "not found")
    _throttled(state.bootstrap_ip.hit(ip_key(request.scope)))
    if not hmac.compare_digest(body.secret.encode("utf-8"), expected.encode("utf-8")):
        raise ApiError(403, "wrong bootstrap secret", code="bad_secret")
    email = normalize_email(body.email)
    name = _display_name(body.display_name)
    validate_password(body.password, email)
    password_hash = hash_password(body.password)
    with state.setup_lock:
        if state.bootstrap_secret is None:
            raise ApiError(404, "not found")  # claimed while we hashed
        with get_session() as db:
            admin = db.exec(
                select(User.id).where(col(User.is_instance_admin).is_(True)).limit(1)
            ).first()
            if admin is not None:
                state.bootstrap_secret = None
                raise ApiError(404, "not found")
            if db.exec(select(User.id).where(User.email == email)).first() is not None:
                raise _email_taken()
            user = User(
                display_name=name,
                email=email,
                password_hash=password_hash,
                is_instance_admin=True,
            )
            db.add(user)
            db.flush()
            assert user.id is not None
            token = _start_admin_session(db, request, base, user.id)
            uid = user.uid
        state.bootstrap_secret = None
    audit("bootstrap_claimed", request, uid=uid)
    set_admin_cookie(response, token, base)
    return {"redirect": f"{base.origin}/admin"}


# ---------------------------------------------------------------------------
# GitHub sign-in (M2d-1 plan section 4.4)
# ---------------------------------------------------------------------------

OAUTH_COOKIE = "whygraph_oauth"
"""The cookie binding a started GitHub sign-in to this browser (its ``state``)."""

OAUTH_COOKIE_PATH = "/api/auth/github"
"""The only path the browser sends :data:`OAUTH_COOKIE` to."""

OAUTH_COOKIE_MAX_AGE = 600
"""Seconds: the pending sign-in's own lifetime."""

_UPSERT_ATTEMPTS = 3
"""Tries of the sign-in transaction when a concurrent one wins a unique index."""


class Refused(enum.Enum):
    """Why :func:`_sign_in_github_user` signed nobody in (nothing was written)."""

    DISABLED = "disabled"
    DELETED = "deleted"


def _github(state: PortalState) -> GitHubOAuth:
    assert state.github is not None  # production startup built it
    return state.github


def _redirect_uri(base: BaseUrl) -> str:
    """The OAuth App's one callback URL: an SPA page (plan section 0.2 #4)."""
    return f"{base.origin}/auth/github"


def _oauth_cookie_args(base: BaseUrl) -> dict:
    return {
        "path": OAUTH_COOKIE_PATH,
        "secure": base.scheme == "https",
        "httponly": True,
        "samesite": "lax",
    }


def _oauth_clearing_header(base: BaseUrl) -> str:
    """The ``Set-Cookie`` value that clears :data:`OAUTH_COOKIE` (host-only)."""
    response = Response()
    response.delete_cookie(OAUTH_COOKIE, **_oauth_cookie_args(base))
    return response.headers["set-cookie"]


def _oauth_cookies(request: Request) -> list[str]:
    """Every :data:`OAUTH_COOKIE` value the request carried (duplicates kept)."""
    found = []
    for header in request.headers.getlist("cookie"):
        for chunk in header.split(";"):
            name, sep, value = chunk.partition("=")
            if sep and name.strip() == OAUTH_COOKIE:
                found.append(value.strip().strip('"'))
    return found


@auth_router.post("/api/auth/github/start", dependencies=_PUBLIC_BASE)
def post_github_start(
    body: GitHubStartBody, request: Request, response: Response
) -> dict:
    """Start a GitHub sign-in: ``state`` + PKCE, and where to send the browser.

    ``409 bootstrap_required`` before the bootstrap; ``429`` past 60
    attempts per ``ip_key`` in 15 minutes (shared with the callback).
    ``next`` is kept server-side with the PKCE verifier when it is a safe
    portal URL (else dropped). Sets the host-only ``whygraph_oauth`` cookie
    (``Path=/api/auth/github``, ``HttpOnly``, ``SameSite=Lax``, ``Secure``
    on https, ten minutes) carrying the same ``state``.
    """
    return _github_start(body, request, response, purpose="user")


@auth_router.post("/api/auth/github/admin/start", dependencies=_PUBLIC_BASE)
def post_github_admin_start(
    body: GitHubStartBody, request: Request, response: Response
) -> dict:
    """Start a GitHub sign-in to the admin panel (plan 59 section 4.2).

    :func:`post_github_start` with ``purpose="admin"``: the same throttle,
    cookie and answers. It lives under ``/api/auth/github`` so the
    ``whygraph_oauth`` cookie's path covers it, and finishes at the one
    callback, which then starts an admin session instead of an everyday one.
    """
    return _github_start(body, request, response, purpose="admin")


def _github_start(
    body: GitHubStartBody, request: Request, response: Response, *, purpose: str
) -> dict:
    """The start routes' body; ``purpose`` is kept with the pending sign-in."""
    state = portal_state(request)
    base = _base(state)
    _require_claimed(state)
    _throttled(state.github_ip.hit(ip_key(request.scope)))
    oauth_state, url = _github(state).begin(
        safe_redirect(body.next, base), _redirect_uri(base), purpose=purpose
    )
    response.set_cookie(
        OAUTH_COOKIE,
        oauth_state,
        max_age=OAUTH_COOKIE_MAX_AGE,
        **_oauth_cookie_args(base),
    )
    return {"authorize_url": url}


@auth_router.post("/api/auth/github/callback", dependencies=_PUBLIC_BASE)
def post_github_callback(
    body: GitHubCallbackBody, request: Request, response: Response
) -> dict:
    """Finish a GitHub sign-in: verify, exchange, refuse or upsert, sign in.

    ``429`` (the start route's throttle), ``409 bootstrap_required``,
    ``400 oauth_state`` (no, several or a mismatched ``whygraph_oauth``
    cookie, or an unknown, expired or already used ``state``), ``400
    github_auth_failed`` (GitHub refused the code - a wrong PKCE verifier
    included - or granted a scope other than ``read:user``), ``502
    github_unavailable`` (network error, timeout, 5xx), ``403
    github_2fa_required`` (with ``fix_url``; no user row is written) and
    ``403 account_disabled`` and ``403 account_deleted`` (a soft-deleted
    account; nothing is written). The ``whygraph_oauth`` cookie is cleared on
    every one of these outcomes and on success. On success the account is
    created or refreshed by its GitHub id, any session the request carried
    ends, a new one starts, and ``redirect`` is the pending ``next`` or
    ``<base>/orgs``. A sign-in by another GitHub user also ends this
    browser's admin session (plan 59 D29) and clears the admin cookie.

    A sign-in started by ``/api/auth/github/admin/start`` (``purpose
    "admin"``) instead starts an **admin** session for an existing, enabled
    instance admin and writes nothing else: ``403 not_an_admin`` for anyone
    else, and its refusals after the state check carry ``surface: "admin"``.
    """
    state = portal_state(request)
    base = _base(state)
    try:
        result = _github_callback(state, base, body, request, response)
    except ApiError as exc:
        # An ApiError builds its own response, so the clearing header rides
        # on the error (plan section 4.4 step 2).
        exc.headers = {
            **(exc.headers or {}),
            "set-cookie": _oauth_clearing_header(base),
        }
        raise
    response.delete_cookie(OAUTH_COOKIE, **_oauth_cookie_args(base))
    return result


def _github_callback(
    state: PortalState,
    base: BaseUrl,
    body: GitHubCallbackBody,
    request: Request,
    response: Response,
) -> dict:
    """The callback's steps; every refusal is an :class:`ApiError`."""
    _throttled(state.github_ip.hit(ip_key(request.scope)))
    _require_claimed(state)
    github = _github(state)

    admin = False  # the pending sign-in's purpose, once it is known

    def refused(reason: str, login: str | None = None) -> None:
        fields = {"surface": "admin"} if admin else {}
        audit(
            "github_signin_refused",
            request,
            reason=reason,
            github_login=login,
            **fields,
        )

    def refusal(exc: ApiError) -> ApiError:
        return _admin_surface(exc) if admin else exc

    # The cookie must be the browser's one and only, and equal the posted
    # state; only then is the state spent (single use).
    cookies = _oauth_cookies(request)
    pending = None
    if len(cookies) == 1 and hmac.compare_digest(
        cookies[0].encode("utf-8"), body.state.encode("utf-8")
    ):
        pending = github.pending.pop(body.state)
    if pending is None:
        refused("oauth_state")
        raise ApiError(
            400,
            "this sign-in expired or was started in another browser: try again",
            code="oauth_state",
        )
    admin = pending.purpose == "admin"

    try:
        user = github.identify(
            body.code,
            pending.verifier,
            _redirect_uri(base),
            on_revoke_failure=lambda: audit("github_token_revoke_failed", request),
        )
    except GitHubAuthFailed as exc:
        refused(exc.reason)
        raise refusal(
            ApiError(
                400,
                "GitHub did not confirm this sign-in: try again",
                code="github_auth_failed",
            )
        ) from None
    except GitHubUnavailable:
        refused("unavailable")
        raise refusal(
            ApiError(
                502,
                "GitHub could not be reached: try again in a moment",
                code="github_unavailable",
            )
        ) from None

    if not user.two_factor:
        refused("2fa_required", user.login)
        raise refusal(
            ApiError(
                403,
                "turn on two-factor authentication for your GitHub account, then "
                "sign in again",
                code="github_2fa_required",
                fix_url=f"{github.config.web_url}/settings/security",
            )
        )
    if admin:
        return _admin_github_sign_in(state, base, user, pending, request, response)
    principal = _principal(request)
    signed_in = _sign_in_github_user(
        user,
        replaces=None if principal is None else principal.session_id,
        user_agent=_user_agent(request),
        replaces_user_id=None if principal is None else principal.user_id,
        admin_tokens=session_cookies(
            request.headers.getlist("cookie"), name=admin_cookie_name(base)
        ),
    )
    if signed_in is Refused.DISABLED:
        refused("disabled", user.login)
        raise ApiError(403, "this account is disabled", code="account_disabled")
    if signed_in is Refused.DELETED:
        refused("deleted", user.login)
        raise _account_deleted()
    token, uid, new_user, released, redeemed, clear_admin = signed_in
    if principal is not None and principal.session_id is not None:
        end_user_tokens(state, session_id=principal.session_id)  # it was revoked
    for target in released:
        audit("github_login_released", request, target=target, github_login=user.login)
    audit("github_signin", request, uid=uid, github_login=user.login, new_user=new_user)
    for joined in redeemed:
        audit(
            "member_joined_by_invite",
            request,
            uid=uid,
            org_id=joined.org_id,
            org=joined.org_slug,
            role=joined.role,
            invitation=joined.invitation_uid,
            github_login=user.login,
        )
    set_cookie(response, token, base)
    if clear_admin:
        clear_admin_cookie(response, base)
    return {"redirect": safe_redirect(pending.next, base) or f"{base.origin}/orgs"}


def _admin_github_sign_in(
    state: PortalState,
    base: BaseUrl,
    user: GitHubUser,
    pending: PendingLogin,
    request: Request,
    response: Response,
) -> dict:
    """Finish a GitHub sign-in to the admin panel (plan 59 section 4.2).

    The account is found by its GitHub id and **nothing else is written**:
    no account is created, no login released, no invitation redeemed, no
    everyday session revoked and no profile field refreshed (so this never
    calls :func:`_sign_in_github_user`). An unknown GitHub id, a user who is
    not an instance admin, a disabled and a soft-deleted one all get the same ``403
    not_an_admin``, audited as ``admin_signin_refused`` with the reason.
    Otherwise rules 1 and 3: the admin cookie's sessions end and a new admin
    session starts, paired with this browser's everyday session of the same
    user.
    """
    with get_session() as db:
        row = db.exec(
            select(
                User.id,
                User.uid,
                User.is_instance_admin,
                User.disabled_at,
                User.deleted_at,
            ).where(User.github_id == user.id)
        ).first()
        reason = None
        if row is None:
            reason = "unknown_user"
        elif row[3] is not None:
            reason = "disabled"
        elif row[4] is not None:
            reason = "deleted"
        elif not row[2]:
            reason = "not_admin"
        if reason is None:
            assert row is not None
            token = _start_admin_session(db, request, base, row[0])
    if reason is not None:
        audit("admin_signin_refused", request, reason=reason, github_login=user.login)
        raise _admin_surface(
            ApiError(
                403,
                "this account is not an instance admin",
                code="not_an_admin",
            )
        )
    assert row is not None
    audit("admin_signin", request, uid=row[1], method="github", github_login=user.login)
    set_admin_cookie(response, token, base)
    return {"redirect": safe_admin_redirect(pending.next, base)}


def _sign_in_github_user(
    user: GitHubUser,
    *,
    replaces: int | None,
    user_agent: str | None,
    replaces_user_id: int | None = None,
    admin_tokens: list[str] | None = None,
) -> tuple[str, str, bool, list[str], list[Redeemed], bool] | Refused:
    """Upsert the account by GitHub id and start its session, in one transaction.

    Any other row holding the incoming login (case-insensitively) has it
    released first (plan section 0.2 #7). ``INSERT ... ON CONFLICT
    (github_id) DO UPDATE`` refreshes ``github_login`` and ``avatar_url``
    only - never the user-editable ``display_name`` - so two concurrent
    first sign-ins converge on one row; a race that still trips a unique
    index (a login claimed by two ids at once) retries the transaction.
    A disabled or a soft-deleted account is refused with every write rolled
    back. After those checks, the open invitations for the GitHub id are
    redeemed (:func:`~whygraph.portal.member_routes.redeem_invitations`,
    M2f-1 plan section 4.8), each in its own savepoint, so a failed one
    never fails the sign-in.

    The browser's admin session follows its everyday one (plan 59 D29,
    D51). ``fk_sessions_paired`` unpairs on delete, so every pairing step
    runs before ``replaces`` is revoked: a ``replaces`` of another user
    ends its paired admin session; one of the same user hands its pairing
    to the new session. Then each admin row the admin cookie names is
    deleted when it is another user's (and the caller clears the cookie),
    or paired with the new session when it is this user's, whatever it
    was paired with before.

    Parameters
    ----------
    user : GitHubUser
        The identified GitHub account.
    replaces : int or None
        The everyday session the request carried, ended by this sign-in.
    user_agent : str or None
        The request's ``User-Agent``.
    replaces_user_id : int or None, optional
        ``users.id`` of ``replaces``.
    admin_tokens : list of str, optional
        Every value of the request's admin cookie.

    Returns
    -------
    tuple or Refused
        ``(session token, uid, new_user, released uids, redeemed,
        clear_admin_cookie)``, or :class:`Refused` when the account is
        disabled or soft-deleted (then nothing is written).
    """
    users = User.__table__
    for attempt in range(_UPSERT_ATTEMPTS):
        try:
            with get_session() as db:
                released = list(
                    db.exec(
                        update(User)
                        .where(
                            func.lower(col(User.github_login)) == user.login.lower(),
                            col(User.github_id) != user.id,
                        )
                        .values(github_login=None)
                        .returning(col(User.uid))
                    ).scalars()
                )
                insert = pg_insert(users).values(
                    uid=str(uuid.uuid4()),
                    display_name=(user.name or user.login).strip()[:100],
                    is_instance_admin=False,
                    github_id=user.id,
                    github_login=user.login,
                    avatar_url=user.avatar_url,
                    created_at=_iso(_now()),
                )
                row = db.exec(
                    insert.on_conflict_do_update(
                        index_elements=[users.c.github_id],
                        set_={
                            "github_login": insert.excluded.github_login,
                            "avatar_url": insert.excluded.avatar_url,
                        },
                    ).returning(
                        users.c.id,
                        users.c.uid,
                        users.c.disabled_at,
                        users.c.deleted_at,
                        literal_column("xmax = 0"),  # inserted, not updated
                    )
                ).one()
                user_id, uid, disabled_at, deleted_at, inserted = row
                if disabled_at is not None:
                    db.rollback()  # the refresh and any release are undone
                    return Refused.DISABLED
                if deleted_at is not None:
                    db.rollback()  # the same: a deleted account is not touched
                    return Refused.DELETED
                redeemed = redeem_invitations(db, github_id=user.id, user_id=user_id)
                # Read before step 1 ends any of them, so the caller still
                # learns that the cookie names another user's session.
                owners = _admin_cookie_owners(db, admin_tokens or [])
                same_user = replaces is not None and replaces_user_id == user_id
                if replaces is not None and not same_user:
                    end_paired_admin(db, replaces)
                token = create_session(db, user_id, user_agent)
                new_id = db.exec(
                    select(UserSession.id).where(
                        UserSession.token_hash == hash_token(token)
                    )
                ).one()
                if same_user:
                    assert replaces is not None
                    repair_admin(db, replaces, new_id)
                if replaces is not None:
                    revoke(replaces, db=db)  # plan section 0.2 #20
                clear_admin = _follow_admin_cookie(db, owners, user_id, new_id)
            return token, uid, bool(inserted), released, redeemed, clear_admin
        except IntegrityError:
            if attempt == _UPSERT_ATTEMPTS - 1:
                raise
    raise AssertionError("unreachable")


def _admin_cookie_owners(db: Session, tokens: list[str]) -> list[tuple[str, int]]:
    """``(token hash, users.id)`` of each admin row the admin cookie names."""
    owners = []
    for value in tokens:
        token_hash = hash_token(value)
        owner = db.exec(
            select(UserSession.user_id).where(
                UserSession.token_hash == token_hash, UserSession.kind == "admin"
            )
        ).first()
        if owner is not None:
            owners.append((token_hash, owner))
    return owners


def _follow_admin_cookie(
    db: Session, owners: list[tuple[str, int]], user_id: int, new_session_id: int
) -> bool:
    """Step 5 of the everyday sign-in's pairing: the admin cookie's own rows.

    Another user's admin row is deleted (a no-op when step 1 already ended
    it); this user's is paired with the new everyday session, whatever its
    pairing was (after duplicate everyday cookies the request had no
    principal, so nothing else moved it).

    Parameters
    ----------
    db : Session
        The sign-in's open transaction.
    owners : list of tuple
        :func:`_admin_cookie_owners`, read before step 1.
    user_id : int
        ``users.id`` of the user signing in.
    new_session_id : int
        ``sessions.id`` of the new everyday session.

    Returns
    -------
    bool
        Whether the caller must clear the admin cookie.
    """
    clear = False
    for token_hash, owner in owners:
        if owner != user_id:
            db.exec(
                delete(UserSession).where(
                    col(UserSession.token_hash) == token_hash,
                    col(UserSession.kind) == "admin",
                )
            )
            clear = True
        else:
            db.exec(
                update(UserSession)
                .where(
                    col(UserSession.token_hash) == token_hash,
                    col(UserSession.kind) == "admin",
                )
                .values(paired_session_id=new_session_id)
            )
    return clear


# ---------------------------------------------------------------------------
# Admin sign-in, logout, reset (plan sections 4.7, 4.8; plan 59 section 4.2)
# ---------------------------------------------------------------------------


@auth_router.post("/api/auth/admin/login", dependencies=_PUBLIC_BASE)
def post_admin_login(body: LoginBody, request: Request, response: Response) -> dict:
    """Sign in to the admin panel with email and password (plan 59 section 4.2).

    The only password sign-in (D4): it starts an **admin** session, never an
    everyday one. ``409 bootstrap_required`` before the bootstrap. A wrong
    password and an unknown email answer the same ``401 bad_credentials``
    in the same time (a dummy verify). Failures count per ``(email,
    ip_key)``, per email and per ``ip_key``; all three are checked first
    (``429``). Only after a correct password: ``403 account_disabled``,
    ``403 account_deleted``, then ``403 not_an_admin``, so the route tells
    nothing to a guesser.
    Every refusal carries ``surface: "admin"`` and is audited as
    ``login_failure`` with the same field. An outdated hash is upgraded.
    The admin cookie's earlier admin sessions end in the same transaction
    (no fixation), and ``redirect`` is ``next`` when it is a URL of the
    admin panel, else ``<base>/admin``.
    """
    try:
        return _admin_login(body, request, response)
    except ApiError as exc:
        _admin_surface(exc)
        raise


def _admin_login(body: LoginBody, request: Request, response: Response) -> dict:
    """The admin password sign-in's steps; every refusal is an :class:`ApiError`."""
    state = portal_state(request)
    base = _base(state)
    _require_claimed(state)
    # Lookup form only: a malformed email is just an unknown account.
    email = body.email.strip().lower()
    rules = _login_throttles(state, email, ip_key(request.scope))
    _check_login_throttles(rules)
    with get_session() as db:
        row = db.exec(
            select(
                User.id,
                User.uid,
                User.password_hash,
                User.disabled_at,
                User.is_instance_admin,
                User.deleted_at,
            ).where(User.email == email)
        ).first()
    ok, needs_rehash = verify_password(None if row is None else row[2], body.password)
    if row is None or not ok:
        for rule, key in rules:
            rule.record(key)
        audit(
            "login_failure",
            request,
            uid=None if row is None else row[1],
            email=truncate_email(email),
            surface="admin",
        )
        raise ApiError(401, "wrong email or password", code="bad_credentials")
    user_id, uid, _, disabled_at, is_admin, deleted_at = row
    if disabled_at is not None:
        audit(
            "login_failure",
            request,
            uid=uid,
            email=truncate_email(email),
            reason="disabled",
            surface="admin",
        )
        raise _account_disabled()
    if deleted_at is not None:
        audit(
            "login_failure",
            request,
            uid=uid,
            email=truncate_email(email),
            reason="deleted",
            surface="admin",
        )
        raise _account_deleted()
    if not is_admin:
        audit(
            "login_failure",
            request,
            uid=uid,
            email=truncate_email(email),
            reason="not_admin",
            surface="admin",
        )
        raise ApiError(
            403, "this account is not an instance admin", code="not_an_admin"
        )
    new_hash = hash_password(body.password) if needs_rehash else None
    with get_session() as db:
        if new_hash is not None:
            user = db.get(User, user_id)
            if user is not None:
                user.password_hash = new_hash
                db.add(user)
        token = _start_admin_session(db, request, base, user_id)
    audit("admin_signin", request, uid=uid, method="password")
    set_admin_cookie(response, token, base)
    return {"redirect": safe_admin_redirect(body.next, base)}


@auth_router.post("/api/auth/logout", dependencies=_PUBLIC_ANY)
def post_logout(request: Request, response: Response) -> dict:
    """End this session (a no-op without one) and clear the cookie, on any host.

    It also ends this browser's admin session (plan 59 D29, D51), in one
    transaction and in this order: the admin session paired with the
    everyday one (the only way from an org host, where the host-only admin
    cookie never arrives); on the base host, with or without an everyday
    session, every admin session the admin cookie names (an unpaired one:
    every password admin's), and the admin cookie is cleared; then the
    everyday session. Ending an admin session is audited as
    ``admin_signout`` with ``reason="signout"``.
    """
    state = portal_state(request)
    base = _base(state)
    principal = _principal(request)
    session_id = None if principal is None else principal.session_id
    on_base = request.scope.get("state", {}).get("host_kind") == "base"
    ended: set[int] = set()
    with get_session() as db:
        if principal is not None and session_id is not None:
            if end_paired_admin(db, session_id):
                ended.add(principal.user_id)
        if on_base:
            ended.update(_end_admin_cookie_sessions(db, request, base))
        if session_id is not None:
            revoke(session_id, db=db)
        uids = (
            db.exec(select(User.id, User.uid).where(col(User.id).in_(ended))).all()
            if ended
            else []
        )
    if principal is not None and session_id is not None:
        end_user_tokens(state, session_id=session_id)
        audit("logout", request, uid=principal.uid)
    for _, uid in sorted(uids):
        audit("admin_signout", request, uid=uid, reason="signout")
    clear_cookie(response, base)
    if on_base:
        clear_admin_cookie(response, base)
    return {"redirect": f"{base.origin}/signin"}


@auth_router.post("/api/auth/reset", dependencies=_PUBLIC_BASE)
def post_reset(body: ResetBody, request: Request, response: Response) -> dict:
    """Set a new password with an admin-issued reset link, and sign in.

    ``429`` past 10 attempts per ``ip_key`` in 15 minutes; ``400 bad_token``
    for an unknown, used, superseded or expired link; ``403
    account_disabled`` for a disabled account; ``409 not_an_admin`` for an
    account that is not an instance admin or is soft-deleted (plan 59 D28:
    nothing changes and the link stays unused); the password rules (``422``). Marks the link
    used, ends **every** session of the user and the admin cookie's, then
    starts an **admin** session and lands on ``<base>/admin``.
    """
    state = portal_state(request)
    base = _base(state)
    _throttled(state.reset_ip.hit(ip_key(request.scope)))
    bad_token = ApiError(
        400, "this reset link is invalid or has expired", code="bad_token"
    )
    token_hash = hash_token(body.token)

    def live_link(db: Session, *, lock: bool) -> PasswordReset:
        statement = select(PasswordReset).where(PasswordReset.token_hash == token_hash)
        if lock:
            statement = statement.with_for_update()
        link = db.exec(statement).first()
        if link is None or link.used_at is not None or link.expires_at <= _iso(_now()):
            raise bad_token
        return link

    with get_session() as db:
        user = db.get(User, live_link(db, lock=False).user_id)
        if user is None:
            raise bad_token
        if user.disabled_at is not None:
            raise _account_disabled()
        if not user.is_instance_admin or user.deleted_at is not None:
            raise _reset_not_an_admin()
        email = user.email or ""
    validate_password(body.password, email)
    password_hash = hash_password(body.password)
    now = _iso(_now())
    with get_session() as db:
        link = live_link(db, lock=True)  # re-checked: a concurrent use loses
        user = db.get(User, link.user_id)
        if user is None:
            raise bad_token
        if user.disabled_at is not None:
            raise _account_disabled()
        if not user.is_instance_admin or user.deleted_at is not None:
            raise _reset_not_an_admin()
        assert user.id is not None
        user.password_hash = password_hash
        user.password_changed_at = now
        db.add(user)
        _invalidate_reset_links(db, user.id)
        revoke_user(user.id, db=db)
        token = _start_admin_session(db, request, base, user.id)
        uid = user.uid
        user_id = user.id
    end_user_tokens(state, user_id=user_id)  # every earlier session ended
    audit("reset_link_used", request, uid=uid)
    set_admin_cookie(response, token, base)
    return {"redirect": f"{base.origin}/admin"}


# ---------------------------------------------------------------------------
# Account (user.self)
# ---------------------------------------------------------------------------


def _account(user: User) -> dict:
    return {
        "uid": user.uid,
        "email": user.email,
        "display_name": user.display_name,
        "is_instance_admin": user.is_instance_admin,
        "github_login": user.github_login,
        "avatar_url": user.avatar_url,
        "has_password": user.password_hash is not None,
    }


@auth_router.get("/api/account")
def get_account(principal: Principal = Depends(user_access())) -> dict:
    """The signed-in user."""
    return {
        "uid": principal.uid,
        "email": principal.email,
        "display_name": principal.display_name,
        "is_instance_admin": principal.is_instance_admin,
        "github_login": principal.github_login,
        "avatar_url": principal.avatar_url,
        "has_password": principal.has_password,
    }


@auth_router.patch("/api/account")
def patch_account(
    body: AccountBody, principal: Principal = Depends(user_access())
) -> dict:
    """Change the signed-in user's display name."""
    name = _display_name(body.display_name)
    with get_session() as db:
        user = db.get(User, principal.user_id)
        if user is None:
            raise ApiError(404, "not found")
        user.display_name = name
        db.add(user)
        db.flush()
        return _account(user)


@auth_router.get("/api/account/orgs")
def get_account_orgs(
    request: Request, principal: Principal = Depends(user_access())
) -> list[dict]:
    """The orgs the user is a member of (an admin's ``reader`` access excluded).

    A soft-deleted org is left out (plan 59 section 4.3.2).
    """
    base = _base(portal_state(request))
    with get_session() as db:
        rows = db.exec(
            select(
                Organization.slug,
                Organization.name,
                Membership.role,
                Membership.welcome_pending,
            )
            .join(Membership, col(Membership.org_id) == col(Organization.id))
            .where(Membership.user_id == principal.user_id)
            .where(col(Organization.deleted_at).is_(None))
            .order_by(Organization.name, Organization.slug)
        ).all()
    return [
        {
            "slug": slug,
            "name": name,
            "role": role,
            "url": base.org_origin(slug),
            "new": bool(new),
        }
        for slug, name, role, new in rows
    ]


@auth_router.get("/api/account/usage")
def get_account_usage(
    request: Request, principal: Principal = Depends(user_access())
) -> dict:
    """The caller's LLM spend this month in each org they are a member of.

    Memberships only (an instance admin's ``reader`` access is not one);
    the numbers come from the spend book (M2f-2 plan section 4.11). The SPA
    links each org to ``url + "/usage/me"``. A soft-deleted org is left out
    (plan 59 section 4.3.2).
    """
    state = portal_state(request)
    base = _base(state)
    with get_session() as db:
        rows = db.exec(
            select(Organization.id, Organization.slug, Organization.name)
            .join(Membership, col(Membership.org_id) == col(Organization.id))
            .where(Membership.user_id == principal.user_id)
            .where(col(Organization.deleted_at).is_(None))
            .order_by(Organization.name, Organization.slug)
        ).all()
    orgs = [(org_id, slug, name, base.org_origin(slug)) for org_id, slug, name in rows]
    return account_usage(state, principal.user_id, orgs)  # type: ignore[arg-type]


@auth_router.get("/api/orgs/slug-check")
def get_slug_check(
    request: Request,
    slug: str = Query(default="", max_length=100),
    principal: Principal = Depends(user_access("base")),
) -> dict:
    """Whether ``slug`` could be used for a new org (the create form's live check).

    Answers ``{slug, available, reason}`` with ``reason`` one of ``null``,
    ``"invalid"`` (the format), ``"reserved"`` or ``"taken"`` (a live org
    or a retired slug - one word, so a deletion is not disclosed). Base
    host only; 60 checks a minute per user (``429 throttled``).
    """
    retry_after = portal_state(request).slug_check.hit(principal.user_id)
    if retry_after is not None:
        raise ApiError(
            429,
            "too many checks; try again in a moment",
            code="throttled",
            headers={"Retry-After": str(retry_after)},
        )
    reason: str | None = None
    if not is_valid_org_slug(slug) or slug[2:4] == "--":
        reason = "invalid"
    elif slug in RESERVED_ORG_SLUGS:
        reason = "reserved"
    else:
        with get_session() as db:
            if (
                db.exec(
                    select(Organization.id).where(Organization.slug == slug)
                ).first()
                is not None
                or db.get(RetiredOrgSlug, slug) is not None
            ):
                reason = "taken"
    return {"slug": slug, "available": reason is None, "reason": reason}


@auth_router.post("/api/orgs", status_code=201)
def post_org(
    body: OrgBody,
    request: Request,
    principal: Principal = Depends(user_access("base")),
) -> dict:
    """Create an org; the caller becomes its owner (one transaction).

    ``422 bad_slug`` (with the rule) for a malformed, reserved or ``xn--``
    style slug, ``409 slug_taken`` when it exists or a deleted org retired
    it (one answer, so a deletion is not disclosed), ``409 user_deleted``
    when the caller's account was soft-deleted meanwhile.
    """
    base = _base(portal_state(request))
    slug = body.slug
    try:
        validate_org_slug(slug)
    except ValueError as exc:
        raise ApiError(422, str(exc), code="bad_slug") from exc
    name = body.name.strip()
    if not name:
        raise ApiError(422, "name must not be blank")
    slug_taken = ApiError(
        409, f"the organization {slug!r} already exists", code="slug_taken"
    )
    with get_session() as db:
        # The caller's own row, FOR SHARE and first (plan 59 section 4.3.1):
        # it waits for a running soft delete of the caller, then refuses.
        caller = db.exec(
            select(User.id, User.deleted_at)
            .where(User.id == principal.user_id)
            .with_for_update(read=True)
        ).first()
        if caller is not None and caller[1] is not None:
            raise user_deleted_error()
        if (
            db.exec(select(Organization.id).where(Organization.slug == slug)).first()
            is not None
        ):
            raise slug_taken
        try:
            org = create_org(db, slug=slug, name=name)
        except OrgSlugTaken as exc:  # retired by a deletion: the same answer
            raise slug_taken from exc
        except IntegrityError as exc:  # a concurrent create of the same slug
            raise slug_taken from exc
        assert org.id is not None
        org_id = org.id
        add_member(db, org_id=org_id, user_id=principal.user_id, role=Role.OWNER)
    audit("org_created", request, uid=principal.uid, org_id=org_id, org=slug)
    return {"slug": slug, "url": base.org_origin(slug)}


# ---------------------------------------------------------------------------
# Instance admin (plan section 4.9)
# ---------------------------------------------------------------------------


@auth_router.get("/api/admin/settings")
def get_admin_settings(
    request: Request, _: Principal = Depends(instance_access())
) -> dict:
    """The base URL and what its DNS self-check found (read-only)."""
    state = portal_state(request)
    return {"base_url": _base(state).origin, "base_check": state.base_check}


@auth_router.get("/api/admin/update")
def get_admin_update(
    request: Request, _: Principal = Depends(instance_access())
) -> dict:
    """The update-notice block; before the loop's first tick only ``current``."""
    block = updates.update_block(portal_state(request))
    if block is not None:
        return block
    current = running_version()
    return {
        "enabled": None,
        "source": None,
        "current": format_version(current) if current is not None else None,
        "latest": None,
        "available": False,
        "manual_steps": False,
        "notes_url": None,
        "upgrading_url": updates.UPGRADING_URL,
        "checked_at": None,
        "error": None,
    }


def _last_activity_day(org_id_column) -> object:
    """The later of the org's newest usage event and agent-call day, as ``YYYY-MM-DD``.

    One correlated lookup per org, each on its ``(org_id, time)`` index.
    """
    usage = (
        select(func.substr(func.max(col(UsageEvent.created_at)), 1, 10))
        .where(col(UsageEvent.org_id) == org_id_column)
        .scalar_subquery()
    )
    calls = (
        select(func.max(col(AgentCallDay.day)))
        .where(col(AgentCallDay.org_id) == org_id_column)
        .scalar_subquery()
    )
    return func.greatest(usage, calls)


@auth_router.get("/api/admin/orgs")
def get_admin_orgs(
    request: Request, _: Principal = Depends(instance_access())
) -> list[dict]:
    """Every org, soft-deleted ones included (plan 59 section 4.7).

    Returns
    -------
    list of dict
        Per org: ``slug``, ``name``, ``url``, ``member_count``,
        ``created_at``, ``project_count`` (live projects), ``owners``
        (their GitHub logins, else display names), ``suspended`` (``{at,
        reason}`` or ``null``), ``deleted_at``, ``deleted_by`` (a user
        uid), ``spend_mtd`` (USD), ``storage_bytes`` (the latest
        samples carried forward, else ``null``) and ``last_activity_day``.
    """
    state = portal_state(request)
    base = _base(state)
    with get_session() as db:
        rows = db.exec(
            select(
                Organization.id,
                Organization.slug,
                Organization.name,
                Organization.created_at,
                Organization.suspended_at,
                Organization.suspended_reason,
                Organization.deleted_at,
                func.count(col(Membership.user_id)),
                _last_activity_day(col(Organization.id)),
            )
            .outerjoin(Membership, col(Membership.org_id) == col(Organization.id))
            .group_by(col(Organization.id))
            .order_by(Organization.slug)
        ).all()
        org_storage: dict[int, int] = {}
        for owner, size in latest_project_bytes(db).values():
            org_storage[owner] = org_storage.get(owner, 0) + size
        projects = dict(
            db.exec(
                select(Project.org_id, func.count())
                .where(col(Project.deleted_at).is_(None))
                .group_by(Project.org_id)
            ).all()
        )
        owners: dict[int, list[str]] = {}
        for org_id, login, name in db.exec(
            select(Membership.org_id, User.github_login, User.display_name)
            .join(User, col(User.id) == col(Membership.user_id))
            .where(Membership.role == Role.OWNER.value)
            .order_by(Membership.org_id, User.id)
        ).all():
            owners.setdefault(org_id, []).append(login or name)
        deleted_by = dict(
            db.exec(
                select(Organization.id, User.uid).join(
                    User, col(User.id) == col(Organization.deleted_by)
                )
            ).all()
        )
    return [
        {
            "slug": slug,
            "name": name,
            "url": base.org_origin(slug),
            "member_count": count,
            "created_at": created_at,
            "project_count": projects.get(org_id, 0),
            "owners": owners.get(org_id, []),
            "suspended": (
                None
                if suspended_at is None
                else {"at": suspended_at, "reason": suspended_reason}
            ),
            "deleted_at": deleted_at,
            "deleted_by": deleted_by.get(org_id),
            "spend_mtd": usd(state.spend.spent(org_id)),
            "storage_bytes": org_storage.get(org_id),
            "last_activity_day": last_day,
        }
        for (
            org_id,
            slug,
            name,
            created_at,
            suspended_at,
            suspended_reason,
            deleted_at,
            count,
            last_day,
        ) in rows
    ]


def _budget_rows(state: PortalState, db: Session, org_id: int) -> list[dict]:
    """The org's budgets (``state.budgets``) with their month-to-date spend."""
    budgets = state.budgets.for_org(org_id)
    if budgets is None or budgets.empty:
        return []
    limits = [
        limit for limit in (budgets.org, budgets.member_default) if limit is not None
    ]
    limits += list(budgets.members.values()) + list(budgets.projects.values())
    project_slugs = dict(
        db.exec(select(Project.id, Project.slug).where(Project.org_id == org_id)).all()
    )
    user_uids = dict(
        db.exec(
            select(User.id, User.uid).where(
                col(User.id).in_([m for m in budgets.members])
            )
        ).all()
    )
    rows = []
    for limit in limits:
        if limit.scope == "org":
            spent = state.spend.spent(org_id)
        elif limit.scope == "project":
            spent = state.spend.spent(org_id, project_id=limit.project_id)
        elif limit.scope == "member":
            spent = state.spend.spent(org_id, user_id=limit.user_id)
        else:
            spent = None
        rows.append(
            {
                "scope": limit.scope,
                "monthly_usd": usd(limit.monthly_usd),
                "hard_stop": limit.hard_stop,
                "spent_usd": None if spent is None else usd(spent),
                "project": project_slugs.get(limit.project_id),
                "member": user_uids.get(limit.user_id),
            }
        )
    return rows


@auth_router.get("/api/admin/orgs/{slug}")
def get_admin_org(
    slug: str,
    request: Request,
    _: Principal = Depends(instance_access()),
) -> dict:
    """One org for the admin panel, soft-deleted or suspended included.

    Returns
    -------
    dict
        The org's fields (``slug``, ``name``, ``url``, ``created_at``,
        ``default_project_role``, ``suspended`` (``at``, ``reason``, ``by``: the
        suspending admin's name or ``null``), ``deleted_at``,
        ``deleted_by``); ``members`` (``uid``, ``display_name``,
        ``github_login``, ``role``, ``joined_at``, ``has_password``,
        ``disabled``); ``open_invitations`` (a count); ``projects``
        (``slug``, ``name``, ``source``, ``last_scan_at``,
        ``last_run_status``, ``access_lost_reason``, ``storage_bytes`` (``null``
        with no sample), ``deleted_at``), soft-deleted ones
        included; ``budgets``; ``spend_mtd``; and ``rows`` (the Postgres row
        counts of ``usage_events``, ``audit_events`` and ``agent_call_days``).
        ``404`` for an unknown org.
    """
    state = portal_state(request)
    base = _base(state)
    now = _iso(_now())
    with get_session() as db:
        org = db.exec(select(Organization).where(Organization.slug == slug)).first()
        if org is None:
            raise ApiError(404, "organization not found")
        assert org.id is not None
        org_id = org.id
        members = [
            {
                "uid": user.uid,
                "display_name": user.display_name,
                "github_login": user.github_login,
                "role": membership.role,
                "joined_at": membership.created_at,
                "has_password": user.password_hash is not None,
                "disabled": user.disabled_at is not None,
            }
            for membership, user in db.exec(
                select(Membership, User)
                .join(User, col(User.id) == col(Membership.user_id))
                .where(Membership.org_id == org_id)
                .order_by(Membership.created_at, User.id)
            ).all()
        ]
        open_invitations = db.exec(
            select(func.count())
            .select_from(Invitation)
            .where(
                col(Invitation.org_id) == org_id,
                col(Invitation.redeemed_at).is_(None),
                col(Invitation.revoked_at).is_(None),
                col(Invitation.expires_at) > now,
            )
        ).one()
        latest_run = (
            select(ScanRun.status)
            .where(col(ScanRun.project_id) == col(Project.id))
            .order_by(col(ScanRun.id).desc())
            .limit(1)
            .scalar_subquery()
        )
        project_storage = latest_project_bytes(db, org_id=org_id)
        projects = [
            {
                "slug": project.slug,
                "name": project.name,
                "source": project.source,
                "last_scan_at": project.last_scan_at,
                "last_run_status": status,
                "access_lost_reason": project.access_lost_reason,
                "storage_bytes": (
                    stored[1] if (stored := project_storage.get(project.id)) else None
                ),
                "deleted_at": project.deleted_at,
            }
            for project, status in db.exec(
                select(Project, latest_run)
                .where(col(Project.org_id) == org_id)
                .order_by(Project.slug)
            ).all()
        ]
        counts = {
            "usage_events": db.exec(
                select(func.count())
                .select_from(UsageEvent)
                .where(col(UsageEvent.org_id) == org_id)
            ).one(),
            "audit_events": db.exec(
                select(func.count())
                .select_from(AuditEvent)
                .where(col(AuditEvent.org_id) == org_id)
            ).one(),
            "agent_call_days": db.exec(
                select(func.count())
                .select_from(AgentCallDay)
                .where(col(AgentCallDay.org_id) == org_id)
            ).one(),
        }
        deleted_by = (
            None
            if org.deleted_by is None
            else db.exec(select(User.uid).where(User.id == org.deleted_by)).first()
        )
        budgets = _budget_rows(state, db, org_id)
        suspended_by = None
        if org.suspended_by is not None:
            who = db.exec(
                select(User.display_name, User.github_login, User.deleted_at).where(
                    User.id == org.suspended_by
                )
            ).first()
            if who is not None:
                suspended_by = user_label(*who)[0]
        body = {
            "slug": org.slug,
            "name": org.name,
            "url": base.org_origin(org.slug),
            "created_at": org.created_at,
            "default_project_role": org.default_project_role,
            "suspended": (
                None
                if org.suspended_at is None
                else {
                    "at": org.suspended_at,
                    "reason": org.suspended_reason,
                    "by": suspended_by,
                }
            ),
            "deleted_at": org.deleted_at,
            "deleted_by": deleted_by,
        }
    return body | {
        "members": members,
        "open_invitations": open_invitations,
        "projects": projects,
        "budgets": budgets,
        "spend_mtd": usd(state.spend.spent(org_id)),
        "rows": counts,
    }


def _owner_candidate(db: Session, uid: str) -> User:
    """The account ``uid`` as an owner or member candidate, read ``FOR SHARE``.

    Must be the first statement of its transaction (plan 59 section 4.3.1):
    it waits for a running soft delete of the account, then refuses.
    ``404`` for an unknown uid, ``409 user_deleted``, ``409 user_disabled``
    (the code ``POST /api/org/members`` answers for one).
    """
    user = db.exec(
        select(User).where(User.uid == uid).with_for_update(read=True)
    ).first()
    if user is None:
        raise ApiError(404, "user not found")
    if user.deleted_at is not None:
        raise user_deleted_error()
    if user.disabled_at is not None:
        raise ApiError(409, "this account is disabled", code="user_disabled")
    return user


def _owner_needs_github() -> ApiError:
    return ApiError(
        409,
        "an organization's owner must be a GitHub account, not a password account",
        code="github_account_required",
    )


@auth_router.post("/api/admin/orgs", status_code=201)
def post_admin_org(
    body: AdminOrgBody,
    request: Request,
    principal: Principal = Depends(instance_access()),
) -> dict:
    """Create an org for an existing GitHub account (plan 59 section 4.7).

    Mirrors ``POST /api/orgs``'s slug rules: ``422 bad_slug``, ``409
    slug_taken`` for a live, soft-deleted or retired slug. The owner's row
    is read ``FOR SHARE`` first: ``404`` unknown, ``409 user_deleted``,
    ``409 user_disabled``, ``409 github_account_required`` for a password
    account. Ignores the org quota (D43).

    Returns
    -------
    dict
        ``{"slug", "name", "url"}``, audited as ``org_created`` with
        ``via="instance_admin"`` and the owner as ``target``.
    """
    base = _base(portal_state(request))
    slug = body.slug
    try:
        validate_org_slug(slug)
    except ValueError as exc:
        raise ApiError(422, str(exc), code="bad_slug") from exc
    name = body.name.strip()
    if not name:
        raise ApiError(422, "name must not be blank")
    slug_taken = ApiError(
        409, f"the organization {slug!r} already exists", code="slug_taken"
    )
    with get_session() as db:
        owner = _owner_candidate(db, body.owner_uid)
        if owner.password_hash is not None:
            raise _owner_needs_github()
        assert owner.id is not None
        if (
            db.exec(select(Organization.id).where(Organization.slug == slug)).first()
            is not None
        ):
            raise slug_taken
        try:
            org = create_org(db, slug=slug, name=name)
        except OrgSlugTaken as exc:  # retired by a deletion: the same answer
            raise slug_taken from exc
        except IntegrityError as exc:  # a concurrent create of the same slug
            raise slug_taken from exc
        assert org.id is not None
        org_id = org.id
        add_member(db, org_id=org_id, user_id=owner.id, role=Role.OWNER)
    audit(
        "org_created",
        request,
        uid=principal.uid,
        target=body.owner_uid,
        org_id=org_id,
        org=slug,
        via="instance_admin",
    )
    return {"slug": slug, "name": name, "url": base.org_origin(slug)}


@auth_router.patch("/api/admin/orgs/{slug}")
def patch_admin_org(
    slug: str,
    body: AdminOrgPatchBody,
    request: Request,
    principal: Principal = Depends(instance_access()),
) -> dict:
    """Rename an org (``422 bad_name`` as the owner's route; works while suspended).

    ``404`` for an unknown org, ``409 org_deleted`` for a soft-deleted one.

    Returns
    -------
    dict
        ``{"slug", "name"}``; audited as ``org_renamed`` with
        ``via="instance_admin"`` when the name changed.
    """
    name = _checked_name(body.name)
    with get_session() as db:
        org = _refuse_deleted(_org_state(slug, lock=True, db=db))
        row = db.exec(select(Organization).where(Organization.id == org.id)).one()
        previous = row.name
        row.name = name
        db.add(row)
    if name != previous:
        audit(
            "org_renamed",
            request,
            uid=principal.uid,
            org_id=org.id,
            org=org.slug,
            name=name,
            previous=previous,
            via="instance_admin",
        )
    return {"slug": org.slug, "name": name}


def _admin_member_row(user: User, role: str, joined_at: str) -> dict:
    return _member_row(user, role, joined_at) | {
        "has_password": user.password_hash is not None
    }


@auth_router.post("/api/admin/orgs/{slug}/members")
def post_admin_org_member(
    slug: str,
    body: AdminMemberBody,
    request: Request,
    principal: Principal = Depends(instance_access()),
) -> dict:
    """Add an existing account to an org (plan 59 section 4.7).

    Existing accounts only: someone with no account is invited by an org
    owner. The account's row is read ``FOR SHARE`` first: ``404`` unknown,
    ``409 user_deleted``, ``409 user_disabled``; ``409
    github_account_required`` for a password account as owner; ``422
    bad_role``; ``409 already_member``; ``404`` unknown org, ``409
    org_deleted``. Works on a suspended org (D33).

    Returns
    -------
    dict
        The member's row (``uid``, ``display_name``, ``github_login``,
        ``avatar_url``, ``role``, ``joined_at``, ``disabled``,
        ``has_password``); audited as ``member_added`` with ``via``.
    """
    role = _checked_role(body.role)
    already = ApiError(
        409, "they are already a member of this organization", code="already_member"
    )
    try:
        with get_session() as db:
            user = _owner_candidate(db, body.uid)
            if role == Role.OWNER.value and user.password_hash is not None:
                raise _owner_needs_github()
            org = db.exec(
                select(Organization.id, Organization.slug, Organization.deleted_at)
                .where(Organization.slug == slug)
                .with_for_update(read=True)
            ).first()
            if org is None:
                raise ApiError(404, "organization not found")
            if org[2] is not None:
                raise org_deleted_error()
            org_id, org_slug = org[0], org[1]
            assert user.id is not None
            if db.get(Membership, (org_id, user.id)) is not None:
                raise already
            membership = add_member(db, org_id=org_id, user_id=user.id, role=role)
            membership.welcome_pending = True
            db.add(membership)
            db.flush()
            row = _admin_member_row(user, membership.role, membership.created_at)
    except IntegrityError:  # a concurrent add of the same account won
        raise already from None
    audit(
        "member_added",
        request,
        uid=principal.uid,
        target=body.uid,
        org_id=org_id,
        org=org_slug,
        role=role,
        github_login=row["github_login"],
        grants=0,
        via="instance_admin",
    )
    return row


@auth_router.patch("/api/admin/orgs/{slug}/members/{uid}")
def patch_admin_org_member(
    slug: str,
    uid: str,
    body: AdminMemberRoleBody,
    request: Request,
    principal: Principal = Depends(instance_access()),
) -> dict:
    """Change a member's role: the owner's rules without "caller must be owner".

    ``409 last_owner`` for the final owner's demotion; ``409
    github_account_required`` promoting a password account to owner (the
    way an owner is handed over, P4); ``404 not_member``; ``409
    org_deleted``. Reads no user row beyond the member's own.

    Returns
    -------
    dict
        The member's row; audited as ``member_role_changed`` with ``via``.
    """
    role = _checked_role(body.role)
    with get_session() as db:
        org = _lock_org_share(db, slug)
        owners = _lock_owners(db, org.id)
        membership, user = _locked_target(db, org.id, uid)
        previous = membership.role
        owner = Role.OWNER.value
        if role == owner and previous != owner and user.password_hash is not None:
            raise _owner_needs_github()
        if previous == owner and role != owner and not set(owners) - {user.id}:
            raise _last_owner()
        membership.role = role
        db.add(membership)
        db.flush()
        assert user.id is not None
        if role in (Role.OWNER.value, Role.ADMIN.value):
            db.exec(
                delete(ProjectGrant).where(
                    col(ProjectGrant.org_id) == org.id,
                    col(ProjectGrant.user_id) == user.id,
                )
            )
        elif previous != role:
            connections.revoke_lost_access(db, org.id, user_id=user.id)
        row = _admin_member_row(user, role, membership.created_at)
    if previous != role:
        audit(
            "member_role_changed",
            request,
            uid=principal.uid,
            target=uid,
            org_id=org.id,
            org=org.slug,
            role=role,
            previous=previous,
            via="instance_admin",
        )
    return row


@auth_router.delete("/api/admin/orgs/{slug}/members/{uid}", status_code=204)
def delete_admin_org_member(
    slug: str,
    uid: str,
    request: Request,
    principal: Principal = Depends(instance_access()),
) -> Response:
    """Remove a member (``409 last_owner`` for the final owner), as the owner's route.

    Their connection tokens for the org are revoked (``member_removed``)
    and their member budget reloaded. ``404 not_member``, ``409
    org_deleted``. ``204``; audited as ``member_removed`` with ``via``.
    """
    with get_session() as db:
        org = _lock_org_share(db, slug)
        owners = _lock_owners(db, org.id)
        membership, user = _locked_target(db, org.id, uid)
        previous = membership.role
        if previous == Role.OWNER.value and not set(owners) - {user.id}:
            raise _last_owner()
        db.delete(membership)
        assert user.id is not None
        connections.revoke_for_member(db, org.id, user.id, "member_removed")
    reload_org_budgets(portal_state(request).budgets, org.id)
    audit(
        "member_removed",
        request,
        uid=principal.uid,
        target=uid,
        org_id=org.id,
        org=org.slug,
        role=previous,
        via="instance_admin",
    )
    return Response(status_code=204)


def _lock_org_share(db: Session, slug: str) -> _OrgState:
    """The org row ``FOR SHARE`` as a transaction's first statement; else ``404`` / ``409 org_deleted``.

    A concurrent soft delete (``FOR UPDATE``) either finished first, so the
    write is refused, or waits for this transaction.
    """
    row = db.exec(
        select(
            Organization.id,
            Organization.slug,
            Organization.suspended_at,
            Organization.suspended_reason,
            Organization.deleted_at,
        )
        .where(Organization.slug == slug)
        .with_for_update(read=True)
    ).first()
    return _refuse_deleted(None if row is None else _OrgState(*row))


def _admin_audit_filter(
    scope: str,
    org: str | None,
    event: str | None,
    actor: str | None,
    since: str | None,
    until: str | None,
) -> audit_store.AuditFilter:
    try:
        return audit_store.make_filter(
            orgless=scope == "orgless",
            org=org,
            event=event,
            actor=actor,
            since=since,
            until=until,
        )
    except ValueError as exc:
        raise ApiError(
            422, "from and to must be ISO-8601 dates or date-times", code="bad_date"
        ) from exc


@auth_router.get("/api/admin/audit")
def get_admin_audit(
    _: Principal = Depends(instance_access()),
    scope: Literal["all", "orgless"] = Query(default="all"),
    org: str | None = Query(default=None, max_length=100),
    event: str | None = Query(default=None, max_length=100),
    actor: str | None = Query(default=None, max_length=200),
    since: str | None = Query(default=None, alias="from", max_length=40),
    until: str | None = Query(default=None, alias="to", max_length=40),
    before: int | None = Query(default=None, ge=1),
    limit: int = Query(
        default=audit_store.PAGE_SIZE, ge=1, le=audit_store.MAX_PAGE_SIZE
    ),
) -> dict:
    """Security events across every org, or only those of no org.

    The filters of ``GET /api/org/audit``, plus ``scope`` (``all``, the
    default, is cross-org; ``orgless`` is sign-ins, instance admin and
    deleted orgs' events) and ``org`` (an org's slug, as its events kept
    it - a deleted org's too). Soft-deleted users show their real identity
    (``mask_deleted=False``, until Erase); a project target is labelled
    against its own event's org. ``422 bad_date`` for a malformed ``from``
    / ``to``.

    Returns
    -------
    dict
        ``{"events": [...], "next": <id or null>}``, newest first.
    """
    f = _admin_audit_filter(scope, org, event, actor, since, until)
    events, next_before = audit_store.query_events(
        f, before=before, limit=limit, mask_deleted=False
    )
    return {"events": events, "next": next_before}


@auth_router.get("/api/admin/audit.csv")
def get_admin_audit_csv(
    _: Principal = Depends(instance_access()),
    scope: Literal["all", "orgless"] = Query(default="all"),
    org: str | None = Query(default=None, max_length=100),
    event: str | None = Query(default=None, max_length=100),
    actor: str | None = Query(default=None, max_length=200),
    since: str | None = Query(default=None, alias="from", max_length=40),
    until: str | None = Query(default=None, alias="to", max_length=40),
) -> StreamingResponse:
    """The same events and filters as CSV, streamed, at most 50 000 rows."""
    f = _admin_audit_filter(scope, org, event, actor, since, until)
    return StreamingResponse(
        audit_store.iter_csv(f, mask_deleted=False),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="whygraph-admin-audit.csv"'
        },
    )


def _admin_user_row(
    user: User,
    *,
    org_count: int,
    owner_of: int,
    session_last_seen_at: str | None,
    deleted_by: str | None,
) -> dict:
    return {
        "uid": user.uid,
        "email": user.email,
        "display_name": user.display_name,
        "is_instance_admin": user.is_instance_admin,
        "github_login": user.github_login,
        "has_password": user.password_hash is not None,
        "disabled": user.disabled_at is not None,
        "created_at": user.created_at,
        "org_count": org_count,
        "owner_of": owner_of,
        "session_last_seen_at": session_last_seen_at,
        "deleted_at": user.deleted_at,
        "deleted_by": deleted_by,
        "erased": user.erased_at is not None,
    }


@auth_router.get("/api/admin/users")
def get_admin_users(_: Principal = Depends(instance_access())) -> list[dict]:
    """Every user, with how many orgs they belong to and own.

    ``session_last_seen_at`` is the newest ``last_seen_at`` of the user's
    everyday sessions (``None`` after a sign-out or a sweep, accurate to
    five minutes); ``deleted_by`` is the uid of the administrator who
    soft-deleted the account (plan 59 section 4.6).
    """
    deleter = aliased(User)
    last_seen = (
        select(func.max(UserSession.last_seen_at))
        .where(
            col(UserSession.user_id) == col(User.id),
            col(UserSession.kind) == "user",
        )
        .scalar_subquery()
    )
    deleted_by = (
        select(deleter.uid).where(col(deleter.id) == col(User.deleted_by))
    ).scalar_subquery()
    with get_session() as db:
        rows = db.exec(
            select(
                User,
                func.count(col(Membership.org_id)),
                func.count(col(Membership.org_id)).filter(
                    col(Membership.role) == Role.OWNER
                ),
                last_seen,
                deleted_by,
            )
            .outerjoin(Membership, col(Membership.user_id) == col(User.id))
            .group_by(col(User.id))
            .order_by(User.created_at, User.id)
        ).all()
        return [
            _admin_user_row(
                user,
                org_count=count,
                owner_of=owned,
                session_last_seen_at=seen,
                deleted_by=by,
            )
            for user, count, owned, seen, by in rows
        ]


def _refuse_gone_user(user: User) -> None:
    """``409 erased`` / ``409 user_deleted`` for a soft-deleted or erased account."""
    if user.erased_at is not None:
        raise _erased_error()
    if user.deleted_at is not None:
        raise user_deleted_error()


def _snapshot_memberships(user: User) -> list[dict]:
    """The memberships a soft delete removed (``[{org_id, role, ...}]``)."""
    if not user.deleted_memberships:
        return []
    try:
        entries = json.loads(user.deleted_memberships)
    except ValueError:
        return []
    return [e for e in entries if isinstance(e, dict) and "org_id" in e]


@auth_router.get("/api/admin/users/{uid}")
def get_admin_user(
    uid: str,
    request: Request,
    _: Principal = Depends(instance_access()),
) -> dict:
    """One user for the admin panel's detail page.

    The list row's fields plus ``memberships`` (org slug, name, role, owner
    flag; a soft-deleted user's snapshot, minus orgs that no longer exist),
    ``sessions`` of both kinds (never the hash; rows past their lifetime
    are left out), ``tokens`` (connection tokens, live and revoked),
    ``spend`` (this month's spend by org), and the latest 20 audit
    ``events`` where the user is the actor or the target.
    """
    state = portal_state(request)
    now = _iso(_now())
    with get_session() as db:
        user = _user_by_uid(db, uid)
        assert user.id is not None
        if user.deleted_at is None:
            member_rows = db.exec(
                select(
                    Organization.id,
                    Organization.slug,
                    Organization.name,
                    Membership.role,
                )
                .join(Membership, col(Membership.org_id) == col(Organization.id))
                .where(col(Membership.user_id) == user.id)
                .order_by(Organization.slug)
            ).all()
        else:
            roles = {e["org_id"]: e.get("role") for e in _snapshot_memberships(user)}
            member_rows = [
                (org_id, slug, name, roles[org_id])
                for org_id, slug, name in db.exec(
                    select(Organization.id, Organization.slug, Organization.name)
                    .where(col(Organization.id).in_(list(roles)))
                    .order_by(Organization.slug)
                ).all()
            ]
        memberships = [
            {
                "org": slug,
                "name": name,
                "role": role,
                "owner": role == Role.OWNER,
            }
            for _id, slug, name, role in member_rows
        ]
        spend = [
            {"org": slug, "spent_usd": usd(state.spend.spent(org_id, user_id=user.id))}
            for org_id, slug, _name, _role in member_rows
        ]
        owned = sum(1 for m in memberships if m["owner"])
        seen = db.exec(
            select(func.max(UserSession.last_seen_at)).where(
                col(UserSession.user_id) == user.id, col(UserSession.kind) == "user"
            )
        ).first()
        deleted_by = (
            db.exec(select(User.uid).where(User.id == user.deleted_by)).first()
            if user.deleted_by is not None
            else None
        )
        session_rows = db.exec(
            select(UserSession)
            .where(
                col(UserSession.user_id) == user.id,
                col(UserSession.expires_at) > now,
            )
            .order_by(col(UserSession.created_at).desc(), col(UserSession.id).desc())
        ).all()
        org_slugs = dict(db.exec(select(Organization.id, Organization.slug)).all())
        tokens = [
            {
                "uid": t.uid,
                "org": org_slugs.get(t.org_id) if t.org_id is not None else None,
                "project": t.project_slug,
                "project_name": t.project_name,
                "client_name": t.client_name,
                "created_at": t.created_at,
                "last_used_at": t.last_used_at,
                "revoked_at": t.revoked_at,
                "revoked_reason": t.revoked_reason,
            }
            for t in connections.list_for_user(db, user.id)
        ]
        result = _admin_user_row(
            user,
            org_count=len(memberships),
            owner_of=owned,
            session_last_seen_at=seen,
            deleted_by=deleted_by,
        )
        result["memberships"] = memberships
        result["sessions"] = [
            {
                "id": row.id,
                "kind": row.kind,
                "created_at": row.created_at,
                "last_seen_at": row.last_seen_at,
                "user_agent": row.user_agent,
            }
            for row in session_rows
        ]
        result["tokens"] = tokens
        result["spend"] = spend
    events, _next = audit_store.query_events(
        audit_store.make_filter(subject=uid), limit=20, mask_deleted=False
    )
    result["events"] = events
    return result


def _user_by_uid(db: Session, uid: str, *, lock: bool = False) -> User:
    statement = select(User).where(User.uid == uid)
    if lock:
        statement = statement.with_for_update()
    user = db.exec(statement).first()
    if user is None:
        raise ApiError(404, "user not found")
    return user


@auth_router.patch("/api/admin/users/{uid}")
def patch_admin_user(
    uid: str,
    body: AdminUserBody,
    request: Request,
    principal: Principal = Depends(instance_access()),
) -> dict:
    """Make a user an instance admin or remove that; disable or enable them.

    ``422`` with neither field. ``409 self_disable`` for your own account;
    ``409 last_admin`` when a demotion or a disable would leave no
    **enabled** admin; ``409 user_deleted`` / ``409 erased`` for a
    soft-deleted or erased account (read from the row already locked).
    The enabled admins' rows are locked (``SELECT ...
    FOR UPDATE``) first, so two concurrent changes cannot both pass the
    count. Disabling ends every session of the user at once, revokes their
    connection tokens (``user_disabled``) and kills their unused reset
    links; memberships and the GitHub id stay, so enabling restores
    everything but the tokens.
    """
    if body.is_instance_admin is None and body.disabled is None:
        raise ApiError(422, "nothing to change: set is_instance_admin or disabled")
    with get_session() as db:
        admins = db.exec(
            select(User.id)
            .where(
                col(User.is_instance_admin).is_(True),
                col(User.disabled_at).is_(None),
                col(User.deleted_at).is_(None),
            )
            .with_for_update()
        ).all()
        user = _user_by_uid(db, uid, lock=True)
        assert user.id is not None
        _refuse_gone_user(user)
        was_admin, was_disabled = user.is_instance_admin, user.disabled_at is not None
        admin = was_admin if body.is_instance_admin is None else body.is_instance_admin
        disabled = was_disabled if body.disabled is None else body.disabled
        if disabled and not was_disabled and user.id == principal.user_id:
            raise ApiError(
                409, "you cannot disable your own account", code="self_disable"
            )
        was_enabled_admin = was_admin and not was_disabled
        if was_enabled_admin and not (admin and not disabled) and len(admins) <= 1:
            raise ApiError(
                409,
                "this is the last enabled instance admin: make someone else an "
                "admin first",
                code="last_admin",
            )
        user.is_instance_admin = admin
        if disabled != was_disabled:
            user.disabled_at = _iso(_now()) if disabled else None
        db.add(user)
        db.flush()
        if disabled and not was_disabled:
            revoke_user(user.id, db=db)
            connections.revoke_for_user(db, user.id, "user_disabled")
            _invalidate_reset_links(db, user.id)
        # A demotion or a disable ends the user's reader grants (plan 59
        # section 4.4), in this transaction.
        revoked_grants: list[tuple[int, str]] = []
        grant_reason = None
        if disabled and not was_disabled:
            grant_reason = "disabled"
        elif was_admin and not admin:
            grant_reason = "demoted"
        if grant_reason is not None:
            revoked_grants = _revoke_reader_grants(db, user.id)
        target = user.uid
        target_id = user.id
        result = {
            "uid": user.uid,
            "email": user.email,
            "display_name": user.display_name,
            "is_instance_admin": user.is_instance_admin,
            "github_login": user.github_login,
            "has_password": user.password_hash is not None,
            "disabled": user.disabled_at is not None,
        }
    if admin != was_admin:
        event = "admin_granted" if admin else "admin_revoked"
        audit(event, request, uid=principal.uid, target=target)
    if disabled and not was_disabled:
        end_user_tokens(portal_state(request), user_id=target_id)
    if disabled != was_disabled:
        event = "user_disabled" if disabled else "user_enabled"
        audit(event, request, uid=principal.uid, target=target)
    for org_id, org_slug in revoked_grants:
        audit(
            "reader_grant_revoked",
            request,
            uid=principal.uid,
            target=target,
            org_id=org_id,
            org=org_slug,
            reason=grant_reason,
        )
    return result


@auth_router.post("/api/admin/users/{uid}/reset-link")
def post_admin_reset_link(
    uid: str,
    request: Request,
    principal: Principal = Depends(instance_access()),
) -> dict:
    """Issue a one-time, 24-hour reset link for a password account.

    ``409 no_password`` for a GitHub account, ``409 user_disabled`` for a
    disabled one, ``409 not_an_admin`` for a password account that is not
    an instance admin (plan 59 D28). The user's earlier unused
    links stop working. The token travels in the
    URL **fragment** (``<base>/reset#token=...``), so it never reaches a
    proxy log or a ``Referer``; only its SHA-256 is stored.
    """
    base = _base(portal_state(request))
    raw = secrets.token_urlsafe(32)
    now = _now()
    with get_session() as db:
        user = _user_by_uid(db, uid)
        assert user.id is not None
        _refuse_gone_user(user)
        if user.password_hash is None:
            raise _no_password()
        if user.disabled_at is not None:
            raise ApiError(
                409, "this account is disabled: enable it first", code="user_disabled"
            )
        if not user.is_instance_admin:
            raise _reset_not_an_admin()
        _invalidate_reset_links(db, user.id)
        db.add(
            PasswordReset(
                token_hash=hash_token(raw),
                user_id=user.id,
                created_by=principal.user_id,
                created_at=_iso(now),
                expires_at=_iso(now + RESET_LINK_LIFETIME),
            )
        )
        target = user.uid
    audit("reset_link_issued", request, uid=principal.uid, target=target)
    return {"url": f"{base.origin}/reset#token={raw}"}


def _caller_pairing(db: Session, principal: Principal) -> int | None:
    """The everyday session the calling admin session is paired with (D51)."""
    if principal.session_id is None:
        return None
    return db.exec(
        select(UserSession.paired_session_id).where(
            col(UserSession.id) == principal.session_id
        )
    ).first()


def _self_session() -> ApiError:
    return ApiError(
        409,
        "that would end the admin session you are using: sign out instead",
        code="self_session",
    )


@auth_router.delete("/api/admin/users/{uid}/sessions")
def delete_admin_user_sessions(
    uid: str,
    request: Request,
    body: AdminSessionsBody | None = None,
    principal: Principal = Depends(instance_access()),
) -> dict:
    """End every session of a user, or every one of one ``kind``.

    Ending everyday sessions first ends the admin sessions paired with them
    (plan 59 D51). ``409 self_session`` when the request would end the admin
    session making it: your own account with ``kind`` ``"admin"`` or
    omitted, or ``kind`` ``"user"`` while your admin session is paired with
    one of those rows. ``409 user_deleted`` / ``409 erased`` for a gone
    account. Audited as ``sessions_revoked``.

    Returns
    -------
    dict
        ``{"revoked": <how many sessions ended>}``.
    """
    kind = body.kind if body is not None else None
    with get_session() as db:
        user = _user_by_uid(db, uid)
        assert user.id is not None
        _refuse_gone_user(user)
        if user.id == principal.user_id and kind in (None, "admin"):
            raise _self_session()
        if kind == "user":
            paired = _caller_pairing(db, principal)
            if (
                paired is not None
                and db.exec(
                    select(UserSession.id).where(
                        col(UserSession.id) == paired,
                        col(UserSession.user_id) == user.id,
                    )
                ).first()
                is not None
            ):
                raise _self_session()
        statement = select(UserSession.id, UserSession.kind).where(
            col(UserSession.user_id) == user.id
        )
        if kind is not None:
            statement = statement.where(col(UserSession.kind) == kind)
        rows = db.exec(statement).all()
        paired_ended = 0
        if kind in (None, "user"):
            for session_id, row_kind in rows:
                if row_kind == "user":
                    paired_ended += end_paired_admin(db, session_id)
        if kind is None:
            revoke_user(user.id, db=db)
        else:
            revoke_user_kind(user.id, kind, db=db)
        count = len(rows) + (paired_ended if kind == "user" else 0)
        target, target_id = user.uid, user.id
        ended = [session_id for session_id, _kind in rows]
    state = portal_state(request)
    if kind == "admin":
        for session_id in ended:
            end_user_tokens(state, session_id=session_id)
    else:
        end_user_tokens(state, user_id=target_id)
    audit(
        "sessions_revoked",
        request,
        uid=principal.uid,
        target=target,
        kind=kind or "all",
        count=count,
    )
    return {"revoked": count}


@auth_router.delete("/api/admin/users/{uid}/sessions/{id}")
def delete_admin_user_session(
    uid: str,
    id: int,
    request: Request,
    principal: Principal = Depends(instance_access()),
) -> dict:
    """End one session of a user (``404`` when it is not theirs).

    ``409 self_session`` for the admin session making the request and for
    the everyday session it is paired with. Ending an everyday session ends
    the admin session paired with it first (D51). Audited as
    ``sessions_revoked``.

    Returns
    -------
    dict
        ``{"revoked": 1}``.
    """
    with get_session() as db:
        user = _user_by_uid(db, uid)
        assert user.id is not None
        _refuse_gone_user(user)
        row = db.exec(
            select(UserSession.id, UserSession.kind).where(
                col(UserSession.id) == id, col(UserSession.user_id) == user.id
            )
        ).first()
        if row is None:
            raise ApiError(404, "session not found")
        if id == principal.session_id or id == _caller_pairing(db, principal):
            raise _self_session()
        if row[1] == "user":
            end_paired_admin(db, id)
        db.exec(
            delete(UserSession).where(
                col(UserSession.id) == id, col(UserSession.user_id) == user.id
            )
        )
        target, session_kind = user.uid, row[1]
    end_user_tokens(portal_state(request), session_id=id)
    audit(
        "sessions_revoked",
        request,
        uid=principal.uid,
        target=target,
        kind=session_kind,
        count=1,
    )
    return {"revoked": 1}


@auth_router.delete("/api/admin/users/{uid}/tokens/{token_uid}", status_code=204)
def delete_admin_user_token(
    uid: str,
    token_uid: str,
    request: Request,
    principal: Principal = Depends(instance_access()),
) -> Response:
    """Revoke one live connection token of a user (``admin_revoked``).

    ``404`` for an unknown token, another user's, or one already revoked.
    Audited as ``connection_revoked`` with the token's org and project, so
    the organization sees it in its own log (``via="instance_admin"``).
    """
    with get_session() as db:
        user = _user_by_uid(db, uid)
        assert user.id is not None
        _refuse_gone_user(user)
        found = db.exec(
            select(ConnectionToken.org_id, Organization.slug, Project.slug)
            .outerjoin(
                Organization, col(Organization.id) == col(ConnectionToken.org_id)
            )
            .outerjoin(Project, col(Project.id) == col(ConnectionToken.project_id))
            .where(
                ConnectionToken.uid == token_uid,
                ConnectionToken.user_id == user.id,
            )
        ).first()
        if found is None or not connections.revoke(
            db, reason="admin_revoked", uid=token_uid, user_id=user.id
        ):
            raise ApiError(404, "token not found")
        org_id, org_slug, project_slug = found
        target = user.uid
    audit(
        "connection_revoked",
        request,
        uid=principal.uid,
        target=target,
        org_id=org_id,
        org=org_slug,
        project=project_slug,
        token_uid=token_uid,
        reason="admin_revoked",
        via="instance_admin",
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Soft delete, restore and erase of a user (plan 59 section 4.3.1)
# ---------------------------------------------------------------------------


class ConfirmUserBody(_Strict):
    """``DELETE /api/admin/users/{uid}`` and ``.../erase``: the account, typed."""

    confirm: str = Field(default="", max_length=1024)


def _confirmed_user(user: User, typed: str, doing: str) -> None:
    """``409 confirm_user`` unless ``typed`` names the account.

    The account is named by its GitHub login, else its email, else its uid
    (a soft-deleted user's login can have been released, and a GitHub
    account has no email: there is always something to type).
    """
    if user.github_login:
        what, expected = "GitHub login", user.github_login
    elif user.email:
        what, expected = "email", user.email
    else:
        what, expected = "ID", user.uid
    if typed.strip() != expected:
        raise ApiError(
            409,
            f"type the account's {what} ({expected}) to {doing}",
            code="confirm_user",
        )


def _lock_user(db: Session, uid: str) -> User:
    """The user row ``FOR NO KEY UPDATE`` (a foreign-key check still passes); else ``404``."""
    user = db.exec(
        select(User).where(User.uid == uid).with_for_update(key_share=True)
    ).first()
    if user is None:
        raise ApiError(404, "user not found")
    return user


@dataclass(frozen=True)
class _Removed:
    """One membership a soft delete removed."""

    org_id: int
    org_slug: str
    org_deleted: bool
    role: str


@auth_router.delete("/api/admin/users/{uid}")
def delete_admin_user(
    uid: str,
    request: Request,
    principal: Principal = Depends(instance_access()),
    body: ConfirmUserBody | None = Body(default=None),
) -> dict:
    """Soft-delete a user (plan 59 section 4.3.1, D12, D49).

    One transaction, in this order (it keeps a role change, which locks
    owners before its target, from deadlocking with it, and leaves no
    membership on a deleted user): the enabled admins and then the user row
    ``FOR NO KEY UPDATE``; the refusals; ``deleted_at`` / ``deleted_by``;
    the user's org rows ``FOR SHARE`` in id order (so a purge cannot
    interleave); each org's owner rows in ascending org id; the last-owner
    check over every org that still exists, soft-deleted ones included;
    then the memberships are deleted and what the delete returned is kept
    as the snapshot Restore uses. Project grants and member budgets go with
    them (not restored). The admin flag is cleared, every session ends,
    connection tokens are revoked (``user_deleted``), reset links and
    reader grants end.

    ``404`` for an unknown user, ``409 user_deleted`` / ``409 erased``,
    ``409 self_delete``, ``409 confirm_user`` unless ``confirm`` is the
    GitHub login (else the email, else the uid), ``409 last_admin`` and
    ``409 last_owner`` with ``orgs: [{slug, deleted}]``.

    Returns
    -------
    dict
        ``{"deleted": <uid>, "orgs": [<slug>, ...]}``, audited as
        ``user_deleted``, one ``member_removed`` per former org
        (``via="instance_admin"``, ``reason="user_deleted"``) and one
        ``reader_grant_revoked`` per grant.
    """
    state = portal_state(request)
    body = body or ConfirmUserBody()
    with get_session() as db:
        # (1) and (2): FOR NO KEY UPDATE, never a plain FOR UPDATE.
        admins = db.exec(
            select(User.id)
            .where(
                col(User.is_instance_admin).is_(True),
                col(User.disabled_at).is_(None),
                col(User.deleted_at).is_(None),
            )
            .with_for_update(key_share=True)
        ).all()
        user = _lock_user(db, uid)
        assert user.id is not None
        _refuse_gone_user(user)
        if user.id == principal.user_id:
            raise ApiError(
                409,
                "you cannot delete your own account: another administrator can",
                code="self_delete",
            )
        _confirmed_user(user, body.confirm, "delete it")
        if user.is_instance_admin and user.disabled_at is None and len(admins) <= 1:
            raise ApiError(
                409,
                "this is the last enabled instance admin: make someone else an "
                "admin first",
                code="last_admin",
            )
        user.deleted_at = _iso(_now())
        user.deleted_by = principal.user_id
        db.add(user)
        db.flush()
        # (3) The user's orgs, no lock; (3a) their rows FOR SHARE, in id order.
        org_ids = sorted(
            set(
                db.exec(
                    select(Membership.org_id).where(Membership.user_id == user.id)
                ).all()
            )
        )
        orgs = {
            org_id: (slug, deleted_at is not None)
            for org_id, slug, deleted_at in db.exec(
                select(Organization.id, Organization.slug, Organization.deleted_at)
                .where(col(Organization.id).in_(org_ids))
                .order_by(col(Organization.id))
                .with_for_update(read=True)
            ).all()
        }
        # (4) Owner rows, ascending org id; (5) the last-owner check (D49).
        last_owned = []
        for org_id in org_ids:
            if org_id not in orgs:
                continue  # purged meanwhile: its memberships went with it
            owners = _lock_owners(db, org_id)
            if owners == [user.id]:
                slug, deleted = orgs[org_id]
                last_owned.append({"slug": slug, "deleted": deleted})
        if last_owned:
            raise ApiError(
                409,
                "this is the last owner of "
                + ", ".join(o["slug"] for o in last_owned)
                + ": make someone else an owner of each first",
                code="last_owner",
                orgs=last_owned,
            )
        # (6) The snapshot is what the delete returns.
        returned = db.exec(
            delete(Membership)
            .where(col(Membership.user_id) == user.id)
            .returning(
                col(Membership.org_id),
                col(Membership.role),
                col(Membership.created_at),
                col(Membership.welcome_pending),
            )
        ).all()
        snapshot = sorted(
            (
                {
                    "org_id": org_id,
                    "role": role,
                    "created_at": created_at,
                    "welcome_pending": bool(welcome_pending),
                }
                for org_id, role, created_at, welcome_pending in returned
            ),
            key=lambda entry: entry["org_id"],
        )
        user.deleted_memberships = json.dumps(snapshot)
        user.is_instance_admin = False  # Restore does not grant it again
        db.add(user)
        db.flush()
        revoke_user(user.id, db=db)
        connections.revoke_for_user(db, user.id, "user_deleted")
        _invalidate_reset_links(db, user.id)
        revoked_grants = _revoke_reader_grants(db, user.id)
        removed = [
            _Removed(e["org_id"], *orgs[e["org_id"]], e["role"])
            for e in snapshot
            if e["org_id"] in orgs
        ]
        target, target_id = user.uid, user.id
    end_user_tokens(state, user_id=target_id)
    for entry in removed:
        if not entry.org_deleted:  # a deleted org's budgets stay unloaded
            reload_org_budgets(state.budgets, entry.org_id)
    audit("user_deleted", request, uid=principal.uid, target=target)
    for entry in removed:
        audit(
            "member_removed",
            request,
            uid=principal.uid,
            target=target,
            org_id=entry.org_id,
            org=entry.org_slug,
            role=entry.role,
            via="instance_admin",
            reason="user_deleted",
        )
    for org_id, org_slug in revoked_grants:
        audit(
            "reader_grant_revoked",
            request,
            uid=principal.uid,
            target=target,
            org_id=org_id,
            org=org_slug,
            reason="user_deleted",
        )
    return {"deleted": target, "orgs": [entry.org_slug for entry in removed]}


@auth_router.post("/api/admin/users/{uid}/restore")
def post_restore_user(
    uid: str,
    request: Request,
    principal: Principal = Depends(instance_access()),
) -> dict:
    """Bring a soft-deleted user back (plan 59 section 4.3.1, D24, D49).

    The user row is locked ``FOR NO KEY UPDATE`` first; ``409 erased`` for
    an erased account, ``409 not_deleted`` for a live one. Then
    ``deleted_at`` / ``deleted_by`` are cleared, the snapshot's org rows
    are locked ``FOR SHARE`` in id order, and every snapshot membership
    (role, join time, welcome banner) whose org still exists comes back -
    a soft-deleted org's too; only a purged org is skipped. Not restored:
    the admin flag, project grants, member budgets, sessions, tokens.

    Returns
    -------
    dict
        ``{"restored": [{org, role, deleted}], "skipped": [<org id>, ...]}``
        (``skipped``: the purged orgs), audited as ``user_restored`` and
        one ``member_added`` per org (``via="instance_admin"``,
        ``reason="user_restored"``).
    """
    state = portal_state(request)
    with get_session() as db:
        user = _lock_user(db, uid)
        assert user.id is not None
        if user.erased_at is not None:
            raise _erased_error()
        if user.deleted_at is None:
            raise not_deleted_error()
        snapshot = sorted(_snapshot_memberships(user), key=lambda e: e["org_id"])
        user.deleted_at = None
        user.deleted_by = None
        db.add(user)
        db.flush()
        orgs = {
            org_id: (slug, deleted_at is not None)
            for org_id, slug, deleted_at in db.exec(
                select(Organization.id, Organization.slug, Organization.deleted_at)
                .where(col(Organization.id).in_([e["org_id"] for e in snapshot]))
                .order_by(col(Organization.id))
                .with_for_update(read=True)
            ).all()
        }
        restored: list[_Removed] = []
        skipped: list[int] = []
        for entry in snapshot:
            org_id = entry["org_id"]
            if org_id not in orgs:
                skipped.append(org_id)
                continue
            db.add(
                Membership(
                    org_id=org_id,
                    user_id=user.id,
                    role=entry["role"],
                    created_at=entry.get("created_at") or _iso(_now()),
                    welcome_pending=bool(entry.get("welcome_pending")),
                )
            )
            restored.append(_Removed(org_id, *orgs[org_id], entry["role"]))
        user.deleted_memberships = None
        db.add(user)
        target = user.uid
    for entry in restored:
        if not entry.org_deleted:
            reload_org_budgets(state.budgets, entry.org_id)
    audit("user_restored", request, uid=principal.uid, target=target)
    for entry in restored:
        audit(
            "member_added",
            request,
            uid=principal.uid,
            target=target,
            org_id=entry.org_id,
            org=entry.org_slug,
            role=entry.role,
            via="instance_admin",
            reason="user_restored",
        )
    return {
        "restored": [
            {"org": e.org_slug, "role": e.role, "deleted": e.org_deleted}
            for e in restored
        ],
        "skipped": skipped,
    }


def _erase_blocked(projects: list[str]) -> ApiError:
    return ApiError(
        409,
        "nothing was erased: the chat history of "
        + ", ".join(projects)
        + " could not be opened; fix or permanently delete it, then erase again",
        code="erase_blocked",
        projects=projects,
    )


_PROJECT_DB_ERRORS = (
    ConfigError,
    UnsafePathError,
    SQLAlchemyError,
    sqlite3.Error,
    AlembicCommandError,
)
"""What makes a project database unopenable for Erase (D54)."""


def _erase_probe(state: PortalState) -> list[tuple[str, ProjectContext]]:
    """Erase step 1: open every project database that still exists (D54).

    Every project counts, soft-deleted ones and those of soft-deleted orgs
    included (the user may have left an org earlier). One without
    Initialize or without its ``whygraph.db`` file is skipped before
    anything opens it, so no empty file is ever created; a linked project
    has no database. The rest are opened the way the request path does:
    the context, the migration to head (with its path checks), then a read
    under :func:`~whygraph.core.context.use_project`.

    Returns
    -------
    list of (str, ProjectContext)
        ``("org/slug", context)`` of each database to clean.

    Raises
    ------
    ApiError
        ``409 erase_blocked`` with ``projects`` when one cannot be opened;
        nothing has changed then.
    """
    with get_session() as db:
        rows = db.exec(
            select(
                Project.id,
                Project.slug,
                Project.source,
                Project.initialized_at,
                Organization.slug,
            )
            .join(Organization, col(Organization.id) == col(Project.org_id))
            .order_by(col(Project.id))
        ).all()
    ready: list[tuple[str, ProjectContext]] = []
    blocked: list[str] = []
    for project_id, slug, source, initialized_at, org_slug in rows:
        label = f"{org_slug}/{slug}"
        if source == "platform" or initialized_at is None:
            continue
        if not project_db_path(state.data_dir, org_slug, slug).is_file():
            continue
        try:
            ctx = state.contexts.get(project_id)
            state.migrations.ensure(ctx)
            with use_project(ctx), project_db_session() as session:
                session.exec(select(ChatSession.id).limit(1)).all()
        except ProjectNotFound:
            continue  # purged meanwhile
        except _PROJECT_DB_ERRORS:
            blocked.append(label)
            continue
        ready.append((label, ctx))
    if blocked:
        raise _erase_blocked(blocked)
    return ready


def _erase_chats(projects: list[tuple[str, ProjectContext]], uid: str) -> None:
    """Erase step 3: delete ``uid``'s chat sessions, one project at a time (D38).

    The file is checked again right before each open: a project purged
    since the probe would otherwise get an empty database created in its
    old directory. One short session per project.
    """
    for label, ctx in projects:
        db_file = ctx.config.whygraph_db
        if db_file is None or not Path(db_file).is_file():
            continue
        try:
            with use_project(ctx), project_db_session() as session:
                ids = list(
                    session.exec(
                        select(ChatSession.id).where(ChatSession.owner_uid == uid)
                    ).all()
                )
                delete_session_rows(session, ids)  # type: ignore[arg-type]
        except _PROJECT_DB_ERRORS as exc:
            raise _erase_blocked([label]) from exc


def _released_logins(db: Session, uid: str) -> list[tuple[str, int]]:
    """``(login, event id)`` of each ``github_login_released`` event of ``uid``.

    A login taken from such an event matches only events up to that one:
    from then on it belongs to another person.
    """
    found = []
    for event_id, fields in db.exec(
        select(AuditEvent.id, AuditEvent.fields).where(
            AuditEvent.event == "github_login_released", AuditEvent.target == uid
        )
    ).all():
        login = fields.get("github_login") if isinstance(fields, dict) else None
        if isinstance(login, str) and login:
            found.append((login.lower(), event_id))
    return found


def _erase_portal_rows(uid: str) -> None:
    """Erase step 4: the portal's own stores, in one transaction (D54).

    The user row is locked ``FOR NO KEY UPDATE`` and checked again (a
    Restore may have committed since the route's first look); its login,
    email and GitHub id are read **before** they are nulled, because the
    audit matches below need them.

    Raises
    ------
    ApiError
        ``404``, ``409 erased`` or ``409 not_deleted``; nothing changed.
    """
    with get_session() as db:
        user = _lock_user(db, uid)
        assert user.id is not None
        if user.erased_at is not None:
            raise _erased_error()
        if user.deleted_at is None:
            raise not_deleted_error()
        user_id, login, github_id = user.id, user.github_login, user.github_id
        # The fields-only match: the current login, each released login up
        # to its release event (by id: created_at has second resolution and
        # the taker's own sign-in lands in the same second), the GitHub id.
        params: dict = {"id": user_id, "uid": uid, "label": DELETED_USER}
        by_fields = []
        if login:
            params["login"] = login.lower()
            by_fields.append("lower(fields->>'github_login') = :login")
        for i, (released, event_id) in enumerate(_released_logins(db, uid)):
            params[f"rl{i}"], params[f"rid{i}"] = released, event_id
            by_fields.append(
                f"(lower(fields->>'github_login') = :rl{i} AND id <= :rid{i})"
            )
        if github_id is not None:
            params["github_id"] = str(github_id)  # ->> yields text
            by_fields.append("fields->>'github_id' = :github_id")
        fields_match = " OR ".join(by_fields) if by_fields else "false"
        db.exec(  # type: ignore[call-overload]
            text(
                "UPDATE audit_events SET actor_label = :label, ip = NULL "
                "WHERE actor_id = :id"
            ),
            params=params,
        )
        # An actorless event that names the person in fields carries their
        # own address (a refused sign-in) - except the release of their
        # login, which carries the address of the person who took it.
        db.exec(  # type: ignore[call-overload]
            text(
                "UPDATE audit_events SET ip = NULL WHERE actor_id IS NULL "
                "AND NOT (event = 'github_login_released' AND target = :uid) "
                f"AND ({fields_match})"
            ),
            params=params,
        )
        db.exec(  # type: ignore[call-overload]
            text(
                "UPDATE audit_events SET fields = (fields::jsonb - "
                "ARRAY['email','github_login','github_id','client_name'])::json "
                f"WHERE actor_id = :id OR target = :uid OR {fields_match}"
            ),
            params=params,
        )
        db.exec(
            update(UsageEvent)
            .where(col(UsageEvent.user_id) == user_id)
            .values(actor_label=DELETED_USER, client_name=None)
        )
        invited = col(Invitation.redeemed_by) == user_id
        if github_id is not None:
            invited = or_(invited, col(Invitation.github_id) == github_id)
        db.exec(delete(Invitation).where(invited))
        db.exec(delete(ConnectionToken).where(col(ConnectionToken.user_id) == user_id))
        db.exec(delete(ReaderGrant).where(col(ReaderGrant.user_id) == user_id))
        user.display_name = DELETED_USER
        user.email = None
        user.github_id = None
        user.github_login = None
        user.avatar_url = None
        user.password_hash = None
        user.deleted_memberships = None
        user.erased_at = _iso(_now())
        db.add(user)


@auth_router.post("/api/admin/users/{uid}/erase")
def post_erase_user(
    uid: str,
    request: Request,
    principal: Principal = Depends(instance_access()),
    body: ConfirmUserBody | None = Body(default=None),
) -> dict:
    """Erase a soft-deleted user's personal data, irreversibly (D13, D38, D54).

    Only a soft-deleted user (``409 not_deleted``; ``409 erased`` again),
    with ``confirm`` as for the soft delete (``409 confirm_user``). All or
    nothing, in this order: (1) every project database that still exists
    is opened, and one that cannot be stops it with ``409 erase_blocked``
    and ``projects``; (2) the audit and usage writers are flushed, so no
    queued row brings the identity back (``503 busy`` when one cannot be);
    (3) the user's chat sessions are deleted project by project; (4) the
    portal's stores are rewritten in one transaction: the user row becomes
    ``[Deleted User]`` (its GitHub id is freed, so the person can sign up
    again as a new account), the audit actor label, IP and identifying
    fields, usage labels and machine names, invitations, connection tokens
    and reader grants. A stopped run leaves the user deleted, not erased.

    Returns
    -------
    dict
        ``{"erased": <uid>}``, audited as ``user_erased`` with the uid only.
    """
    state = portal_state(request)
    body = body or ConfirmUserBody()
    with get_session() as db:
        user = _user_by_uid(db, uid)
        if user.erased_at is not None:
            raise _erased_error()
        if user.deleted_at is None:
            raise not_deleted_error()
        _confirmed_user(user, body.confirm, "erase it")
    projects = _erase_probe(state)
    writer = current_audit_writer()
    if (writer is not None and not writer.flush()) or not state.usage_writer.flush():
        raise ApiError(
            503,
            "recent events are still being written: try again in a minute",
            code="busy",
        )
    _erase_chats(projects, uid)
    _erase_portal_rows(uid)
    audit("user_erased", request, uid=principal.uid, target=uid)
    return {"erased": uid}


# ---------------------------------------------------------------------------
# The admin panel's own account (plan 59 section 4.2)
# ---------------------------------------------------------------------------


@auth_router.get("/api/admin/me")
def get_admin_me(
    request: Request, principal: Principal = Depends(instance_access())
) -> dict:
    """The signed-in admin and this admin session.

    ``session`` holds the admin session's start and its absolute end (8
    hours; there is no idle limit). ``reader_grants`` lists the admin's live
    per-org reader grants, the soonest to end first.
    """
    assert principal.session_id is not None  # an admin session principal
    base = _base(portal_state(request))
    with get_session() as db:
        row = db.exec(
            select(UserSession.created_at, UserSession.expires_at).where(
                UserSession.id == principal.session_id
            )
        ).first()
        grants = db.exec(
            select(
                ReaderGrant.id,
                Organization.slug,
                Organization.name,
                ReaderGrant.expires_at,
            )
            .join(Organization, col(Organization.id) == col(ReaderGrant.org_id))
            .where(
                col(ReaderGrant.user_id) == principal.user_id,
                col(ReaderGrant.revoked_at).is_(None),
                col(ReaderGrant.expires_at) > _iso(_now()),
            )
            .order_by(col(ReaderGrant.expires_at), col(ReaderGrant.id))
        ).all()
    if row is None:  # ended since this request was resolved
        raise ApiError(401, "admin sign-in required", code="admin_login_required")
    created_at, expires_at = row
    return {
        "uid": principal.uid,
        "display_name": principal.display_name,
        "github_login": principal.github_login,
        "avatar_url": principal.avatar_url,
        "has_password": principal.has_password,
        "session": {"created_at": created_at, "expires_at": expires_at},
        "reader_grants": [
            {
                "id": grant_id,
                "org_slug": slug,
                "org_name": name,
                "org_url": base.org_origin(slug),
                "expires_at": grant_expires_at,
            }
            for grant_id, slug, name, grant_expires_at in grants
        ],
    }


# ---------------------------------------------------------------------------
# Per-org reader grants (plan 59 section 4.4, D3)
# ---------------------------------------------------------------------------


@auth_router.post("/api/admin/orgs/{slug}/reader-grant")
def post_reader_grant(
    slug: str,
    request: Request,
    principal: Principal = Depends(instance_access()),
) -> dict:
    """Give the calling admin's everyday session read-only access to one org.

    The grant is the admin's **own** and lasts :data:`READER_GRANT_TTL`
    with no renewal (D17): while the admin holds a live grant on the org,
    this returns that grant unchanged and audits nothing. ``409
    github_account_required`` for a password account (it has no everyday
    session to carry the grant, D4), ``404`` for an unknown org, ``409
    org_deleted`` for a soft-deleted one (D34); a suspended org is allowed
    (D33). The admin's row is locked first, so two requests (or a request
    and a demotion) are serialized and leave at most one live grant.

    Returns
    -------
    dict
        ``{"id", "org_url", "expires_at"}``. A new grant is audited as
        ``reader_grant_created`` in the org's own log.
    """
    base = _base(portal_state(request))
    now = _now()
    with get_session() as db:
        user = db.exec(
            select(User)
            .where(User.id == principal.user_id)
            .with_for_update(key_share=True)
        ).first()
        if user is None or not user.is_instance_admin or user.disabled_at is not None:
            raise ApiError(
                403,
                f"only instance admins can {Action.INSTANCE_ADMIN}",
                code="forbidden",
                action=str(Action.INSTANCE_ADMIN),
            )
        if user.password_hash is not None:
            raise ApiError(
                409,
                "a password account has no everyday session to read an "
                "organization with: sign in with GitHub",
                code="github_account_required",
            )
        org = db.exec(
            select(Organization)
            .where(Organization.slug == slug)
            .with_for_update(read=True)
        ).first()
        if org is None:
            raise ApiError(404, "organization not found")
        if org.deleted_at is not None:
            raise ApiError(
                409,
                "this organization is deleted: restore it first",
                code="org_deleted",
            )
        assert org.id is not None
        org_id = org.id
        live = db.exec(
            select(ReaderGrant)
            .where(
                col(ReaderGrant.user_id) == principal.user_id,
                col(ReaderGrant.org_id) == org_id,
                col(ReaderGrant.revoked_at).is_(None),
                col(ReaderGrant.expires_at) > _iso(now),
            )
            .order_by(col(ReaderGrant.expires_at).desc())
        ).first()
        if live is not None:
            return {
                "id": live.id,
                "org_url": base.org_origin(org.slug),
                "expires_at": live.expires_at,
            }
        grant = ReaderGrant(
            user_id=principal.user_id,
            org_id=org_id,
            created_at=_iso(now),
            expires_at=_iso(now + READER_GRANT_TTL),
        )
        db.add(grant)
        db.flush()
        result = {
            "id": grant.id,
            "org_url": base.org_origin(org.slug),
            "expires_at": grant.expires_at,
        }
        org_slug = org.slug
    audit(
        "reader_grant_created", request, uid=principal.uid, org_id=org_id, org=org_slug
    )
    return result


@auth_router.delete("/api/admin/reader-grants/{id}", status_code=204)
def delete_reader_grant(
    id: int,
    request: Request,
    principal: Principal = Depends(instance_access()),
) -> None:
    """End one of the calling admin's own reader grants now.

    ``404`` for an unknown grant and for another admin's. A grant that has
    already ended (revoked or expired) is left as it is and audits
    nothing; a live one is audited as ``reader_grant_revoked`` in the
    org's log.
    """
    now = _iso(_now())
    with get_session() as db:
        row = db.exec(
            select(ReaderGrant, Organization.slug)
            .join(Organization, col(Organization.id) == col(ReaderGrant.org_id))
            .where(
                col(ReaderGrant.id) == id,
                col(ReaderGrant.user_id) == principal.user_id,
            )
            .with_for_update(of=ReaderGrant)
        ).first()
        if row is None:
            raise ApiError(404, "reader grant not found")
        grant, org_slug = row
        if grant.revoked_at is not None or grant.expires_at <= now:
            return None
        grant.revoked_at = now
        db.add(grant)
        org_id = grant.org_id
    audit(
        "reader_grant_revoked", request, uid=principal.uid, org_id=org_id, org=org_slug
    )
    return None


# ---------------------------------------------------------------------------
# Org suspension (plan 59 section 4.5, D6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _OrgState:
    """An org's id, slug and the columns suspend / unsuspend decide on."""

    id: int
    slug: str
    suspended_at: str | None
    suspended_reason: str | None
    deleted_at: str | None


def _org_state(
    slug: str, *, lock: bool = False, db: Session | None = None
) -> _OrgState | None:
    """The org ``slug``'s :class:`_OrgState` (``FOR UPDATE`` with ``lock``), or ``None``."""
    statement = select(
        Organization.id,
        Organization.slug,
        Organization.suspended_at,
        Organization.suspended_reason,
        Organization.deleted_at,
    ).where(Organization.slug == slug)
    if lock:
        statement = statement.with_for_update()
    if db is None:
        with get_session() as session:
            row = session.exec(statement).first()
    else:
        row = db.exec(statement).first()
    return None if row is None else _OrgState(*row)


def _refuse_deleted(org: _OrgState | None) -> _OrgState:
    """``404`` for an unknown org and ``409 org_deleted`` for a soft-deleted one (D34)."""
    if org is None:
        raise ApiError(404, "organization not found")
    if org.deleted_at is not None:
        raise ApiError(
            409, "this organization is deleted: restore it first", code="org_deleted"
        )
    return org


def _suspension_body(org: _OrgState) -> dict:
    """The answer of both routes: the org's slug and its suspension, or ``None``."""
    suspended = (
        None
        if org.suspended_at is None
        else {"at": org.suspended_at, "reason": org.suspended_reason}
    )
    return {"slug": org.slug, "suspended": suspended}


def _suspend_rows(
    slug: str, reason: str, by: int
) -> tuple[_OrgState, list[str] | None]:
    """Lock the org row, set the three columns (one transaction); the org and its project slugs.

    Re-checked under the lock: a concurrent soft delete answers ``409
    org_deleted``, and a suspension that committed first is returned as
    it is, with ``None`` for the slugs (nothing changed).
    """
    with get_session() as db:
        org = _refuse_deleted(_org_state(slug, lock=True, db=db))
        if org.suspended_at is not None:
            return org, None
        now = _iso(_now())
        db.exec(
            update(Organization)
            .where(col(Organization.id) == org.id)
            .values(suspended_at=now, suspended_reason=reason, suspended_by=by)
        )
        project_slugs = list(
            db.exec(select(Project.slug).where(Project.org_id == org.id)).all()
        )
    return _OrgState(org.id, org.slug, now, reason, None), project_slugs


@auth_router.post("/api/admin/orgs/{slug}/suspend")
async def post_suspend_org(
    slug: str,
    body: SuspendBody,
    request: Request,
    principal: Principal = Depends(instance_access()),
) -> dict:
    """Suspend an org: its members only read until it is unsuspended (plan 59 D6).

    ``404`` for an unknown org, ``409 org_deleted`` for a soft-deleted one
    (D34). An org already suspended answers ``200`` and changes nothing,
    before any reservation. Otherwise, under
    :meth:`~whygraph.portal.runner.ScanRunner.reserve_projects` (new run
    requests refused, the ones in flight waited for, queued runs dropped as
    ``cancelled`` and running ones ended): the org row is locked and its
    ``suspended_at`` / ``suspended_reason`` / ``suspended_by`` set, then
    every describe running for one of its projects is stopped (D35). ``409
    busy`` when the reservation cannot be taken (the org is being deleted,
    one of its projects removed, or a sync cannot be stopped yet): nothing
    changes. Every gate reads the columns per request, so no context is
    invalidated. A chat turn or a purpose generation already running
    finishes; the next one is refused.

    Returns
    -------
    dict
        ``{"slug", "suspended": {"at", "reason"}}``. A new suspension is
        audited as ``org_suspended`` in the org's own log.
    """
    state = portal_state(request)
    org = _refuse_deleted(await anyio.to_thread.run_sync(_org_state, slug))
    if org.suspended_at is not None:
        return _suspension_body(org)
    try:
        async with state.runner.reserve_projects(org.id, reason="suspended"):
            org, project_slugs = await anyio.to_thread.run_sync(
                _suspend_rows, slug, body.reason, principal.user_id
            )
            for project_slug in project_slugs or ():
                stop_describes(project_db_path(state.data_dir, org.slug, project_slug))
    except ProjectBusy as exc:
        raise ApiError(409, str(exc), code="busy") from exc
    if project_slugs is not None:  # this request suspended it
        audit("org_suspended", request, uid=principal.uid, org_id=org.id, org=org.slug)
    return _suspension_body(org)


@auth_router.post("/api/admin/orgs/{slug}/unsuspend")
def post_unsuspend_org(
    slug: str,
    request: Request,
    principal: Principal = Depends(instance_access()),
) -> dict:
    """Lift an org's suspension: every gate opens on the next request.

    ``404`` for an unknown org, ``409 org_deleted`` for a soft-deleted one
    (D34); an org that is not suspended answers ``200`` and changes
    nothing. Runs and imports the suspension cancelled are not queued
    again.

    Returns
    -------
    dict
        ``{"slug", "suspended": null}``. Lifting a suspension is audited as
        ``org_unsuspended`` in the org's own log.
    """
    with get_session() as db:
        org = _refuse_deleted(_org_state(slug, lock=True, db=db))
        if org.suspended_at is None:
            return _suspension_body(org)
        db.exec(
            update(Organization)
            .where(col(Organization.id) == org.id)
            .values(suspended_at=None, suspended_reason=None, suspended_by=None)
        )
    audit("org_unsuspended", request, uid=principal.uid, org_id=org.id, org=org.slug)
    return {"slug": org.slug, "suspended": None}


# ---------------------------------------------------------------------------
# Soft delete, restore and purge of orgs and projects (plan 59 sections
# 4.3.2, 4.3.3, D12, D14, D34)
# ---------------------------------------------------------------------------


class ConfirmSlugBody(_Strict):
    """``DELETE /api/admin/orgs/{slug}`` and ``.../purge``: the slug, typed to confirm."""

    confirm_slug: str = Field(default="", max_length=100)


class ConfirmNameBody(_Strict):
    """``DELETE /api/admin/projects/{org}/{slug}`` and ``.../purge``: the name, typed."""

    confirm_name: str = Field(default="", max_length=1000)


def _confirmed_slug(org: _OrgState, typed: str, doing: str) -> None:
    """``409 confirm_slug`` unless ``typed`` is the org's slug."""
    if typed != org.slug:
        raise ApiError(
            409,
            f"type the organization's slug ({org.slug}) to {doing}",
            code="confirm_slug",
        )


def _admin_busy(exc: ProjectBusy) -> ApiError:
    return ApiError(409, str(exc), code="busy")


@auth_router.delete("/api/admin/orgs/{slug}")
async def delete_admin_org(
    slug: str,
    request: Request,
    principal: Principal = Depends(instance_access()),
    body: ConfirmSlugBody | None = Body(default=None),
) -> dict:
    """Soft-delete an org (:func:`~whygraph.portal.org_routes.soft_delete_org`).

    The owner's ``DELETE /api/org``, from the admin panel: ``404`` for an
    unknown org, ``409 org_deleted`` for one already deleted (D34), ``409
    confirm_slug`` unless ``confirm_slug`` is its slug, ``409 busy`` as
    the owner's route. Works on a suspended org (D33).

    Returns
    -------
    dict
        ``{"deleted": <slug>, "projects": <count>}``, audited as
        ``org_deleted`` with ``soft`` and ``via="instance_admin"``.
    """
    state = portal_state(request)
    body = body or ConfirmSlugBody()
    org = _refuse_deleted(await anyio.to_thread.run_sync(_org_state, slug))
    _confirmed_slug(org, body.confirm_slug, "delete it")
    count = await soft_delete_org(state, org.id, org.slug, principal.user_id)
    audit(
        "org_deleted",
        request,
        uid=principal.uid,
        org_id=org.id,
        org=org.slug,
        projects=count,
        soft=True,
        via="instance_admin",
    )
    return {"deleted": org.slug, "projects": count}


def _restore_org_rows(org_id: int) -> list[int]:
    """Clear ``deleted_at`` / ``deleted_by`` under the row lock; the org's project ids.

    ``409 not_deleted`` when it is live (a concurrent Restore won); ``404``
    when it is gone (a purge won).
    """
    with get_session() as db:
        row = db.exec(
            select(Organization.id, Organization.deleted_at)
            .where(Organization.id == org_id)
            .with_for_update()
        ).first()
        if row is None:
            raise ApiError(404, "organization not found")
        if row[1] is None:
            raise not_deleted_error()
        db.exec(
            update(Organization)
            .where(col(Organization.id) == org_id)
            .values(deleted_at=None, deleted_by=None)
        )
        return list(db.exec(select(Project.id).where(Project.org_id == org_id)).all())


@auth_router.post("/api/admin/orgs/{slug}/restore")
async def post_restore_org(
    slug: str,
    request: Request,
    principal: Principal = Depends(instance_access()),
) -> dict:
    """Bring a soft-deleted org back for its members (plan 59 section 4.3.2).

    ``404`` for an unknown org, ``409 not_deleted`` for a live one. Under
    :meth:`~whygraph.portal.runner.ScanRunner.reserve_projects` (``restored``;
    ``409 busy`` while it is held), so it cannot interleave with a purge of
    the org or of one of its projects. Its budgets are reloaded and its
    projects' contexts dropped. Not restored: connection tokens (revoked),
    a cancelled import (Retry), a rescan, and projects soft-deleted on
    their own.

    Returns
    -------
    dict
        ``{"restored": <slug>}``, audited as ``org_restored``.
    """
    state = portal_state(request)
    org = await anyio.to_thread.run_sync(_org_state, slug)
    if org is None:
        raise ApiError(404, "organization not found")
    if org.deleted_at is None:
        raise not_deleted_error()
    try:
        async with state.runner.reserve_projects(org.id, reason="restored"):
            project_ids = await anyio.to_thread.run_sync(_restore_org_rows, org.id)
    except ProjectBusy as exc:
        raise _admin_busy(exc) from exc
    reload_org_budgets(state.budgets, org.id)
    for project_id in project_ids:
        state.contexts.invalidate(project_id)
    audit("org_restored", request, uid=principal.uid, org_id=org.id, org=org.slug)
    return {"restored": org.slug}


@auth_router.post("/api/admin/orgs/{slug}/purge")
async def post_purge_org(
    slug: str,
    request: Request,
    principal: Principal = Depends(instance_access()),
    body: ConfirmSlugBody | None = Body(default=None),
) -> dict:
    """Delete a soft-deleted org permanently (:func:`~whygraph.portal.org_routes.purge_org`).

    ``404`` for an unknown org, ``409 not_deleted`` for a live one (checked
    again under the purge's row lock, so it cannot race a Restore), ``409
    confirm_slug`` unless ``confirm_slug`` is its slug, ``409 busy``. Its
    projects, clones, history, run files, memberships, config and keys are
    deleted and the slug is retired.

    Returns
    -------
    dict
        ``{"purged": <slug>, "projects": <count>}``, audited as
        ``org_purged``.
    """
    state = portal_state(request)
    body = body or ConfirmSlugBody()
    org = await anyio.to_thread.run_sync(_org_state, slug)
    if org is None:
        raise ApiError(404, "organization not found")
    if org.deleted_at is None:
        raise not_deleted_error()
    _confirmed_slug(org, body.confirm_slug, "delete it permanently")
    count = await purge_org(state, org.id, org.slug)
    audit(
        "org_purged",
        request,
        uid=principal.uid,
        org_id=org.id,
        org=org.slug,
        projects=count,
    )
    return {"purged": org.slug, "projects": count}


@dataclass(frozen=True)
class _ProjectState:
    """An admin route's project: the row and its org's id, slug and deleted state."""

    project: Project
    org_id: int
    org_slug: str
    org_deleted: bool


def _project_state(org_slug: str, slug: str) -> _ProjectState:
    """The project ``slug`` of the org ``org_slug``, soft-deleted or not; else ``404``."""
    with get_session() as db:
        row = db.exec(
            select(Project, Organization.deleted_at)
            .join(Organization, col(Organization.id) == col(Project.org_id))
            .where(Organization.slug == org_slug, Project.slug == slug)
        ).first()
        if row is None:
            raise ApiError(404, "project not found")
        project, org_deleted_at = row
        db.expunge(project)
    return _ProjectState(project, project.org_id, org_slug, org_deleted_at is not None)


def _bound(state: PortalState, project: Project) -> BoundProject:
    """``project`` bound as its admin would be, for the removal code (blocking)."""
    assert project.id is not None
    try:
        ctx = state.contexts.get(project.id)
    except ProjectNotFound as exc:
        raise ApiError(404, "project not found") from exc
    except ConfigError as exc:
        raise ApiError(400, f"invalid project config: {exc}") from exc
    return bound_from(project, ctx, ProjectRole.ADMIN)


def _project_deleted_error() -> ApiError:
    return ApiError(
        409, "this project is deleted: restore it first", code="project_deleted"
    )


@auth_router.delete("/api/admin/projects/{org}/{slug}")
def delete_admin_project(
    org: str,
    slug: str,
    request: Request,
    principal: Principal = Depends(instance_access()),
    body: ConfirmNameBody | None = Body(default=None),
) -> dict:
    """Soft-delete a project (:func:`~whygraph.portal.routes.soft_delete_project`).

    The org admin's production ``DELETE /api/projects/{slug}``, from the
    admin panel: ``404`` for an unknown org or project, ``409 org_deleted``
    / ``409 project_deleted`` when either is already deleted (D34), ``409
    confirm_name`` unless ``confirm_name`` is the project's name, ``409
    busy`` while a scan or sync is queued, running or being requested.

    Returns
    -------
    dict
        The ``DeleteProjectResult`` shape with ``soft: true``, audited as
        ``project_removed`` with ``soft`` and ``via="instance_admin"``.
    """
    state = portal_state(request)
    body = body or ConfirmNameBody()
    found = _project_state(org, slug)
    if found.org_deleted:
        raise org_deleted_error()
    if found.project.deleted_at is not None:
        raise _project_deleted_error()
    if body.confirm_name != found.project.name:
        raise ApiError(409, "type the project name to remove it", code="confirm_name")
    bound = _bound(state, found.project)
    try:
        soft_delete_project(state, bound, principal.user_id)
    except ProjectBusy as exc:
        raise _admin_busy(exc) from exc
    except ProjectGone as exc:  # deleted meanwhile
        raise ApiError(409, str(exc), code=exc.code) from exc
    audit(
        "project_removed",
        request,
        uid=principal.uid,
        org_id=found.org_id,
        org=found.org_slug,
        project=bound.slug,
        source=bound.source,
        checkout_deleted=False,
        history_deleted=False,
        soft=True,
        via="instance_admin",
    )
    return soft_delete_result(bound)


@auth_router.post("/api/admin/projects/{org}/{slug}/restore")
def post_restore_project(
    org: str,
    slug: str,
    request: Request,
    principal: Principal = Depends(instance_access()),
) -> dict:
    """Bring a soft-deleted project back (plan 59 section 4.3.3).

    ``404`` for an unknown org or project; ``409 org_deleted`` while its
    org is deleted (restore the org first); ``409 not_deleted`` for a live
    project. Under the runner's removal reservation (``409 busy``), the
    claim a purge takes too, so a purge that passed its check cannot
    destroy a project that was just restored. Its connection tokens stay
    revoked.

    Returns
    -------
    dict
        ``{"restored": <slug>}``, audited as ``project_restored``.
    """
    state = portal_state(request)
    found = _project_state(org, slug)
    if found.org_deleted:
        raise org_deleted_error()
    project_id = found.project.id
    assert project_id is not None
    try:
        with state.runner.reserve_removal(project_id):
            with get_session() as db:
                org_deleted_at = db.exec(
                    select(Organization.deleted_at)
                    .where(Organization.id == found.org_id)
                    .with_for_update(read=True)
                ).first()
                if org_deleted_at is not None:
                    raise org_deleted_error()
                restored = db.exec(
                    update(Project)
                    .where(
                        col(Project.id) == project_id,
                        col(Project.deleted_at).is_not(None),
                    )
                    .values(deleted_at=None, deleted_by=None)
                ).rowcount
                if not restored:
                    raise not_deleted_error()
            state.contexts.invalidate(project_id)
    except ProjectBusy as exc:
        raise _admin_busy(exc) from exc
    audit(
        "project_restored",
        request,
        uid=principal.uid,
        org_id=found.org_id,
        org=found.org_slug,
        project=slug,
    )
    return {"restored": slug}


@auth_router.post("/api/admin/projects/{org}/{slug}/purge")
def post_purge_project(
    org: str,
    slug: str,
    request: Request,
    principal: Principal = Depends(instance_access()),
    body: ConfirmNameBody | None = Body(default=None),
) -> dict:
    """Delete a soft-deleted project permanently: today's hard removal (plan 59 D14).

    Allowed when the project is soft-deleted **or its org is** (D34);
    otherwise ``409 not_deleted``, read inside the runner's removal
    reservation (the claim a Restore takes too). ``409 confirm_name``
    unless ``confirm_name`` is the project's name; ``409 busy``. The
    removal is the org admin's pre-plan-59 one
    (:func:`~whygraph.portal.routes._remove_project`): row, clone, run
    files and - always - the history are deleted. A sync route, because
    the removal is.

    Returns
    -------
    dict
        The ``DeleteProjectResult`` shape, audited as ``project_purged``.
    """
    state = portal_state(request)
    body = body or ConfirmNameBody()
    found = _project_state(org, slug)
    project = found.project
    assert project.id is not None
    if project.deleted_at is None and not found.org_deleted:
        raise not_deleted_error()  # checked again under the reservation
    if body.confirm_name != project.name:
        raise ApiError(
            409,
            "type the project name to delete it permanently",
            code="confirm_name",
        )
    bound = _bound(state, project)
    try:
        with state.runner.reserve_removal(project.id):
            with get_session() as db:
                row = db.exec(
                    select(Project.deleted_at, Organization.deleted_at)
                    .join(Organization, col(Organization.id) == col(Project.org_id))
                    .where(Project.id == project.id)
                ).first()
            if row is None:
                raise ApiError(404, "project not found")
            if row[0] is None and row[1] is None:
                raise not_deleted_error()
            result = _remove_project(
                state,
                bound,
                DeleteProjectBody(confirm_name=body.confirm_name, delete_history=True),
            )
    except ProjectBusy as exc:
        raise _admin_busy(exc) from exc
    # Its budget went with the row (FK cascade).
    reload_org_budgets(state.budgets, found.org_id)
    audit(
        "project_purged",
        request,
        uid=principal.uid,
        org_id=found.org_id,
        org=found.org_slug,
        project=slug,
        checkout_deleted=result["checkout_deleted"],
        history_deleted=result["history_deleted"],
    )
    return result


@auth_router.post("/api/admin/me/logout")
def post_admin_logout(
    request: Request,
    response: Response,
    principal: Principal = Depends(instance_access()),
) -> dict:
    """End this admin session and clear the admin cookie.

    The everyday session of the same browser is left alone (plan 59 D29).
    Audited as ``admin_signout``.
    """
    base = _base(portal_state(request))
    assert principal.session_id is not None
    revoke(principal.session_id)
    audit("admin_signout", request, uid=principal.uid)
    clear_admin_cookie(response, base)
    return {"redirect": f"{base.origin}/admin/login"}


@auth_router.post("/api/admin/me/password")
def post_admin_password(
    body: PasswordBody,
    request: Request,
    response: Response,
    principal: Principal = Depends(instance_access()),
) -> dict:
    """Change a password admin's password; rotate this session, end every other.

    ``409 no_password`` for a GitHub account. A wrong ``current`` is ``403
    bad_credentials`` and counts as a sign-in failure (the same three
    throttles, ``429``). On success this admin session is replaced by a new
    one, set in the **admin** cookie; the user's other sessions end and
    their unused reset links stop working.
    """
    state = portal_state(request)
    base = _base(state)
    with get_session() as db:
        user = db.get(User, principal.user_id)
        if user is None:
            raise ApiError(404, "not found")
        email, stored = user.email or "", user.password_hash
    if stored is None:
        raise _no_password()
    rules = _login_throttles(state, email, ip_key(request.scope))
    _check_login_throttles(rules)
    ok, _ = verify_password(stored, body.current)
    if not ok:
        for rule, key in rules:
            rule.record(key)
        audit(
            "login_failure",
            request,
            uid=principal.uid,
            email=truncate_email(email),
            reason="password_change",
        )
        raise ApiError(403, "the current password is wrong", code="bad_credentials")
    validate_password(body.new, email)
    password_hash = hash_password(body.new)
    assert principal.session_id is not None  # an admin session principal
    with get_session() as db:
        user = db.get(User, principal.user_id)
        if user is None:
            raise ApiError(404, "not found")
        user.password_hash = password_hash
        user.password_changed_at = _iso(_now())
        db.add(user)
        revoke_user(principal.user_id, except_id=principal.session_id, db=db)
        token = rotate(
            db, principal.session_id, principal.user_id, _user_agent(request)
        )
        _invalidate_reset_links(db, principal.user_id)
    # Every session ended: the others, and this one (rotated).
    end_user_tokens(state, user_id=principal.user_id)
    audit("password_changed", request, uid=principal.uid)
    set_admin_cookie(response, token, base)
    return {"changed": True}


__all__ = [
    "OAUTH_COOKIE",
    "OAUTH_COOKIE_PATH",
    "RESET_LINK_LIFETIME",
    "auth_router",
]
