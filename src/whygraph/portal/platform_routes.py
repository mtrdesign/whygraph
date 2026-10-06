"""Local mode's connect and link to a WhyGraph platform (M2e plan section 4.8).

A local checkout is linked to a platform project in one browser round trip
(OAuth 2.0 authorization code + PKCE S256 + RFC 9207 ``iss``, loopback
``redirect_uri``) and one wizard step:

* ``POST /api/platform/connect`` checks the platform's ``meta``, remembers a
  :class:`~whygraph.portal.platform_pending.PendingConnect` (10 min) and
  returns the consent URL on the platform;
* ``POST /api/platform/callback`` takes what the platform sent back to
  ``/connect/callback``: it pops the connect, refuses an ``iss`` that is
  not the platform it started with **before anything else** (the mix-up
  attack), exchanges the code, computes the org origin **locally** and the
  checkout candidates once, and remembers a
  :class:`~whygraph.portal.platform_pending.PendingLink` (30 min);
* ``GET /api/platform/pending/{link_id}`` feeds the wizard's checkout
  picker; ``DELETE`` abandons the link and revokes its token;
* ``POST /api/projects {"source": "platform", ...}``
  (:func:`whygraph.portal.routes._add_platform`) consumes the link.

Every route is **local-mode only** (:func:`~whygraph.portal.deps.require_local`
on the router, before :func:`~whygraph.portal.deps.current_user`, so
production answers ``404`` on every host) and declares
``org.add_project``. The connection token is never in a response, a log
line or an error; a pending link that expires or is evicted has its token
revoked on the platform, best effort (``removed_locally``), and a token
lost to a restart expires unused after an hour on the platform.

Notes
-----
The machine name a platform shows (``client_name``) defaults to
:func:`default_client_name`, which ``GET /api/portal/state`` serves as
``hostname`` in local mode so the connect page can prefill it.
"""

from __future__ import annotations

import logging
import re
import secrets
import socket
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, col, select

from whygraph.services.git import GitError, Repository, strip_userinfo
from whygraph.services.github import remote_identity

from .authz import Action, OrgAccess
from .connections import is_valid_client_name
from .db import get_session
from .deps import (
    ApiError,
    PortalState,
    current_user,
    org_access,
    portal_state,
    require_local,
)
from .github_auth import pkce_challenge
from .models import PlatformLink, Project
from .orgs import is_valid_org_slug
from .platform_client import (
    PlatformError,
    PlatformHttp,
    PlatformRefused,
    PlatformUnreachable,
    UpdateRequired,
    api_origin_for,
    dev_platform_http_enabled,
    parse_platform_url,
)
from .platform_pending import PendingLink
from .projects import is_valid_slug

_log = logging.getLogger(__name__)

CALLBACK_PATH = "/connect/callback"
"""The SPA page the platform redirects back to."""

DEFAULT_CLIENT_NAME = "WhyGraph portal"
"""The machine name when the hostname has no usable character."""

_NAME_JUNK = re.compile(r"[^A-Za-z0-9._ -]+")

platform_router = APIRouter(
    prefix="/api/platform",
    dependencies=[Depends(require_local), Depends(current_user)],
)
"""The connect and link routes; ``require_local`` runs first."""

_ADD = org_access(Action.ORG_ADD_PROJECT)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConnectBody(_Strict):
    """``POST /api/platform/connect``: the link page's form."""

    platform_url: str = Field(max_length=2048)
    client_name: str | None = Field(default=None, max_length=256)
    org: str | None = Field(default=None, max_length=100)
    project: str | None = Field(default=None, max_length=100)


class CallbackBody(_Strict):
    """``POST /api/platform/callback``: the query the platform redirected with."""

    state: str = Field(max_length=256)
    iss: str | None = Field(default=None, max_length=2048)
    code: str | None = Field(default=None, max_length=256)
    error: str | None = Field(default=None, max_length=256)


# ---------------------------------------------------------------------------
# Helpers (also used by routes._add_platform)
# ---------------------------------------------------------------------------


def default_client_name() -> str:
    """The host's name trimmed to ``[A-Za-z0-9._ -]{1,64}`` (the prefilled name).

    Returns
    -------
    str
        :func:`socket.gethostname` with every other character dropped, cut
        to 64; :data:`DEFAULT_CLIENT_NAME` when nothing is left.
    """
    try:
        raw = socket.gethostname()
    except OSError:
        raw = ""
    name = _NAME_JUNK.sub("", raw).strip()[:64].strip()
    return name if name and is_valid_client_name(name) else DEFAULT_CLIENT_NAME


def platform_http(
    state: PortalState,
    platform_origin: str,
    *,
    api_origin: str | None = None,
    token: str | None = None,
) -> PlatformHttp:
    """A :class:`PlatformHttp` over ``state.platform_transport`` (the network by default)."""
    return PlatformHttp(
        platform_origin=platform_origin,
        api_origin=api_origin,
        token=token,
        transport=state.platform_transport,
    )


def revoke_pending(state: PortalState, entry: PendingLink) -> bool:
    """Revoke a pending link's token on the platform, best effort.

    Returns
    -------
    bool
        Whether the platform confirmed it; a failure is logged (never the
        token) and the token then expires unused on the platform.
    """
    try:
        client = platform_http(
            state,
            entry.platform_origin,
            api_origin=entry.api_origin,
            token=entry.token,
        )
    except ValueError:
        return False
    try:
        client.revoke(entry.reply.project.slug)
        return True
    except PlatformError as exc:
        _log.warning("could not revoke a pending platform link: %s", exc)
        return False
    finally:
        client.close()


def sweep_links(state: PortalState) -> None:
    """Revoke the tokens of the pending links that expired or were evicted."""
    for entry in state.pending_links.take_dropped():
        revoke_pending(state, entry)


def origin_identity(root: Path) -> tuple[str, str, str] | None:
    """The :func:`~whygraph.services.github.remote_identity` of ``root``'s ``origin``."""
    try:
        url = Repository(root).origin_url
    except GitError:
        return None
    return remote_identity(strip_userinfo(url)) if url else None


def origin_url(root: Path) -> str | None:
    """``root``'s ``origin`` URL without credentials, or ``None``."""
    try:
        url = Repository(root).origin_url
    except GitError:
        return None
    return strip_userinfo(url) if url else None


def _candidates(state: PortalState, clone_url: str) -> tuple[str, ...]:
    """Discovered checkouts whose ``origin`` names ``clone_url``'s repository."""
    wanted = remote_identity(strip_userinfo(clone_url))
    if wanted is None:
        return ()
    return tuple(
        str(root)
        for root in state.discovery.get(state.shared_folders)
        if origin_identity(root) == wanted
    )


def reconnect_target(
    session: Session,
    org_id: int,
    entry: PendingLink,
    *,
    root: Path | None = None,
) -> Project | None:
    """The linked project ``entry`` *replaces* rather than adds, or ``None``.

    The one rule behind both call sites of plan section 4.11's "reconnect or
    remove": :func:`whygraph.portal.routes._add_platform`, which replaces the
    row, and :func:`get_pending`, which offers it. A pending link reconnects
    an existing project only when it is unambiguously the same link target:

    * the project is this org's, its ``source`` is ``platform`` and its slug
      is the platform's slug,
    * its link row's ``platform_origin``, ``org_slug``, ``remote_slug`` and
      ``clone_url`` all match the reply,
    * and - when ``root`` is given - its ``root`` is that checkout.

    Any other collision (another checkout of the same project, another
    project at this path, the same org and slug on a *different* platform) is
    not a reconnect and stays the refusal it was.

    Parameters
    ----------
    session : Session
        An open portal DB session.
    org_id : int
        The local org.
    entry : PendingLink
        The pending link the wizard is consuming.
    root : Path, optional
        The checkout being linked. Omitted by the picker, which has no path
        yet: the project's own ``root`` is then the path to offer.

    Returns
    -------
    Project or None
        The row to replace (attached to ``session``), or ``None`` to add a
        new project.
    """
    reply = entry.reply
    row = session.exec(
        select(Project).where(
            Project.org_id == org_id,
            Project.slug == reply.project.slug,
            Project.source == "platform",
        )
    ).first()
    if row is None or (root is not None and row.root != str(root)):
        return None
    link = session.get(PlatformLink, row.id)
    if link is None:
        return None
    same = (
        link.platform_origin == entry.platform_origin
        and link.org_slug == reply.org
        and link.remote_slug == reply.project.slug
        and link.clone_url == (reply.project.clone_url or "")
    )
    return row if same else None


def _registered_roots(org_id: int) -> set[str]:
    with get_session() as session:
        return set(
            session.exec(select(Project.root).where(Project.org_id == org_id)).all()
        )


def _redirect_uri(request: Request, state: PortalState) -> str:
    """``http://127.0.0.1:<the browser's port>/connect/callback``.

    The port is the request ``Origin``'s (the Vite dev server's in
    development), which must be one of the portal's own origins; a request
    without ``Origin`` gets the portal's port.
    """
    origins = state.origins
    assert origins is not None  # the guard refuses requests before startup
    origin = request.headers.get("origin")
    if origin is None:
        return f"http://127.0.0.1:{origins.port}{CALLBACK_PATH}"
    if origin not in origins.origins:
        raise ApiError(403, "origin not allowed")
    parts = urlsplit(origin)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


def _same_origin(named: str, computed: str) -> bool:
    """Whether a reply's ``api_origin`` normalizes to the one computed locally."""
    try:
        return parse_platform_url(named, allow_http=True) == computed
    except ValueError:
        return False


def _bad_reply(message: str = "the platform's reply is not acceptable") -> ApiError:
    return ApiError(502, message, code="bad_platform_reply")


def _unreachable(exc: PlatformError) -> ApiError:
    return ApiError(502, str(exc), code="platform_unreachable")


def _link_expired() -> ApiError:
    return ApiError(
        410, "this link expired or was used; connect again", code="link_expired"
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@platform_router.post("/connect")
def post_connect(
    body: ConnectBody, request: Request, access: OrgAccess = Depends(_ADD)
) -> dict:
    """Start a link: check the platform, return its consent URL.

    Returns ``{authorize_url, platform_origin, redirect_uri, known_platform}``;
    ``known_platform`` is false when no project of this portal links to that
    platform yet (the page warns). Errors: ``422 bad_platform_url`` /
    ``bad_client_name`` / ``bad_connect_request``, ``409 update_required``,
    ``502 platform_unreachable`` / ``bad_platform_reply``.
    """
    state = portal_state(request)
    sweep_links(state)
    try:
        platform_origin = parse_platform_url(
            body.platform_url, allow_http=dev_platform_http_enabled()
        )
    except ValueError as exc:
        raise ApiError(422, str(exc), code="bad_platform_url") from exc
    client_name = (
        body.client_name.strip()
        if body.client_name is not None
        else default_client_name()
    )
    if not is_valid_client_name(client_name):
        raise ApiError(
            422,
            "the machine name must be 1-64 characters of letters, digits, '.', '_', "
            "'-' and spaces",
            code="bad_client_name",
        )
    if body.org is not None and not is_valid_org_slug(body.org):
        raise ApiError(422, "not a valid org", code="bad_connect_request")
    if body.project is not None and not is_valid_slug(body.project):
        raise ApiError(422, "not a valid project", code="bad_connect_request")
    redirect_uri = _redirect_uri(request, state)

    client = platform_http(state, platform_origin)
    try:
        client.meta()
    except UpdateRequired as exc:
        raise ApiError(409, str(exc), code="update_required") from exc
    except PlatformUnreachable as exc:
        raise _unreachable(exc) from exc
    except PlatformRefused as exc:
        raise _bad_reply("that address does not answer as a WhyGraph platform") from exc
    finally:
        client.close()

    verifier = secrets.token_urlsafe(48)
    oauth_state = state.pending_connects.put(
        org_id=access.org_id,
        verifier=verifier,
        platform_origin=platform_origin,
        redirect_uri=redirect_uri,
        client_name=client_name,
    )
    params = {
        "redirect_uri": redirect_uri,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
        "state": oauth_state,
        "client_name": client_name,
    }
    if body.org is not None:
        params["org"] = body.org
    if body.project is not None:
        params["project"] = body.project
    with get_session() as session:
        known = (
            session.exec(
                select(PlatformLink.project_id)
                .join(Project, col(Project.id) == col(PlatformLink.project_id))
                .where(
                    Project.org_id == access.org_id,
                    PlatformLink.platform_origin == platform_origin,
                )
            ).first()
            is not None
        )
    return {
        "authorize_url": (
            f"{platform_origin}/connect?{urlencode(params, quote_via=quote)}"
        ),
        "platform_origin": platform_origin,
        "redirect_uri": redirect_uri,
        "known_platform": known,
    }


@platform_router.post("/callback")
def post_callback(
    body: CallbackBody, request: Request, access: OrgAccess = Depends(_ADD)
) -> dict:
    """Finish the consent: check ``iss``, exchange the code; return ``{link_id}``.

    Errors: ``410 connect_expired`` (unknown, used or expired ``state``),
    ``422 issuer_mismatch`` (checked before anything else), ``409
    access_denied`` (Cancel on the platform), ``409 connect_failed`` (another
    ``error``, or the platform refused the code), ``422`` without a code,
    ``502 platform_unreachable`` / ``bad_platform_reply`` (including a reply
    that names an org origin other than the one computed here).
    """
    state = portal_state(request)
    sweep_links(state)
    pending = state.pending_connects.pop(body.state, access.org_id)
    if pending is None:
        raise ApiError(
            410,
            "this connect expired or was already used; connect again",
            code="connect_expired",
        )
    # RFC 9207: the answer must come from the platform this connect went to.
    if body.iss != pending.platform_origin:
        raise ApiError(
            422,
            "the answer came from another platform than the one you connected to",
            code="issuer_mismatch",
        )
    if body.error is not None:
        if body.error == "access_denied":
            raise ApiError(409, "the link was cancelled", code="access_denied")
        raise ApiError(
            409, "the platform did not allow the link", code="connect_failed"
        )
    if not body.code:
        raise ApiError(422, "the platform sent no code", code="connect_failed")

    client = platform_http(state, pending.platform_origin)
    try:
        reply = client.exchange(body.code, pending.verifier, pending.redirect_uri)
    except PlatformUnreachable as exc:
        raise _unreachable(exc) from exc
    except PlatformRefused as exc:
        raise ApiError(
            409, "the platform refused the link; connect again", code="connect_failed"
        ) from exc
    finally:
        client.close()

    project = reply.project
    try:
        api_origin = api_origin_for(pending.platform_origin, reply.org)
    except ValueError as exc:
        raise _bad_reply("the platform named an invalid org") from exc
    entry = PendingLink(
        link_id="",
        org_id=access.org_id,
        platform_origin=pending.platform_origin,
        api_origin=api_origin,
        token=reply.token,
        reply=reply,
        candidates=(),
        expires_at=0.0,
    )
    # The org origin is computed here, never taken from the reply.
    if reply.api_origin is not None and not _same_origin(reply.api_origin, api_origin):
        revoke_pending(state, entry)
        raise _bad_reply("the platform named an unexpected address for its org")
    if not is_valid_slug(project.slug) or not project.clone_url:
        revoke_pending(state, entry)
        raise _bad_reply("the platform's project has no usable slug or clone URL")

    link_id = state.pending_links.put(
        org_id=access.org_id,
        platform_origin=pending.platform_origin,
        api_origin=api_origin,
        token=reply.token,
        reply=reply,
        candidates=_candidates(state, project.clone_url),
    )
    return {"link_id": link_id}


@platform_router.get("/pending/{link_id}")
def get_pending(
    link_id: str, request: Request, access: OrgAccess = Depends(_ADD)
) -> dict:
    """What the wizard's checkout picker shows for a pending link.

    ``{link_id, platform_origin, org, project, clone_url, clone_command,
    slug_taken, reconnect, candidates: [{path, name, match: "origin"}],
    other_repos: [{path, name}]}`` - registered checkouts are left out of
    both lists. ``410 link_expired`` for an unknown or expired link.

    ``reconnect`` is ``{"path": ..., "slug": ...}`` when a project of this
    org is the very same link target (:func:`reconnect_target`, minus the
    path part - the picker has no path yet, so that project's own ``root``
    *is* the path to offer), else ``None``. Its root is one of the
    registered ones, so it is in neither list and the picker offers it as
    its own row.

    ``slug_taken`` is therefore a **genuine** name collision only: the
    platform's slug belongs to a project here that this link cannot
    reconnect (another platform, another org or project there, or a local
    project of that name). The two are mutually exclusive, so the picker
    reads "you may reconnect this" off ``reconnect`` and "that name is
    taken" off ``slug_taken`` without comparing them.
    """
    state = portal_state(request)
    sweep_links(state)
    entry = state.pending_links.get(link_id, access.org_id)
    if entry is None:
        raise _link_expired()
    registered = _registered_roots(access.org_id)
    candidates = [p for p in entry.candidates if p not in registered]
    others = [
        str(root)
        for root in state.discovery.get(state.shared_folders)
        if str(root) not in registered and str(root) not in entry.candidates
    ]
    project = entry.reply.project
    with get_session() as session:
        target = reconnect_target(session, access.org_id, entry)
        reconnect = (
            None if target is None else {"path": target.root, "slug": target.slug}
        )
        slug_taken = reconnect is None and (
            session.exec(
                select(Project.id).where(
                    Project.org_id == access.org_id, Project.slug == project.slug
                )
            ).first()
            is not None
        )
    return {
        "link_id": entry.link_id,
        "platform_origin": entry.platform_origin,
        "org": entry.reply.org,
        "project": project.model_dump(mode="json"),
        "clone_url": project.clone_url,
        "clone_command": f"git clone {project.clone_url}",
        "slug_taken": slug_taken,
        "reconnect": reconnect,
        "candidates": [
            {"path": p, "name": Path(p).name, "match": "origin"} for p in candidates
        ],
        "other_repos": [{"path": p, "name": Path(p).name} for p in others],
    }


@platform_router.delete("/pending/{link_id}")
def delete_pending(
    link_id: str, request: Request, access: OrgAccess = Depends(_ADD)
) -> dict:
    """Abandon a pending link and revoke its token now; ``{"revoked": bool}``.

    ``410 link_expired`` for an unknown or expired link.
    """
    state = portal_state(request)
    sweep_links(state)
    entry = state.pending_links.pop(link_id, access.org_id)
    if entry is None:
        raise _link_expired()
    return {"revoked": revoke_pending(state, entry)}


__all__ = [
    "CALLBACK_PATH",
    "DEFAULT_CLIENT_NAME",
    "default_client_name",
    "origin_identity",
    "origin_url",
    "platform_http",
    "platform_router",
    "reconnect_target",
    "revoke_pending",
    "sweep_links",
]
