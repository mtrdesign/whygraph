"""The portal's daily release check: the switch, the book and the loop's work.

Plan 43 sections 4.2 and 4.4. The lifespan's watcher (``_check_for_updates``
in :mod:`whygraph.portal.app`) wakes every :data:`TICK_SEC` seconds, resolves
the switch (:func:`read_switch`), hands it to the :class:`UpdateBook` on
``PortalState.updates`` and, when a check is due, runs :func:`fetch_into_book`
on a worker thread that it abandons on cancel. A check is due at start, then
:data:`CHECK_EVERY_SEC` after a success and :data:`RETRY_EVERY_SEC` after a
failure.

The book keeps only what :func:`whygraph.core.releases.fetch_releases`
returns - validated versions, publish times and one boolean per release -
and :meth:`UpdateBook.view` resolves it on read, so the one-hour grace
(:data:`RELEASE_GRACE_SEC`) needs no extra fetch. A generation counter makes
a fetch that was in flight when the switch flipped land nowhere.

The switch, first match wins: ``DO_NOT_TRACK`` truthy, then
``WHYGRAPH_UPDATE_CHECK``, then (local mode) ``settings.update_check``, then
on (:func:`check_switch`).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from whygraph.core.releases import (
    NOTES_URL,
    Release,
    ReleaseCheckError,
    build_client,
    fetch_releases,
    format_version,
    resolve,
    running_version,
)

from .db import get_session
from .models import Setting

if TYPE_CHECKING:
    from .deps import PortalState

__all__ = [
    "CHECK_EVERY_SEC",
    "ENV_VAR",
    "RELEASE_GRACE_SEC",
    "RETRY_EVERY_SEC",
    "TICK_SEC",
    "UPGRADING_URL",
    "USER_AGENT",
    "UpdateBook",
    "check_switch",
    "fetch_into_book",
    "read_switch",
    "update_block",
]

_log = logging.getLogger(__name__)

TICK_SEC = 15 * 60
"""How often the loop wakes to re-read the switch and ask :meth:`UpdateBook.due`."""

CHECK_EVERY_SEC = 24 * 3600
"""How long after a successful check the next one is due."""

RETRY_EVERY_SEC = 6 * 3600
"""How long after a failed check the next one is due."""

RELEASE_GRACE_SEC = 3600
"""How long after publishing a release is announced (its image is built by then)."""

USER_AGENT = "whygraph-portal"
"""The ``User-Agent`` of the portal's release check (never a version)."""

UPGRADING_URL = "https://mtrdesign.github.io/whygraph/portal/upgrading/"
"""The upgrade guide, linked from every notice."""

ENV_VAR = "WHYGRAPH_UPDATE_CHECK"
"""The environment switch (second in precedence, after ``DO_NOT_TRACK``)."""

_DO_NOT_TRACK = "DO_NOT_TRACK"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})
_warned: set[str] = set()
_warned_lock = threading.Lock()


def _parse_flag(name: str, raw: str | None) -> bool | None:
    """A boolean environment value, or ``None`` when unset, empty or unknown."""
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        return None
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    with _warned_lock:
        first = name not in _warned
        _warned.add(name)
    if first:
        _log.warning("ignoring %s: not a boolean value", name)
    return None


def check_switch(
    mode: str | None, setting_on: bool, env: Mapping[str, str] | None = None
) -> tuple[bool, str]:
    """Resolve whether the release check is on, and why.

    Parameters
    ----------
    mode : str or None
        The portal mode; only ``"local"`` consults ``setting_on``.
    setting_on : bool
        ``settings.update_check`` (local mode's Settings switch).
    env : Mapping[str, str], optional
        The environment to read; ``os.environ`` (read at call time) when
        omitted.

    Returns
    -------
    tuple of (bool, str)
        ``(enabled, source)``. ``source`` is ``"do_not_track"`` (a truthy
        ``DO_NOT_TRACK`` turns it off), ``"env"`` (``WHYGRAPH_UPDATE_CHECK``
        set to a boolean), ``"setting"`` (local mode's switch) or
        ``"default"`` (on).

    Notes
    -----
    Truthy values are ``1``, ``true``, ``yes``, ``on``; falsy ones ``0``,
    ``false``, ``no``, ``off`` (case-insensitive, stripped). An unknown
    value is ignored and logged once per variable. A falsy ``DO_NOT_TRACK``
    does not count.
    """
    source = os.environ if env is None else env
    if _parse_flag(_DO_NOT_TRACK, source.get(_DO_NOT_TRACK)) is True:
        return (False, "do_not_track")
    explicit = _parse_flag(ENV_VAR, source.get(ENV_VAR))
    if explicit is not None:
        return (explicit, "env")
    if mode == "local":
        return (bool(setting_on), "setting")
    return (True, "default")


def _iso_utc(moment: datetime) -> str:
    """``moment`` as ISO-8601 UTC with a ``Z``, to the second."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class UpdateBook:
    """What the release check knows, shared by the loop and request threads.

    Thread-safe (one lock). Scheduling uses ``time.monotonic()`` values and
    ``checked_at`` is wall-clock UTC; the two are never mixed.

    Attributes
    ----------
    generation : int
        Bumped whenever the switch's enabled flag changes; a result for an
        older generation is dropped.
    releases : tuple of Release
        The last fetched release list (kept across a failed check).
    checked_at : datetime or None
        When the last check (success or failure) finished, UTC.
    error : str or None
        The last check's failure kind (``"network"``, ``"rate_limited"``,
        ``"bad_response"``), ``None`` after a success.
    next_due : float
        The ``time.monotonic()`` value from which a check is due (``0``:
        now).
    switch : tuple of (bool, str) or None
        The last resolved switch (:func:`check_switch`); ``None`` until the
        loop's first tick.
    switch_epoch : int
        Bumped by every :meth:`set_switch`, so the loop can tell that a PUT
        stored a newer switch while it was reading the setting.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.generation = 0
        self.releases: tuple[Release, ...] = ()
        self.checked_at: datetime | None = None
        self.error: str | None = None
        self.next_due = 0.0
        self.switch: tuple[bool, str] | None = None
        self.switch_epoch = 0

    def epoch(self) -> int:
        """Return the switch epoch to pass to :meth:`set_switch` as ``if_epoch``."""
        with self._lock:
            return self.switch_epoch

    def due(self, now_mono: float) -> bool:
        """Whether a check is due at ``now_mono`` (a ``time.monotonic()`` value)."""
        with self._lock:
            return now_mono >= self.next_due

    def begin(self) -> int:
        """Return the generation to pass to :meth:`record` / :meth:`fail` after a fetch."""
        with self._lock:
            return self.generation

    def record(
        self,
        gen: int,
        releases: Iterable[Release],
        now_mono: float,
        now_utc: datetime,
    ) -> None:
        """Keep a successful check's releases; the next is due in a day.

        Parameters
        ----------
        gen : int
            The generation :meth:`begin` returned before the fetch; a stale
            one makes this a no-op.
        releases : iterable of Release
            What :func:`whygraph.core.releases.fetch_releases` returned.
        now_mono : float
            ``time.monotonic()`` when the fetch ended.
        now_utc : datetime
            Wall-clock UTC when the fetch ended.
        """
        kept = tuple(releases)
        with self._lock:
            if gen != self.generation:
                return
            self.releases = kept
            self.checked_at = now_utc
            self.error = None
            self.next_due = now_mono + CHECK_EVERY_SEC

    def fail(self, gen: int, kind: str, now_mono: float, now_utc: datetime) -> None:
        """Note a failed check, keeping the earlier releases; retry in six hours.

        Parameters
        ----------
        gen : int
            The generation :meth:`begin` returned; a stale one makes this a
            no-op.
        kind : str
            The :class:`~whygraph.core.releases.ReleaseCheckError` kind.
        now_mono : float
            ``time.monotonic()`` when the fetch ended.
        now_utc : datetime
            Wall-clock UTC when the fetch ended.
        """
        with self._lock:
            if gen != self.generation:
                return
            self.checked_at = now_utc
            self.error = kind
            self.next_due = now_mono + RETRY_EVERY_SEC

    def set_switch(
        self, switch: tuple[bool, str], *, if_epoch: int | None = None
    ) -> bool:
        """Store the resolved switch.

        When the enabled flag changes (or on the first call), the generation
        is bumped, the releases, error and ``checked_at`` are cleared and a
        check is due at once - so turning the check off empties the book,
        a fetch in flight cannot refill it, and turning it back on checks
        at the next tick.

        Parameters
        ----------
        switch : tuple of (bool, str)
            ``(enabled, source)`` from :func:`check_switch`.
        if_epoch : int, optional
            Store only if :attr:`switch_epoch` still equals this (the loop
            passes what :meth:`epoch` returned before it read the setting, so
            a switch a PUT stored meanwhile is not overwritten by a stale one).

        Returns
        -------
        bool
            Whether the switch was stored.
        """
        with self._lock:
            if if_epoch is not None and if_epoch != self.switch_epoch:
                return False
            self.switch_epoch += 1
            previous = self.switch
            self.switch = switch
            if previous is not None and previous[0] == switch[0]:
                return True
            self.generation += 1
            self.releases = ()
            self.checked_at = None
            self.error = None
            self.next_due = 0.0
            return True

    def view(
        self, current: tuple[int, int, int] | None, now_utc: datetime
    ) -> dict[str, Any] | None:
        """The ``update`` block of the wire shape (plan 43 section 5).

        Parameters
        ----------
        current : tuple of int or None
            The running version (:func:`whygraph.core.releases.running_version`).
        now_utc : datetime
            Now (timezone-aware), for the release grace.

        Returns
        -------
        dict or None
            ``None`` while the switch is unknown (before the loop's first
            tick). Otherwise ``enabled``, ``source``, ``current``,
            ``latest``, ``available``, ``manual_steps``, ``notes_url``,
            ``upgrading_url``, ``checked_at`` and ``error``. With the check
            off, ``latest`` / ``notes_url`` are ``None`` and ``available`` /
            ``manual_steps`` are false.
        """
        with self._lock:
            switch = self.switch
            releases = self.releases
            checked_at = self.checked_at
            error = self.error
        if switch is None:
            return None
        enabled, source = switch
        latest: str | None = None
        notes_url: str | None = None
        available = False
        manual_steps = False
        if enabled:
            resolution = resolve(
                releases, current, now=now_utc, grace_sec=RELEASE_GRACE_SEC
            )
            if resolution.target is not None:
                latest = format_version(resolution.target.version)
                notes_url = NOTES_URL.format(version=latest)
                manual_steps = resolution.manual or not resolution.verified
            available = resolution.status == "update"
        return {
            "enabled": enabled,
            "source": source,
            "current": format_version(current) if current is not None else None,
            "latest": latest,
            "available": available,
            "manual_steps": manual_steps,
            "notes_url": notes_url,
            "upgrading_url": UPGRADING_URL,
            "checked_at": _iso_utc(checked_at) if checked_at is not None else None,
            "error": error,
        }


def read_switch(state: PortalState) -> tuple[bool, str]:
    """Resolve the switch for this portal (blocking: run it on a worker thread).

    Parameters
    ----------
    state : PortalState
        The portal; only ``mode`` is read.

    Returns
    -------
    tuple of (bool, str)
        :func:`check_switch`'s answer. Local mode reads
        ``settings.update_check`` in a fresh session (on when the row is
        missing); production passes ``True`` and is configured by the
        environment alone.
    """
    setting_on = True
    if state.mode == "local":
        with get_session() as session:
            setting = session.get(Setting, 1)
            if setting is not None:
                setting_on = bool(setting.update_check)
    return check_switch(state.mode, setting_on)


def fetch_into_book(
    book: UpdateBook, gen: int, transport: httpx.BaseTransport | None
) -> None:
    """Fetch the release list into ``book`` (blocking; never raises).

    Touches no database, so the loop may abandon the thread on cancel: a
    late result only reaches the in-memory book.

    Parameters
    ----------
    book : UpdateBook
        Where the result goes (:meth:`UpdateBook.record` or
        :meth:`UpdateBook.fail`).
    gen : int
        The generation :meth:`UpdateBook.begin` returned.
    transport : httpx.BaseTransport or None
        Replaces the network (tests); ``None`` in a real run.
    """
    try:
        with build_client(USER_AGENT, transport) as client:
            releases = fetch_releases(client)
    except ReleaseCheckError as exc:
        kind = exc.kind
        _log.warning("update check failed: %s", kind)
    except Exception:  # noqa: BLE001 -- never fatal; retried like a network error
        kind = "network"
        _log.exception("update check failed")
    else:
        book.record(gen, releases, time.monotonic(), datetime.now(UTC))
        _log.info("update check: %d release(s) listed", len(releases))
        return
    book.fail(gen, kind, time.monotonic(), datetime.now(UTC))


def update_block(state: PortalState) -> dict[str, Any] | None:
    """The ``update`` block for ``state`` now (:meth:`UpdateBook.view`).

    Parameters
    ----------
    state : PortalState
        The portal.

    Returns
    -------
    dict or None
        See :meth:`UpdateBook.view`.
    """
    return state.updates.view(running_version(), datetime.now(UTC))
