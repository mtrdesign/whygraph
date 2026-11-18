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

import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, literal_column, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, func, select

from whygraph.core.releases import format_version, running_version

from . import audit_store, connections, updates
from .audit import audit, truncate_email
from .authz import Action, Role
from .db import get_session
from .deps import (
    ApiError,
    PortalState,
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
from .member_routes import Redeemed, redeem_invitations
from .models import (
    Membership,
    Organization,
    PasswordReset,
    ReaderGrant,
    RetiredOrgSlug,
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
from .passwords import (
    hash_password,
    normalize_email,
    validate_password,
    verify_password,
)
from .security import Principal
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
    rotate,
    session_cookies,
    set_admin_cookie,
    set_cookie,
)
from .throttle import ip_key
from .usage_routes import account_usage

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


class AdminUserBody(_Strict):
    """``PATCH /api/admin/users/{uid}``: either field, or both (at least one)."""

    is_instance_admin: bool | None = None
    disabled: bool | None = None


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
    ``403 account_disabled``. The ``whygraph_oauth`` cookie is cleared on
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
    if signed_in is None:
        refused("disabled", user.login)
        raise ApiError(403, "this account is disabled", code="account_disabled")
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
    not an instance admin and a disabled one all get the same ``403
    not_an_admin``, audited as ``admin_signin_refused`` with the reason.
    Otherwise rules 1 and 3: the admin cookie's sessions end and a new admin
    session starts, paired with this browser's everyday session of the same
    user.
    """
    with get_session() as db:
        row = db.exec(
            select(User.id, User.uid, User.is_instance_admin, User.disabled_at).where(
                User.github_id == user.id
            )
        ).first()
        reason = None
        if row is None:
            reason = "unknown_user"
        elif row[3] is not None:
            reason = "disabled"
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
) -> tuple[str, str, bool, list[str], list[Redeemed], bool] | None:
    """Upsert the account by GitHub id and start its session, in one transaction.

    Any other row holding the incoming login (case-insensitively) has it
    released first (plan section 0.2 #7). ``INSERT ... ON CONFLICT
    (github_id) DO UPDATE`` refreshes ``github_login`` and ``avatar_url``
    only - never the user-editable ``display_name`` - so two concurrent
    first sign-ins converge on one row; a race that still trips a unique
    index (a login claimed by two ids at once) retries the transaction.
    After the disabled check, the open invitations for the GitHub id are
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
    tuple or None
        ``(session token, uid, new_user, released uids, redeemed,
        clear_admin_cookie)``, or ``None`` when the account is disabled
        (then nothing is written).
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
                        literal_column("xmax = 0"),  # inserted, not updated
                    )
                ).one()
                user_id, uid, disabled_at, inserted = row
                if disabled_at is not None:
                    db.rollback()  # the refresh and any release are undone
                    return None
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
    then ``403 not_an_admin``, so the route tells nothing to a guesser.
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
    user_id, uid, _, disabled_at, is_admin = row
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
    account that is not an instance admin (plan 59 D28: nothing changes and
    the link stays unused); the password rules (``422``). Marks the link
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
        if not user.is_instance_admin:
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
        if not user.is_instance_admin:
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
    """The orgs the user is a member of (an admin's ``reader`` access excluded)."""
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
    links each org to ``url + "/usage/me"``.
    """
    state = portal_state(request)
    base = _base(state)
    with get_session() as db:
        rows = db.exec(
            select(Organization.id, Organization.slug, Organization.name)
            .join(Membership, col(Membership.org_id) == col(Organization.id))
            .where(Membership.user_id == principal.user_id)
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
    it (one answer, so a deletion is not disclosed).
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


@auth_router.get("/api/admin/orgs")
def get_admin_orgs(
    request: Request, _: Principal = Depends(instance_access())
) -> list[dict]:
    """Every org, with its member count."""
    base = _base(portal_state(request))
    with get_session() as db:
        rows = db.exec(
            select(
                Organization.slug,
                Organization.name,
                Organization.created_at,
                func.count(col(Membership.user_id)),
            )
            .outerjoin(Membership, col(Membership.org_id) == col(Organization.id))
            .group_by(col(Organization.id))
            .order_by(Organization.slug)
        ).all()
    return [
        {
            "slug": slug,
            "name": name,
            "url": base.org_origin(slug),
            "member_count": count,
            "created_at": created_at,
        }
        for slug, name, created_at, count in rows
    ]


@auth_router.get("/api/admin/audit")
def get_admin_audit(
    _: Principal = Depends(instance_access()),
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
    """Security events of no org: sign-ins, instance admin, deleted orgs' events.

    The filters of ``GET /api/org/audit``, plus ``org`` (a deleted org's
    slug, as its events kept it); ``422 bad_date`` for a malformed
    ``from`` / ``to``.

    Returns
    -------
    dict
        ``{"events": [...], "next": <id or null>}``, newest first.
    """
    try:
        f = audit_store.make_filter(
            orgless=True, org=org, event=event, actor=actor, since=since, until=until
        )
    except ValueError as exc:
        raise ApiError(
            422, "from and to must be ISO-8601 dates or date-times", code="bad_date"
        ) from exc
    events, next_before = audit_store.query_events(f, before=before, limit=limit)
    return {"events": events, "next": next_before}


@auth_router.get("/api/admin/users")
def get_admin_users(_: Principal = Depends(instance_access())) -> list[dict]:
    """Every user, with how many orgs they belong to."""
    with get_session() as db:
        rows = db.exec(
            select(User, func.count(col(Membership.org_id)))
            .outerjoin(Membership, col(Membership.user_id) == col(User.id))
            .group_by(col(User.id))
            .order_by(User.created_at, User.id)
        ).all()
        return [
            {
                "uid": user.uid,
                "email": user.email,
                "display_name": user.display_name,
                "is_instance_admin": user.is_instance_admin,
                "github_login": user.github_login,
                "has_password": user.password_hash is not None,
                "disabled": user.disabled_at is not None,
                "created_at": user.created_at,
                "org_count": count,
            }
            for user, count in rows
        ]


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
    **enabled** admin. The enabled admins' rows are locked (``SELECT ...
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
            )
            .with_for_update()
        ).all()
        user = _user_by_uid(db, uid, lock=True)
        assert user.id is not None
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
