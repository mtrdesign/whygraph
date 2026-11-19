"""Server-side browser sessions for production mode.

The cookie carries 32 random bytes (:func:`secrets.token_urlsafe`); the
``sessions`` table stores only their SHA-256, so a database read alone can
never be replayed as a cookie. A session ends 30 days after sign-in
(:data:`ABSOLUTE`) or after 7 days without a request (:data:`IDLE`);
``last_seen_at`` is written at most every 5 minutes (:data:`TOUCH_EVERY`),
so a sliding session costs no write per request.

There are two kinds of session (``sessions.kind``, plan 59 D2). The
everyday ``"user"`` session above rides on :data:`COOKIE_NAME`, set for the
base host and every org host. The ``"admin"`` session opens only the admin
panel: it lives 8 hours (:data:`ADMIN_ABSOLUTE`) with **no** idle limit
(D50), rides on its own host-only cookie (:func:`admin_cookie_name`), and
may record the everyday session of the same browser it was started beside
(``paired_session_id``, D51), so ending that one ends it
(:func:`end_paired_admin`).

Every function here is sync (it runs in a worker thread). Timestamps are
ISO-8601 UTC strings in the one format :mod:`whygraph.portal.models` writes,
so they compare correctly as strings.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, or_, update
from sqlmodel import Session, col, select
from starlette.responses import Response

from .db import get_session
from .hosts import BaseUrl
from .models import SESSION_KINDS, User, UserSession

COOKIE_NAME = "whygraph_session"
"""The session cookie's name."""

ADMIN_COOKIE_NAME = "whygraph_admin"
"""The admin cookie's name on an ``http`` base URL (dev only); on ``https``
it carries the ``__Host-`` prefix (:func:`admin_cookie_name`)."""

ABSOLUTE = timedelta(days=30)
"""A session's absolute lifetime (also the cookie's ``Max-Age``)."""

IDLE = timedelta(days=7)
"""A session unused for this long is refused."""

ADMIN_ABSOLUTE = timedelta(hours=8)
"""An admin session's absolute lifetime (also the admin cookie's
``Max-Age``). An admin session has no idle limit (plan 59 D50)."""

TOUCH_EVERY = timedelta(minutes=5)
"""``last_seen_at`` is written at most this often (both kinds; for an admin
row it is only what a session list shows)."""

USER_AGENT_MAX = 200
"""How much of the ``User-Agent`` header a session row keeps."""


@dataclass(frozen=True)
class SessionRow:
    """A live session joined with its user.

    Attributes
    ----------
    session_id : int
        ``sessions.id``.
    last_seen_at : str
        When the session was last touched (ISO-8601 UTC).
    user_id : int
        ``users.id``.
    uid : str
        The user's stable uid.
    email : str or None
        The user's login name.
    display_name : str
        Shown in the UI.
    is_instance_admin : bool
        Whether the user is an instance admin.
    github_login : str or None
        The GitHub username of a GitHub account (M2d-1).
    avatar_url : str or None
        The GitHub avatar of a GitHub account.
    has_password : bool
        Whether the account signs in with a password.
    kind : str
        ``"user"`` or ``"admin"`` (:data:`~whygraph.portal.models.SESSION_KINDS`).
    created_at : str
        When the session started (ISO-8601 UTC).
    expires_at : str
        Its absolute expiry (ISO-8601 UTC).
    """

    session_id: int
    last_seen_at: str
    user_id: int
    uid: str
    email: str | None
    display_name: str
    is_instance_admin: bool
    github_login: str | None = None
    avatar_url: str | None = None
    has_password: bool = False
    kind: str = "user"
    created_at: str = ""
    expires_at: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def hash_token(token: str) -> str:
    """Return the SHA-256 hex digest stored for a raw session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _limits(kind: str) -> tuple[timedelta, timedelta | None]:
    """``(absolute, idle)`` of a session kind; ``idle`` is ``None`` for admin."""
    if kind == "user":
        return ABSOLUTE, IDLE
    if kind == "admin":
        return ADMIN_ABSOLUTE, None
    raise ValueError(f"unknown session kind {kind!r} (expected one of {SESSION_KINDS})")


def create_session(
    db: Session,
    user_id: int,
    user_agent: str | None,
    kind: str = "user",
    paired_session_id: int | None = None,
) -> str:
    """Start a session for ``user_id`` and return its raw token.

    Expired rows of both kinds and idle rows of the user kind (anyone's) are
    deleted first, so the table never grows without bound. An admin row is
    never swept for being idle (plan 59 D50).

    Parameters
    ----------
    db : Session
        An open portal DB session; the caller commits.
    user_id : int
        ``users.id`` of the signed-in user.
    user_agent : str or None
        The request's ``User-Agent`` (only its first 200 characters are kept).
    kind : str, optional
        ``"user"`` (the default, 30 days) or ``"admin"`` (8 hours).
    paired_session_id : int, optional
        For an admin session, the everyday session of the same user it is
        started beside (D51); ``None`` for an unpaired one.

    Returns
    -------
    str
        The token - it belongs only in the ``Set-Cookie`` header
        (:func:`set_cookie` or :func:`set_admin_cookie`), never in a log or a
        response body.

    Raises
    ------
    ValueError
        For an unknown ``kind``.
    """
    absolute, _ = _limits(kind)
    now = _now()
    db.exec(
        delete(UserSession).where(
            or_(
                col(UserSession.expires_at) < _iso(now),
                and_(
                    col(UserSession.kind) == "user",
                    col(UserSession.last_seen_at) < _iso(now - IDLE),
                ),
            )
        )
    )
    token = secrets.token_urlsafe(32)
    db.add(
        UserSession(
            token_hash=hash_token(token),
            user_id=user_id,
            created_at=_iso(now),
            last_seen_at=_iso(now),
            expires_at=_iso(now + absolute),
            user_agent=(user_agent or "")[:USER_AGENT_MAX] or None,
            kind=kind,
            paired_session_id=paired_session_id,
        )
    )
    db.flush()
    return token


def rotate(db: Session, session_id: int, user_id: int, user_agent: str | None) -> str:
    """Replace a session with a fresh one (after a credential change).

    The new session has the old one's kind and is unpaired.

    Parameters
    ----------
    db : Session
        An open portal DB session; the caller commits.
    session_id : int
        The session to end.
    user_id : int
        Its user, who gets the new session.
    user_agent : str or None
        The request's ``User-Agent``.

    Returns
    -------
    str
        The new raw token; the old one is refused from now on.
    """
    kind = db.exec(
        select(UserSession.kind).where(col(UserSession.id) == session_id)
    ).first()
    db.exec(delete(UserSession).where(col(UserSession.id) == session_id))
    return create_session(db, user_id, user_agent, kind=kind or "user")


def lookup(token: str, kind: str = "user") -> SessionRow | None:
    """Return the live session of ``kind`` a raw token names, with its user.

    Parameters
    ----------
    token : str
        The cookie value.
    kind : str, optional
        ``"user"`` (the default; the everyday cookie) or ``"admin"`` (the
        admin cookie). A token of the other kind is unknown here.

    Returns
    -------
    SessionRow or None
        ``None`` for an unknown token, a session of another kind, one past
        its absolute lifetime (or, for the user kind, its idle lifetime), or
        one whose user is disabled (so a disabled user's surviving session
        simply resolves to signed out). A user-kind session of a password
        account is refused (plan 59 D4), and an admin-kind session of a
        user who is no longer an instance admin is refused, so a demotion
        ends it on the next request.

    Raises
    ------
    ValueError
        For an unknown ``kind``.
    """
    _, idle = _limits(kind)
    if not token:
        return None
    now = _now()
    conditions = [
        col(UserSession.token_hash) == hash_token(token),
        col(UserSession.kind) == kind,
        col(UserSession.expires_at) > _iso(now),
        col(User.disabled_at).is_(None),
    ]
    if idle is not None:
        conditions.append(col(UserSession.last_seen_at) > _iso(now - idle))
    if kind == "user":
        conditions.append(col(User.password_hash).is_(None))
    else:
        conditions.append(col(User.is_instance_admin).is_(True))
    with get_session() as db:
        row = db.exec(
            select(
                UserSession.id,
                UserSession.last_seen_at,
                User.id,
                User.uid,
                User.email,
                User.display_name,
                User.is_instance_admin,
                User.github_login,
                User.avatar_url,
                col(User.password_hash).is_not(None),
                UserSession.kind,
                UserSession.created_at,
                UserSession.expires_at,
            )
            .join(User, col(User.id) == col(UserSession.user_id))
            .where(*conditions)
        ).first()
    if row is None:
        return None
    return SessionRow(*row)


def is_stale(row: SessionRow) -> bool:
    """Whether ``row`` was last touched more than :data:`TOUCH_EVERY` ago."""
    return row.last_seen_at < _iso(_now() - TOUCH_EVERY)


def touch(session_id: int) -> bool:
    """Record that a session was used, at most once per :data:`TOUCH_EVERY`.

    Parameters
    ----------
    session_id : int
        ``sessions.id``.

    Returns
    -------
    bool
        Whether a row was written.
    """
    now = _now()
    with get_session() as db:
        result = db.exec(
            update(UserSession)
            .where(
                col(UserSession.id) == session_id,
                col(UserSession.last_seen_at) < _iso(now - TOUCH_EVERY),
            )
            .values(last_seen_at=_iso(now))
        )
        return bool(result.rowcount)


def revoke(session_id: int, *, db: Session | None = None) -> None:
    """End one session (sign-out).

    Parameters
    ----------
    session_id : int
        ``sessions.id``.
    db : Session, optional
        Run in this open session (the caller commits); a new one by default.
    """
    statement = delete(UserSession).where(col(UserSession.id) == session_id)
    if db is not None:
        db.exec(statement)
        return
    with get_session() as own:
        own.exec(statement)


def revoke_user(
    user_id: int, except_id: int | None = None, *, db: Session | None = None
) -> None:
    """End every session of a user, of both kinds, optionally keeping one.

    Parameters
    ----------
    user_id : int
        ``users.id``.
    except_id : int, optional
        A session to keep (the one that just changed the password).
    db : Session, optional
        Run in this open session (the caller commits); a new one by default.
    """
    statement = delete(UserSession).where(col(UserSession.user_id) == user_id)
    if except_id is not None:
        statement = statement.where(col(UserSession.id) != except_id)
    if db is not None:
        db.exec(statement)
        return
    with get_session() as own:
        own.exec(statement)


def revoke_user_kind(user_id: int, kind: str, *, db: Session | None = None) -> None:
    """End every session of one kind of a user (an admin demotion ends ``"admin"``).

    Parameters
    ----------
    user_id : int
        ``users.id``.
    kind : str
        ``"user"`` or ``"admin"``.
    db : Session, optional
        Run in this open session (the caller commits); a new one by default.

    Raises
    ------
    ValueError
        For an unknown ``kind``.
    """
    _limits(kind)
    statement = delete(UserSession).where(
        col(UserSession.user_id) == user_id, col(UserSession.kind) == kind
    )
    if db is not None:
        db.exec(statement)
        return
    with get_session() as own:
        own.exec(statement)


def end_paired_admin(db: Session, session_id: int) -> int:
    """End the admin sessions paired with an everyday session (plan 59 D51).

    Call it **before** the everyday row is deleted: ``fk_sessions_paired`` is
    ``ON DELETE SET NULL`` and unpairs them the moment it goes.

    Parameters
    ----------
    db : Session
        An open portal DB session; the caller commits.
    session_id : int
        ``sessions.id`` of the everyday session.

    Returns
    -------
    int
        How many admin rows were deleted.
    """
    result = db.exec(
        delete(UserSession).where(
            col(UserSession.kind) == "admin",
            col(UserSession.paired_session_id) == session_id,
        )
    )
    return int(result.rowcount or 0)


def repair_admin(db: Session, old_session_id: int, new_session_id: int) -> None:
    """Move admin pairings from an everyday session to its replacement (D51).

    For an everyday session replaced by a new one of the **same** user (a
    re-sign-in); the caller checks the user. Call it before the old row is
    deleted, for the reason :func:`end_paired_admin` gives.

    Parameters
    ----------
    db : Session
        An open portal DB session; the caller commits.
    old_session_id : int
        ``sessions.id`` of the everyday session being replaced.
    new_session_id : int
        ``sessions.id`` of its replacement.
    """
    db.exec(
        update(UserSession)
        .where(
            col(UserSession.kind) == "admin",
            col(UserSession.paired_session_id) == old_session_id,
        )
        .values(paired_session_id=new_session_id)
    )


def set_cookie(response: Response, token: str, base: BaseUrl) -> None:
    """Set the session cookie on the base host (and so on every org host).

    ``Domain=<base host>`` (no port), ``Path=/``, ``HttpOnly``,
    ``SameSite=Lax``, ``Secure`` for an ``https`` base URL, ``Max-Age`` the
    absolute lifetime.

    Parameters
    ----------
    response : Response
        The response to add the ``Set-Cookie`` header to.
    token : str
        The raw token from :func:`create_session`.
    base : BaseUrl
        The portal's base URL.
    """
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(ABSOLUTE.total_seconds()),
        path="/",
        domain=base.host,
        secure=base.scheme == "https",
        httponly=True,
        samesite="lax",
    )


def clear_cookie(response: Response, base: BaseUrl) -> None:
    """Clear the session cookie, for ``Domain=<base host>`` and host-only.

    Parameters
    ----------
    response : Response
        The response to add the two ``Set-Cookie`` headers to.
    base : BaseUrl
        The portal's base URL.
    """
    for domain in (base.host, None):
        response.delete_cookie(
            COOKIE_NAME,
            path="/",
            domain=domain,
            secure=base.scheme == "https",
            httponly=True,
            samesite="lax",
        )


def clearing_headers(base: BaseUrl) -> list[tuple[bytes, bytes]]:
    """The raw ``Set-Cookie`` headers of :func:`clear_cookie`, for ASGI code."""
    response = Response()
    clear_cookie(response, base)
    return [(k, v) for k, v in response.raw_headers if k == b"set-cookie"]


def admin_cookie_name(base: BaseUrl) -> str:
    """The admin cookie's name: ``__Host-whygraph_admin`` on ``https``.

    The ``__Host-`` prefix makes a browser refuse the cookie unless it is
    ``Secure``, ``Path=/`` and host-only, so an org host can neither set nor
    shadow it. On ``http`` (dev only) a browser refuses the prefix, so the
    plain :data:`ADMIN_COOKIE_NAME` is used.

    Parameters
    ----------
    base : BaseUrl
        The portal's base URL.

    Returns
    -------
    str
        The cookie name.
    """
    if base.scheme == "https":
        return f"__Host-{ADMIN_COOKIE_NAME}"
    return ADMIN_COOKIE_NAME


def set_admin_cookie(response: Response, token: str, base: BaseUrl) -> None:
    """Set the admin cookie, host-only on the base host.

    **No** ``Domain`` (so it is never sent to an org host), ``Path=/``,
    ``HttpOnly``, ``SameSite=Strict``, ``Secure`` for an ``https`` base URL,
    ``Max-Age`` the admin lifetime (:data:`ADMIN_ABSOLUTE`).

    Parameters
    ----------
    response : Response
        The response to add the ``Set-Cookie`` header to.
    token : str
        The raw token from :func:`create_session` with ``kind="admin"``.
    base : BaseUrl
        The portal's base URL.
    """
    response.set_cookie(
        admin_cookie_name(base),
        token,
        max_age=int(ADMIN_ABSOLUTE.total_seconds()),
        path="/",
        secure=base.scheme == "https",
        httponly=True,
        samesite="strict",
    )


def clear_admin_cookie(response: Response, base: BaseUrl) -> None:
    """Clear the admin cookie.

    On ``https`` this is **one** host-only, ``Secure`` header: a browser
    rejects a ``__Host-`` cookie that carries a ``Domain``, so the two-form
    loop of :func:`clear_cookie` must not be copied here. On ``http`` both
    forms (host-only and ``Domain=<base host>``) are sent, because there an
    org host can set a cookie of the same name for the base domain.

    Parameters
    ----------
    response : Response
        The response to add the ``Set-Cookie`` header(s) to.
    base : BaseUrl
        The portal's base URL.
    """
    secure = base.scheme == "https"
    domains: tuple[str | None, ...] = (None,) if secure else (base.host, None)
    for domain in domains:
        response.delete_cookie(
            admin_cookie_name(base),
            path="/",
            domain=domain,
            secure=secure,
            httponly=True,
            samesite="strict",
        )


def admin_clearing_headers(base: BaseUrl) -> list[tuple[bytes, bytes]]:
    """The raw ``Set-Cookie`` headers of :func:`clear_admin_cookie`, for ASGI code."""
    response = Response()
    clear_admin_cookie(response, base)
    return [(k, v) for k, v in response.raw_headers if k == b"set-cookie"]


def session_cookies(cookie_headers: list[str], name: str = COOKIE_NAME) -> list[str]:
    """Every value of cookie ``name`` in a request's ``Cookie`` headers.

    Unlike a dict-based parser this keeps duplicates: more than one value
    means another host tossed a cookie in (the caller treats the request as
    signed out).

    Parameters
    ----------
    cookie_headers : list of str
        The raw ``Cookie`` header values.
    name : str, optional
        The cookie's name: :data:`COOKIE_NAME` by default, or
        :func:`admin_cookie_name` for the admin cookie.

    Returns
    -------
    list of str
        The values, in order (possibly empty strings).
    """
    found = []
    for header in cookie_headers:
        for chunk in header.split(";"):
            key, sep, value = chunk.partition("=")
            if sep and key.strip() == name:
                found.append(value.strip().strip('"'))
    return found


__all__ = [
    "ABSOLUTE",
    "ADMIN_ABSOLUTE",
    "ADMIN_COOKIE_NAME",
    "COOKIE_NAME",
    "IDLE",
    "TOUCH_EVERY",
    "SessionRow",
    "admin_clearing_headers",
    "admin_cookie_name",
    "clear_admin_cookie",
    "clear_cookie",
    "clearing_headers",
    "create_session",
    "end_paired_admin",
    "hash_token",
    "is_stale",
    "lookup",
    "repair_admin",
    "revoke",
    "revoke_user",
    "revoke_user_kind",
    "rotate",
    "session_cookies",
    "set_admin_cookie",
    "set_cookie",
    "touch",
]
