"""Production identity routes: bootstrap, sign-in, account, orgs and admin.

Every route here is **production-only** - local mode answers ``404`` - and
none names an organization, so none is org-scoped (M2c plan section 4.7):

* Public routes (bootstrap, GitHub sign-in, login, logout, reset) declare
  no action. Their gate (:func:`~whygraph.portal.deps.require_mode_and_host`)
  runs as a route dependency, so a wrong mode or host is a ``404`` before
  the body is even validated. The credential routes are served on the base
  host only; logout on any host. End users sign in with GitHub (M2d-1 plan
  section 4.4); email + password is kept for password accounts only - in
  practice the bootstrap instance admin - and no route creates another.
* ``user.self`` routes (:func:`~whygraph.portal.deps.user_access`): the
  caller's own account, and org creation (base host).
* ``instance.admin`` routes (:func:`~whygraph.portal.deps.instance_access`):
  the admin page's data (base host).

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

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import literal_column, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, func, select

from .audit import audit, truncate_email
from .authz import Role
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
)
from .hosts import BaseUrl, safe_redirect
from .models import Membership, Organization, PasswordReset, User
from .orgs import OrgSlugTaken, add_member, create_org, validate_org_slug
from .passwords import (
    hash_password,
    normalize_email,
    validate_password,
    verify_password,
)
from .security import Principal
from .sessions import (
    clear_cookie,
    create_session,
    hash_token,
    revoke,
    revoke_user,
    rotate,
    set_cookie,
)
from .throttle import ip_key

RESET_LINK_LIFETIME = timedelta(hours=24)
"""How long an admin-issued reset link works."""

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
    """``POST /api/auth/login``."""

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
    """``POST /api/account/password``."""

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
    submit makes one admin.
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
            token = create_session(db, user.id, _user_agent(request))
            uid = user.uid
        state.bootstrap_secret = None
    audit("bootstrap_claimed", request, uid=uid)
    set_cookie(response, token, base)
    return {"redirect": f"{base.origin}/orgs/new"}


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
    state = portal_state(request)
    base = _base(state)
    _require_claimed(state)
    _throttled(state.github_ip.hit(ip_key(request.scope)))
    oauth_state, url = _github(state).begin(
        safe_redirect(body.next, base), _redirect_uri(base)
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
    ``<base>/orgs``.
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

    def refused(reason: str, login: str | None = None) -> None:
        audit("github_signin_refused", request, reason=reason, github_login=login)

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

    try:
        user = github.identify(
            body.code,
            pending.verifier,
            _redirect_uri(base),
            on_revoke_failure=lambda: audit("github_token_revoke_failed", request),
        )
    except GitHubAuthFailed as exc:
        refused(exc.reason)
        raise ApiError(
            400,
            "GitHub did not confirm this sign-in: try again",
            code="github_auth_failed",
        ) from None
    except GitHubUnavailable:
        refused("unavailable")
        raise ApiError(
            502,
            "GitHub could not be reached: try again in a moment",
            code="github_unavailable",
        ) from None

    if not user.two_factor:
        refused("2fa_required", user.login)
        raise ApiError(
            403,
            "turn on two-factor authentication for your GitHub account, then "
            "sign in again",
            code="github_2fa_required",
            fix_url=f"{github.config.web_url}/settings/security",
        )
    principal: Principal | None = request.scope.get("state", {}).get("principal")
    signed_in = _sign_in_github_user(
        user,
        replaces=None if principal is None else principal.session_id,
        user_agent=_user_agent(request),
    )
    if signed_in is None:
        refused("disabled", user.login)
        raise ApiError(403, "this account is disabled", code="account_disabled")
    token, uid, new_user, released = signed_in
    if principal is not None and principal.session_id is not None:
        end_user_tokens(state, session_id=principal.session_id)  # it was revoked
    for target in released:
        audit("github_login_released", request, target=target, github_login=user.login)
    audit("github_signin", request, uid=uid, github_login=user.login, new_user=new_user)
    set_cookie(response, token, base)
    return {"redirect": safe_redirect(pending.next, base) or f"{base.origin}/orgs"}


def _sign_in_github_user(
    user: GitHubUser, *, replaces: int | None, user_agent: str | None
) -> tuple[str, str, bool, list[str]] | None:
    """Upsert the account by GitHub id and start its session, in one transaction.

    Any other row holding the incoming login (case-insensitively) has it
    released first (plan section 0.2 #7). ``INSERT ... ON CONFLICT
    (github_id) DO UPDATE`` refreshes ``github_login`` and ``avatar_url``
    only - never the user-editable ``display_name`` - so two concurrent
    first sign-ins converge on one row; a race that still trips a unique
    index (a login claimed by two ids at once) retries the transaction.

    Returns
    -------
    tuple or None
        ``(session token, uid, new_user, released uids)``, or ``None`` when
        the account is disabled (then nothing is written).
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
                if replaces is not None:
                    revoke(replaces, db=db)  # plan section 0.2 #20
                token = create_session(db, user_id, user_agent)
            return token, uid, bool(inserted), released
        except IntegrityError:
            if attempt == _UPSERT_ATTEMPTS - 1:
                raise
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Login, logout, reset (plan sections 4.7, 4.8)
# ---------------------------------------------------------------------------


@auth_router.post("/api/auth/login", dependencies=_PUBLIC_BASE)
def post_login(body: LoginBody, request: Request, response: Response) -> dict:
    """Sign in with email and password.

    ``409 bootstrap_required`` before the bootstrap. A wrong password and
    an unknown email answer the same ``401 bad_credentials`` in the same
    time (a dummy verify). Failures count per ``(email, ip_key)``, per
    email and per ``ip_key``; all three are checked first (``429``). A
    disabled account is ``403 account_disabled`` - only after a correct
    password, so the route tells nothing to a guesser. An outdated hash is
    upgraded. ``redirect`` is ``next`` when it is a safe
    portal URL, else ``<base>/orgs``.
    """
    state = portal_state(request)
    base = _base(state)
    _require_claimed(state)
    # Lookup form only: a malformed email is just an unknown account.
    email = body.email.strip().lower()
    rules = _login_throttles(state, email, ip_key(request.scope))
    _check_login_throttles(rules)
    with get_session() as db:
        row = db.exec(
            select(User.id, User.uid, User.password_hash, User.disabled_at).where(
                User.email == email
            )
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
        )
        raise ApiError(401, "wrong email or password", code="bad_credentials")
    user_id, uid, _, disabled_at = row
    if disabled_at is not None:
        audit(
            "login_failure",
            request,
            uid=uid,
            email=truncate_email(email),
            reason="disabled",
        )
        raise _account_disabled()
    new_hash = hash_password(body.password) if needs_rehash else None
    with get_session() as db:
        if new_hash is not None:
            user = db.get(User, user_id)
            if user is not None:
                user.password_hash = new_hash
                db.add(user)
        token = create_session(db, user_id, _user_agent(request))
    audit("login_success", request, uid=uid, email=truncate_email(email))
    set_cookie(response, token, base)
    return {"redirect": safe_redirect(body.next, base) or f"{base.origin}/orgs"}


@auth_router.post("/api/auth/logout", dependencies=_PUBLIC_ANY)
def post_logout(request: Request, response: Response) -> dict:
    """End this session (a no-op without one) and clear the cookie, on any host."""
    state = portal_state(request)
    base = _base(state)
    principal: Principal | None = request.scope.get("state", {}).get("principal")
    if principal is not None and principal.session_id is not None:
        revoke(principal.session_id)
        end_user_tokens(state, session_id=principal.session_id)
        audit("logout", request, uid=principal.uid)
    clear_cookie(response, base)
    return {"redirect": f"{base.origin}/signin"}


@auth_router.post("/api/auth/reset", dependencies=_PUBLIC_BASE)
def post_reset(body: ResetBody, request: Request, response: Response) -> dict:
    """Set a new password with an admin-issued reset link, and sign in.

    ``429`` past 10 attempts per ``ip_key`` in 15 minutes; ``400 bad_token``
    for an unknown, used, superseded or expired link; ``403
    account_disabled`` for a disabled account; the password rules
    (``422``). Marks the link used, ends **every** session of the user,
    then starts a new one.
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
        assert user.id is not None
        user.password_hash = password_hash
        user.password_changed_at = now
        db.add(user)
        _invalidate_reset_links(db, user.id)
        revoke_user(user.id, db=db)
        token = create_session(db, user.id, _user_agent(request))
        uid = user.uid
        user_id = user.id
    end_user_tokens(state, user_id=user_id)  # every earlier session ended
    audit("reset_link_used", request, uid=uid)
    set_cookie(response, token, base)
    return {"redirect": f"{base.origin}/orgs"}


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


@auth_router.post("/api/account/password")
def post_account_password(
    body: PasswordBody,
    request: Request,
    response: Response,
    principal: Principal = Depends(user_access()),
) -> dict:
    """Change the password; rotate this session, end every other one.

    ``409 no_password`` for a GitHub account. A wrong ``current`` is ``403
    bad_credentials`` and counts as a sign-in failure (the same three
    throttles, ``429``). On success the current
    session is replaced by a new one (new cookie), the user's other
    sessions end and their unused reset links stop working.
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
    with get_session() as db:
        user = db.get(User, principal.user_id)
        if user is None:
            raise ApiError(404, "not found")
        user.password_hash = password_hash
        user.password_changed_at = _iso(_now())
        db.add(user)
        revoke_user(principal.user_id, except_id=principal.session_id, db=db)
        if principal.session_id is not None:
            token = rotate(
                db, principal.session_id, principal.user_id, _user_agent(request)
            )
        else:
            token = create_session(db, principal.user_id, _user_agent(request))
        _invalidate_reset_links(db, principal.user_id)
    # Every session ended: the others, and this one (rotated).
    end_user_tokens(state, user_id=principal.user_id)
    audit("password_changed", request, uid=principal.uid)
    set_cookie(response, token, base)
    return {"changed": True}


@auth_router.get("/api/account/orgs")
def get_account_orgs(
    request: Request, principal: Principal = Depends(user_access())
) -> list[dict]:
    """The orgs the user is a member of (an admin's ``reader`` access excluded)."""
    base = _base(portal_state(request))
    with get_session() as db:
        rows = db.exec(
            select(Organization.slug, Organization.name, Membership.role)
            .join(Membership, col(Membership.org_id) == col(Organization.id))
            .where(Membership.user_id == principal.user_id)
            .order_by(Organization.name, Organization.slug)
        ).all()
    return [
        {"slug": slug, "name": name, "role": role, "url": base.org_origin(slug)}
        for slug, name, role in rows
    ]


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
        add_member(db, org_id=org.id, user_id=principal.user_id, role=Role.OWNER)
    audit("org_created", request, uid=principal.uid, org=slug)
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
    count. Disabling ends every session of the user at once and kills
    their unused reset links; memberships and the GitHub id stay, so
    enabling restores everything.
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
            _invalidate_reset_links(db, user.id)
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
    return result


@auth_router.post("/api/admin/users/{uid}/reset-link")
def post_admin_reset_link(
    uid: str,
    request: Request,
    principal: Principal = Depends(instance_access()),
) -> dict:
    """Issue a one-time, 24-hour reset link for a password account.

    ``409 no_password`` for a GitHub account, ``409 user_disabled`` for a
    disabled one. The user's earlier unused
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


__all__ = [
    "OAUTH_COOKIE",
    "OAUTH_COOKIE_PATH",
    "RESET_LINK_LIFETIME",
    "auth_router",
]
