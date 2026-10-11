"""Production's consent and code exchange for connected portals (M2e plan section 4.4).

A local portal links one of its checkouts to a platform project through an
OAuth 2.0 authorization code with PKCE (S256), RFC 9207 ``iss`` and a
loopback ``redirect_uri`` (RFC 8252 section 7.3). Every route is
**production-only** - the router's
:func:`~whygraph.portal.deps.require_production` runs first, so local mode
answers ``404``:

* ``GET /api/v1/meta`` (public, any host): what this platform speaks.
* ``POST /api/connect/validate``, ``GET /api/connect/projects`` and
  ``POST /api/connect/authorize`` (base host, ``user.self``): the consent
  page's server side. ``validate`` checks the consent query before the
  page renders anything and returns the Cancel URL; ``authorize`` issues a
  single-use code (60 s, :class:`~whygraph.portal.connections.PendingCodes`)
  and returns the whole redirect. Both URLs are built **here**, every
  value URL-encoded, and an invalid request never yields one, so the page
  can only navigate to a URL this module returned.
* ``POST /api/connect/token`` (base host, public): the local portal trades
  the code for a ``wgc_`` token. The code is popped **before** anything is
  verified, so a failed PKCE or ``redirect_uri`` check spends it; then the
  membership, the project access and the account are re-checked.
* ``GET /api/connect/tokens`` and ``DELETE /api/connect/tokens/{uid}``
  (base host, ``user.self``): the caller's own connected portals.
* ``GET /api/projects/{slug}/connections`` and ``DELETE
  /api/projects/{slug}/connections/{uid}`` (org host,
  ``project.configure``): every member's live tokens of one project, which
  an owner or admin may revoke (``admin_revoked``).
* ``DELETE /api/v1/projects/{slug}/token`` (org host, bearer,
  ``project.read``): a connected portal revokes its own token
  (``removed_locally``).

Tokens are only ever for real org members with access to the project
(M2f-1): an instance admin's ``reader`` access never consents, a project
the member cannot see (Restricted without a grant, or the org default
``none``) is answered exactly like a missing one in the consent list, the
validate hint, the authorize step and the exchange, and the exchange
re-reads the ``memberships`` row and the project access.
Audit records carry ``client_name`` ``repr()``-quoted and never a token or
a code.
"""

from __future__ import annotations

import hmac
import re
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, col, select

from whygraph.api_v1 import API_VERSION, MetaOut, TokenReply

from . import connections
from .audit import audit
from .authz import Action
from .connections import member_access_select, member_project_role
from .db import get_session
from .deps import (
    ApiError,
    BoundProject,
    PortalState,
    current_user,
    portal_state,
    project_access,
    require_mode_and_host,
    require_production,
    user_access,
    v1_project_access,
    v1_user,
)
from .github_auth import pkce_challenge
from .hosts import BaseUrl
from .models import ConnectionToken, Membership, Organization, Project, User
from .routes import _github_full_name
from .security import Principal
from .sessions import hash_token
from .throttle import ip_key
from .v1_status import load_status

MIN_CLIENT_VERSION = "2.0.0"
"""The oldest WhyGraph a connected portal may run (``GET /api/v1/meta``).

The first release with a platform client: there is no older one to serve.
Raise it when the ``/api/v1`` contract changes in a way an older client
would misread.
"""

CAPABILITIES: tuple[str, ...] = ("evidence", "rationale", "history", "resources")
"""The ``/api/v1`` route families this platform offers."""

REDIRECT_URI_RE = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/connect/callback")
"""The only ``redirect_uri`` shape accepted (with a port of 1-65535).

Byte-exact: ``http``, the IPv4 loopback literal (``localhost`` is refused,
RFC 8252 section 8.3), an explicit port without a leading zero, the one
callback path, no userinfo, query or fragment.
"""

CODE_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}")
"""An S256 challenge: base64url of a SHA-256 digest, unpadded (RFC 7636 section 4.2)."""

CODE_VERIFIER_RE = re.compile(r"[A-Za-z0-9._~-]{43,128}")
"""A PKCE code verifier (RFC 7636 section 4.1)."""

STATE_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
"""The local portal's ``state``."""

_PUBLIC_BASE = [Depends(require_mode_and_host("base"))]

connect_router = APIRouter(dependencies=[Depends(require_production)])
"""Every route of this module; included before the ``/api`` 404 catch-all."""


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConnectBody(_Strict):
    """``POST /api/connect/validate``: the consent page's query."""

    redirect_uri: str = Field(max_length=2048)
    code_challenge: str = Field(max_length=256)
    code_challenge_method: str = Field(max_length=32)
    state: str = Field(max_length=256)
    client_name: str = Field(max_length=256)
    org: str | None = Field(default=None, max_length=100)
    project: str | None = Field(default=None, max_length=100)


class AuthorizeBody(ConnectBody):
    """``POST /api/connect/authorize``: the validated query plus the choice."""

    org: str = Field(max_length=100)
    project: str = Field(max_length=100)


class TokenBody(_Strict):
    """``POST /api/connect/token``."""

    code: str = Field(max_length=256)
    code_verifier: str = Field(max_length=256)
    redirect_uri: str = Field(max_length=2048)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def redirect_port(redirect_uri: str) -> int | None:
    """The port of a valid ``redirect_uri``, or ``None`` (:data:`REDIRECT_URI_RE`).

    Deliberately not a loopback-host check: only the exact literal form
    passes, so ``localhost``, ``[::1]``, ``127.0.0.2``, ``https``, another
    path, a query, a fragment, userinfo and a missing or out-of-range port
    are all refused.
    """
    match = REDIRECT_URI_RE.fullmatch(redirect_uri)
    if match is None:
        return None
    port = int(match.group(1))
    return port if 1 <= port <= 65535 else None


def _bad(field: str, message: str) -> ApiError:
    return ApiError(422, message, code="bad_connect_request", field=field)


def checked_request(body: ConnectBody) -> int:
    """Validate a consent request's fields; return the ``redirect_uri`` port.

    Raises
    ------
    ApiError
        ``422 {"code": "bad_connect_request", "field": ...}`` naming the
        first field that breaks its rule (never with a URL).
    """
    port = redirect_port(body.redirect_uri)
    if port is None:
        raise _bad(
            "redirect_uri",
            "redirect_uri must be exactly http://127.0.0.1:<port>/connect/callback",
        )
    if body.code_challenge_method != "S256":
        raise _bad("code_challenge_method", "code_challenge_method must be S256")
    if CODE_CHALLENGE_RE.fullmatch(body.code_challenge) is None:
        raise _bad("code_challenge", "code_challenge must be 43 base64url characters")
    if STATE_RE.fullmatch(body.state) is None:
        raise _bad("state", "state must be 16-128 characters of [A-Za-z0-9_-]")
    if not connections.is_valid_client_name(body.client_name):
        raise _bad(
            "client_name", "client_name must be 1-64 characters of [A-Za-z0-9._ -]"
        )
    return port


def _base(state: PortalState) -> BaseUrl:
    assert state.base_url is not None  # production startup set it
    return state.base_url


def callback_url(redirect_uri: str, params: dict[str, str]) -> str:
    """``redirect_uri`` with ``params`` as its query, every value percent-encoded."""
    return f"{redirect_uri}?{urlencode(params, quote_via=quote)}"


def _throttled(retry_after: int | None) -> None:
    if retry_after is not None:
        raise ApiError(
            429,
            "too many attempts; try again later",
            code="throttled",
            headers={"Retry-After": str(retry_after)},
        )


def _member_orgs(db: Session, user_id: int) -> list[tuple[int, str, str, str]]:
    """``(id, slug, name, role)`` of each org ``user_id`` holds a membership in."""
    rows = db.exec(
        select(Organization.id, Organization.slug, Organization.name, Membership.role)
        .join(Membership, col(Membership.org_id) == col(Organization.id))
        .where(Membership.user_id == user_id)
        .order_by(Organization.name, Organization.slug)
    ).all()
    return [(org_id, slug, name, role) for org_id, slug, name, role in rows]


def _not_found() -> ApiError:
    return ApiError(404, "no such connection", code="not_found")


# ---------------------------------------------------------------------------
# Meta (any host, public)
# ---------------------------------------------------------------------------


@connect_router.get("/api/v1/meta")
def get_meta() -> dict:
    """What this platform speaks: ``api_version``, ``min_client``, ``capabilities``."""
    return MetaOut(
        api_version=API_VERSION,
        min_client=MIN_CLIENT_VERSION,
        capabilities=list(CAPABILITIES),
    ).model_dump()


# ---------------------------------------------------------------------------
# Consent (base host, session)
# ---------------------------------------------------------------------------


@connect_router.post("/api/connect/validate")
def post_validate(
    body: ConnectBody,
    request: Request,
    principal: Principal = Depends(user_access("base")),
) -> dict:
    """Check a consent request before the page shows it; return the Cancel URL.

    A hinted ``org`` must be one the caller is a member of, and a hinted
    ``project`` one of its projects the caller has access to (``422
    bad_connect_request`` otherwise, the same answer for an org or a
    project that does not exist). ``orgs`` lists the caller's member orgs.
    """
    port = checked_request(body)
    base = _base(portal_state(request))
    with get_session() as db:
        orgs = _member_orgs(db, principal.user_id)
        if body.org is not None:
            org_id = next((o[0] for o in orgs if o[1] == body.org), None)
            if org_id is None:
                raise _bad("org", "that is not one of your organizations")
            if body.project is not None:
                found = db.exec(
                    member_access_select(Project.id, user_id=principal.user_id).where(
                        Project.org_id == org_id, Project.slug == body.project
                    )
                ).first()
                if found is None or member_project_role(*found[1:]) is None:
                    raise _bad("project", "no such project in that organization")
        elif body.project is not None:
            raise _bad("project", "a project needs its org")
    return {
        "ok": True,
        "client_name": body.client_name,
        "port": port,
        "org": body.org,
        "project": body.project,
        "orgs": [
            {"slug": slug, "name": name, "role": role} for _, slug, name, role in orgs
        ],
        "cancel_url": callback_url(
            body.redirect_uri,
            {"error": "access_denied", "state": body.state, "iss": base.origin},
        ),
    }


@connect_router.get("/api/connect/projects")
def get_connect_projects(
    principal: Principal = Depends(user_access("base")),
) -> list[dict]:
    """The projects of the caller's **member** orgs they have access to.

    Never a ``reader``'s, and never a project the caller cannot see
    (Restricted without a grant, or the org default ``none``).
    """
    with get_session() as db:
        rows = db.exec(
            member_access_select(
                Project, Organization.slug, Organization.name, user_id=principal.user_id
            ).order_by(Organization.name, Organization.slug, Project.name, Project.slug)
        ).all()
        return [
            {
                "org": org_slug,
                "org_name": org_name,
                "slug": project.slug,
                "name": project.name,
                "github_full_name": _github_full_name(project),
                "access_lost": project.access_lost_at is not None,
            }
            for project, org_slug, org_name, *access in rows
            if member_project_role(*access) is not None
        ]


@connect_router.post("/api/connect/authorize")
def post_authorize(
    body: AuthorizeBody,
    request: Request,
    principal: Principal = Depends(user_access("base")),
) -> dict:
    """Allow: issue a single-use code and return the redirect to the local portal.

    ``429`` past 30 attempts per user an hour; ``422 bad_connect_request``
    as :func:`post_validate`; ``404`` unless the caller holds a real
    membership of ``org``, it has ``project`` and the caller has access to
    it (the same ``404`` for a Restricted project). ``redirect`` is
    ``redirect_uri?code=...&state=...&iss=<base origin>``.
    """
    state = portal_state(request)
    _throttled(state.connect_user.hit(principal.user_id))
    port = checked_request(body)
    base = _base(state)
    with get_session() as db:
        row = db.exec(
            member_access_select(
                Project.id,
                Project.org_id,
                Project.access_lost_at,
                user_id=principal.user_id,
            ).where(Organization.slug == body.org, Project.slug == body.project)
        ).first()
    if row is None or member_project_role(*row[3:]) is None:
        raise ApiError(404, "not found")
    project_id, org_id, access_lost_at = row[:3]
    code = state.connect_codes.put(
        user_id=principal.user_id,
        org_id=org_id,
        project_id=project_id,
        redirect_uri=body.redirect_uri,
        code_challenge=body.code_challenge,
        client_name=body.client_name,
    )
    audit(
        "connection_authorized",
        request,
        uid=principal.uid,
        org_id=org_id,
        org=body.org,
        project=body.project,
        client_name=repr(body.client_name),
        port=port,
    )
    return {
        "redirect": callback_url(
            body.redirect_uri, {"code": code, "state": body.state, "iss": base.origin}
        ),
        "access_lost": access_lost_at is not None,
    }


# ---------------------------------------------------------------------------
# The exchange (base host, public)
# ---------------------------------------------------------------------------


@connect_router.post("/api/connect/token", dependencies=_PUBLIC_BASE)
def post_token(body: TokenBody, request: Request) -> dict:
    """Trade an authorization code for a connection token.

    ``429`` past 60 **failed** exchanges per ``ip_key`` in 10 minutes (a
    successful exchange is never counted, so many people behind one
    address can still connect; the 256-bit codes, not the throttle, stop
    guessing);
    ``400 invalid_grant`` for an unknown, used or expired code, a wrong
    ``code_verifier``, a ``redirect_uri`` that is not byte-equal to the
    consent's, a disabled account, a lost membership, lost access to the
    project (audited as ``project_access_removed``) or a deleted project -
    one answer for all. The code is spent by the first attempt, whatever
    its outcome. Returns :class:`~whygraph.api_v1.TokenReply`.
    """
    state = portal_state(request)
    client = ip_key(request.scope)
    _throttled(state.connect_ip.check(client))

    def refused(reason: str, **fields: object) -> ApiError:
        state.connect_ip.record(client)
        audit("connection_token_refused", request, reason=reason, **fields)
        return ApiError(
            400,
            "this connection request is invalid or has expired: connect again",
            code="invalid_grant",
        )

    pending = state.connect_codes.pop(body.code)  # first: a failed check spends it
    if pending is None:
        raise refused("unknown_code")
    if CODE_VERIFIER_RE.fullmatch(
        body.code_verifier
    ) is None or not hmac.compare_digest(
        pkce_challenge(body.code_verifier), pending.code_challenge
    ):
        raise refused("pkce")
    if not hmac.compare_digest(
        body.redirect_uri.encode("utf-8"), pending.redirect_uri.encode("utf-8")
    ):
        raise refused("redirect_uri")
    with get_session() as db:
        user = db.get(User, pending.user_id)
        if user is None or user.disabled_at is not None:
            raise refused("user_disabled")
        project = db.get(Project, pending.project_id)
        if project is None or project.org_id != pending.org_id:
            raise refused("project_deleted")
        status = load_status(db, pending.project_id, pending.user_id)
        if status is None:
            member = db.get(Membership, (project.org_id, user.id))
            reason = "member_removed" if member is None else "project_access_removed"
            raise refused(reason, target=user.uid)
        org_slug = db.exec(
            select(Organization.slug).where(Organization.id == project.org_id)
        ).one()
        token = connections.issue(
            db, user=user, project=project, client_name=pending.client_name
        )
        token_uid = db.exec(
            select(ConnectionToken.uid).where(
                ConnectionToken.token_hash == hash_token(token)
            )
        ).one()
        uid, project_slug, org_id = user.uid, project.slug, project.org_id
    audit(
        "connection_token_issued",
        request,
        uid=uid,
        org_id=org_id,
        org=org_slug,
        project=project_slug,
        client_name=repr(pending.client_name),
        token_uid=token_uid,
    )
    return TokenReply(
        token=token, org=org_slug, project=status, api_version=API_VERSION
    ).model_dump(mode="json", exclude={"api_origin"})  # the client computes it


# ---------------------------------------------------------------------------
# The caller's own connected portals (base host, session)
# ---------------------------------------------------------------------------


@connect_router.get("/api/connect/tokens")
def get_tokens(principal: Principal = Depends(user_access("base"))) -> list[dict]:
    """The caller's tokens, live and revoked (until swept), newest first."""
    with get_session() as db:
        infos = connections.list_for_user(db, principal.user_id)
        org_ids = {info.org_id for info in infos if info.org_id is not None}
        slugs = (
            dict(
                db.exec(
                    select(Organization.id, Organization.slug).where(
                        col(Organization.id).in_(org_ids)
                    )
                ).all()
            )
            if org_ids
            else {}
        )
    return [
        {
            "uid": info.uid,
            "org": slugs.get(info.org_id) if info.org_id is not None else None,
            "project": info.project_slug,
            "project_name": info.project_name,
            "client_name": info.client_name,
            "created_at": info.created_at,
            "last_used_at": info.last_used_at,
            "revoked_at": info.revoked_at,
            "revoked_reason": info.revoked_reason,
        }
        for info in infos
    ]


@connect_router.delete("/api/connect/tokens/{uid}", status_code=204)
def delete_token(
    uid: str, request: Request, principal: Principal = Depends(user_access("base"))
) -> Response:
    """Revoke one of the caller's live tokens (``user_revoked``); else ``404``."""
    with get_session() as db:
        revoked = connections.revoke(
            db, reason="user_revoked", uid=uid, user_id=principal.user_id
        )
    if not revoked:
        raise _not_found()
    audit(
        "connection_revoked",
        request,
        uid=principal.uid,
        token_uid=uid,
        reason="user_revoked",
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# A project's connected portals (org host, project.configure)
# ---------------------------------------------------------------------------


@connect_router.get("/api/projects/{slug}/connections")
def get_project_connections(
    project: BoundProject = Depends(project_access(Action.PROJECT_CONFIGURE)),
    include: str | None = None,
) -> list[dict]:
    """Every member's live tokens of the project, newest first.

    ``?include=revoked`` also lists the tokens revoked in the last 30 days
    (the sweep deletes older ones), each with ``revoked_at`` and
    ``revoked_reason`` (M2f-3 plan section 4.13); any other ``include`` is
    ``422 bad_filter``.
    """
    if include not in (None, "revoked"):
        raise ApiError(422, "include must be 'revoked'", code="bad_filter")
    revoked = include == "revoked"
    with get_session() as db:
        infos = connections.list_for_project(db, project.id, include_revoked=revoked)
    rows = []
    for info in infos:
        row = {
            "uid": info.uid,
            "user_login": info.user_login,
            "user_name": info.user_name,
            "client_name": info.client_name,
            "created_at": info.created_at,
            "last_used_at": info.last_used_at,
        }
        if revoked:
            row["revoked_at"] = info.revoked_at
            row["revoked_reason"] = info.revoked_reason
        rows.append(row)
    return rows


@connect_router.delete("/api/projects/{slug}/connections/{uid}", status_code=204)
def delete_project_connection(
    uid: str,
    request: Request,
    project: BoundProject = Depends(project_access(Action.PROJECT_CONFIGURE)),
    principal: Principal = Depends(current_user),
) -> Response:
    """Revoke a live token **of this project** (``admin_revoked``); else ``404``."""
    with get_session() as db:
        target = db.exec(
            select(User.uid)
            .join(ConnectionToken, col(ConnectionToken.user_id) == col(User.id))
            .where(
                ConnectionToken.uid == uid,
                ConnectionToken.project_id == project.id,
                col(ConnectionToken.revoked_at).is_(None),
            )
        ).first()
        revoked = target is not None and connections.revoke(
            db, reason="admin_revoked", uid=uid, project_id=project.id
        )
    if not revoked:
        raise _not_found()
    audit(
        "connection_revoked",
        request,
        uid=principal.uid,
        target=target,
        org_id=project.org_id,
        org=request.scope.get("state", {}).get("org_slug"),
        project=project.slug,
        token_uid=uid,
        reason="admin_revoked",
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# A connected portal revokes itself (org host, bearer)
# ---------------------------------------------------------------------------


@connect_router.delete("/api/v1/projects/{slug}/token", status_code=204)
def delete_v1_token(
    request: Request,
    project: BoundProject = Depends(v1_project_access(Action.PROJECT_READ)),
    principal: Principal = Depends(v1_user),
) -> Response:
    """Revoke the bearer token of this request (``removed_locally``)."""
    assert principal.token_id is not None  # v1_user only passes token principals
    with get_session() as db:
        token_uid = db.exec(
            select(ConnectionToken.uid).where(ConnectionToken.id == principal.token_id)
        ).first()
        revoked = connections.revoke(
            db,
            reason="removed_locally",
            token_id=principal.token_id,
            project_id=project.id,
        )
    if not revoked:  # revoked concurrently since the lookup
        raise ApiError(
            401,
            "this connection token was revoked",
            code="token_revoked",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    audit(
        "connection_revoked",
        request,
        uid=principal.uid,
        org_id=project.org_id,
        org=request.scope.get("state", {}).get("org_slug"),
        project=project.slug,
        token_uid=token_uid,
        reason="removed_locally",
    )
    return Response(status_code=204)


__all__ = [
    "CAPABILITIES",
    "CODE_CHALLENGE_RE",
    "CODE_VERIFIER_RE",
    "MIN_CLIENT_VERSION",
    "REDIRECT_URI_RE",
    "STATE_RE",
    "callback_url",
    "checked_request",
    "connect_router",
    "redirect_port",
]
