"""The platform side of a linked project's MCP calls (M2e step 8).

:class:`LinkedProject` is the one implementation of
:class:`whygraph.core.remote.RemoteProject`: it turns a tool body's
request into an ``/api/v1`` call through :class:`PlatformHttp`, hands back
JSON-ready data, and turns every failure into a
:class:`~whygraph.core.remote.RemoteError` whose message names the fix.

Two things live here rather than in :mod:`whygraph.mcp` on purpose:

* **The credential.** Only this side knows the connection token, the org
  and the project slug on the platform; the MCP tool bodies see seven
  method names and nothing else.
* **The link status.** Every answer and every refusal maps to the link's
  status through :func:`~whygraph.portal.platform_client.link_status_for`
  (the table of plan section 4.11). The mapping is recorded on the
  instance (:attr:`LinkedProject.status`, :attr:`LinkedProject.reason`),
  which is what a tool result's ``platform`` block reports, and - when the
  instance knows its ``project_id`` - persisted to the ``platform_links``
  row by :func:`save_status`, which writes only when the status, the
  platform's name for the project or its head actually changed.

Nothing of the platform's *content* is stored: the row carries the link's
bookkeeping (status, reason, the project's name and its last scanned head)
and nothing else, which is what keeps "no shared data on the laptop"
structural.

Beside the per-call update, :func:`refresh_links` probes every link
(one ``meta`` per distinct platform, then each link's status) - the portal
runs it once at start and then for whatever ``GET /api/projects`` put in
the :class:`LinkRefresh` queue, so a listing never waits on a platform.

Request bodies are built with :mod:`whygraph.api_v1`, so the privacy and
size rules the platform enforces are checked **before** anything leaves
the machine: a hunk whose path or SHA the shared models refuse is dropped
rather than sent.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Sequence, TypeVar

from cryptography.fernet import InvalidToken
from pydantic import ValidationError
from sqlmodel import Session, select

from whygraph.api_v1 import (
    MAX_HUNKS,
    MAX_LIMIT,
    MAX_ORIGINS,
    EvidenceIn,
    HunkIn,
    OriginIn,
    RationaleIn,
    StatusOut,
    TargetIn,
)
from whygraph.core.remote import RemoteError, RemoteNotFound
from whygraph.mcp.targets import Target
from whygraph.services.git import BlameHunk

from .db import get_session
from .models import PlatformLink, Project
from .platform_client import (
    PlatformError,
    PlatformHttp,
    PlatformRefused,
    link_status_for,
)
from .secrets import decrypt

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

NOT_FOUND_CODE = "not_found"
"""The platform's code for an object it does not hold."""

REFRESH_AFTER_SEC = 300
"""How old a link's recorded status may be before ``GET /api/projects``
schedules a refresh of it (plan section 4.11); the listing itself never waits."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _target_in(target: Target) -> TargetIn:
    """The wire form of an already-reduced target (see ``mcp.evidence``)."""
    return TargetIn(
        path=target.path,
        line_start=target.line_start,
        line_end=target.line_end,
        qualified_name=target.qualified_name,
    )


def _hunks_in(hunks: Sequence[BlameHunk]) -> list[HunkIn]:
    """The wire form of the pushed hunks, within the contract's size limits.

    A hunk the shared models refuse - an odd path, an out-of-range line -
    is skipped rather than sent, because the platform would refuse the
    whole request for it.
    """
    out: list[HunkIn] = []
    for hunk in hunks[:MAX_HUNKS]:
        try:
            out.append(
                HunkIn(
                    sha=hunk.sha,
                    origins=[
                        OriginIn(path=o.path, start=o.start, end=o.end)
                        for o in hunk.origins[:MAX_ORIGINS]
                    ],
                )
            )
        except ValidationError:
            logger.debug("skipping a hunk the /api/v1 contract refuses")
    return out


class LinkedProject:
    """A project on a WhyGraph platform, as its linked checkout sees it.

    Parameters
    ----------
    client : PlatformHttp
        Bound to the platform's org origin and carrying the connection
        token.
    slug : str
        The project's slug **on the platform**.
    org : str
        Its org slug there.
    platform_origin : str
        The platform's base origin, for the messages.
    status : str, optional
        The link's last known status (:data:`~whygraph.core.remote.LINK_STATUSES`).
    reason : str or None, optional
        Its last known reason.
    project_id : int or None, optional
        The **local** ``projects.id``. When it is set, every call persists
        what it learned to the project's ``platform_links`` row
        (:func:`save_status`); ``None`` keeps the instance in memory only
        (the tests that drive the tool bodies without a portal database).

    Attributes
    ----------
    url : str
        The org origin - where the project is managed.
    status : str
        The status implied by the most recent call.
    reason : str or None
        Its reason, when the platform gave one.
    """

    def __init__(
        self,
        client: PlatformHttp,
        *,
        slug: str,
        org: str,
        platform_origin: str,
        status: str = "ok",
        reason: str | None = None,
        project_id: int | None = None,
    ) -> None:
        self._client = client
        self.slug = slug
        self.org = org
        self.platform_origin = platform_origin
        self.status = status
        self.reason = reason
        self.project_id = project_id

    @property
    def url(self) -> str:
        """Where the project is managed (the platform's org origin)."""
        return self._client.api_origin or self.platform_origin

    def __repr__(self) -> str:
        return (
            f"LinkedProject({self.org}/{self.slug} on {self.platform_origin}, "
            f"status={self.status})"
        )

    # -- plumbing -----------------------------------------------------------

    def _guard(self, call: Callable[[], _T]) -> _T:
        """Run one platform call, recording what it says about the link.

        The mapping lands on the instance (what a tool result's ``platform``
        block reports) and, when :attr:`project_id` is set, in the project's
        ``platform_links`` row - so the Projects page shows what the agent's
        last call saw without a status loop (plan section 4.11).
        """
        try:
            value = call()
        except PlatformError as exc:
            status, reason = link_status_for(exc)
            self._record(status, reason, None)
            raise self._error(exc, status, reason) from exc
        project = self._client.last_project
        status, reason = link_status_for(project)
        self._record(status, reason, project)
        return value

    def _record(
        self, status: str, reason: str | None, project: StatusOut | None
    ) -> None:
        """Keep ``(status, reason)`` on the instance and in the link row."""
        self.status, self.reason = status, reason
        if self.project_id is not None:
            save_status(self.project_id, status, reason, project)

    def _error(
        self, exc: PlatformError, status: str, reason: str | None
    ) -> RemoteError:
        """The tool-facing error for a failed call: what happened, and the fix."""
        if (
            isinstance(exc, PlatformRefused)
            and exc.code == NOT_FOUND_CODE
            and status == "ok"
        ):
            return RemoteNotFound(str(exc), status=status, reason=reason)
        where = f"{self.org}/{self.slug} on {self.platform_origin}"
        if status == "unreachable":
            message = (
                f"platform unreachable - {self.url} did not answer; until it "
                "does, only this checkout's git history is available"
            )
        elif status == "update_required":
            message = (
                f"update WhyGraph - {self.platform_origin} no longer speaks this "
                "version's API; run the installer again, then `whygraph up`"
            )
        elif status == "removed":
            message = (
                f"{where} was removed on the platform - remove it from this "
                "machine in the WhyGraph portal"
            )
        elif status == "revoked":
            named = f" ({reason})" if reason else ""
            message = (
                f"access to {where} was revoked{named} - reconnect the project "
                "from the WhyGraph portal, or remove it from this machine"
            )
        elif status == "access_lost":
            message = (
                f"the platform can no longer reach {where}'s repository - fix "
                "its access on the platform"
            )
        else:
            message = str(exc)
        return RemoteError(message, status=status, reason=reason)

    @staticmethod
    def _without_project(body: dict) -> dict:
        """The platform's reply without its ``project`` bookkeeping block."""
        return {key: value for key, value in body.items() if key != "project"}

    # -- the RemoteProject surface ------------------------------------------

    def evidence(
        self, target: Target, hunks: Sequence[BlameHunk], limit: int
    ) -> tuple[list[dict], list[str]]:
        """``POST /evidence``: the platform's evidence for the pushed hunks.

        Parameters
        ----------
        target : Target
            Already reduced to what may be sent
            (:func:`whygraph.mcp.evidence.pushed_target`).
        hunks : Sequence[BlameHunk]
            The pushed hunks only.
        limit : int
            Cap on the items, clamped to the contract's maximum.

        Returns
        -------
        tuple[list[dict], list[str]]
            The evidence items and the SHAs its database does not know.

        Raises
        ------
        RemoteError
            The platform could not be reached, or refused.
        """
        body = self._body(
            lambda: EvidenceIn(
                target=_target_in(target),
                hunks=_hunks_in(hunks),
                limit=max(1, min(int(limit), MAX_LIMIT)),
            )
        )
        reply = self._guard(lambda: self._client.evidence(self.slug, body))
        return (
            [item.model_dump(mode="json") for item in reply.evidence],
            list(reply.unknown_shas),
        )

    def rationale(self, target: Target, hunks: Sequence[BlameHunk]) -> dict:
        """``POST /rationale``: a card from the platform's shared cache.

        Raises
        ------
        RemoteError
            The platform could not be reached, or refused (a generation
            limit and a missing key arrive this way too, with the
            platform's own message).
        """
        body = self._body(
            lambda: RationaleIn(target=_target_in(target), hunks=_hunks_in(hunks))
        )
        card = self._guard(lambda: self._client.rationale(self.slug, body))
        return card.model_dump(mode="json")

    def history(self, path: str, limit: int, include_renames: bool) -> list[dict]:
        """``GET /history``: the area-history items for ``path``."""
        data = self._guard(
            lambda: self._client.history(
                self.slug,
                path,
                max(1, min(int(limit), MAX_LIMIT)),
                include_renames,
            )
        )
        items = data.get("evidence")
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def commit(self, sha: str) -> dict:
        """``GET /commits/{sha}``."""
        return self._without_project(
            self._guard(lambda: self._client.commit(self.slug, sha))
        )

    def pr(self, number: int) -> dict:
        """``GET /prs/{number}``."""
        return self._without_project(
            self._guard(lambda: self._client.pr(self.slug, number))
        )

    def issue(self, number: int) -> dict:
        """``GET /issues/{number}``."""
        return self._without_project(
            self._guard(lambda: self._client.issue(self.slug, number))
        )

    def overview(self) -> dict:
        """``GET /overview``."""
        return self._without_project(
            self._guard(lambda: self._client.overview(self.slug))
        )

    def revoke(self) -> None:
        """``DELETE /token``: give up this machine's access (step 9's removal)."""
        self._guard(lambda: self._client.revoke(self.slug))

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def _body(self, build: Callable[[], _T]) -> _T:
        """Build a request body, turning a contract violation into a refusal."""
        try:
            return build()
        except ValidationError as exc:
            raise RemoteError(
                "this target cannot be sent to the platform: "
                f"{exc.error_count()} field(s) the /api/v1 contract refuses",
                status="ok",
                reason="bad_request",
            ) from None


def status_of(outcome: Any) -> tuple[str, str | None]:
    """A platform answer's ``(status, reason)`` for a link (plan section 4.11).

    A thin alias of
    :func:`whygraph.portal.platform_client.link_status_for`, re-exported
    here so step 9's status refresh and this module read the mapping from
    one place.

    Parameters
    ----------
    outcome : Any
        A client exception, a ``StatusOut``, an HTTP status, or ``None``.

    Returns
    -------
    tuple[str, str or None]
        The link status and its reason.
    """
    return link_status_for(outcome)


# ---------------------------------------------------------------------------
# The link row: status, the card's URLs, and the refresh
# ---------------------------------------------------------------------------


def link_block(row: PlatformLink) -> dict[str, Any]:
    """The ``link`` block of a linked project's summary (plan section 4.11).

    Parameters
    ----------
    row : PlatformLink
        The project's link row (detached from its session is fine - only
        its columns are read).

    Returns
    -------
    dict
        ``platform_origin``, ``org``, ``remote_slug``, ``status``,
        ``status_reason``, ``last_platform_head`` and the three deep links
        into the platform's SPA: ``manage_url`` (the project's home),
        ``explorer_url`` and ``chat_url``. All three live on the org host,
        because that is where the project is.
    """
    home = f"{row.api_origin}/p/{row.remote_slug}"
    return {
        "platform_origin": row.platform_origin,
        "org": row.org_slug,
        "remote_slug": row.remote_slug,
        "status": row.status,
        "status_reason": row.status_reason,
        "last_platform_head": row.last_platform_head,
        "explorer_url": f"{home}/explorer",
        "chat_url": f"{home}/chat",
        "manage_url": home,
    }


def save_status(
    project_id: int,
    status: str,
    reason: str | None,
    project: StatusOut | None = None,
) -> bool:
    """Record what a platform answer says about one link (plan section 4.11).

    The row is written **only** when the status, the platform's name for
    the project or its last scanned head changed, so an agent's steady
    stream of MCP calls does not write on every tool call. A rename on the
    platform renames the local project too: a linked project's name follows
    the platform's.

    A ``status`` of ``ok`` stores no ``status_reason``: a per-request code
    (``not_found`` for an unknown commit, ``generation_limited``) is an
    answer about that one request, not a fact about the link.

    Parameters
    ----------
    project_id : int
        The local ``projects.id``.
    status : str
        One of :data:`~whygraph.core.remote.LINK_STATUSES`.
    reason : str or None
        The platform's reason, when it gave one.
    project : StatusOut or None, optional
        The ``project`` block the answer carried, for the name and the head.

    Returns
    -------
    bool
        Whether anything was written.
    """
    reason = None if status == "ok" else reason
    with get_session() as session:
        row = session.get(PlatformLink, project_id)
        if row is None:  # the project was removed while a call was in flight
            return False
        changed = False
        if (row.status, row.status_reason) != (status, reason):
            row.status, row.status_reason = status, reason
            row.status_at = _now()
            changed = True
        if project is not None:
            if project.name and row.remote_name != project.name:
                row.remote_name = project.name
                local = session.get(Project, project_id)
                if local is not None and local.name != project.name:
                    local.name = project.name
                    session.add(local)
                changed = True
            if row.last_platform_head != project.last_scanned_head:
                row.last_platform_head = project.last_scanned_head
                changed = True
        if changed:
            session.add(row)
        return changed


def touch_status(project_id: int) -> None:
    """Mark a link's status as just checked, without changing it.

    Keeps a link that is already ``ok`` (or already ``unreachable``) out of
    the next listing's refresh queue, which :func:`save_status` alone would
    not do: it writes nothing when nothing changed.
    """
    with get_session() as session:
        row = session.get(PlatformLink, project_id)
        if row is not None:
            row.status_at = _now()
            session.add(row)


def due_for_refresh(
    session: Session, org_id: int | None = None, *, after_sec: int = REFRESH_AFTER_SEC
) -> list[int]:
    """The project ids of the links whose recorded status is stale.

    Parameters
    ----------
    session : Session
        An open portal DB session.
    org_id : int or None, optional
        Only the links of this organization's projects; every link when
        ``None``.
    after_sec : int, optional
        How old a status may be (default :data:`REFRESH_AFTER_SEC`).

    Returns
    -------
    list[int]
        Sorted ``projects.id`` values; a row whose ``status_at`` cannot be
        parsed counts as stale.
    """
    statement = select(PlatformLink.project_id, PlatformLink.status_at)
    if org_id is not None:
        statement = statement.join(
            Project,
            Project.id == PlatformLink.project_id,  # type: ignore[arg-type]
        ).where(Project.org_id == org_id)
    cutoff = datetime.now(timezone.utc).timestamp() - after_sec
    due: list[int] = []
    for project_id, status_at in session.exec(statement).all():
        try:
            seen = datetime.fromisoformat(status_at).timestamp()
        except (TypeError, ValueError):
            seen = 0.0
        if seen <= cutoff:
            due.append(project_id)
    return sorted(due)


class LinkRefresh:
    """The link statuses a listing asked to have refreshed (plan section 4.11).

    ``GET /api/projects`` serves each link's **recorded** status and calls
    :meth:`schedule` for the stale ones; the portal's lifespan task drains
    the queue with :func:`refresh_links`. So the listing never waits on a
    platform, and there is no status poll loop - the queue stays empty
    unless someone looked at the Projects page.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._due: set[int] = set()

    def schedule(self, project_id: int) -> None:
        """Ask for ``project_id``'s link to be refreshed."""
        with self._lock:
            self._due.add(project_id)

    def take(self) -> list[int]:
        """Return and clear what is queued (sorted)."""
        with self._lock:
            due, self._due = sorted(self._due), set()
        return due

    def pending(self) -> list[int]:
        """What is queued, without clearing it (tests, logs)."""
        with self._lock:
            return sorted(self._due)


def refresh_links(
    *, project_ids: Sequence[int] | None = None, transport: Any = None
) -> int:
    """Probe each linked project's platform and record what it says.

    One ``GET /api/v1/meta`` per distinct ``platform_origin`` (the version
    check, no credentials), then one ``GET /api/v1/projects/{slug}`` per
    link. A platform whose ``meta`` fails decides its links' status without
    a second request: ``update_required`` for an incompatible version,
    ``unreachable`` otherwise.

    Blocking: the portal runs it on a worker thread. It never raises - a
    link that cannot be probed is recorded, not propagated.

    Parameters
    ----------
    project_ids : Sequence[int] or None, optional
        Only these projects' links; every link when ``None``.
    transport : httpx.BaseTransport, optional
        Replaces the network (tests plug a fake platform in).

    Returns
    -------
    int
        How many links were probed.
    """
    with get_session() as session:
        rows = list(session.exec(_rows_statement(project_ids)).all())
        for row in rows:
            session.expunge(row)
    if not rows:
        return 0
    verdicts = {
        origin: _meta_verdict(origin, transport)
        for origin in sorted({row.platform_origin for row in rows})
    }
    for row in rows:
        verdict = verdicts[row.platform_origin]
        if verdict is None:
            _refresh_one(row, transport)
        else:
            save_status(row.project_id, *verdict)
            touch_status(row.project_id)
    return len(rows)


def revoke_token(row: PlatformLink, *, transport: Any = None) -> str | None:
    """Give up a connection token on its platform, best effort.

    ``DELETE /api/v1/projects/{slug}/token``, so the token this machine
    holds stops working even when the row that holds it is about to be
    deleted or replaced here. Used by "Remove from this machine" and by a
    reconnect, which revokes the token it supersedes (plan section 4.11).

    Parameters
    ----------
    row : PlatformLink
        The link whose token to revoke (detached from its session is fine).
    transport : httpx.BaseTransport, optional
        Replaces the network (tests plug a fake platform in).

    Returns
    -------
    str or None
        ``None`` when the platform confirmed it, else why it could not be
        done - a message for the user, free of the token.
    """
    try:
        token: str | None = decrypt(row.token_ciphertext)
    except InvalidToken:
        return "this portal can no longer read the token it held"
    try:
        client = PlatformHttp(
            platform_origin=row.platform_origin,
            api_origin=row.api_origin,
            token=token,
            transport=transport,
        )
    except ValueError as exc:
        return str(exc)
    try:
        client.revoke(row.remote_slug)
    except PlatformError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 -- a removal must still finish
        # Only the kind of failure: an unexpected exception's message has
        # not been through the client's redaction.
        logger.exception("revoking the token of project %s failed", row.project_id)
        return f"the platform could not be reached ({type(exc).__name__})"
    finally:
        client.close()
    return None


def _rows_statement(project_ids: Sequence[int] | None):
    """The ``select`` of the link rows to refresh."""
    statement = select(PlatformLink)
    if project_ids is None:
        return statement
    return statement.where(
        PlatformLink.project_id.in_(tuple(project_ids))  # type: ignore[attr-defined]
    )


def _meta_verdict(
    platform_origin: str, transport: Any
) -> tuple[str, str | None] | None:
    """``None`` when the platform's ``meta`` is fine, else its links' status."""
    try:
        client = PlatformHttp(platform_origin=platform_origin, transport=transport)
    except ValueError:
        # A stored origin the current settings no longer accept (an `http`
        # platform without the development switch): nothing can be asked.
        return "unreachable", None
    try:
        client.meta()
    except PlatformError as exc:
        return link_status_for(exc)
    except Exception:  # noqa: BLE001 -- a probe must never take the portal down
        logger.exception("the meta probe of %s failed", platform_origin)
        return "unreachable", None
    finally:
        client.close()
    return None


def _refresh_one(row: PlatformLink, transport: Any) -> None:
    """Ask one platform for one project's status and record the answer."""
    try:
        token: str | None = decrypt(row.token_ciphertext)
    except InvalidToken:
        token = None
    try:
        client = PlatformHttp(
            platform_origin=row.platform_origin,
            api_origin=row.api_origin,
            token=token,
            transport=transport,
        )
    except ValueError:
        save_status(row.project_id, "unreachable", None)
        touch_status(row.project_id)
        return
    try:
        project = client.status(row.remote_slug)
    except PlatformError as exc:
        save_status(row.project_id, *link_status_for(exc))
    except Exception:  # noqa: BLE001 -- a probe must never take the portal down
        logger.exception("the status probe of project %s failed", row.project_id)
        save_status(row.project_id, "unreachable", None)
    else:
        status, reason = link_status_for(project)
        save_status(row.project_id, status, reason, project)
    finally:
        client.close()
    touch_status(row.project_id)


__all__ = [
    "NOT_FOUND_CODE",
    "REFRESH_AFTER_SEC",
    "LinkRefresh",
    "LinkedProject",
    "due_for_refresh",
    "link_block",
    "refresh_links",
    "revoke_token",
    "save_status",
    "status_of",
    "touch_status",
]
