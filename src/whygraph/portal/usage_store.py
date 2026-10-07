"""The usage ledger's storage side: the writer thread, the spend book, prices.

M2f-2 plan section 4.6. A metered call in the portal process reaches
:class:`~whygraph.portal.usage.PortalUsageSink`, which prices it and hands
one :class:`UsageRow` to two places:

- the :class:`SpendBook`, synchronously - the in-memory month-to-date spend
  per org, per (org, project) and per (org, user) that budget checks read
  (an O(1) lookup instead of SQL on every request); and
- the :class:`UsageWriter`, a daemon thread on the
  :class:`~whygraph.portal.audit_store.AuditWriter` model that batches the
  rows into ``usage_events`` off the request path.

The book is seeded from the ledger at start-up (:func:`seed_spend_book`)
and is authoritative afterwards: the portal is exactly one process. A full
writer queue drops rows (logged once a minute) but the book has already
counted them, so budgets stay enforced; a crash loses at most the writer's
unflushed second, as for the audit log.

The org price overrides (``price_overrides``) live in memory too, in a
:class:`PriceBook` loaded at start-up and reloaded per org by
:func:`reload_org_prices` whenever an org's overrides change.

Rows older than :data:`RETENTION_DAYS` (and budget alerts older than
:data:`ALERT_RETENTION_MONTHS` months) are deleted by :func:`prune`, which
the lifespan runs once a day in both modes.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Integer,
    Numeric,
    Text,
    bindparam,
    delete,
    exists,
    func,
    select,
)
from sqlalchemy import insert as sa_insert
from sqlmodel import col

from whygraph.services.git.credentials import redact_tokens

from .audit_store import RETENTION_DAYS
from .db import get_session
from .models import (
    BudgetAlert,
    ConnectionToken,
    Organization,
    PriceOverride,
    Project,
    ScanRun,
    UsageEvent,
    User,
)
from .prices import Price, PriceOverrides

_log = logging.getLogger(__name__)

QUEUE_SIZE = 50_000
"""Rows waiting for the writer; past this, new ones are dropped (logged)."""

BATCH_SIZE = 500
"""Most rows one insert writes."""

BATCH_WAIT_SEC = 1.0
"""Longest a row waits for its batch to fill."""

DROP_WARNING_EVERY_SEC = 60.0
"""At most one overflow warning per this many seconds."""

SUBJECT_MAX_CHARS = 200
"""Longest ``subject`` stored (after redaction)."""

ALERT_RETENTION_MONTHS = 13
"""How many months of ``budget_alerts`` :func:`prune` keeps."""

COUNTED_COST_SOURCES: tuple[str, ...] = ("provider", "estimated")
"""Cost sources that count toward spend (``unpriced`` rows never do)."""

_STOP = object()


@dataclass(frozen=True)
class _Call:
    """A function queued to run on the writer thread (:meth:`UsageWriter.call`)."""

    fn: Callable[[], None]


__all__ = [
    "ALERT_RETENTION_MONTHS",
    "BATCH_SIZE",
    "COUNTED_COST_SOURCES",
    "QUEUE_SIZE",
    "RETENTION_DAYS",
    "SUBJECT_MAX_CHARS",
    "PriceBook",
    "SpendBook",
    "SpendTotals",
    "UsageRow",
    "UsageWriter",
    "current_month",
    "load_price_overrides",
    "prune",
    "reload_org_prices",
    "seed_spend_book",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def current_month(now: datetime | None = None) -> str:
    """The calendar month (UTC) as ``"YYYY-MM"``.

    Parameters
    ----------
    now : datetime, optional
        The moment to read (default: now).

    Returns
    -------
    str
        E.g. ``"2026-10"``.
    """
    return (now or _utcnow()).astimezone(timezone.utc).strftime("%Y-%m")


def _month_start(month: str) -> str:
    """The ISO timestamp (seconds, UTC) a ``"YYYY-MM"`` month starts at."""
    return f"{month}-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# The row
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageRow:
    """One ``usage_events`` row waiting to be written (and counted).

    Built by :class:`~whygraph.portal.usage.PortalUsageSink` (and, for a scan
    child's ``usage`` events, by the runner) with the attribution already
    resolved and the cost already computed. Strings are redacted and the
    subject capped by the writer, not here.

    Attributes
    ----------
    org_id : int
        The organization.
    project_id : int or None
        The project.
    project_slug, project_name : str
        Snapshots.
    actor_kind : str
        ``"member"`` or ``"system"``.
    user_id : int or None
        The member who triggered the call; ``None`` for the system actor.
    actor_label : str
        Snapshot: ``"Name (@login)"``, ``"Name"`` or ``"System"``.
    source : str
        ``"scan"``, ``"explorer"``, ``"chat"``, ``"mcp"`` or ``"agent"``.
    task : str
        ``"analyze"``, ``"rationale"`` or ``"chat"``.
    provider, model_requested : str
        The provider tag and the model asked for.
    key_scope : str
        ``"project"``, ``"org"``, ``"environment"`` or ``"none"``.
    cost_source : str
        ``"provider"``, ``"estimated"`` or ``"unpriced"``.
    created_at : str
        ISO-8601 UTC timestamp, seconds.
    scan_run_id, chat_session_id, connection_id : int or None
        The scan run, chat session (project-DB id) or connection token.
    client_name : str or None
        The connection's ``client_name`` (the machine).
    subject : str or None
        A SHA, path or qualified name.
    model_served : str or None
        The model the provider reported.
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens : int or None
        What the provider reported (``None`` = not reported).
    cost_usd : Decimal or None
        The cost; ``None`` for an unpriced row.
    price_version : str or None
        ``"bundled:<as_of>"`` or ``"org:<updated_at>"``; ``None`` for a
        provider-reported or unpriced cost.
    duration_ms : int or None
        How long the provider call took.
    """

    org_id: int
    project_id: int | None
    project_slug: str
    project_name: str
    actor_kind: str
    user_id: int | None
    actor_label: str
    source: str
    task: str
    provider: str
    model_requested: str
    key_scope: str
    cost_source: str
    created_at: str
    scan_run_id: int | None = None
    chat_session_id: int | None = None
    connection_id: int | None = None
    client_name: str | None = None
    subject: str | None = None
    model_served: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: Decimal | None = None
    price_version: str | None = None
    duration_ms: int | None = None

    @property
    def counted(self) -> bool:
        """Whether the row counts toward spend (priced, with a cost)."""
        return self.cost_usd is not None and self.cost_source in COUNTED_COST_SOURCES


# ---------------------------------------------------------------------------
# The spend book
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpendTotals:
    """Month-to-date spend right after a :meth:`SpendBook.add`.

    Attributes
    ----------
    month : str
        The book's month (``"YYYY-MM"``).
    org : Decimal
        The row's org.
    project : Decimal or None
        The row's (org, project); ``None`` when the row has no project.
    user : Decimal or None
        The row's (org, user); ``None`` for the system actor.
    """

    month: str
    org: Decimal
    project: Decimal | None
    user: Decimal | None


_ZERO = Decimal(0)


class SpendBook:
    """Month-to-date LLM spend per org, per (org, project) and per (org, user).

    Seeded from the ledger at start-up (:func:`seed_spend_book`) and
    incremented synchronously by every recorded call, before the writer
    flushes. Calendar months are UTC. Unpriced rows add nothing.

    Parameters
    ----------
    clock : callable, optional
        Returns the current time (tests move it across a month boundary).

    Attributes
    ----------
    on_add : callable or None
        Called after every counted :meth:`add` with the row and its
        :class:`SpendTotals`, outside the book's lock. The budget layer's
        hook (threshold alerts, M2f-2 plan section 4.8); ``None`` until it
        is wired.
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or _utcnow
        self._lock = threading.Lock()
        self._month = current_month(self._clock())
        self._org: dict[int, Decimal] = {}
        self._project: dict[tuple[int, int], Decimal] = {}
        self._user: dict[tuple[int, int], Decimal] = {}
        self.on_add: Callable[[UsageRow, SpendTotals], None] | None = None

    @property
    def month(self) -> str:
        """The month the book currently holds (``"YYYY-MM"``)."""
        return self._month

    def seed(
        self,
        month: str,
        sums: Iterable[tuple[int, int | None, int | None, Decimal]],
    ) -> None:
        """Replace the book's contents with one month's grouped sums.

        Parameters
        ----------
        month : str
            The month the sums are for.
        sums : iterable of (org_id, project_id, user_id, cost)
            One entry per ``(org, project, user)`` group; ``None`` ids
            count toward the org (and the project) only.
        """
        org: dict[int, Decimal] = {}
        project: dict[tuple[int, int], Decimal] = {}
        user: dict[tuple[int, int], Decimal] = {}
        for org_id, project_id, user_id, amount in sums:
            amount = Decimal(amount or 0)
            org[org_id] = org.get(org_id, _ZERO) + amount
            if project_id is not None:
                key = (org_id, project_id)
                project[key] = project.get(key, _ZERO) + amount
            if user_id is not None:
                key = (org_id, user_id)
                user[key] = user.get(key, _ZERO) + amount
        with self._lock:
            self._month = month
            self._org, self._project, self._user = org, project, user

    def add(self, row: UsageRow) -> SpendTotals | None:
        """Count one row; a no-op for an unpriced row or a past month.

        A row of a later month than the book's rolls the book over (the
        previous month's sums are dropped).

        Parameters
        ----------
        row : UsageRow
            The recorded call.

        Returns
        -------
        SpendTotals or None
            The month-to-date totals after the add; ``None`` when nothing
            was counted.
        """
        if not row.counted:
            return None
        assert row.cost_usd is not None
        month = row.created_at[:7]
        with self._lock:
            if month < self._month:
                return None  # a row stamped before a rollover: last month's
            if month > self._month:
                self._month = month
                self._org, self._project, self._user = {}, {}, {}
            amount = row.cost_usd
            org = self._org[row.org_id] = self._org.get(row.org_id, _ZERO) + amount
            project = user = None
            if row.project_id is not None:
                key = (row.org_id, row.project_id)
                project = self._project[key] = self._project.get(key, _ZERO) + amount
            if row.user_id is not None:
                key = (row.org_id, row.user_id)
                user = self._user[key] = self._user.get(key, _ZERO) + amount
            totals = SpendTotals(month=month, org=org, project=project, user=user)
        hook = self.on_add
        if hook is not None:
            try:
                hook(row, totals)
            except Exception:  # noqa: BLE001 -- the book must keep counting
                _log.exception("spend book: the add hook failed")
        return totals

    def spent(
        self,
        org_id: int,
        *,
        project_id: int | None = None,
        user_id: int | None = None,
    ) -> Decimal:
        """Month-to-date spend of an org, or of one project / user in it.

        Parameters
        ----------
        org_id : int
            The organization.
        project_id : int, optional
            Read that project's spend instead of the org's.
        user_id : int, optional
            Read that member's spend instead of the org's.

        Returns
        -------
        Decimal
            The spend; ``0`` when the book holds a month that is not the
            current UTC month (so a new month lifts a stop before any new
            spend).

        Raises
        ------
        ValueError
            If both ``project_id`` and ``user_id`` are given.
        """
        if project_id is not None and user_id is not None:
            raise ValueError("pass project_id or user_id, not both")
        with self._lock:
            if self._month != current_month(self._clock()):
                return _ZERO
            if project_id is not None:
                return self._project.get((org_id, project_id), _ZERO)
            if user_id is not None:
                return self._user.get((org_id, user_id), _ZERO)
            return self._org.get(org_id, _ZERO)

    def spent_by_user(self, org_id: int) -> dict[int, Decimal]:
        """Month-to-date spend of every member of an org who spent this month.

        Parameters
        ----------
        org_id : int
            The organization.

        Returns
        -------
        dict[int, Decimal]
            ``users.id`` -> spend; empty when the book holds a month that is
            not the current UTC month.
        """
        with self._lock:
            if self._month != current_month(self._clock()):
                return {}
            return {
                user_id: amount
                for (org, user_id), amount in self._user.items()
                if org == org_id
            }


def seed_spend_book(book: SpendBook, *, now: datetime | None = None) -> None:
    """Seed ``book`` with this month's priced spend from ``usage_events``.

    One grouped ``SUM(cost_usd)`` over this month's ``provider`` /
    ``estimated`` rows.

    Parameters
    ----------
    book : SpendBook
        The book to fill (its contents are replaced).
    now : datetime, optional
        The current time (tests).

    Raises
    ------
    sqlalchemy.exc.SQLAlchemyError
        When the ledger cannot be read; the caller leaves the portal
        degraded (fail closed).
    """
    month = current_month(now)
    with get_session() as db:
        rows = db.execute(
            select(
                UsageEvent.org_id,
                UsageEvent.project_id,
                UsageEvent.user_id,
                func.sum(UsageEvent.cost_usd),
            )
            .where(
                col(UsageEvent.created_at) >= _month_start(month),
                col(UsageEvent.cost_source).in_(COUNTED_COST_SOURCES),
                col(UsageEvent.cost_usd).is_not(None),
            )
            .group_by(UsageEvent.org_id, UsageEvent.project_id, UsageEvent.user_id)
        ).all()
    book.seed(month, [(r[0], r[1], r[2], Decimal(r[3] or 0)) for r in rows])


# ---------------------------------------------------------------------------
# Org price overrides
# ---------------------------------------------------------------------------


class PriceBook:
    """Every org's price overrides, in memory.

    Loaded at start-up (:func:`load_price_overrides`); an org's map is
    replaced with :func:`reload_org_prices` after its overrides change.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_org: dict[int, PriceOverrides] = {}

    def for_org(self, org_id: int) -> PriceOverrides:
        """The org's overrides (an empty mapping when it has none).

        Parameters
        ----------
        org_id : int
            The organization.

        Returns
        -------
        PriceOverrides
            ``(provider, model) -> (Price, updated_at)``.
        """
        with self._lock:
            return self._by_org.get(org_id, {})

    def replace_all(self, by_org: Mapping[int, PriceOverrides]) -> None:
        """Replace every org's overrides.

        Parameters
        ----------
        by_org : Mapping[int, PriceOverrides]
            The overrides per org id.
        """
        with self._lock:
            self._by_org = dict(by_org)

    def set_org(self, org_id: int, overrides: PriceOverrides) -> None:
        """Replace one org's overrides.

        Parameters
        ----------
        org_id : int
            The organization.
        overrides : PriceOverrides
            Its new overrides (empty to clear them).
        """
        with self._lock:
            if overrides:
                self._by_org[org_id] = overrides
            else:
                self._by_org.pop(org_id, None)


def load_price_overrides(org_id: int | None = None) -> dict[int, PriceOverrides]:
    """Read ``price_overrides`` into :data:`~whygraph.portal.prices.PriceOverrides` maps.

    Parameters
    ----------
    org_id : int, optional
        Read only this org's rows (default: every org's).

    Returns
    -------
    dict[int, PriceOverrides]
        The overrides per org id (an org without rows is absent).
    """
    statement = select(PriceOverride)
    if org_id is not None:
        statement = statement.where(col(PriceOverride.org_id) == org_id)
    by_org: dict[int, dict[tuple[str, str], tuple[Price, str]]] = {}
    with get_session() as db:
        for row in db.execute(statement).scalars():
            by_org.setdefault(row.org_id, {})[(row.provider, row.model)] = (
                Price(
                    input=Decimal(row.input_per_mtok),
                    output=Decimal(row.output_per_mtok),
                    cache_read=None
                    if row.cache_read_per_mtok is None
                    else Decimal(row.cache_read_per_mtok),
                    cache_write=None
                    if row.cache_write_per_mtok is None
                    else Decimal(row.cache_write_per_mtok),
                ),
                row.updated_at,
            )
    return dict(by_org)


def reload_org_prices(book: PriceBook, org_id: int) -> None:
    """Re-read one org's overrides into ``book`` (after a price change).

    Parameters
    ----------
    book : PriceBook
        The portal's price book.
    org_id : int
        The organization whose overrides changed.
    """
    book.set_org(org_id, load_price_overrides(org_id).get(org_id, {}))


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


_COLUMNS: tuple[str, ...] = tuple(UsageRow.__dataclass_fields__)  # type: ignore[attr-defined]
"""The ``usage_events`` columns a row fills, in :class:`UsageRow` order."""

_TEXT_FIELDS = frozenset(
    {
        "project_slug",
        "project_name",
        "actor_label",
        "client_name",
        "subject",
        "provider",
        "model_requested",
        "model_served",
        "price_version",
    }
)
"""Free strings, redacted before they are written."""

_FK_TABLES: dict[str, Any] = {
    "project_id": Project.__table__,  # type: ignore[attr-defined]
    "user_id": User.__table__,  # type: ignore[attr-defined]
    "scan_run_id": ScanRun.__table__,  # type: ignore[attr-defined]
    "connection_id": ConnectionToken.__table__,  # type: ignore[attr-defined]
}
"""Nullable foreign keys: a row gone before the flush becomes ``NULL``."""

_BIG = frozenset(
    {
        "connection_id",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
    }
)


def _bind_type(name: str) -> Any:
    if name == "cost_usd":
        return Numeric(14, 6)
    if name in _BIG:
        return BigInteger
    if name.endswith("_id") or name == "duration_ms":
        return Integer
    return Text


def _insert_statement():  # noqa: ANN202 -- a SQLAlchemy Insert
    """``INSERT ... SELECT ... WHERE EXISTS (the org)``, FK-safe.

    A project / user / run / connection deleted between submit and flush
    becomes ``NULL`` (a scalar subquery that finds nothing); a row whose
    org is gone selects nothing and is dropped without failing the batch.
    """
    org = Organization.__table__  # type: ignore[attr-defined]
    binds = {name: bindparam(f"b_{name}", type_=_bind_type(name)) for name in _COLUMNS}
    values = []
    for name in _COLUMNS:
        table = _FK_TABLES.get(name)
        if table is None:
            values.append(binds[name])
        else:
            values.append(
                select(table.c.id).where(table.c.id == binds[name]).scalar_subquery()
            )
    source = select(*values).where(
        exists(select(org.c.id).where(org.c.id == binds["org_id"]))
    )
    return sa_insert(UsageEvent.__table__).from_select(  # type: ignore[attr-defined]
        list(_COLUMNS), source
    )


def _params(row: UsageRow) -> dict[str, Any]:
    """The insert parameters of one row: strings redacted, subject capped."""
    values = asdict(row)
    for name in _TEXT_FIELDS:
        value = values[name]
        if value is not None:
            values[name] = redact_tokens(str(value))
    if values["subject"] is not None:
        values["subject"] = values["subject"][:SUBJECT_MAX_CHARS]
    return {f"b_{name}": value for name, value in values.items()}


class UsageWriter:
    """A daemon thread that writes queued :class:`UsageRow` s in batches.

    :meth:`submit` never blocks: past the queue bound it drops the new row
    and logs one warning per minute (the :class:`SpendBook` has already
    counted it). A failed batch is retried row by row, so one bad row never
    drops the others.

    Parameters
    ----------
    maxsize : int, optional
        The queue bound.
    batch_size : int, optional
        Most rows per insert.
    batch_wait : float, optional
        Longest a row waits for its batch, in seconds.
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

    @property
    def running(self) -> bool:
        """Whether the thread is started."""
        return self._thread is not None

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Start the thread (once; again after :meth:`stop`)."""
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="whygraph-usage-writer", daemon=True
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
            _log.warning("usage writer: queue still full at shutdown")
        thread.join(timeout)
        if thread.is_alive():
            _log.warning("usage writer: did not finish within %.0f s", timeout)
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
            Whether the writer confirmed the flush in time (``False`` when
            it is not running).
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

    def submit(self, row: UsageRow) -> bool:
        """Queue one row.

        Parameters
        ----------
        row : UsageRow
            The row to write.

        Returns
        -------
        bool
            ``False`` when the queue was full and the row was dropped.
        """
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            self._note_drop()
            return False
        return True

    def call(self, fn: Callable[[], None]) -> bool:
        """Queue ``fn`` to run on the writer thread, after the rows before it.

        The budget layer records its threshold alerts this way (a
        ``budget_alerts`` insert and an audit event, M2f-2 plan section
        4.8), off the request path. ``fn`` must not raise (it is logged
        and swallowed if it does).

        Parameters
        ----------
        fn : callable
            Called with no arguments.

        Returns
        -------
        bool
            ``False`` when the queue was full and ``fn`` was dropped.
        """
        try:
            self._queue.put_nowait(_Call(fn))
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
        _log.warning("usage writer: queue full, dropped %d row(s)", dropped)

    # -- the thread ------------------------------------------------------

    def _run(self) -> None:
        batch: list[UsageRow] = []
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
            if isinstance(item, _Call):
                batch = self._write(batch)  # the rows that led to it first
                try:
                    item.fn()
                except Exception:  # noqa: BLE001 -- the writer must survive anything
                    _log.exception("usage writer: a queued call failed")
                continue
            if not batch:
                deadline = time.monotonic() + self._batch_wait
            batch.append(item)
            if len(batch) >= self._batch_size:
                batch = self._write(batch)

    def _write(self, batch: list[UsageRow]) -> list[UsageRow]:
        """Insert ``batch``; never raises. Returns a fresh empty batch."""
        if not batch:
            return []
        try:
            rows = [_params(row) for row in batch]
            statement = _insert_statement()
        except Exception:  # noqa: BLE001 -- the writer must survive anything
            _log.exception("usage writer: could not prepare %d row(s)", len(batch))
            return []
        try:
            with get_session() as db:
                db.execute(statement, rows)
        except Exception:  # noqa: BLE001 -- retried row by row below
            _log.warning("usage writer: batch insert failed; retrying row by row")
            for row in rows:
                try:
                    with get_session() as db:
                        db.execute(statement, [row])
                except Exception:  # noqa: BLE001 -- one bad row never drops the rest
                    _log.exception(
                        "usage writer: dropped a %s row of org %s",
                        row["b_task"],
                        row["b_org_id"],
                    )
        return []


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


def _months_back(moment: datetime, months: int) -> str:
    """The ``"YYYY-MM"`` month ``months`` calendar months before ``moment``."""
    index = moment.year * 12 + (moment.month - 1) - months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def prune(days: int = RETENTION_DAYS, *, now: datetime | None = None) -> int:
    """Delete ledger rows older than ``days`` and stale budget alerts.

    Budget alerts are kept for :data:`ALERT_RETENTION_MONTHS` months.

    Parameters
    ----------
    days : int, optional
        The ledger's retention, in days (shared with the audit log).
    now : datetime, optional
        The current time (tests).

    Returns
    -------
    int
        How many ``usage_events`` rows were deleted.
    """
    moment = (now or _utcnow()).astimezone(timezone.utc)
    cutoff = (moment - timedelta(days=days)).isoformat(timespec="seconds")
    oldest_month = _months_back(moment, ALERT_RETENTION_MONTHS)
    with get_session() as db:
        result = db.execute(
            delete(UsageEvent).where(col(UsageEvent.created_at) < cutoff)
        )
        db.execute(delete(BudgetAlert).where(col(BudgetAlert.month) < oldest_month))
        return int(result.rowcount or 0)
