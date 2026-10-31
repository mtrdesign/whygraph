"""Project notes over REST: ``/api/projects/{slug}/notes`` (plan 40 section 5.4).

Two routers, mounted by :mod:`whygraph.portal.app` beside the Explorer's
``data_router`` / ``generate_router`` with ``linked_guard("Notes")``:

- :data:`notes_read_router` (``project.read``): the list, ``/summary``,
  ``/for-target`` and one note;
- :data:`notes_write_router` (``project.chat``): propose, confirm, reconfirm,
  retire and the bulk reject of one proposer's agent proposals.

The routes live in ``portal/`` rather than ``serve/`` because they need the
audit log and the portal's ``users`` table (``serve/`` never imports
``portal/``). The domain is :mod:`whygraph.mcp.notes`; this module maps its
:class:`~whygraph.serve.errors.WhyGraphError` codes to statuses, adds display
names and writes the audit events. An audit record carries ids and enums
only, never a note body.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Iterator

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func
from sqlmodel import col, select

from whygraph.db.engine import get_session as project_session
from whygraph.db.models.project_note import ProjectNote
from whygraph.mcp import notes
from whygraph.mcp.targets import resolve_target
from whygraph.serve.errors import ServeError, WhyGraphError

from . import authz
from .audit import audit
from .authz import Action, OrgAccess
from .db import get_session
from .deps import bound_project, current_org
from .models import User

notes_read_router = APIRouter()
"""Note reads; mounted under ``project_db_access(PROJECT_READ)``."""

notes_write_router = APIRouter()
"""Note writes; mounted under ``project_db_access(PROJECT_CHAT)``."""

FORMER_MEMBER = "Former member"
"""The display name of a uid with no ``users`` row (a removed member)."""

ORIGIN_RE = re.compile(r"(ui|mcp|chat:[0-9]{1,18})")
"""What a bulk reject's ``origin`` must match in full."""

_STATUS = {
    "note_not_found": 404,
    "note_state": 409,
    "note_invalid": 400,
    "note_limit": 429,
    "note_needs_second_confirmer": 403,
    "note_no_actor": 500,
}
"""The REST status of each domain error code (plan section 6.4)."""


@contextmanager
def _mapped() -> Iterator[None]:
    """Re-raise a domain :class:`WhyGraphError` as a :class:`ServeError` with its status."""
    try:
        yield
    except ServeError:
        raise
    except WhyGraphError as exc:
        status = _STATUS.get(exc.code or "")
        if status is None:
            raise
        raise ServeError(status, str(exc), code=exc.code) from exc


def _uid(request: Request) -> str | None:
    """The uid of whoever the guard resolved (``None`` if nobody)."""
    principal = request.scope.get("state", {}).get("principal")
    return None if principal is None else principal.uid


def _names(uids: set[str]) -> dict[str, str]:
    """``uid`` -> display name from the portal's ``users`` table."""
    if not uids:
        return {}
    with get_session() as db:
        return dict(
            db.exec(
                select(User.uid, User.display_name).where(col(User.uid).in_(uids))
            ).all()
        )


def _with_names(views: list[dict]) -> list[dict]:
    """Add ``proposed_by_name`` and ``confirmed_by_name`` to each note."""
    uids = {v["proposed_by"] for v in views if v.get("proposed_by")}
    uids |= {v["confirmed_by"] for v in views if v.get("confirmed_by")}
    names = _names(uids)
    for view in views:
        view["proposed_by_name"] = names.get(view.get("proposed_by"), FORMER_MEMBER)
        if view.get("confirmed_by"):
            view["confirmed_by_name"] = names.get(view["confirmed_by"], FORMER_MEMBER)
    return views


def _one(view: dict) -> dict:
    return _with_names([dict(view)])[0]


def _can_configure(request: Request) -> bool:
    """Whether the caller holds ``project.configure`` on the bound project."""
    return authz.project_allowed(bound_project(request).role, Action.PROJECT_CONFIGURE)


def _audit(
    event: str,
    request: Request,
    access: OrgAccess,
    view: dict | None = None,
    **fields: Any,
) -> None:
    """Write one note audit event (ids and enums only, never a body)."""
    project = bound_project(request)
    uid = _uid(request)
    if view is not None:
        fields.update(
            note_id=view["id"],
            source=view.get("source"),
            anchor_kind=(view.get("anchor") or {}).get("kind"),
            self_confirmed=view.get("proposed_by") == uid,
        )
    audit(
        event,
        request,
        uid=uid,
        org_id=project.org_id,
        org=access.org_slug,
        project=project.slug,
        **fields,
    )


class ProposeBody(BaseModel):
    """``POST /notes``; the domain validates every field (``note_invalid``)."""

    model_config = ConfigDict(extra="ignore")

    body: Any = None
    kind: Any = None
    anchor: Any = None
    refs: Any = None
    supersedes_id: Any = None


class ConfirmBody(BaseModel):
    """``POST /notes/{id}/confirm``: an optional edit."""

    model_config = ConfigDict(extra="ignore")

    body: Any = None
    kind: Any = None


class ReconfirmBody(BaseModel):
    """``POST /notes/{id}/reconfirm``: the new range of a changed range note."""

    model_config = ConfigDict(extra="ignore")

    line_start: Any = None
    line_end: Any = None


class RetireBody(BaseModel):
    """``POST /notes/{id}/retire``."""

    model_config = ConfigDict(extra="ignore")

    reason: Any = None


class RejectBody(BaseModel):
    """``POST /notes/reject-agent-proposals``."""

    model_config = ConfigDict(extra="ignore")

    proposed_by: Any = None
    origin: Any = None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@notes_read_router.get("")
def list_notes(
    status: str | None = None,
    anchor_kind: str | None = None,
    path: str | None = None,
    area: str | None = None,
    q: str | None = None,
    attention: bool = False,
    limit: int = 100,
    before_id: int | None = None,
) -> list[dict]:
    """List notes, newest first, each with its ``anchor_state`` and names.

    Parameters
    ----------
    status, anchor_kind, path, area, q : str, optional
        The filters of :func:`whygraph.mcp.notes.list_notes`.
    attention : bool, optional
        The Notes page's "Needs attention" (server-side).
    limit : int, optional
        Page size (default 100, at most 200).
    before_id : int, optional
        Return notes with a smaller id (the next page).

    Returns
    -------
    list of dict
        Notes as :class:`~whygraph.mcp.notes.NoteView` plus
        ``proposed_by_name`` / ``confirmed_by_name``.
    """
    with _mapped():
        views = notes.list_notes(
            status=status,
            anchor_kind=anchor_kind,
            path=path,
            area=area,
            q=q,
            limit=limit,
            before_id=before_id,
            attention=attention,
        )
    return _with_names([dict(v) for v in views])


@notes_read_router.get("/summary")
def notes_summary() -> dict[str, int]:
    """Counts only: ``{proposed, proposed_by_agents, confirmed}``."""
    with project_session() as session:

        def count(*conditions: Any) -> int:
            return int(
                session.exec(
                    select(func.count()).select_from(ProjectNote).where(*conditions)
                ).one()
            )

        proposed = col(ProjectNote.status) == "proposed"
        return {
            "proposed": count(proposed),
            "proposed_by_agents": count(
                proposed, col(ProjectNote.source).in_(notes.AGENT_SOURCES)
            ),
            "confirmed": count(col(ProjectNote.status) == "confirmed"),
        }


@notes_read_router.get("/for-target")
def notes_for_target(
    qualified_name: str | None = None,
    path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    include_proposed: bool = True,
    include_missing: bool = True,
) -> list[dict]:
    """The notes that apply to a symbol, or to a ``path`` + line range.

    Returns
    -------
    list of dict
        As :func:`list_notes`; ``bad_target`` / ``not_indexed`` /
        ``symbol_not_found`` pass through from target resolution.
    """
    with _mapped():
        target = resolve_target(
            path=path,
            line_start=line_start,
            line_end=line_end,
            qualified_name=qualified_name,
        )
        views = notes.notes_for_target(
            target, include_proposed=include_proposed, include_missing=include_missing
        )
    return _with_names([dict(v) for v in views])


@notes_read_router.get("/{note_id}")
def get_note(note_id: int) -> dict:
    """One note of any status, with the ids of the notes that revise it."""
    with _mapped():
        view = dict(notes.get_note(note_id))
    with project_session() as session:
        view["superseded_by_ids"] = list(
            session.exec(
                select(ProjectNote.id)
                .where(ProjectNote.supersedes_id == note_id)
                .order_by(col(ProjectNote.id))
            ).all()
        )
    return _one(view)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


@notes_write_router.post("")
def propose_note(payload: ProposeBody, request: Request, response: Response) -> dict:
    """Propose a note (or, with ``supersedes_id``, a revision of a confirmed one).

    Returns
    -------
    dict
        The note; ``201``, or ``200`` with ``duplicate: true`` when it
        matched an existing note.
    """
    with _mapped():
        view = notes.propose(
            payload.body,
            payload.kind,
            payload.anchor,
            source="human",
            actor_uid=_uid(request),
            origin="ui",
            refs=payload.refs if payload.refs is not None else (),
            supersedes_id=payload.supersedes_id,
        )
    response.status_code = 200 if view.get("duplicate") else 201
    return _one(view)


@notes_write_router.post("/reject-agent-proposals")
def reject_agent_proposals(
    payload: RejectBody,
    request: Request,
    access: OrgAccess = Depends(current_org),
) -> dict[str, int]:
    """Reject one proposer's pending agent proposals from one origin."""
    origin, proposed_by = payload.origin, payload.proposed_by
    if (
        not isinstance(origin, str)
        or ORIGIN_RE.fullmatch(origin) is None
        or not isinstance(proposed_by, str)
        or not proposed_by
    ):
        raise ServeError(
            400,
            "proposed_by and origin (ui, mcp or chat:<id>) are required",
            code="note_invalid",
        )
    with _mapped():
        count = notes.reject_agent_proposals(
            actor_uid=_uid(request), proposed_by=proposed_by, origin=origin
        )
    _audit(
        "note_bulk_rejected",
        request,
        access,
        target=proposed_by,
        origin=origin,
        count=count,
    )
    return {"rejected": count}


@notes_write_router.post("/{note_id}/confirm")
def confirm_note(
    note_id: int,
    request: Request,
    payload: ConfirmBody | None = None,
    access: OrgAccess = Depends(current_org),
) -> dict:
    """Confirm a proposed note, optionally editing its body or kind."""
    payload = payload or ConfirmBody()
    with _mapped():
        view = notes.confirm(
            note_id,
            actor_uid=_uid(request),
            actor_can_configure=_can_configure(request),
            body=payload.body,
            kind=payload.kind,
        )
    _audit("note_confirmed", request, access, view)
    return _one(view)


@notes_write_router.post("/{note_id}/reconfirm")
def reconfirm_note(
    note_id: int,
    request: Request,
    payload: ReconfirmBody | None = None,
    access: OrgAccess = Depends(current_org),
) -> dict:
    """Mark a confirmed note as still true (a range note may move its lines)."""
    payload = payload or ReconfirmBody()
    with _mapped():
        view = notes.reconfirm(
            note_id,
            actor_uid=_uid(request),
            actor_can_configure=_can_configure(request),
            line_start=payload.line_start,
            line_end=payload.line_end,
        )
    _audit("note_reconfirmed", request, access, view)
    return _one(view)


@notes_write_router.post("/{note_id}/retire")
def retire_note(
    note_id: int,
    payload: RetireBody,
    request: Request,
    access: OrgAccess = Depends(current_org),
) -> dict:
    """Retire a note: a proposal as ``rejected``, a confirmed one as ``outdated`` / ``wrong``."""
    with _mapped():
        view = notes.retire(note_id, actor_uid=_uid(request), reason=payload.reason)
    _audit("note_retired", request, access, view, reason=view.get("retire_reason"))
    return _one(view)
