"""Agent-activity counters: the in-memory book and its ``agent_call_days`` rows.

M2f-3 plan section 4.10. Every MCP tool / resource / prompt call (local
``/mcp/<slug>``) and every ``/api/v1`` data call (a connected portal) is
counted - cache hits included - through
:func:`whygraph.core.usage.count_agent_call`, which reaches
:meth:`whygraph.portal.usage.PortalUsageSink.count_call`, which adds one to
the :class:`AgentCallBook` on ``PortalState.agent_calls``.

The book is a lock and a :class:`~collections.Counter` keyed ``(org_id,
project_id, day, source, user_id, connection_id, kind)``. The lifespan's
watcher (:mod:`whygraph.portal.app`) wakes every second and calls
:meth:`AgentCallBook.flush` when :meth:`AgentCallBook.due` says so - every
:data:`FLUSH_EVERY_SEC` seconds, or at once above :data:`MAX_KEYS` keys -
and once more after the runner stops. A flush is one batched upsert
(``INSERT ... SELECT ... WHERE EXISTS (project) ON CONFLICT ON CONSTRAINT
uq_agent_call_days_key DO UPDATE``). At most one flush interval of counts is
lost on a crash.

A row stores ids, a UTC day, a source, a code-defined ``kind`` and a count -
never a URI, an argument, a path or a token. Rows are pruned after
:data:`RETENTION_DAYS` days (:func:`prune`) and cascade with the org and the
project.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from .db import get_session
from .models import AgentCallDay, Project

_log = logging.getLogger(__name__)

FLUSH_EVERY_SEC = 30.0
"""How often the book is written to the portal database (at most)."""

MAX_KEYS = 20_000
"""Above this many keys the book is flushed at the next one-second check."""

CHECK_EVERY_SEC = 1.0
"""How often the lifespan's watcher asks :meth:`AgentCallBook.due`."""

RETENTION_DAYS = 400
"""How long ``agent_call_days`` rows are kept (the usage ledger's retention)."""

PRUNE_EVERY_SEC = 24 * 60 * 60
"""How often the counters are pruned past :data:`RETENTION_DAYS` (also at start)."""


class CallKey(NamedTuple):
    """One counter row's identity.

    Attributes
    ----------
    org_id, project_id : int
        The project called.
    day : str
        The UTC day, ``YYYY-MM-DD``.
    source : str
        ``"mcp"`` or ``"agent"``.
    user_id : int or None
        The calling member.
    connection_id : int or None
        The connection token of an ``/api/v1`` call.
    kind : str
        The tool / resource / prompt name, or the ``v1:`` route name.
    """

    org_id: int
    project_id: int
    day: str
    source: str
    user_id: int | None
    connection_id: int | None
    kind: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_UPSERT = text(
    """
    INSERT INTO agent_call_days
        (org_id, project_id, day, source, user_id, connection_id, kind, calls)
    SELECT v.org_id, v.project_id, v.day, v.source, v.user_id, v.connection_id,
           v.kind, v.calls
    FROM unnest(
        CAST(:org_ids AS integer[]),
        CAST(:project_ids AS integer[]),
        CAST(:days AS text[]),
        CAST(:sources AS text[]),
        CAST(:user_ids AS integer[]),
        CAST(:connection_ids AS bigint[]),
        CAST(:kinds AS text[]),
        CAST(:calls AS bigint[])
    ) AS v(org_id, project_id, day, source, user_id, connection_id, kind, calls)
    WHERE EXISTS (
        SELECT 1 FROM projects p WHERE p.id = v.project_id AND p.org_id = v.org_id
    )
    ON CONFLICT ON CONSTRAINT uq_agent_call_days_key
    DO UPDATE SET calls = agent_call_days.calls + excluded.calls
    """
)


def _upsert(batch: dict[CallKey, int]) -> None:
    """Write ``batch`` in one statement (one transaction); raises on failure."""
    keys = list(batch)
    params = {
        "org_ids": [k.org_id for k in keys],
        "project_ids": [k.project_id for k in keys],
        "days": [k.day for k in keys],
        "sources": [k.source for k in keys],
        "user_ids": [k.user_id for k in keys],
        "connection_ids": [k.connection_id for k in keys],
        "kinds": [k.kind for k in keys],
        "calls": [batch[k] for k in keys],
    }
    with get_session() as db:
        db.execute(_UPSERT, params)


def _live_projects(keys: Iterable[CallKey]) -> set[tuple[int, int]]:
    """The ``(org_id, project_id)`` pairs of ``keys`` whose project still exists."""
    ids = {k.project_id for k in keys}
    if not ids:
        return set()
    with get_session() as db:
        rows = db.exec(
            select(Project.org_id, Project.id).where(col(Project.id).in_(ids))
        ).all()
    return {(org_id, project_id) for org_id, project_id in rows}


class AgentCallBook:
    """The in-memory agent-call counters, flushed to ``agent_call_days``.

    Thread-safe: MCP bodies count on the event loop, ``/api/v1``
    dependencies on the event loop or a worker thread, the flush runs on a
    worker thread.

    Parameters
    ----------
    flush_every : float, optional
        Seconds between flushes (:data:`FLUSH_EVERY_SEC`).
    max_keys : int, optional
        The key count above which :meth:`due` answers at once
        (:data:`MAX_KEYS`).
    clock : callable, optional
        Returns the current UTC time (tests).
    """

    def __init__(
        self,
        *,
        flush_every: float = FLUSH_EVERY_SEC,
        max_keys: int = MAX_KEYS,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.flush_every = flush_every
        self.max_keys = max_keys
        self._clock = clock
        self._lock = threading.Lock()
        self._counts: Counter[CallKey] = Counter()
        self._last_flush = time.monotonic()

    def __len__(self) -> int:
        with self._lock:
            return len(self._counts)

    def add(
        self,
        *,
        org_id: int,
        project_id: int,
        source: str,
        user_id: int | None,
        connection_id: int | None,
        kind: str,
    ) -> None:
        """Count one call on today's (UTC) row of its key.

        Parameters
        ----------
        org_id, project_id : int
            The project called.
        source : str
            ``"mcp"`` or ``"agent"``.
        user_id : int or None
            The calling member.
        connection_id : int or None
            The connection token of an ``/api/v1`` call.
        kind : str
            The call's kind (cut to 64 characters, the column's limit).
        """
        key = CallKey(
            org_id,
            project_id,
            self._clock().astimezone(timezone.utc).date().isoformat(),
            source,
            user_id,
            connection_id,
            kind[:64],
        )
        with self._lock:
            self._counts[key] += 1

    def has_any(self, org_id: int) -> bool:
        """Whether a call of ``org_id`` is waiting in the book (not yet flushed).

        Parameters
        ----------
        org_id : int
            The organization.

        Returns
        -------
        bool
            ``True`` when the book holds a count for one of its projects.
        """
        with self._lock:
            return any(key.org_id == org_id for key in self._counts)

    def pending(self, project_id: int) -> list[tuple[CallKey, int]]:
        """The unflushed counts of one project.

        Parameters
        ----------
        project_id : int
            The project.

        Returns
        -------
        list of (CallKey, int)
            Each waiting key and its count.
        """
        with self._lock:
            return [
                (key, calls)
                for key, calls in self._counts.items()
                if key.project_id == project_id
            ]

    def due(self) -> bool:
        """Whether the watcher should flush now.

        Returns
        -------
        bool
            ``True`` above :attr:`max_keys` keys, or when the book holds a
            count and :attr:`flush_every` seconds passed since the last flush
            (or failed flush).
        """
        with self._lock:
            size = len(self._counts)
            elapsed = time.monotonic() - self._last_flush
        if size == 0:
            return False
        return size > self.max_keys or elapsed >= self.flush_every

    def _take(self) -> Counter[CallKey]:
        with self._lock:
            batch, self._counts = self._counts, Counter()
            self._last_flush = time.monotonic()
        return batch

    def _restore(self, batch: Counter[CallKey]) -> None:
        with self._lock:
            self._counts.update(batch)

    def flush(self) -> int:
        """Write every waiting count to ``agent_call_days``; never raises.

        One batched upsert. A project deleted between the statement's
        ``EXISTS`` snapshot and its foreign-key check fails the batch with an
        ``IntegrityError``: the live project ids are then re-read and the
        batch retried once without the vanished ones; what still fails is
        dropped (logged). Any other failure (the database unreachable) puts
        the counts back for the next flush.

        Returns
        -------
        int
            How many keys were written.
        """
        batch = self._take()
        if not batch:
            return 0
        try:
            _upsert(batch)
            return len(batch)
        except IntegrityError:
            _log.info("agent calls: a project vanished during a flush; retrying")
        except Exception:  # noqa: BLE001 -- keep the counts for the next flush
            _log.exception("agent calls: flush failed; keeping %d key(s)", len(batch))
            self._restore(batch)
            return 0
        try:
            live = _live_projects(batch)
            kept = {k: n for k, n in batch.items() if (k.org_id, k.project_id) in live}
            if kept:
                _upsert(kept)
            return len(kept)
        except Exception:  # noqa: BLE001 -- the retry is the last attempt
            _log.exception(
                "agent calls: dropping %d key(s) that would not write", len(batch)
            )
            return 0


def prune(days: int = RETENTION_DAYS, *, now: datetime | None = None) -> int:
    """Delete counter rows older than ``days`` days.

    Parameters
    ----------
    days : int, optional
        The retention, in days (:data:`RETENTION_DAYS`).
    now : datetime, optional
        The current time (tests).

    Returns
    -------
    int
        How many rows were deleted.
    """
    moment = (now or _utcnow()).astimezone(timezone.utc)
    cutoff = (moment - timedelta(days=days)).date().isoformat()
    with get_session() as db:
        result = db.execute(delete(AgentCallDay).where(col(AgentCallDay.day) < cutoff))
        return int(result.rowcount or 0)


__all__ = [
    "CHECK_EVERY_SEC",
    "FLUSH_EVERY_SEC",
    "MAX_KEYS",
    "PRUNE_EVERY_SEC",
    "RETENTION_DAYS",
    "AgentCallBook",
    "CallKey",
    "prune",
]
