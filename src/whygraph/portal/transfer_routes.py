"""Project transfer over REST: ``/api/projects/{slug}/transfer`` (plan 42 section 5.8).

One router, mounted by :mod:`whygraph.portal.app` under
``project_db_access(PROJECT_CONFIGURE, guard=linked_guard("Project transfer"))``:
export, import and removing imported rationale cards are a project admin's
(D2, Q8), and a linked project answers ``403 managed_on_platform`` before
any database is opened.

- ``GET ""``: the Settings section's state (scanned, imported cards, the
  last imports);
- ``GET /export?chats=0|1``: the bundle, as an ``application/zip`` download;
- ``POST /import?dry_run=0|1&apply_config=0|1``: a raw ``application/zip``
  body; the dry run is the preview (D4);
- ``DELETE /rationale``: remove every imported rationale card (Q6).

Export and import are bulk work, so each holds two non-blocking slots, one
per org (:attr:`~whygraph.portal.deps.PortalState.transfer_org`) and two for
the whole portal (:attr:`~whygraph.portal.deps.PortalState.transfer_portal`),
plus the runner's :meth:`~whygraph.portal.runner.ScanRunner.claim_project`
(a removal or an org deletion waits, then refuses). All three are taken in
the worker thread (the claim and ``is_busy`` are blocking calls) and an
export releases them before its download starts streaming (D5). Every
temp file lives under ``<data>/tmp`` (0700, a symlink refused), is 0600 and
is gone when the request ends; :func:`~whygraph.portal.transfer_export.sweep_transfer_tmp`
catches what a crash left behind.

Audit records carry ids, counts, the mode and the bundle's sha256 - never a
note, a chat or a setting.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlmodel import col, select

from whygraph.core.safe_paths import UnsafePathError
from whygraph.db.engine import get_session as project_session
from whygraph.db.models import ProjectImport, RationaleCache
from whygraph.mcp.rationale_cache import remove_imported
from whygraph.serve.errors import ServeError

from . import transfer_bundle as tb
from .audit import audit
from .authz import OrgAccess
from .config_layers import ConfigPolicyError
from .db import get_session as portal_session
from .deps import (
    ApiError,
    BoundProject,
    PortalState,
    bound_project,
    current_org,
    current_user,
    portal_state,
    unsafe_path_error,
)
from .models import Project
from .notes_routes import _can_configure, _names
from .runner import ProjectBusy
from .security import Principal
from .transfer_export import TMP_PREFIX, _tmp_dir, build_export
from .transfer_import import _is_scanned, apply_import

_log = logging.getLogger(__name__)

transfer_router = APIRouter()
"""Mounted under ``project_db_access(PROJECT_CONFIGURE)`` at ``/api/projects/{slug}/transfer``."""

EXPORT_LIMIT = 10
"""Exports per member and org per hour (``429 throttled`` past it)."""

IMPORT_USER_LIMIT = 20
"""Imports (dry runs included) per member and org per hour."""

IMPORT_PROJECT_LIMIT = 20
"""Imports (dry runs included) per project per hour."""

IMPORTS_LISTED = 10
"""How many of the last ``project_import`` rows ``GET`` lists."""

ZIP_TYPE = "application/zip"
"""The one content type an import accepts (``415 bundle_invalid`` otherwise)."""

STREAM_CHUNK = 256 * 1024
"""Bytes per chunk of an export download."""

_PORTAL_SLOT = "portal"
"""The one key of the portal-wide slot."""


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def _audit(
    event: str,
    request: Request,
    access: OrgAccess,
    project: BoundProject,
    uid: str,
    **fields: Any,
) -> None:
    """Write one transfer audit event (ids, counts, mode and sha256 only)."""
    audit(
        event,
        request,
        uid=uid,
        org_id=project.org_id,
        org=access.org_slug,
        project=project.slug,
        **fields,
    )


def _throttled(retry: int, what: str) -> ApiError:
    """``429 throttled`` with ``retry_after`` and ``Retry-After``."""
    return ApiError(
        429,
        f"too many project {what}s; try again in {retry} seconds",
        code="throttled",
        headers={"Retry-After": str(retry)},
        retry_after=retry,
    )


@contextmanager
def _slots(state: PortalState, org_id: int) -> Iterator[None]:
    """Hold the org's transfer slot and one of the portal's, or ``409 busy`` at once."""
    if not state.transfer_org.try_acquire(org_id):
        raise ServeError(
            409,
            "another export or import is running in this organization; "
            "try again when it finishes",
            code="busy",
        )
    try:
        if not state.transfer_portal.try_acquire(_PORTAL_SLOT):
            raise ServeError(
                409,
                "other exports or imports are running; try again shortly",
                code="busy",
            )
        try:
            yield
        finally:
            state.transfer_portal.release(_PORTAL_SLOT)
    finally:
        state.transfer_org.release(org_id)


@contextmanager
def _claimed(state: PortalState, project_id: int) -> Iterator[None]:
    """The runner's claim on the project (``409 busy`` while it is being removed)."""
    try:
        with state.runner.claim_project(project_id):
            yield
    except ProjectBusy as exc:
        raise ServeError(409, str(exc), code="busy") from exc


def _project_row(project_id: int) -> Project:
    """The project's portal row, detached (``404`` if it is gone)."""
    with portal_session() as session:
        row = session.get(Project, project_id)
        if row is None:
            raise ApiError(404, "not found")
        session.expunge(row)
    return row


def _unlink(path: Path) -> None:
    """Remove a temp file; a missing one is fine."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        _log.exception("project transfer: could not remove %s", path)


# ---------------------------------------------------------------------------
# GET - the Settings section's state
# ---------------------------------------------------------------------------


def _counts(text: str) -> dict[str, Any]:
    """A ``project_import.counts`` value as a dict (``{}`` when unreadable)."""
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


@transfer_router.get("")
def transfer_status(request: Request) -> dict:
    """The project's transfer state for the Settings section.

    Returns
    -------
    dict
        ``scanned`` (an import or export needs a finished first scan:
        ``false`` means both answer ``409 transfer_needs_scan``),
        ``imported_rationale`` (rationale cards an import wrote, what
        ``DELETE /rationale`` would remove), ``max_bundle_bytes`` and
        ``imports``: the last :data:`IMPORTS_LISTED` applied imports, newest
        first, each ``{id, imported_at, imported_by, imported_by_name,
        source_label, source_mode, whygraph_version, counts}`` (``counts``
        as in the import result: ``notes``, ``purpose_runs``,
        ``rationale``, ``descriptions``, ``chats``; a cache block carries
        ``partial`` when its batches did not all land).
    """
    project = bound_project(request)
    scanned = _is_scanned(_project_row(project.id))
    with project_session() as session:
        imported = session.exec(
            select(func.count())
            .select_from(RationaleCache)
            .where(col(RationaleCache.import_id).is_not(None))
        ).one()
        rows = session.exec(
            select(ProjectImport)
            .order_by(col(ProjectImport.id).desc())
            .limit(IMPORTS_LISTED)
        ).all()
        imports = [
            {
                "id": row.id,
                "imported_at": row.imported_at,
                "imported_by": row.imported_by,
                "source_label": row.source_label,
                "source_mode": row.source_mode,
                "whygraph_version": row.whygraph_version,
                "counts": _counts(row.counts),
            }
            for row in rows
        ]
    names = _names({i["imported_by"] for i in imports if i["imported_by"]})
    for item in imports:
        item["imported_by_name"] = names.get(item["imported_by"], "Former member")
    return {
        "scanned": scanned,
        "imported_rationale": int(imported),
        "max_bundle_bytes": tb.MAX_BUNDLE_BYTES,
        "imports": imports,
    }


# ---------------------------------------------------------------------------
# GET /export
# ---------------------------------------------------------------------------


def _member_counts(export_counts: Mapping[str, int]) -> dict[str, int]:
    """Rows per JSONL member, keyed without the extension (for the audit)."""
    return {
        name.removesuffix(".jsonl"): int(rows)
        for name, rows in export_counts.items()
        if name.endswith(".jsonl")
    }


def _download(fh: Any) -> Iterator[bytes]:
    """Stream an open export file and close it, whatever happens to the response."""
    try:
        while chunk := fh.read(STREAM_CHUNK):
            yield chunk
    finally:
        fh.close()


@transfer_router.get("/export")
def export_bundle(
    request: Request,
    chats: bool = False,
    access: OrgAccess = Depends(current_org),
    principal: Principal = Depends(current_user),
) -> StreamingResponse:
    """Download the project's bundle (plan 42 section 5.7).

    The bundle is built into a 0600 temp file while the slots and the
    runner's claim are held; all three are released **before** the
    response streams. The file is opened and then unlinked at once, so it
    is gone from disk however the download ends; the open handle is closed
    by the stream.

    Parameters
    ----------
    chats : bool, optional
        ``?chats=1`` carries the caller's own chats (in local mode also the
        sessions with no owner). Default off.

    Returns
    -------
    StreamingResponse
        ``application/zip`` with ``Content-Disposition: attachment;
        filename="whygraph-<slug>-<YYYY-MM-DD>.zip"`` and ``Content-Length``.

    Raises
    ------
    ServeError
        ``429 throttled``; ``409 busy`` (another transfer in the org, two
        in the portal, or the project is being removed); ``409
        transfer_needs_scan`` before the first scan; ``413
        bundle_too_large`` over 100 MiB (export without chats); ``422
        bad_config`` when the settings to export hold anything
        secret-looking.
    ApiError
        ``409 unsafe_path`` when ``<data>/tmp`` is a symlink.
    """
    project = bound_project(request)
    state = portal_state(request)
    retry = state.transfer_export.hit(
        ("u", access.org_id, access.user_id), limit=EXPORT_LIMIT
    )
    if retry is not None:
        raise _throttled(retry, "export")
    with _slots(state, project.org_id), _claimed(state, project.id):
        try:
            export = build_export(
                _project_row(project.id),
                org_slug=access.org_slug,
                uid=principal.uid,
                include_chats=chats,
                mode=state.mode or "local",
                data_dir=state.data_dir,
            )
        except ConfigPolicyError as exc:
            raise ServeError(422, str(exc), code="bad_config") from exc
        except UnsafePathError as exc:
            raise unsafe_path_error(exc) from exc
    try:
        fh = open(export.path, "rb")  # noqa: SIM115 - closed by _download
    finally:
        _unlink(export.path)
    _audit(
        "project_bundle_exported",
        request,
        access,
        project,
        principal.uid,
        include_chats=chats,
        bundle_sha256=export.sha256,
        size=export.size,
        skipped=export.skipped,
        **_member_counts(export.counts),
    )
    return StreamingResponse(
        _download(fh),
        media_type=ZIP_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{export.filename}"',
            "Content-Length": str(export.size),
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# POST /import
# ---------------------------------------------------------------------------


def _upload_too_large() -> ApiError:
    return ApiError(
        413,
        f"the bundle is larger than {tb.MAX_BUNDLE_BYTES} bytes",
        code="body_too_large",
    )


async def _receive(request: Request, data_dir: Path) -> Path:
    """Stream the request body to a 0600 temp file, capped at 100 MiB.

    Returns
    -------
    Path
        ``<data>/tmp/transfer-in-<uuid>.zip``; the caller unlinks it.

    Raises
    ------
    ApiError
        ``400 bad_request`` for a malformed ``Content-Length``; ``413
        body_too_large`` when it, or the bytes actually sent, pass
        :data:`~whygraph.portal.transfer_bundle.MAX_BUNDLE_BYTES` (nothing
        is kept); ``409 unsafe_path`` when ``<data>/tmp`` is a symlink.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isdigit():
            raise ApiError(400, "invalid Content-Length", code="bad_request")
        if int(declared) > tb.MAX_BUNDLE_BYTES:
            raise _upload_too_large()
    try:
        tmp = _tmp_dir(data_dir)
    except UnsafePathError as exc:
        raise unsafe_path_error(exc) from exc
    path = tmp / f"{TMP_PREFIX}in-{uuid.uuid4()}.zip"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > tb.MAX_BUNDLE_BYTES:
                    raise _upload_too_large()
                fh.write(chunk)
    except BaseException:
        _unlink(path)
        raise
    return path


def _content_type(request: Request) -> str:
    """The request's media type, lower case, without parameters."""
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _apply(
    state: PortalState,
    project: BoundProject,
    path: Path,
    *,
    uid: str,
    keep_status: bool,
    apply_config: bool,
    dry_run: bool,
) -> tuple[dict[str, Any], str]:
    """The guarded import, in the worker thread: slots, claim, ``is_busy``, read, apply."""
    with _slots(state, project.org_id), _claimed(state, project.id):
        if state.runner.is_busy(project.id):
            raise ServeError(
                409,
                "a scan or sync is queued or running for this project; "
                "import when it finishes",
                code="busy",
            )
        row = _project_row(project.id)
        with tb.read_bundle(path) as bundle:
            result = apply_import(
                row,
                project,
                bundle,
                uid=uid,
                keep_status=keep_status,
                apply_config=apply_config,
                dry_run=dry_run,
                mode=state.mode,
                state=state,
                data_dir=state.data_dir,
            )
            return result, bundle.sha256


@transfer_router.post("/import")
async def import_bundle(
    request: Request,
    dry_run: bool = False,
    apply_config: bool = False,
    access: OrgAccess = Depends(current_org),
    principal: Principal = Depends(current_user),
) -> dict:
    """Import a bundle into this project, or preview it (plan 42 sections 5.7, 7.2).

    The body is the raw bundle (``Content-Type: application/zip``, at most
    100 MiB), streamed to a 0600 temp file that is removed when the request
    ends. The work runs in a worker thread under the slots, the runner's
    claim and an ``is_busy`` check.

    Parameters
    ----------
    dry_run : bool, optional
        ``?dry_run=1``: do everything and roll it back - the preview. Counts
        towards the throttle like an apply.
    apply_config : bool, optional
        ``?apply_config=1``: save the bundle's settings after the data
        (the bundle wins for every key it carries, Q9). Default off.

    Returns
    -------
    dict
        The ``ImportResult`` of plan 42 section 7.2 (the same shape for the
        dry run and the apply; ``applied`` tells them apart).

    Raises
    ------
    BundleError
        ``415 bundle_invalid`` for another content type; ``400
        bundle_invalid`` / ``bundle_version``; ``413 bundle_too_large``;
        ``409 bundle_wrong_repo``; ``409 transfer_needs_scan``.
    ServeError
        ``429 throttled``; ``409 busy`` (another transfer in the org, two
        in the portal, a scan queued or running, or a removal).
    ApiError
        ``413 body_too_large`` past 100 MiB; ``409 unsafe_path``.
    """
    project = bound_project(request)
    state = portal_state(request)
    if _content_type(request) != ZIP_TYPE:
        raise tb.BundleError(
            415,
            "Invalid bundle: send the bundle file as application/zip.",
            code="bundle_invalid",
        )
    retry = state.transfer_import.hit_all(
        [
            (("u", access.org_id, access.user_id), IMPORT_USER_LIMIT),
            (("p", project.id), IMPORT_PROJECT_LIMIT),
        ]
    )
    if retry is not None:
        raise _throttled(retry, "import")
    keep_status = _can_configure(request)
    path = await _receive(request, state.data_dir)
    try:
        result, sha256 = await anyio.to_thread.run_sync(
            partial(
                _apply,
                state,
                project,
                path,
                uid=principal.uid,
                keep_status=keep_status,
                apply_config=apply_config,
                dry_run=dry_run,
            )
        )
    finally:
        _unlink(path)
    if result["applied"]:
        notes = result["notes"]
        chats = result["chats"]
        _audit(
            "project_bundle_imported",
            request,
            access,
            project,
            principal.uid,
            bundle_sha256=sha256,
            source_mode=result["source"]["mode"],
            config_applied=bool(result["config"]["applied"]),
            notes=sum(notes["inserted"].values()),
            purpose_runs=result["purpose_runs"]["inserted"],
            rationale=result["rationale"]["inserted"],
            descriptions=result["descriptions"]["filled"],
            chat_sessions=chats["sessions"],
            chat_messages=chats["messages"],
            partial=bool(
                result["rationale"].get("partial")
                or result["descriptions"].get("partial")
            ),
        )
    return result


# ---------------------------------------------------------------------------
# DELETE /rationale
# ---------------------------------------------------------------------------


@transfer_router.delete("/rationale")
def remove_imported_rationale(
    request: Request,
    access: OrgAccess = Depends(current_org),
    principal: Principal = Depends(current_user),
) -> dict:
    """Delete every rationale card an import wrote (plan 42 Q6).

    Native cards are kept. Runs under the runner's claim, so a removal of
    the project waits.

    Returns
    -------
    dict
        ``{"removed": n}``.

    Raises
    ------
    ServeError
        ``409 busy`` while the project is being removed.
    """
    project = bound_project(request)
    state = portal_state(request)
    with _claimed(state, project.id), project_session() as session:
        removed = remove_imported(session)
        session.commit()
    _audit(
        "imported_rationale_removed",
        request,
        access,
        project,
        principal.uid,
        count=removed,
    )
    return {"removed": removed}


__all__ = [
    "EXPORT_LIMIT",
    "IMPORT_PROJECT_LIMIT",
    "IMPORT_USER_LIMIT",
    "transfer_router",
]
