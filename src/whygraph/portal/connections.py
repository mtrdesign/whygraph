"""Connection tokens: a local portal acting as one user on one project (M2e).

A token is ``wgc_`` plus 32 random bytes (:func:`secrets.token_urlsafe`);
the ``connection_tokens`` table stores only its SHA-256
(:func:`whygraph.portal.sessions.hash_token`), so a database read alone can
never be replayed as a token. A token reaches **one project**, as its user,
and :func:`lookup` re-checks that user's membership **and project access**
(M2f-1: the org default, a grant, Restricted) on every call, so a role
change applies to the next request.

Revocation records a reason (:data:`~whygraph.portal.models.REVOKED_REASONS`),
so the local portal can say precisely what happened, and it follows
membership and project access: the routes that remove a member, a project
or an org, or take someone's access to a project away, revoke the affected
tokens in the same transaction (``revoke_for_*``,
:func:`revoke_lost_access`). Re-enabling a disabled user does **not**
restore their tokens.

Expiry: a token unused for :data:`IDLE` (90 days), or never used within
:data:`UNUSED` (1 hour) of being issued, is revoked as ``idle`` - by
:func:`lookup` when it is presented, and by :func:`sweep` (production's
start-up + hourly task) otherwise. The sweep also deletes rows revoked
:data:`PURGE_AFTER` (30 days) ago, so the Connected-portals lists show a
revoked token for a month.

Every function here is sync (it runs in a worker thread). Timestamps are
ISO-8601 UTC strings in the one format :mod:`whygraph.portal.models`
writes, so they compare correctly as strings; :func:`_now` is the clock
(tests age rows instead of patching it).
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, or_, update
from sqlmodel import Session, col, select

from .authz import ProjectRole, Role, effective_project_role
from .db import get_session
from .models import (
    ConnectionToken,
    Membership,
    Organization,
    Project,
    ProjectGrant,
    User,
)
from .sessions import hash_token

TOKEN_PREFIX = "wgc_"
"""What every connection token starts with (also matched by the redaction pattern)."""

TOKEN_RE = re.compile(r"wgc_[A-Za-z0-9_-]{43}")
"""The exact shape of a token :func:`issue` hands out."""

CLIENT_NAME_RE = re.compile(r"[A-Za-z0-9._ -]{1,64}")
"""A connected portal's name (the host machine's hostname by default)."""

IDLE = timedelta(days=90)
"""A token unused for this long is revoked as ``idle``."""

UNUSED = timedelta(hours=1)
"""A token never used within this long of being issued is revoked as ``idle``."""

PURGE_AFTER = timedelta(days=30)
""":func:`sweep` deletes a row revoked this long ago."""

TOUCH_EVERY = timedelta(minutes=5)
"""``last_used_at`` is written at most this often (the first use always)."""


@dataclass(frozen=True)
class TokenPrincipal:
    """A live connection token joined with its user.

    Attributes
    ----------
    token_id : int
        ``connection_tokens.id``.
    token_uid : str
        ``connection_tokens.uid``.
    org_id, project_id : int
        The one project the token reaches, and its org.
    last_used_at : str or None
        When the token was last used (``None``: this is its first use).
    user_id : int
        ``users.id``.
    uid : str
        The user's stable uid.
    display_name : str
        Shown in the UI.
    email : str or None
        The user's login name.
    github_login : str or None
        The GitHub username of a GitHub account.
    avatar_url : str or None
        The GitHub avatar of a GitHub account.
    has_password : bool
        Whether the account signs in with a password.
    is_instance_admin : bool
        Whether the user is an instance admin - informational only: a token
        principal is **never** an instance admin
        (:class:`whygraph.portal.deps.TokenIdentity` never copies it).
    client_name : str or None
        The token's ``client_name`` (the machine), for usage attribution.
    """

    token_id: int
    token_uid: str
    org_id: int
    project_id: int
    last_used_at: str | None
    user_id: int
    uid: str
    display_name: str
    email: str | None
    github_login: str | None
    avatar_url: str | None
    has_password: bool
    is_instance_admin: bool
    client_name: str | None = None


@dataclass(frozen=True)
class Refusal:
    """Why :func:`lookup` refused a token.

    Attributes
    ----------
    code : str
        ``"invalid_token"`` (unknown or malformed) or ``"token_revoked"``.
    reason : str or None
        For ``token_revoked``, one of
        :data:`~whygraph.portal.models.REVOKED_REASONS`; ``None`` for
        ``invalid_token``.
    retry_after : int or None
        Set (seconds) instead when the caller's address is over the failure
        throttle (:class:`whygraph.portal.deps.TokenIdentity`); the code is
        then ``"throttled"``.
    """

    code: str
    reason: str | None = None
    retry_after: int | None = None


INVALID = Refusal("invalid_token")
"""The refusal of an unknown, malformed or missing token."""


@dataclass(frozen=True)
class TokenInfo:
    """One row of a Connected-portals list (never the token or its hash).

    Attributes
    ----------
    uid : str
        ``connection_tokens.uid``.
    user_id : int
        The token's user.
    user_login : str or None
        The user's GitHub username (``None`` for a password account).
    user_name : str
        The user's display name.
    org_id, project_id : int or None
        The project the token reaches; ``None`` once deleted.
    project_slug, project_name : str or None
        That project's slug and name; ``None`` once deleted.
    client_name : str
        The connected portal's name.
    created_at, last_used_at, revoked_at, revoked_reason : str or None
        As stored (``created_at`` always set).
    """

    uid: str
    user_id: int
    user_login: str | None
    user_name: str
    org_id: int | None
    project_id: int | None
    project_slug: str | None
    project_name: str | None
    client_name: str
    created_at: str
    last_used_at: str | None
    revoked_at: str | None
    revoked_reason: str | None


CODE_TTL_SEC = 60.0
"""How long an authorization code from ``POST /api/connect/authorize`` works."""

CODE_MAX = 10_000
"""Most codes in flight; past it the oldest is evicted."""


@dataclass(frozen=True)
class PendingCode:
    """What an authorization code was issued for, kept until its exchange.

    Attributes
    ----------
    user_id, org_id, project_id : int
        Who allowed the connection, and the one project it reaches.
    redirect_uri : str
        The exact ``redirect_uri`` of the consent (the exchange must repeat it
        byte for byte).
    code_challenge : str
        The PKCE S256 challenge.
    client_name : str
        The connecting portal's name.
    expires_at : float
        Clock reading after which the code is dead.
    """

    user_id: int
    org_id: int
    project_id: int
    redirect_uri: str
    code_challenge: str
    client_name: str
    expires_at: float


class PendingCodes:
    """In-memory map of authorization codes in flight, keyed by the code's SHA-256.

    A copy of :class:`whygraph.portal.github_auth.PendingLogins`: thread-safe,
    entries live :data:`CODE_TTL_SEC` and are single use (:meth:`pop`), and
    past :data:`CODE_MAX` entries the oldest is evicted. Only the hash of a
    code is held, so a memory dump does not hand out live codes.

    Parameters
    ----------
    clock : callable, optional
        Returns seconds; injectable for tests (default ``time.monotonic``).
    ttl : float
        Entry lifetime in seconds.
    max_entries : int
        Capacity before the oldest entry is evicted.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = CODE_TTL_SEC,
        max_entries: int = CODE_MAX,
    ) -> None:
        self._clock = clock
        self._ttl = ttl
        self._max = max_entries
        self._entries: OrderedDict[str, PendingCode] = OrderedDict()
        self._lock = threading.Lock()

    def put(
        self,
        *,
        user_id: int,
        org_id: int,
        project_id: int,
        redirect_uri: str,
        code_challenge: str,
        client_name: str,
    ) -> str:
        """Issue a new code for a consent; return it (the raw code, once).

        Returns
        -------
        str
            ``secrets.token_urlsafe(32)``: 43 URL-safe characters.
        """
        code = secrets.token_urlsafe(32)
        entry = PendingCode(
            user_id=user_id,
            org_id=org_id,
            project_id=project_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            client_name=client_name,
            expires_at=self._clock() + self._ttl,
        )
        key = hash_token(code)
        with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = entry
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
        return code

    def pop(self, code: str) -> PendingCode | None:
        """Take a code out of the map (single use), whatever happens next.

        Parameters
        ----------
        code : str
            The raw authorization code.

        Returns
        -------
        PendingCode or None
            The entry, or ``None`` when it is unknown, used or expired.
        """
        with self._lock:
            entry = self._entries.pop(hash_token(code), None)
        if entry is None or entry.expires_at <= self._clock():
            return None
        return entry

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _live() -> list:
    return [col(ConnectionToken.revoked_at).is_(None)]


def _expired(now: datetime):  # noqa: ANN202 -- a SQL expression
    """Live rows past :data:`IDLE`, or never used within :data:`UNUSED`."""
    return or_(
        col(ConnectionToken.last_used_at) < _iso(now - IDLE),
        and_(
            col(ConnectionToken.last_used_at).is_(None),
            col(ConnectionToken.created_at) < _iso(now - UNUSED),
        ),
    )


def member_access_select(*columns, user_id: int):  # noqa: ANN002, ANN201
    """A ``SELECT`` of ``columns`` plus what decides ``user_id``'s project role.

    Selects ``columns`` from ``projects`` joined with ``user_id``'s
    membership of the project's org (inner: no membership, no row), the org
    and ``user_id``'s grant on the project (outer), and appends four
    columns - ``memberships.role``, ``organizations.default_project_role``,
    ``projects.restricted`` and ``project_grants.role`` - in the order
    :func:`member_project_role` takes them. The caller adds its ``WHERE``.

    Parameters
    ----------
    *columns
        What to select first; the first must be ``Project`` or one of its
        columns (it is the ``FROM``).
    user_id : int
        ``users.id`` of the member.

    Returns
    -------
    Select
        The statement; each row ends with the four access columns.
    """
    return (
        select(
            *columns,
            Membership.role,
            Organization.default_project_role,
            Project.restricted,
            ProjectGrant.role,
        )
        .join(
            Membership,
            (col(Membership.org_id) == col(Project.org_id))
            & (col(Membership.user_id) == user_id),
        )
        .join(Organization, col(Organization.id) == col(Project.org_id))
        .outerjoin(
            ProjectGrant,
            (col(ProjectGrant.project_id) == col(Project.id))
            & (col(ProjectGrant.user_id) == user_id),
        )
    )


def member_project_role(
    org_role: str, default: str, restricted: bool, grant: str | None
) -> ProjectRole | None:
    """A member's effective project role from the stored columns, or ``None``.

    :func:`~whygraph.portal.authz.effective_project_role` over the raw
    values :func:`member_access_select` appends (a stored org role is never
    ``reader``).
    """
    return effective_project_role(
        Role(org_role),
        default,
        bool(restricted),
        None if grant is None else ProjectRole(grant),
    )


def is_valid_client_name(name: str) -> bool:
    """Whether ``name`` matches :data:`CLIENT_NAME_RE` in full."""
    return CLIENT_NAME_RE.fullmatch(name) is not None


def issue(db: Session, *, user: User, project: Project, client_name: str) -> str:
    """Issue a token for ``user`` on ``project`` and return it.

    Parameters
    ----------
    db : Session
        An open portal DB session; the caller commits (and has checked the
        membership).
    user : User
        The user the token acts as.
    project : Project
        The one project it reaches (its org comes from the row).
    client_name : str
        The connected portal's name.

    Returns
    -------
    str
        The raw ``wgc_...`` token - it belongs only in the exchange's
        response, never in a log, an audit record or the database.

    Raises
    ------
    ValueError
        When ``client_name`` breaks :data:`CLIENT_NAME_RE`.
    """
    if not is_valid_client_name(client_name):
        raise ValueError("client_name must match [A-Za-z0-9._ -]{1,64}")
    assert user.id is not None and project.id is not None
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    db.add(
        ConnectionToken(
            token_hash=hash_token(token),
            user_id=user.id,
            org_id=project.org_id,
            project_id=project.id,
            client_name=client_name,
            created_at=_iso(_now()),
        )
    )
    db.flush()
    return token


def lookup(db: Session, raw: str) -> TokenPrincipal | Refusal:
    """Return the principal a raw token names, or why it is refused.

    The refusals, in order: an unknown or malformed token
    (``invalid_token``); a revoked one (``token_revoked`` + its reason); a
    token whose org or project is gone (``org_deleted`` / ``project_deleted``);
    a disabled user (``user_disabled``); an expired one (``idle`` - the row
    is revoked); a user who no longer holds a ``memberships`` row in the
    token's org (``member_removed``), whether or not that removal revoked
    the token; and a member who has no access to the project any more - a
    grant removed, the project Restricted, the org default ``none``
    (``project_access_removed``, M2f-1). The last two write nothing, so a
    restored membership or grant makes the token work again unless an eager
    revocation already ran.

    Parameters
    ----------
    db : Session
        An open portal DB session; the caller commits (an ``idle`` refusal
        writes the revocation).
    raw : str
        The bearer token.

    Returns
    -------
    TokenPrincipal or Refusal
        The live token and its user, or the :class:`Refusal`.
    """
    if not raw or TOKEN_RE.fullmatch(raw) is None:
        return INVALID
    row = db.exec(
        select(
            ConnectionToken,
            User.uid,
            User.display_name,
            User.email,
            User.github_login,
            User.avatar_url,
            col(User.password_hash).is_not(None),
            User.is_instance_admin,
            User.disabled_at,
        )
        .join(User, col(User.id) == col(ConnectionToken.user_id))
        .where(col(ConnectionToken.token_hash) == hash_token(raw))
    ).first()
    if row is None:
        return INVALID
    token, uid, name, email, login, avatar, has_password, admin, disabled_at = row
    if token.revoked_at is not None:
        return Refusal("token_revoked", token.revoked_reason)
    if token.org_id is None:
        return Refusal("token_revoked", "org_deleted")
    if token.project_id is None:
        return Refusal("token_revoked", "project_deleted")
    if disabled_at is not None:
        return Refusal("token_revoked", "user_disabled")
    now = _now()
    last = token.last_used_at
    if (last is not None and last < _iso(now - IDLE)) or (
        last is None and token.created_at < _iso(now - UNUSED)
    ):
        assert token.id is not None
        revoke(db, token_id=token.id, reason="idle")
        return Refusal("token_revoked", "idle")
    access = db.exec(
        member_access_select(Project.id, user_id=token.user_id).where(
            Project.id == token.project_id, Project.org_id == token.org_id
        )
    ).first()
    if access is None:
        return Refusal("token_revoked", "member_removed")
    if member_project_role(*access[1:]) is None:
        return Refusal("token_revoked", "project_access_removed")
    assert token.id is not None
    return TokenPrincipal(
        token_id=token.id,
        token_uid=token.uid,
        org_id=token.org_id,
        project_id=token.project_id,
        last_used_at=last,
        user_id=token.user_id,
        uid=uid,
        display_name=name,
        email=email,
        github_login=login,
        avatar_url=avatar,
        has_password=bool(has_password),
        is_instance_admin=bool(admin),
        client_name=token.client_name,
    )


def is_stale(principal: TokenPrincipal) -> bool:
    """Whether a token's ``last_used_at`` should be written now."""
    last = principal.last_used_at
    return last is None or last < _iso(_now() - TOUCH_EVERY)


def touch(token_id: int) -> bool:
    """Record that a token was used: always the first time, then every :data:`TOUCH_EVERY`.

    Parameters
    ----------
    token_id : int
        ``connection_tokens.id``.

    Returns
    -------
    bool
        Whether a row was written.
    """
    now = _now()
    with get_session() as db:
        result = db.exec(
            update(ConnectionToken)
            .where(
                col(ConnectionToken.id) == token_id,
                col(ConnectionToken.revoked_at).is_(None),
                or_(
                    col(ConnectionToken.last_used_at).is_(None),
                    col(ConnectionToken.last_used_at) < _iso(now - TOUCH_EVERY),
                ),
            )
            .values(last_used_at=_iso(now))
        )
        return bool(result.rowcount)


def _revoke_where(db: Session, reason: str, *conditions) -> int:  # noqa: ANN002
    result = db.exec(
        update(ConnectionToken)
        .where(*conditions, *_live())
        .values(revoked_at=_iso(_now()), revoked_reason=reason)
    )
    return int(result.rowcount or 0)


def revoke(
    db: Session,
    *,
    reason: str,
    token_id: int | None = None,
    uid: str | None = None,
    user_id: int | None = None,
    project_id: int | None = None,
) -> bool:
    """Revoke one live token, named by ``token_id`` or ``uid``.

    ``user_id`` / ``project_id`` narrow the match, so a caller can only
    revoke a token that is theirs (``user_revoked``) or of the project they
    administer (``admin_revoked``) - anything else matches nothing.

    Parameters
    ----------
    db : Session
        An open portal DB session; the caller commits.
    reason : str
        One of :data:`~whygraph.portal.models.REVOKED_REASONS`.
    token_id : int, optional
        ``connection_tokens.id``.
    uid : str, optional
        ``connection_tokens.uid``.
    user_id, project_id : int, optional
        Extra conditions on the row.

    Returns
    -------
    bool
        Whether a live token was revoked.

    Raises
    ------
    ValueError
        Without ``token_id`` and ``uid``.
    """
    if token_id is None and uid is None:
        raise ValueError("revoke needs a token_id or a uid")
    conditions = []
    if token_id is not None:
        conditions.append(col(ConnectionToken.id) == token_id)
    if uid is not None:
        conditions.append(col(ConnectionToken.uid) == uid)
    if user_id is not None:
        conditions.append(col(ConnectionToken.user_id) == user_id)
    if project_id is not None:
        conditions.append(col(ConnectionToken.project_id) == project_id)
    return _revoke_where(db, reason, *conditions) > 0


def revoke_for_member(db: Session, org_id: int, user_id: int, reason: str) -> int:
    """Revoke every live token of ``user_id`` in ``org_id``; return how many."""
    return _revoke_where(
        db,
        reason,
        col(ConnectionToken.org_id) == org_id,
        col(ConnectionToken.user_id) == user_id,
    )


def revoke_for_project_user(
    db: Session, project_id: int, user_id: int, reason: str
) -> int:
    """Revoke every live token of ``user_id`` on ``project_id``; return how many.

    The eager half of "tokens follow project access" (M2f-1): run it in the
    transaction that takes ``user_id``'s access to the project away, with
    ``project_access_removed``. :func:`revoke_lost_access` finds who lost it.
    """
    return _revoke_where(
        db,
        reason,
        col(ConnectionToken.project_id) == project_id,
        col(ConnectionToken.user_id) == user_id,
    )


def revoke_lost_access(
    db: Session,
    org_id: int,
    *,
    project_id: int | None = None,
    user_id: int | None = None,
) -> int:
    """Revoke the live tokens in ``org_id`` whose user lost access to their project.

    Run it in the same transaction, **after** the change (a grant removed,
    a project made Restricted, the org default set to ``none``, a member
    demoted) is flushed: every live token of the org - narrowed to
    ``project_id`` and / or ``user_id`` when given - whose user's effective
    project role is now ``None`` is revoked as ``project_access_removed``
    (:func:`revoke_for_project_user`). A token whose user is no longer a
    member is left alone: removing a member revokes with its own reason.

    Parameters
    ----------
    db : Session
        An open portal DB session; the caller commits.
    org_id : int
        The org whose tokens are checked.
    project_id, user_id : int, optional
        Only the tokens of this project / this user.

    Returns
    -------
    int
        How many tokens were revoked.
    """
    conditions = [col(ConnectionToken.org_id) == org_id, *_live()]
    if project_id is not None:
        conditions.append(col(ConnectionToken.project_id) == project_id)
    if user_id is not None:
        conditions.append(col(ConnectionToken.user_id) == user_id)
    rows = db.exec(
        select(
            ConnectionToken.project_id,
            ConnectionToken.user_id,
            Membership.role,
            Organization.default_project_role,
            Project.restricted,
            ProjectGrant.role,
        )
        .join(Project, col(Project.id) == col(ConnectionToken.project_id))
        .join(Organization, col(Organization.id) == col(Project.org_id))
        .join(
            Membership,
            (col(Membership.org_id) == col(Project.org_id))
            & (col(Membership.user_id) == col(ConnectionToken.user_id)),
        )
        .outerjoin(
            ProjectGrant,
            (col(ProjectGrant.project_id) == col(Project.id))
            & (col(ProjectGrant.user_id) == col(ConnectionToken.user_id)),
        )
        .where(*conditions)
        .distinct()
    ).all()
    revoked = 0
    for token_project, token_user, *access in rows:
        if member_project_role(*access) is None:
            revoked += revoke_for_project_user(
                db, token_project, token_user, "project_access_removed"
            )
    return revoked


def revoke_for_user(db: Session, user_id: int, reason: str) -> int:
    """Revoke every live token of ``user_id``; return how many."""
    return _revoke_where(db, reason, col(ConnectionToken.user_id) == user_id)


def revoke_for_project(db: Session, project_id: int, reason: str) -> int:
    """Revoke every live token of ``project_id``; return how many.

    Run it **before** deleting the project: the delete sets ``project_id``
    to ``NULL``.
    """
    return _revoke_where(db, reason, col(ConnectionToken.project_id) == project_id)


def revoke_for_org(db: Session, org_id: int, reason: str) -> int:
    """Revoke every live token of ``org_id``; return how many.

    Run it **before** deleting the org's projects and the org.
    """
    return _revoke_where(db, reason, col(ConnectionToken.org_id) == org_id)


def _infos(db: Session, *conditions) -> list[TokenInfo]:  # noqa: ANN002
    rows = db.exec(
        select(
            ConnectionToken,
            User.github_login,
            User.display_name,
            Project.slug,
            Project.name,
        )
        .join(User, col(User.id) == col(ConnectionToken.user_id))
        .outerjoin(Project, col(Project.id) == col(ConnectionToken.project_id))
        .where(*conditions)
        .order_by(
            col(ConnectionToken.created_at).desc(), col(ConnectionToken.id).desc()
        )
    ).all()
    return [
        TokenInfo(
            uid=token.uid,
            user_id=token.user_id,
            user_login=login,
            user_name=user_name,
            org_id=token.org_id,
            project_id=token.project_id,
            project_slug=slug,
            project_name=project_name,
            client_name=token.client_name,
            created_at=token.created_at,
            last_used_at=token.last_used_at,
            revoked_at=token.revoked_at,
            revoked_reason=token.revoked_reason,
        )
        for token, login, user_name, slug, project_name in rows
    ]


def list_for_user(db: Session, user_id: int) -> list[TokenInfo]:
    """Every token of ``user_id``, live and revoked (until swept), newest first."""
    return _infos(db, col(ConnectionToken.user_id) == user_id)


def list_for_project(db: Session, project_id: int) -> list[TokenInfo]:
    """The live tokens of ``project_id`` (any member's), newest first."""
    return _infos(db, col(ConnectionToken.project_id) == project_id, *_live())


def sweep() -> tuple[int, int]:
    """Revoke expired tokens as ``idle``; delete rows revoked :data:`PURGE_AFTER` ago.

    Returns
    -------
    tuple of int
        ``(revoked, deleted)``.
    """
    now = _now()
    with get_session() as db:
        revoked = _revoke_where(db, "idle", _expired(now))
        result = db.exec(
            delete(ConnectionToken).where(
                col(ConnectionToken.revoked_at) < _iso(now - PURGE_AFTER)
            )
        )
        return revoked, int(result.rowcount or 0)


__all__ = [
    "CLIENT_NAME_RE",
    "CODE_MAX",
    "CODE_TTL_SEC",
    "IDLE",
    "INVALID",
    "PURGE_AFTER",
    "TOKEN_PREFIX",
    "TOKEN_RE",
    "TOUCH_EVERY",
    "UNUSED",
    "PendingCode",
    "PendingCodes",
    "Refusal",
    "TokenInfo",
    "TokenPrincipal",
    "is_stale",
    "is_valid_client_name",
    "issue",
    "list_for_project",
    "list_for_user",
    "lookup",
    "member_access_select",
    "member_project_role",
    "revoke",
    "revoke_for_member",
    "revoke_for_org",
    "revoke_for_project",
    "revoke_for_project_user",
    "revoke_lost_access",
    "revoke_for_user",
    "sweep",
    "touch",
]
