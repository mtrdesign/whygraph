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
from collections.abc import Callable, Hashable, Mapping
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
