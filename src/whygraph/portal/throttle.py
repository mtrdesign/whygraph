"""In-memory rate limiting for the credential routes.

One process holds all state (the portal is exactly one process), so a dict of
timestamp deques under a lock is enough.
"""

from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Hashable, Mapping, Sequence
from typing import Any


class Throttle:
    """Allow ``limit`` recorded events per key within ``window`` seconds.

    Parameters
    ----------
    limit : int
        Events allowed per key in the window.
    window : float
        Window length in seconds.
    max_keys : int, optional
        Most keys kept; the least recently used key is dropped past it.
    clock : callable, optional
        A monotonic clock returning seconds; tests inject a fake one.
    """

    def __init__(
        self,
        limit: int,
        window: float,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._clock = clock
        self._events: OrderedDict[Hashable, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _live(self, key: Hashable, now: float) -> deque[float] | None:
        events = self._events.get(key)
        if events is None:
            return None
        while events and events[0] <= now - self.window:
            events.popleft()
        if not events:
            del self._events[key]
            return None
        return events

    def _retry_after(
        self, events: deque[float] | None, now: float, limit: int | None = None
    ) -> int | None:
        allowed = self.limit if limit is None else limit
        if allowed <= 0:  # nothing is ever allowed: wait a whole window
            return max(1, math.ceil(self.window))
        if events is None or len(events) < allowed:
            return None
        return max(1, math.ceil(events[0] + self.window - now))

    def check(self, key: Hashable, *, limit: int | None = None) -> int | None:
        """Return seconds to wait if ``key`` is over the limit, else ``None``.

        Does not count an event.

        Parameters
        ----------
        key : Hashable
            What is counted.
        limit : int, optional
            This call's limit instead of :attr:`limit` (a per-org budget,
            say); the window stays the throttle's. ``0`` (or less) refuses
            every event with a whole window's wait.
        """
        with self._lock:
            now = self._clock()
            return self._retry_after(self._live(key, now), now, limit)

    def remaining(self, key: Hashable, *, limit: int | None = None) -> int:
        """Return how many more events ``key`` may record now (``0`` at the limit).

        Does not count an event (like :meth:`check`).

        Parameters
        ----------
        key : Hashable
            What is counted.
        limit : int, optional
            As for :meth:`check`.

        Returns
        -------
        int
            The limit minus the events still in the window, never negative.
        """
        with self._lock:
            now = self._clock()
            events = self._live(key, now)
            allowed = self.limit if limit is None else limit
            return max(0, allowed - (0 if events is None else len(events)))

    def record(self, key: Hashable) -> None:
        """Count one event for ``key``."""
        with self._lock:
            now = self._clock()
            events = self._live(key, now)
            if events is None:
                events = self._events[key] = deque()
                while len(self._events) > self.max_keys:
                    self._events.popitem(last=False)
            else:
                self._events.move_to_end(key)
            events.append(now)

    def hit(self, key: Hashable, *, limit: int | None = None) -> int | None:
        """Check ``key`` and, if allowed, count it; return ``check``'s answer.

        ``limit`` is as for :meth:`check`; a refused call is not counted.
        """
        with self._lock:
            now = self._clock()
            events = self._live(key, now)
            retry = self._retry_after(events, now, limit)
            if retry is not None:
                return retry
            if events is None:
                events = self._events[key] = deque()
                while len(self._events) > self.max_keys:
                    self._events.popitem(last=False)
            else:
                self._events.move_to_end(key)
            events.append(now)
            return None

    def hit_all(self, pairs: Sequence[tuple[Hashable, int]]) -> int | None:
        """Check every ``(key, limit)`` pair and count all of them, or none.

        Parameters
        ----------
        pairs : sequence of (Hashable, int)
            Each key with its limit (as for :meth:`check`).

        Returns
        -------
        int or None
            ``None`` when every key had room (one event is then counted on
            each); otherwise the longest wait among the refusing keys, with
            nothing counted.
        """
        with self._lock:
            now = self._clock()
            lives = [self._live(key, now) for key, _ in pairs]
            retries = [
                retry
                for (_, limit), events in zip(pairs, lives)
                if (retry := self._retry_after(events, now, limit)) is not None
            ]
            if retries:
                return max(retries)
            for (key, _), events in zip(pairs, lives):
                if events is None:
                    events = self._events[key] = deque()
                    while len(self._events) > self.max_keys:
                        self._events.popitem(last=False)
                else:
                    self._events.move_to_end(key)
                events.append(now)
            return None


class InFlight:
    """At most ``limit`` requests in flight per key; never waits.

    Parameters
    ----------
    limit : int
        Concurrent holders allowed per key.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._held: dict[Hashable, int] = {}
        self._lock = threading.Lock()

    def try_acquire(self, key: Hashable) -> bool:
        """Take a slot for ``key``; ``False`` (and nothing taken) when all are held."""
        with self._lock:
            held = self._held.get(key, 0)
            if held >= self.limit:
                return False
            self._held[key] = held + 1
            return True

    def release(self, key: Hashable) -> None:
        """Give back a slot :meth:`try_acquire` took."""
        with self._lock:
            held = self._held.get(key, 0) - 1
            if held > 0:
                self._held[key] = held
            else:
                self._held.pop(key, None)


def ip_key(scope: Mapping[str, Any]) -> str:
    """Return the throttle key for a request's client address.

    Parameters
    ----------
    scope : mapping
        An ASGI scope; ``scope["client"][0]`` is the peer address (uvicorn
        has already applied a trusted ``X-Forwarded-For``).

    Returns
    -------
    str
        The IPv4 address as is; an IPv6 address as its ``/64`` network, since
        one host owns a whole /64. ``"unknown"`` when there is no client.
    """
    client = scope.get("client")
    if not client:
        return "unknown"
    raw = str(client[0])
    try:
        addr = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return raw
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)
