"""The GitHub App's webhook: ``POST /github/webhook`` (M2d-2 plan section 4.7).

GitHub delivers the app's events here - ``push``, ``installation``,
``installation_repositories`` and ``repository`` - so a push to a
project's default branch queues a sync within seconds and an uninstalled
app, a removed or deleted repository marks its projects access-lost
(plan section 0.2 #14).

The route lives on the **base host, outside** ``/api`` (plan section 0.2
#8): GitHub sends neither ``Origin`` nor ``X-WhyGraph-Client``, and outside
``/api`` the guard still validates ``Host`` without an exemption. In order:

* ``503`` while the portal is degraded; ``404`` in local mode and on an
  org host; ``503 github_app_not_configured`` without the GitHub App
  client (a guard only: production refuses to start without it);
* the body is capped at :data:`MAX_BODY_BYTES` - ``Content-Length`` first,
  then a counting read of the stream, never an unbounded read - else
  ``413``;
* ``X-Hub-Signature-256`` is required (the legacy SHA-1 header alone is
  not enough) and compared with :func:`hmac.compare_digest` over the whole
  ``sha256=<64 hex>`` value, against the raw body, **before** anything is
  parsed; a missing or wrong one is ``401``, audited ``webhook_rejected``;
* ``X-GitHub-Delivery`` ids are remembered (:class:`DeliveryIds`), so a
  replayed delivery is answered ``202`` and does nothing;
* the work is queued as a background task and the answer is ``202`` at once.

Every handler finds projects by the payload's **numeric** ids, never by a
name (a name can be reused by an unrelated repository). Database and
GitHub calls run in worker threads; a run request goes through
:meth:`~whygraph.portal.runner.ScanRunner.request_sync` like any internal
caller's, so the runner's refusals (a removal or an org deletion holding
the project, lost access, a source the mode does not accept) apply.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlmodel import col, select
from starlette.background import BackgroundTask

from .audit import audit
from .db import get_session
from .github_auth import GitHubUnavailable
from .models import Project
from .runner import (
    REASON_GIT_DENIED,
    REASON_NO_ACCESS,
    REASON_REPO_DELETED,
    REASON_TRACKED_STATE,
    ProjectAccessLost,
    ProjectBusy,
    RunnerUnavailable,
    SourceNotAllowed,
    _clear_access_lost,
    _mark_access_lost,
)

if TYPE_CHECKING:  # pragma: no cover
    from .deps import PortalState

_log = logging.getLogger(__name__)

WEBHOOK_PATH = "/github/webhook"
"""Where the GitHub App delivers its events (the app's webhook URL is ``<base>`` + this)."""

MAX_BODY_BYTES = 5 * 1024 * 1024
"""The largest delivery accepted (GitHub caps payloads at 25 MB; a push is ~8 KB)."""

DELIVERY_IDS_MAX = 10_000
"""How many ``X-GitHub-Delivery`` ids are remembered (the oldest is dropped)."""

HANDLED_EVENTS = frozenset(
    {"push", "installation", "installation_repositories", "repository"}
)
"""Events with a handler; anything else (``ping``, ``github_app_authorization``) is ignored."""

SIGNATURE_HEADER = "x-hub-signature-256"
DELIVERY_HEADER = "x-github-delivery"
EVENT_HEADER = "x-github-event"

_RESTORABLE = (REASON_NO_ACCESS, REASON_GIT_DENIED)
"""Access-lost reasons an installation event may clear (not a deleted repository)."""

_FULL_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_LOGGABLE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class DeliveryIds:
    """The ``X-GitHub-Delivery`` ids seen lately: a bounded, thread-safe set.

    Parameters
    ----------
    max_entries : int
        Capacity; past it the oldest id is forgotten.
    """

    def __init__(self, max_entries: int = DELIVERY_IDS_MAX) -> None:
        self._max = max_entries
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, delivery: str) -> bool:
        """Remember a delivery id.

        Parameters
        ----------
        delivery : str
            The ``X-GitHub-Delivery`` value.

        Returns
        -------
        bool
            ``True`` when it is new, ``False`` for a replay.
        """
        with self._lock:
            if delivery in self._ids:
                return False
            self._ids[delivery] = None
            while len(self._ids) > self._max:
                self._ids.popitem(last=False)
            return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._ids)


def signature_ok(secret: str, body: bytes, header: str | None) -> bool:
    """Whether ``X-Hub-Signature-256`` signs ``body`` with ``secret``.

    Parameters
    ----------
    secret : str
        The app's webhook secret.
    body : bytes
        The raw request body, exactly as received.
    header : str or None
        The header's value.

    Returns
    -------
    bool
        ``True`` only when the header equals ``sha256=`` + the 64-hex HMAC,
        compared in constant time over the whole value (a truncated or
        prefix-only value is a mismatch).
    """
    if not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode("ascii"), header.encode("latin-1"))


webhook_router = APIRouter()
"""``POST /github/webhook``; included before the SPA's catch-all."""


def _answer(
    status: int, body: dict, background: BackgroundTask | None = None
) -> JSONResponse:
    return JSONResponse(body, status_code=status, background=background)


def _loggable(value: str | None) -> str | None:
    """A header value safe to put in the audit log, or ``None``."""
    return value if value and _LOGGABLE.fullmatch(value) else None


async def _read_capped(request: Request) -> bytes | None:
    """The body, or ``None`` once it grows past :data:`MAX_BODY_BYTES`."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@webhook_router.post(WEBHOOK_PATH, include_in_schema=False)
async def github_webhook(request: Request) -> JSONResponse:
    """Verify a GitHub App delivery and queue its work (see the module docstring)."""
    state: PortalState = request.app.state.portal
    if state.degraded:
        return _answer(503, {"error": "portal database unavailable"})
    if state.mode != "production":
        return _answer(404, {"error": "not found"})
    if request.scope.get("state", {}).get("host_kind") != "base":
        return _answer(404, {"error": "not found"})
    app = state.github_app
    if app is None:
        return _answer(
            503,
            {
                "error": "this portal has no GitHub App configured",
                "code": "github_app_not_configured",
            },
        )
    length = request.headers.get("content-length")
    if length is not None:
        try:
            size = int(length)
        except ValueError:
            return _answer(400, {"error": "invalid Content-Length"})
        if size > MAX_BODY_BYTES:
            return _answer(413, {"error": "payload too large"})
    event = request.headers.get(EVENT_HEADER)
    delivery = request.headers.get(DELIVERY_HEADER)
    signature = request.headers.get(SIGNATURE_HEADER)
    if not signature:
        audit(
            "webhook_rejected",
            request,
            reason="missing_signature",
            github_event=_loggable(event),
            delivery=_loggable(delivery),
        )
        return _answer(401, {"error": "missing X-Hub-Signature-256"})
    body = await _read_capped(request)
    if body is None:
        return _answer(413, {"error": "payload too large"})
    if not signature_ok(app.config.webhook_secret, body, signature):
        audit(
            "webhook_rejected",
            request,
            reason="bad_signature",
            github_event=_loggable(event),
            delivery=_loggable(delivery),
        )
        return _answer(401, {"error": "invalid signature"})
    # Verified: from here on the delivery is GitHub's.
    if delivery and not state.webhook_deliveries.add(delivery):
        return _answer(202, {"status": "duplicate"})
    if not event:
        return _answer(400, {"error": "missing X-GitHub-Event"})
    if event not in HANDLED_EVENTS:
        return _answer(202, {"status": "ignored"})
    try:
        payload = json.loads(body)
    except ValueError:
        return _answer(400, {"error": "the body is not JSON"})
    if not isinstance(payload, dict):
        return _answer(400, {"error": "the body is not a JSON object"})
    return _answer(
        202,
        {"status": "queued"},
        BackgroundTask(handle_event, state, event, payload),
    )


# ---------------------------------------------------------------------------
# The events
# ---------------------------------------------------------------------------


async def handle_event(state: PortalState, event: str, payload: dict) -> None:
    """Act on one verified delivery; never raises (failures are logged).

    Parameters
    ----------
    state : PortalState
        The portal state (runner, GitHub App).
    event : str
        ``X-GitHub-Event``.
    payload : dict
        The parsed body.
    """
    try:
        if event == "push":
            await _on_push(state, payload)
        elif event == "installation":
            await anyio.to_thread.run_sync(_on_installation, payload)
        elif event == "installation_repositories":
            await anyio.to_thread.run_sync(_on_installation_repositories, payload)
        elif event == "repository":
            await _on_repository(state, payload)
    except Exception:  # noqa: BLE001 -- a bad delivery must not hurt the portal
        _log.exception("webhook: handling a %s delivery failed", event)


def _id(value: Any) -> int | None:
    """A positive integer id from a payload, else ``None``."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _obj(payload: dict, key: str) -> dict:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _repo_ids(items: Any) -> list[int]:
    """The ids of a payload's repository list (malformed entries skipped)."""
    if not isinstance(items, list):
        return []
    return [
        i for item in items if isinstance(item, dict) and (i := _id(item.get("id")))
    ]


def _branch(value: Any) -> str | None:
    """A usable branch name from a payload, else ``None``."""
    if (
        isinstance(value, str)
        and value
        and not value.startswith("-")
        and len(value) <= 255
    ):
        return value
    return None


async def _on_push(state: PortalState, payload: dict) -> None:
    """A push to the default branch syncs every project of that repository."""
    repo = _obj(payload, "repository")
    repo_id = _id(repo.get("id"))
    branch = _branch(repo.get("default_branch"))
    if repo_id is None or branch is None:
        return
    if (
        payload.get("ref") != f"refs/heads/{branch}"
        or payload.get("deleted") is not False
    ):
        return  # another branch, a tag, or the branch's deletion
    for project_id in await anyio.to_thread.run_sync(_push_targets, repo_id):
        try:
            await state.runner.request_sync(project_id, trigger="push")
        except (ProjectBusy, RunnerUnavailable, ProjectAccessLost, SourceNotAllowed):
            continue  # removed or its org deleted, stopping, access lost


def _push_targets(repo_id: int) -> list[int]:
    """The projects a push to ``repo_id``'s default branch syncs, across orgs.

    Initialized, already scanned (a never-scanned project is left to its
    first, explicit scan), and not access-lost - a project marked only for
    tracking WhyGraph's state is synced, since the push may be the fix.
    A project being removed, or one of an org being deleted, is refused by
    the runner itself (:class:`~whygraph.portal.runner.ProjectBusy`).
    """
    with get_session() as session:
        rows = session.exec(
            select(Project.id, Project.access_lost_at, Project.access_lost_reason)
            .where(Project.github_repo_id == repo_id)
            .where(Project.source == "github")
            .where(col(Project.initialized_at).is_not(None))
            .where(col(Project.last_scanned_head).is_not(None))
            .order_by(col(Project.id))
        ).all()
    return [
        pid
        for pid, lost_at, reason in rows
        if lost_at is None or reason == REASON_TRACKED_STATE
    ]


def _project_ids(*conditions: Any) -> list[int]:
    """The ids of the GitHub projects matching every condition."""
    with get_session() as session:
        return list(
            session.exec(
                select(Project.id)
                .where(Project.source == "github")
                .where(*conditions)
                .order_by(col(Project.id))
            ).all()
        )


def _mark(project_ids: Iterable[int], reason: str) -> None:
    for project_id in project_ids:
        _mark_access_lost(project_id, reason)


def _restore(*conditions: Any) -> None:
    """Clear the access loss of the matching projects (not a deleted repository's)."""
    for project_id in _project_ids(
        col(Project.access_lost_reason).in_(_RESTORABLE), *conditions
    ):
        _clear_access_lost(project_id, keep=REASON_TRACKED_STATE)


def _adopt(repo_ids: list[int], installation_id: int) -> None:
    """Record ``installation_id`` as the one covering these repositories."""
    if not repo_ids:
        return
    with get_session() as session:
        for project in session.exec(
            select(Project)
            .where(Project.source == "github")
            .where(col(Project.github_repo_id).in_(repo_ids))
        ).all():
            if project.github_installation_id != installation_id:
                project.github_installation_id = installation_id
                session.add(project)


def _on_installation(payload: dict) -> None:
    """Uninstalled / suspended: access lost; installed / unsuspended: restored."""
    action = payload.get("action")
    installation_id = _id(_obj(payload, "installation").get("id"))
    if installation_id is None:
        return
    on_installation = Project.github_installation_id == installation_id
    if action in ("deleted", "suspend"):
        _mark(_project_ids(on_installation), REASON_NO_ACCESS)
    elif action in ("created", "unsuspend"):
        # A re-install gets a new id: adopt the repositories it lists.
        repo_ids = _repo_ids(payload.get("repositories"))
        _adopt(repo_ids, installation_id)
        _restore(on_installation)


def _on_installation_repositories(payload: dict) -> None:
    """Repositories removed from / added to an installation."""
    action = payload.get("action")
    installation_id = _id(_obj(payload, "installation").get("id"))
    if installation_id is None:
        return
    if action == "removed":
        repo_ids = _repo_ids(payload.get("repositories_removed"))
        if repo_ids:
            _mark(
                _project_ids(
                    col(Project.github_repo_id).in_(repo_ids),
                    Project.github_installation_id == installation_id,
                ),
                REASON_NO_ACCESS,
            )
    elif action == "added":
        repo_ids = _repo_ids(payload.get("repositories_added"))
        if repo_ids:
            _adopt(repo_ids, installation_id)
            _restore(col(Project.github_repo_id).in_(repo_ids))


async def _on_repository(state: PortalState, payload: dict) -> None:
    """Renamed, transferred, a new default branch, or deleted."""
    action = payload.get("action")
    repo = _obj(payload, "repository")
    repo_id = _id(repo.get("id"))
    if repo_id is None:
        return
    on_repo = Project.github_repo_id == repo_id
    full_name = repo.get("full_name")
    if not isinstance(full_name, str) or not _FULL_NAME.fullmatch(full_name):
        full_name = None
    app = state.github_app
    if action == "renamed" and full_name and app is not None:
        await anyio.to_thread.run_sync(
            _update, repo_id, {"remote_url": f"{app.config.web_url}/{full_name}"}
        )
    elif action == "transferred" and full_name and app is not None:
        try:
            installation_id = await anyio.to_thread.run_sync(
                app.repository_installation, full_name
            )
        except (GitHubUnavailable, ValueError) as exc:
            _log.warning(
                "webhook: transfer of repository %s not followed: %s", repo_id, exc
            )
            return
        fields: dict[str, Any] = {"remote_url": f"{app.config.web_url}/{full_name}"}
        if installation_id is not None:
            fields["github_installation_id"] = installation_id
        await anyio.to_thread.run_sync(_update, repo_id, fields)
        if installation_id is None:
            ids = await anyio.to_thread.run_sync(_project_ids, on_repo)
            await anyio.to_thread.run_sync(_mark, ids, REASON_NO_ACCESS)
    elif action == "edited":
        changes = _obj(payload, "changes")
        branch = _branch(repo.get("default_branch"))
        if "default_branch" in changes and branch is not None:
            await anyio.to_thread.run_sync(_update, repo_id, {"default_branch": branch})
    elif action == "deleted":
        ids = await anyio.to_thread.run_sync(_project_ids, on_repo)
        await anyio.to_thread.run_sync(_mark, ids, REASON_REPO_DELETED)


def _update(repo_id: int, fields: dict[str, Any]) -> None:
    """Set ``fields`` on every GitHub project of ``repo_id``."""
    with get_session() as session:
        for project in session.exec(
            select(Project)
            .where(Project.source == "github")
            .where(Project.github_repo_id == repo_id)
        ).all():
            for name, value in fields.items():
                setattr(project, name, value)
            session.add(project)


__all__ = [
    "DELIVERY_IDS_MAX",
    "HANDLED_EVENTS",
    "MAX_BODY_BYTES",
    "WEBHOOK_PATH",
    "DeliveryIds",
    "handle_event",
    "signature_ok",
    "webhook_router",
]
