"""Server-side browser sessions for production mode.

The cookie carries 32 random bytes (:func:`secrets.token_urlsafe`); the
``sessions`` table stores only their SHA-256, so a database read alone can
never be replayed as a cookie. A session ends 30 days after sign-in
(:data:`ABSOLUTE`) or after 7 days without a request (:data:`IDLE`);
``last_seen_at`` is written at most every 5 minutes (:data:`TOUCH_EVERY`),
so a sliding session costs no write per request.

Every function here is sync (it runs in a worker thread). Timestamps are
ISO-8601 UTC strings in the one format :mod:`whygraph.portal.models` writes,
so they compare correctly as strings.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, update
from sqlmodel import Session, col, select
from starlette.responses import Response

from .db import get_session
from .hosts import BaseUrl
from .models import User, UserSession

COOKIE_NAME = "whygraph_session"
"""The session cookie's name."""

ABSOLUTE = timedelta(days=30)
"""A session's absolute lifetime (also the cookie's ``Max-Age``)."""

IDLE = timedelta(days=7)
"""A session unused for this long is refused."""

TOUCH_EVERY = timedelta(minutes=5)
"""``last_seen_at`` is written at most this often."""

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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def hash_token(token: str) -> str:
    """Return the SHA-256 hex digest stored for a raw session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db: Session, user_id: int, user_agent: str | None) -> str:
    """Start a session for ``user_id`` and return its raw token.

    Expired and idle rows (anyone's) are deleted first, so the table never
    grows without bound.

    Parameters
    ----------
    db : Session
        An open portal DB session; the caller commits.
    user_id : int
        ``users.id`` of the signed-in user.
    user_agent : str or None
        The request's ``User-Agent`` (only its first 200 characters are kept).

    Returns
    -------
    str
        The token - it belongs only in the ``Set-Cookie`` header
        (:func:`set_cookie`), never in a log or a response body.
    """
    now = _now()
    db.exec(
        delete(UserSession).where(
            or_(
                col(UserSession.expires_at) < _iso(now),
                col(UserSession.last_seen_at) < _iso(now - IDLE),
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
            expires_at=_iso(now + ABSOLUTE),
            user_agent=(user_agent or "")[:USER_AGENT_MAX] or None,
        )
    )
    db.flush()
    return token


def rotate(db: Session, session_id: int, user_id: int, user_agent: str | None) -> str:
    """Replace a session with a fresh one (after a credential change).

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
    db.exec(delete(UserSession).where(col(UserSession.id) == session_id))
    return create_session(db, user_id, user_agent)


def lookup(token: str) -> SessionRow | None:
    """Return the live session a raw token names, with its user.

    Parameters
    ----------
    token : str
        The cookie value.

    Returns
    -------
    SessionRow or None
        ``None`` for an unknown token, a session past its absolute or idle
        lifetime, or one whose user is disabled (so a disabled user's
        surviving session simply resolves to signed out).
    """
    if not token:
        return None
    now = _now()
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
            )
            .join(User, col(User.id) == col(UserSession.user_id))
            .where(
                col(UserSession.token_hash) == hash_token(token),
                col(UserSession.expires_at) > _iso(now),
                col(UserSession.last_seen_at) > _iso(now - IDLE),
                col(User.disabled_at).is_(None),
            )
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
    """End every session of a user, optionally keeping one.

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


def session_cookies(cookie_headers: list[str]) -> list[str]:
    """Every ``whygraph_session`` value in a request's ``Cookie`` headers.

    Unlike a dict-based parser this keeps duplicates: more than one value
    means another host tossed a cookie in (the caller treats the request as
    signed out).

    Parameters
    ----------
    cookie_headers : list of str
        The raw ``Cookie`` header values.

    Returns
    -------
    list of str
        The values, in order (possibly empty strings).
    """
    found = []
    for header in cookie_headers:
        for chunk in header.split(";"):
            name, sep, value = chunk.partition("=")
            if sep and name.strip() == COOKIE_NAME:
                found.append(value.strip().strip('"'))
    return found


__all__ = [
    "ABSOLUTE",
    "COOKIE_NAME",
    "IDLE",
    "TOUCH_EVERY",
    "SessionRow",
    "clear_cookie",
    "clearing_headers",
    "create_session",
    "hash_token",
    "is_stale",
    "lookup",
    "revoke",
    "revoke_user",
    "rotate",
    "session_cookies",
    "set_cookie",
    "touch",
]
