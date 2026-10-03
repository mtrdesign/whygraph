"""Production identity routes: bootstrap, sign-in, account, orgs and admin.

Every route here is **production-only** - local mode answers ``404`` - and
none names an organization, so none is org-scoped (M2c plan section 4.7):

* Public routes (bootstrap, register, login, logout, reset) declare no
  action. Their gate (:func:`~whygraph.portal.deps.require_mode_and_host`)
  runs as a route dependency, so a wrong mode or host is a ``404`` before
  the body is even validated. The credential routes are served on the base
  host only; logout on any host.
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
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update
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
from .hosts import BaseUrl, safe_redirect
from .models import Membership, Organization, PasswordReset, User
from .orgs import add_member, create_org, validate_org_slug
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


class RegisterBody(_Strict):
    """``POST /api/auth/register``."""

    email: str = Field(max_length=1024)
    display_name: str = Field(min_length=1, max_length=100)
    password: str = _PASSWORD


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
    """``PATCH /api/admin/users/{uid}``."""

    is_instance_admin: bool


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
    bad_secret``, the register validation (``422``), ``409 email_taken``.
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
# Register, login, logout, reset (plan sections 4.7, 4.8)
# ---------------------------------------------------------------------------


@auth_router.post("/api/auth/register", dependencies=_PUBLIC_BASE)
def post_register(body: RegisterBody, request: Request, response: Response) -> dict:
    """Create an account (registration is always open) and sign it in.

    ``409 bootstrap_required`` before the bootstrap, ``429`` past 5
    attempts per ``ip_key`` an hour, ``422`` ``bad_email`` /
    ``weak_password`` / ``common_password``, ``409 email_taken``.
    """
    state = portal_state(request)
    base = _base(state)
    _require_claimed(state)
    _throttled(state.register_ip.hit(ip_key(request.scope)))
    email = normalize_email(body.email)
    name = _display_name(body.display_name)
    validate_password(body.password, email)
    password_hash = hash_password(body.password)
    with get_session() as db:
        if db.exec(select(User.id).where(User.email == email)).first() is not None:
            raise _email_taken()
        user = User(display_name=name, email=email, password_hash=password_hash)
        db.add(user)
        try:
            db.flush()
        except IntegrityError as exc:  # a concurrent register of the same email
            raise _email_taken() from exc
        assert user.id is not None
        token = create_session(db, user.id, _user_agent(request))
        uid = user.uid
    audit("register", request, uid=uid)
    set_cookie(response, token, base)
    return {"redirect": f"{base.origin}/orgs"}


@auth_router.post("/api/auth/login", dependencies=_PUBLIC_BASE)
def post_login(body: LoginBody, request: Request, response: Response) -> dict:
    """Sign in with email and password.

    ``409 bootstrap_required`` before the bootstrap. A wrong password and
    an unknown email answer the same ``401 bad_credentials`` in the same
    time (a dummy verify). Failures count per ``(email, ip_key)``, per
    email and per ``ip_key``; all three are checked first (``429``). An
    outdated hash is upgraded. ``redirect`` is ``next`` when it is a safe
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
            select(User.id, User.uid, User.password_hash).where(User.email == email)
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
    user_id, uid, _ = row
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
        audit("logout", request, uid=principal.uid)
    clear_cookie(response, base)
    return {"redirect": f"{base.origin}/signin"}


@auth_router.post("/api/auth/reset", dependencies=_PUBLIC_BASE)
def post_reset(body: ResetBody, request: Request, response: Response) -> dict:
    """Set a new password with an admin-issued reset link, and sign in.

    ``429`` past 10 attempts per ``ip_key`` in 15 minutes; ``400 bad_token``
    for an unknown, used, superseded or expired link; the password rules
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
        email = user.email or ""
    validate_password(body.password, email)
    password_hash = hash_password(body.password)
    now = _iso(_now())
    with get_session() as db:
        link = live_link(db, lock=True)  # re-checked: a concurrent use loses
        user = db.get(User, link.user_id)
        if user is None:
            raise bad_token
        assert user.id is not None
        user.password_hash = password_hash
        user.password_changed_at = now
        db.add(user)
        _invalidate_reset_links(db, user.id)
        revoke_user(user.id, db=db)
        token = create_session(db, user.id, _user_agent(request))
        uid = user.uid
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
    }


@auth_router.get("/api/account")
def get_account(principal: Principal = Depends(user_access())) -> dict:
    """The signed-in user."""
    return {
        "uid": principal.uid,
        "email": principal.email,
        "display_name": principal.display_name,
        "is_instance_admin": principal.is_instance_admin,
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

    A wrong ``current`` is ``403 bad_credentials`` and counts as a sign-in
    failure (the same three throttles, ``429``). On success the current
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
    style slug, ``409 slug_taken`` when it exists.
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
    """Make a user an instance admin, or remove that.

    ``409 last_admin`` when it would leave no admin. The admin rows are
    locked (``SELECT ... FOR UPDATE``) first, so two concurrent demotions
    cannot both pass the count.
    """
    with get_session() as db:
        admins = db.exec(
            select(User.id)
            .where(col(User.is_instance_admin).is_(True))
            .with_for_update()
        ).all()
        user = _user_by_uid(db, uid, lock=True)
        changed = user.is_instance_admin != body.is_instance_admin
        if changed and not body.is_instance_admin and len(admins) <= 1:
            raise ApiError(
                409,
                "this is the last instance admin: make someone else an admin first",
                code="last_admin",
            )
        user.is_instance_admin = body.is_instance_admin
        db.add(user)
        db.flush()
        target = user.uid
        result = {
            "uid": user.uid,
            "email": user.email,
            "display_name": user.display_name,
            "is_instance_admin": user.is_instance_admin,
        }
    if changed:
        event = "admin_granted" if body.is_instance_admin else "admin_revoked"
        audit(event, request, uid=principal.uid, target=target)
    return result


@auth_router.post("/api/admin/users/{uid}/reset-link")
def post_admin_reset_link(
    uid: str,
    request: Request,
    principal: Principal = Depends(instance_access()),
) -> dict:
    """Issue a one-time, 24-hour reset link for a user.

    The user's earlier unused links stop working. The token travels in the
    URL **fragment** (``<base>/reset#token=...``), so it never reaches a
    proxy log or a ``Referer``; only its SHA-256 is stored.
    """
    base = _base(portal_state(request))
    raw = secrets.token_urlsafe(32)
    now = _now()
    with get_session() as db:
        user = _user_by_uid(db, uid)
        assert user.id is not None
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


__all__ = ["RESET_LINK_LIFETIME", "auth_router"]
