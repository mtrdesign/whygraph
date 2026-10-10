"""The persisted audit log: the background writer, queries, CSV and pruning.

M2f-1 plan section 4.9. :func:`~whygraph.portal.audit.audit` is called from
sync and async code, inside and outside transactions, and sometimes after
the row it names is gone (``org_deleted`` runs after the org's deletion
committed). So it never writes itself: it hands a record to the
:class:`AuditWriter`, a daemon thread that batches inserts into the
``audit_events`` table (up to :data:`BATCH_SIZE` rows or
:data:`BATCH_WAIT_SEC`).

What the table holds:

- **attempts**, not outcomes: a refusal audited inside a transaction that
  rolled back is still a row, with its ``reason``;
- ``org_id`` / ``actor_id`` inserted through ``(SELECT id ... WHERE id =
  :x)``, so an org or a user that is gone becomes ``NULL`` while the slug
  and label snapshots stay;
- every string passed through
  :func:`~whygraph.services.git.credentials.redact_tokens`; no request
  body, no secret;
- at most one ``reader_request`` GET per (actor, org) per hour (every
  refused non-GET is kept).

Rows older than :data:`RETENTION_DAYS` are deleted by :func:`prune`, which
the lifespan runs once a day.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import JSON, Integer, Text, bindparam, delete, func, or_
from sqlalchemy import insert as sa_insert
from sqlmodel import Session, col, select

from whygraph.services.git.credentials import redact_tokens

from .csv_export import csv_cell
from .csv_export import csv_line as _csv_line
from .db import get_session
from .models import AuditEvent, Organization, Project, User

_log = logging.getLogger(__name__)

QUEUE_SIZE = 10_000
"""Records waiting for the writer; past this, new ones are dropped (logged)."""

BATCH_SIZE = 200
"""Most rows one insert writes."""

BATCH_WAIT_SEC = 1.0
"""Longest a record waits for its batch to fill."""

DROP_WARNING_EVERY_SEC = 60.0
"""At most one overflow warning per this many seconds."""

READER_DEDUPE_SEC = 60 * 60
"""Window in which one ``reader_request`` GET per (actor, org) is stored."""

RETENTION_DAYS = 400
"""How long a row is kept (M2f-1 plan section 0.3 #5)."""

PAGE_SIZE = 50
"""Default page of ``GET /api/org/audit``."""

MAX_PAGE_SIZE = 200
"""Largest page a caller may ask for."""

CSV_MAX_ROWS = 50_000
"""Most rows one CSV export streams."""

CSV_COLUMNS = (
    "id",
    "created_at",
    "org",
    "actor",
    "actor_uid",
    "event",
    "target",
    "ip",
    "fields",
)
"""The CSV export's header row."""

_STOP = object()


@dataclass(frozen=True)
class AuditRecord:
    """One event waiting to be written.

    Attributes
    ----------
    created_at : str
        ISO-8601 UTC timestamp of the attempt.
    event : str
        The event name.
    uid : str or None
        The actor's user uid.
    target : str or None
        What the event is about.
    ip : str or None
        The client address.
    org_id : int or None
        The organization's id.
    org : str or None
        The organization's slug (snapshot).
    fields : dict
        More context, not yet redacted.
    """

    created_at: str
    event: str
    uid: str | None = None
    target: str | None = None
    ip: str | None = None
    org_id: int | None = None
    org: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)


def _clean(value: Any) -> Any:
    """``value`` as JSON-safe data with every string redacted."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_tokens(value)
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean(v) for v in value]
    return redact_tokens(str(value))


def _redacted(value: str | None) -> str | None:
    return None if value is None else redact_tokens(str(value))


def _insert_statement():  # noqa: ANN202 -- a SQLAlchemy Insert
    """The FK-safe insert: a gone org / actor becomes ``NULL``.

    ``org_id`` falls back to the org named by the slug when the caller
    passed none (a slug-only caller); an org deleted before the write
    matches neither.
    """
    org = Organization.__table__  # type: ignore[attr-defined]
    users = User.__table__  # type: ignore[attr-defined]
    org_slug = bindparam("b_org_slug", type_=Text)
    return sa_insert(AuditEvent.__table__).values(  # type: ignore[attr-defined]
        created_at=bindparam("b_created_at", type_=Text),
        org_id=func.coalesce(
            select(org.c.id)
            .where(org.c.id == bindparam("b_org_id", type_=Integer))
            .scalar_subquery(),
            select(org.c.id).where(org.c.slug == org_slug).scalar_subquery(),
        ),
        org_slug=org_slug,
        actor_id=select(users.c.id)
        .where(users.c.id == bindparam("b_actor_id", type_=Integer))
        .scalar_subquery(),
        actor_label=bindparam("b_actor_label", type_=Text),
        event=bindparam("b_event", type_=Text),
        target=bindparam("b_target", type_=Text),
        ip=bindparam("b_ip", type_=Text),
        fields=bindparam("b_fields", type_=JSON),
    )


class AuditWriter:
    """A daemon thread that writes queued audit records in batches.

    :meth:`submit` never blocks: past :data:`QUEUE_SIZE` waiting records it
    drops the new one and logs one warning per minute. A failed batch is
    retried row by row, so one bad row never drops the others.

    Parameters
    ----------
    maxsize : int, optional
        The queue bound.
    batch_size : int, optional
        Most rows per insert.
    batch_wait : float, optional
        Longest a record waits for its batch, in seconds.
    """

    def __init__(
        self,
        *,
        maxsize: int = QUEUE_SIZE,
        batch_size: int = BATCH_SIZE,
        batch_wait: float = BATCH_WAIT_SEC,
    ) -> None:
        self._queue: queue.Queue[Any] = queue.Queue(maxsize)
        self._batch_size = batch_size
        self._batch_wait = batch_wait
        self._thread: threading.Thread | None = None
        self._drop_lock = threading.Lock()
        self._dropped = 0
        self._last_drop_warning: float | None = None
        self._reader_seen: dict[tuple[str | None, int | str | None], float] = {}

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Start the thread (once)."""
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="whygraph-audit-writer", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Write everything queued, then end the thread.

        Parameters
        ----------
        timeout : float, optional
            Longest wait for the queue to drain, in seconds.
        """
        thread = self._thread
        if thread is None:
            return
        try:
            self._queue.put(_STOP, timeout=timeout)
        except queue.Full:
            _log.warning("audit writer: queue still full at shutdown")
        thread.join(timeout)
        if thread.is_alive():
            _log.warning("audit writer: did not finish within %.0f s", timeout)
        self._thread = None

    def flush(self, timeout: float = 10.0) -> bool:
        """Block until everything submitted so far is written.

        Parameters
        ----------
        timeout : float, optional
            Longest wait, in seconds.

        Returns
        -------
        bool
            Whether the writer confirmed the flush in time.
        """
        if self._thread is None:
            return False
        done = threading.Event()
        try:
            self._queue.put(done, timeout=timeout)
        except queue.Full:
            return False
        return done.wait(timeout)

    # -- producer side ---------------------------------------------------

    def submit(self, **values: Any) -> bool:
        """Queue one record (the keyword arguments of :class:`AuditRecord`).

        Returns
        -------
        bool
            ``False`` when the queue was full and the record was dropped.
        """
        record = AuditRecord(**values)
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self._note_drop()
            return False
        return True

    def _note_drop(self) -> None:
        now = time.monotonic()
        with self._drop_lock:
            self._dropped += 1
            last = self._last_drop_warning
            if last is not None and now - last < DROP_WARNING_EVERY_SEC:
                return
            dropped, self._dropped = self._dropped, 0
            self._last_drop_warning = now
        _log.warning("audit writer: queue full, dropped %d event(s)", dropped)

    # -- the thread ------------------------------------------------------

    def _run(self) -> None:
        batch: list[AuditRecord] = []
        deadline = 0.0
        while True:
            timeout = max(0.0, deadline - time.monotonic()) if batch else None
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                batch = self._write(batch)
                continue
            if item is _STOP:
                self._write(batch)
                return
            if isinstance(item, threading.Event):
                batch = self._write(batch)
                item.set()
                continue
            if self._duplicate(item):
                continue
            if not batch:
                deadline = time.monotonic() + self._batch_wait
            batch.append(item)
            if len(batch) >= self._batch_size:
                batch = self._write(batch)

    def _duplicate(self, record: AuditRecord) -> bool:
        """Whether ``record`` is a ``reader_request`` GET already stored this hour."""
        if record.event != "reader_request":
            return False
        if str(record.fields.get("method", "")).upper() not in ("GET", "HEAD"):
            return False  # every refused non-GET is kept
        now = time.monotonic()
        if len(self._reader_seen) > 1000:
            self._reader_seen = {
                k: t
                for k, t in self._reader_seen.items()
                if now - t < READER_DEDUPE_SEC
            }
        key = (record.uid, record.org_id if record.org_id is not None else record.org)
        seen = self._reader_seen.get(key)
        if seen is not None and now - seen < READER_DEDUPE_SEC:
            return True
        self._reader_seen[key] = now
        return False

    def _write(self, batch: list[AuditRecord]) -> list[AuditRecord]:
        """Insert ``batch``; never raises. Returns a fresh empty batch."""
        if not batch:
            return []
        try:
            rows = self._rows(batch)
        except Exception:  # noqa: BLE001 -- the writer must survive a DB outage
            _log.exception("audit writer: could not prepare %d event(s)", len(batch))
            return []
        statement = _insert_statement()
        try:
            with get_session() as db:
                db.execute(statement, rows)
        except Exception:  # noqa: BLE001 -- retried row by row below
            _log.warning("audit writer: batch insert failed; retrying row by row")
            for row in rows:
                try:
                    with get_session() as db:
                        db.execute(statement, [row])
                except Exception:  # noqa: BLE001 -- one bad row never drops the rest
                    _log.exception("audit writer: dropped event %s", row["b_event"])
        return []

    @staticmethod
    def _rows(batch: list[AuditRecord]) -> list[dict[str, Any]]:
        """The insert parameters, with actors resolved and strings redacted."""
        uids = {r.uid for r in batch if r.uid}
        actors: dict[str, tuple[int, str | None]] = {}
        if uids:
            with get_session() as db:
                for uid, user_id, login, email in db.exec(
                    select(User.uid, User.id, User.github_login, User.email).where(
                        col(User.uid).in_(uids)
                    )
                ).all():
                    assert user_id is not None
                    actors[uid] = (user_id, f"@{login}" if login else email)
        rows = []
        for r in batch:
            actor_id, label = actors.get(r.uid or "", (None, None))
            rows.append(
                {
                    "b_created_at": r.created_at,
                    "b_org_id": r.org_id,
                    "b_org_slug": _redacted(r.org),
                    "b_actor_id": actor_id,
                    "b_actor_label": _redacted(label),
                    "b_event": r.event,
                    "b_target": _redacted(r.target),
                    "b_ip": _redacted(r.ip),
                    "b_fields": _clean(r.fields),
                }
            )
        return rows


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


def prune(days: int = RETENTION_DAYS, *, now: datetime | None = None) -> int:
    """Delete the rows older than ``days``.

    Parameters
    ----------
    days : int, optional
        The retention, in days.
    now : datetime, optional
        The current time (tests).

    Returns
    -------
    int
        How many rows were deleted.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = (moment - timedelta(days=days)).isoformat(timespec="seconds")
    with get_session() as db:
        result = db.exec(  # type: ignore[call-overload]
            delete(AuditEvent).where(col(AuditEvent.created_at) < cutoff)
        )
        return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditFilter:
    """What a reader asked for.

    Attributes
    ----------
    org_id : int or None
        Only this org's events (``None`` with ``orgless``: none of any org).
    orgless : bool
        Only rows with no org (org-less events and deleted orgs' events).
    org : str or None
        Only rows whose org snapshot is this slug.
    event : str or None
        Only this event name.
    actor : str or None
        Only this actor: a user uid, or a label with or without ``@``.
    since, until : str or None
        ISO-8601 bounds on ``created_at`` (``since`` inclusive, ``until``
        exclusive).
    """

    org_id: int | None = None
    orgless: bool = False
    org: str | None = None
    event: str | None = None
    actor: str | None = None
    since: str | None = None
    until: str | None = None


def parse_bound(raw: str | None, *, end: bool) -> str | None:
    """Turn a ``from`` / ``to`` query value into a ``created_at`` bound.

    A date-only value means the start of that day (``from``) or the start
    of the next one (``to``, so the whole day is included); a date-time
    without an offset is UTC.

    Parameters
    ----------
    raw : str or None
        What the caller sent.
    end : bool
        Whether this is the upper bound.

    Returns
    -------
    str or None
        An ISO-8601 UTC timestamp, comparable with ``created_at``.

    Raises
    ------
    ValueError
        When ``raw`` is not an ISO-8601 date or date-time.
    """
    if raw is None or not raw.strip():
        return None
    value = raw.strip()
    if len(value) == 10:
        day = date.fromisoformat(value)
        moment = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        if end:
            moment += timedelta(days=1)
    else:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(timezone.utc)
        if end:
            moment += timedelta(seconds=1)  # created_at has second resolution
    return moment.isoformat(timespec="seconds")


def make_filter(
    *,
    org_id: int | None = None,
    orgless: bool = False,
    org: str | None = None,
    event: str | None = None,
    actor: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> AuditFilter:
    """Build an :class:`AuditFilter` from query values.

    Parameters
    ----------
    org_id, orgless, org, event, actor
        As on :class:`AuditFilter`; blank strings mean no filter.
    since, until : str, optional
        The raw ``from`` / ``to`` values (:func:`parse_bound`).

    Returns
    -------
    AuditFilter
        The filter.

    Raises
    ------
    ValueError
        When ``since`` or ``until`` is not an ISO-8601 date or date-time.
    """
    return AuditFilter(
        org_id=org_id,
        orgless=orgless,
        org=(org or "").strip() or None,
        event=(event or "").strip() or None,
        actor=(actor or "").strip() or None,
        since=parse_bound(since, end=False),
        until=parse_bound(until, end=True),
    )


def _conditions(f: AuditFilter) -> list:
    conditions: list = []
    if f.orgless:
        conditions.append(col(AuditEvent.org_id).is_(None))
    elif f.org_id is not None:
        conditions.append(col(AuditEvent.org_id) == f.org_id)
    if f.org:
        conditions.append(col(AuditEvent.org_slug) == f.org)
    if f.event:
        conditions.append(col(AuditEvent.event) == f.event)
    if f.actor:
        actor = f.actor.strip()
        label = "@" + actor.removeprefix("@").lower()
        conditions.append(
            or_(
                col(User.uid) == actor,
                func.lower(col(AuditEvent.actor_label)) == actor.lower(),
                func.lower(col(AuditEvent.actor_label)) == label,
            )
        )
    if f.since:
        conditions.append(col(AuditEvent.created_at) >= f.since)
    if f.until:
        conditions.append(col(AuditEvent.created_at) < f.until)
    return conditions


def _target_labels(db: Session, events: list[AuditEvent], org_id: int | None) -> dict:
    """``target`` -> label for the users and projects the events name.

    A user ``uid`` becomes ``Name (@login)`` (``Name`` without a login),
    a project slug of the event's org its name; anything else is absent.
    """
    targets = {e.target for e in events if e.target}
    if not targets:
        return {}
    labels: dict[str, str] = {}
    for uid, name, login in db.exec(
        select(User.uid, User.display_name, User.github_login).where(
            col(User.uid).in_(targets)
        )
    ).all():
        labels[uid] = f"{name} (@{login})" if login else name
    wanted = targets - labels.keys()
    if wanted and org_id is not None:
        for slug, name in db.exec(
            select(Project.slug, Project.name).where(
                Project.org_id == org_id, col(Project.slug).in_(wanted)
            )
        ).all():
            labels[slug] = name
    return labels


def _row(
    event: AuditEvent, actor_uid: str | None, labels: dict | None = None
) -> dict[str, Any]:
    return {
        "id": event.id,
        "created_at": event.created_at,
        "org": event.org_slug,
        "actor": (
            None
            if event.actor_label is None and actor_uid is None
            else {"uid": actor_uid, "label": event.actor_label}
        ),
        "event": event.event,
        "target": event.target,
        "target_label": (labels or {}).get(event.target) if event.target else None,
        "ip": event.ip,
        "fields": event.fields,
    }


def query_events(
    f: AuditFilter, *, before: int | None = None, limit: int = PAGE_SIZE
) -> tuple[list[dict[str, Any]], int | None]:
    """One page of events, newest first (keyset on ``id``).

    Parameters
    ----------
    f : AuditFilter
        The filters.
    before : int, optional
        Only rows with an ``id`` below this (the previous page's ``next``).
    limit : int, optional
        Page size.

    Returns
    -------
    tuple of (list of dict, int or None)
        The rows, and the ``before`` value of the next page (``None`` on
        the last page).
    """
    conditions = _conditions(f)
    if before is not None:
        conditions.append(col(AuditEvent.id) < before)
    with get_session() as db:
        rows = db.exec(
            select(AuditEvent, User.uid)
            .outerjoin(User, col(User.id) == col(AuditEvent.actor_id))
            .where(*conditions)
            .order_by(col(AuditEvent.id).desc())
            .limit(limit + 1)
        ).all()
        page = rows[:limit]
        labels = _target_labels(db, [e for e, _ in page], f.org_id)
        events = [_row(event, uid, labels) for event, uid in page]
    more = len(rows) > limit
    return events, (events[-1]["id"] if more and events else None)


def iter_csv(
    f: AuditFilter, *, max_rows: int = CSV_MAX_ROWS, chunk: int = 1000
) -> Iterator[str]:
    """Stream the filtered events as CSV lines, newest first.

    Parameters
    ----------
    f : AuditFilter
        The filters.
    max_rows : int, optional
        The cap on data rows.
    chunk : int, optional
        Rows read per query.

    Yields
    ------
    str
        The header line, then one line per event.
    """
    yield _csv_line(list(CSV_COLUMNS))
    before: int | None = None
    sent = 0
    while sent < max_rows:
        events, before = query_events(
            f, before=before, limit=min(chunk, max_rows - sent)
        )
        for e in events:
            actor = e["actor"] or {}
            yield _csv_line(
                [
                    e["id"],
                    e["created_at"],
                    e["org"],
                    actor.get("label"),
                    actor.get("uid"),
                    e["event"],
                    e["target"],
                    e["ip"],
                    json.dumps(e["fields"], sort_keys=True),
                ]
            )
        sent += len(events)
        if before is None:
            return


__all__ = [
    "BATCH_SIZE",
    "BATCH_WAIT_SEC",
    "CSV_COLUMNS",
    "CSV_MAX_ROWS",
    "MAX_PAGE_SIZE",
    "PAGE_SIZE",
    "QUEUE_SIZE",
    "RETENTION_DAYS",
    "AuditFilter",
    "AuditRecord",
    "AuditWriter",
    "csv_cell",
    "iter_csv",
    "make_filter",
    "parse_bound",
    "prune",
    "query_events",
]
