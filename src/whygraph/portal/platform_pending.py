"""The local portal's connects and links in flight (M2e plan section 4.8).

Two in-memory stores, copies of
:class:`whygraph.portal.github_auth.PendingLogins`: thread-safe, a TTL per
entry and the oldest evicted past a capacity.

* :class:`PendingConnects` (10 min, single use): one per click on
  **Connect** - the OAuth ``state``, the PKCE verifier, the platform origin
  and the exact ``redirect_uri`` - until the callback pops it.
* :class:`PendingLinks` (30 min): one per successful code exchange - the
  connection token, the platform's reply and the checkout candidates -
  until ``POST /api/projects`` consumes it or the wizard abandons it. An
  entry that expires or is evicted still holds a live token, so it is kept
  aside for :meth:`PendingLinks.take_dropped`, whose caller revokes it on
  the platform (best effort; ``removed_locally``).

Both live only in memory: a restart loses them, and the platform expires a
token never used within an hour (plan section 0.1 #8). Every entry is bound
to the org it was started in; a lookup from another org misses.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from whygraph.api_v1 import TokenReply

CONNECT_TTL_SEC = 10 * 60
"""How long a started connect waits for its callback."""

CONNECT_MAX = 64
"""Connects in flight before the oldest is evicted."""

LINK_TTL_SEC = 30 * 60
"""How long an exchanged link waits for the wizard's **Link**."""

LINK_MAX = 32
"""Links in flight before the oldest is evicted (and its token revoked)."""


@dataclass(frozen=True)
class PendingConnect:
    """A started connect, kept until its callback.

    Attributes
    ----------
    org_id : int
        The local org the wizard runs in.
    verifier : str
        The PKCE code verifier (never leaves the portal but in the exchange).
    platform_origin : str
        The normalized platform origin; the callback's ``iss`` must equal it.
    redirect_uri : str
        ``http://127.0.0.1:<port>/connect/callback``, repeated in the exchange.
    client_name : str
        The name this portal asked to be shown as.
    expires_at : float
        Clock reading after which the entry is dead.
    """

    org_id: int
    verifier: str = field(repr=False)
    platform_origin: str
    redirect_uri: str
    client_name: str
    expires_at: float


@dataclass(frozen=True)
class PendingLink:
    """An exchanged connection waiting for the wizard to pick a checkout.

    Attributes
    ----------
    link_id : str
        The wizard's handle (URL-safe, random).
    org_id : int
        The local org the wizard runs in.
    platform_origin, api_origin : str
        The platform's base origin and its org origin, the latter computed
        locally (:func:`~whygraph.portal.platform_client.api_origin_for`).
    token : str
        The connection token (never in a response).
    reply : TokenReply
        The exchange's answer: the org and the platform project.
    candidates : tuple[str, ...]
        Discovered checkouts whose ``origin`` names the project's repo.
    expires_at : float
        Clock reading after which the entry is dead.
    """

    link_id: str
    org_id: int
    platform_origin: str
    api_origin: str
    token: str = field(repr=False)
    reply: TokenReply = field(repr=False)
    candidates: tuple[str, ...]
    expires_at: float


class PendingConnects:
    """In-memory map of connects in flight, keyed by OAuth ``state``.

    Parameters
    ----------
    clock : callable, optional
        Returns seconds; injectable for tests (default ``time.monotonic``).
    ttl : float
        Entry lifetime in seconds.
    max_entries : int
        Capacity before the oldest entry is evicted.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = CONNECT_TTL_SEC,
        max_entries: int = CONNECT_MAX,
    ) -> None:
        self._clock = clock
        self._ttl = ttl
        self._max = max_entries
        self._entries: OrderedDict[str, PendingConnect] = OrderedDict()
        self._lock = threading.Lock()

    def put(
        self,
        *,
        org_id: int,
        verifier: str,
        platform_origin: str,
        redirect_uri: str,
        client_name: str,
    ) -> str:
        """Remember a started connect; return its new ``state``.

        Returns
        -------
        str
            ``secrets.token_urlsafe(32)``: 43 URL-safe characters.
        """
        state = secrets.token_urlsafe(32)
        entry = PendingConnect(
            org_id=org_id,
            verifier=verifier,
            platform_origin=platform_origin,
            redirect_uri=redirect_uri,
            client_name=client_name,
            expires_at=self._clock() + self._ttl,
        )
        with self._lock:
            self._entries[state] = entry
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
        return state

    def pop(self, state: str, org_id: int) -> PendingConnect | None:
        """Take a connect out of the map (single use, whatever happens next).

        Parameters
        ----------
        state : str
            The callback's ``state``.
        org_id : int
            The org of the callback request; another org's entry is a miss
            (and stays in place).

        Returns
        -------
        PendingConnect or None
            The entry, or ``None`` when it is unknown, used or expired.
        """
        with self._lock:
            entry = self._entries.get(state)
            if entry is None or entry.org_id != org_id:
                return None
            del self._entries[state]
        if entry.expires_at <= self._clock():
            return None
        return entry

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class PendingLinks:
    """In-memory map of exchanged links, keyed by ``link_id``.

    An entry leaves by :meth:`pop` (consumed or abandoned: the caller owns
    its token then) or by expiry / eviction (kept for :meth:`take_dropped`,
    so the caller can revoke its token).

    Parameters
    ----------
    clock : callable, optional
        Returns seconds; injectable for tests (default ``time.monotonic``).
    ttl : float
        Entry lifetime in seconds.
    max_entries : int
        Capacity before the oldest entry is evicted.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = LINK_TTL_SEC,
        max_entries: int = LINK_MAX,
    ) -> None:
        self._clock = clock
        self._ttl = ttl
        self._max = max_entries
        self._entries: OrderedDict[str, PendingLink] = OrderedDict()
        self._dropped: list[PendingLink] = []
        self._lock = threading.Lock()

    def _expire(self) -> None:
        """Move the expired entries aside (the lock is held)."""
        now = self._clock()
        for link_id in [k for k, e in self._entries.items() if e.expires_at <= now]:
            self._dropped.append(self._entries.pop(link_id))

    def put(
        self,
        *,
        org_id: int,
        platform_origin: str,
        api_origin: str,
        token: str,
        reply: TokenReply,
        candidates: tuple[str, ...],
    ) -> str:
        """Remember an exchanged link; return its new ``link_id``."""
        link_id = secrets.token_urlsafe(16)
        entry = PendingLink(
            link_id=link_id,
            org_id=org_id,
            platform_origin=platform_origin,
            api_origin=api_origin,
            token=token,
            reply=reply,
            candidates=candidates,
            expires_at=self._clock() + self._ttl,
        )
        with self._lock:
            self._expire()
            self._entries[link_id] = entry
            while len(self._entries) > self._max:
                self._dropped.append(self._entries.popitem(last=False)[1])
        return link_id

    def get(self, link_id: str, org_id: int) -> PendingLink | None:
        """The live entry of ``link_id`` in ``org_id``, left in place, or ``None``."""
        with self._lock:
            self._expire()
            entry = self._entries.get(link_id)
        if entry is None or entry.org_id != org_id:
            return None
        return entry

    def pop(self, link_id: str, org_id: int) -> PendingLink | None:
        """Take the live entry of ``link_id`` in ``org_id`` out, or ``None``."""
        with self._lock:
            self._expire()
            entry = self._entries.get(link_id)
            if entry is None or entry.org_id != org_id:
                return None
            del self._entries[link_id]
        return entry

    def take_dropped(self) -> list[PendingLink]:
        """Return (and forget) the entries that expired or were evicted."""
        with self._lock:
            self._expire()
            dropped, self._dropped = self._dropped, []
        return dropped

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


__all__ = [
    "CONNECT_MAX",
    "CONNECT_TTL_SEC",
    "LINK_MAX",
    "LINK_TTL_SEC",
    "PendingConnect",
    "PendingConnects",
    "PendingLink",
    "PendingLinks",
]
