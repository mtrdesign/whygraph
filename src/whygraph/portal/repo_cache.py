"""The GitHub repository list cache behind the import's repo search (M2f-3 plan section 4.8).

``GET /api/github/installations/{id}/repos`` searches **every** repository an
installation lets the signed-in user see, so the portal pages GitHub's list
(``GitHubApp.installation_repos``) to the end once and filters it in memory.
The list is read with the **user's** token, so it is cached per ``(user id,
installation id)`` - a cache shared by installation would show one member's
visible repositories to another (decision 0.3 #7). Entries live
:data:`REPO_CACHE_TTL_SEC`; there is no webhook invalidation, the short TTL
and :meth:`RepoListCache.drop` after an import replace it.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field

from .github_app import GitHubRepo, RepoPage

REPO_CACHE_TTL_SEC = 60.0
"""How long a filled list is served from memory."""

REPO_CACHE_MAX_KEYS = 200
"""The most ``(user, installation)`` lists kept (least recently used dropped)."""

REPO_CACHE_MAX_PAGES = 30
"""GitHub pages one fill reads at most (3,000 repositories); beyond, ``truncated``."""

REPO_REFRESH_INTERVAL_SEC = 10.0
"""A ``refresh`` refills a list at most this often (else the cached one is served)."""


@dataclass(frozen=True)
class RepoList:
    """One filled repository list.

    Attributes
    ----------
    repos : tuple of GitHubRepo
        Every repository read, in GitHub's order.
    total_count : int
        GitHub's own count of what the user can see through the installation.
    truncated : bool
        Whether the fill stopped at :data:`REPO_CACHE_MAX_PAGES` with more
        repositories left.
    fetched_at : float
        The cache clock's time of the fill.
    """

    repos: tuple[GitHubRepo, ...]
    total_count: int
    truncated: bool
    fetched_at: float


@dataclass
class _Slot:
    """A key's lock (one fill at a time) and its last list."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    value: RepoList | None = None


class RepoListCache:
    """Repository lists per ``(user id, installation id)``, filled on demand.

    Parameters
    ----------
    ttl : float
        Seconds a list is served from memory.
    max_keys : int
        Most keys kept; the least recently used one is dropped past it.
    max_pages : int
        GitHub pages one fill reads at most.
    refresh_interval : float
        Seconds after a fill during which ``refresh`` serves the cached list.
    clock : callable, optional
        A monotonic clock returning seconds; tests inject a fake one.
    """

    def __init__(
        self,
        *,
        ttl: float = REPO_CACHE_TTL_SEC,
        max_keys: int = REPO_CACHE_MAX_KEYS,
        max_pages: int = REPO_CACHE_MAX_PAGES,
        refresh_interval: float = REPO_REFRESH_INTERVAL_SEC,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = ttl
        self.max_keys = max_keys
        self.max_pages = max_pages
        self.refresh_interval = refresh_interval
        self._clock = clock
        self._slots: OrderedDict[Hashable, _Slot] = OrderedDict()
        self._lock = threading.Lock()

    def _slot(self, key: Hashable) -> _Slot:
        with self._lock:
            slot = self._slots.get(key)
            if slot is None:
                slot = self._slots[key] = _Slot()
                while len(self._slots) > self.max_keys:
                    self._slots.popitem(last=False)
            else:
                self._slots.move_to_end(key)
            return slot

    def get(
        self,
        key: Hashable,
        fetch: Callable[[int], RepoPage],
        *,
        per_page: int,
        refresh: bool = False,
    ) -> RepoList:
        """Return the key's list, filling it when missing, expired or refreshed.

        Two callers of one key never fill twice: the second waits for the
        first's fill and gets its list.

        Parameters
        ----------
        key : Hashable
            ``(user id, installation id)``.
        fetch : callable
            ``fetch(page) -> RepoPage``, pages from 1; its exceptions propagate
            and nothing is cached.
        per_page : int
            The page size ``fetch`` asks GitHub for (a shorter page is the last).
        refresh : bool
            Fill again even when the list is fresh - unless it was filled less
            than :attr:`refresh_interval` seconds ago.

        Returns
        -------
        RepoList
            The list.
        """
        slot = self._slot(key)
        with slot.lock:
            now = self._clock()
            cached = slot.value
            if cached is not None:
                age = now - cached.fetched_at
                fresh = age < self.ttl
                if fresh and (not refresh or age < self.refresh_interval):
                    return cached
            filled = self._fill(fetch, per_page)
            slot.value = filled
            return filled

    def _fill(self, fetch: Callable[[int], RepoPage], per_page: int) -> RepoList:
        """Read pages until a short one or :attr:`max_pages`."""
        repos: list[GitHubRepo] = []
        total = 0
        capped = False
        for page in range(1, self.max_pages + 1):
            listed = fetch(page)
            repos.extend(listed.repos)
            total = listed.total_count
            if len(listed.repos) < per_page:
                break
            capped = page == self.max_pages
        return RepoList(
            repos=tuple(repos),
            total_count=total,
            truncated=capped and total > len(repos),
            fetched_at=self._clock(),
        )

    def drop(self, key: Hashable) -> None:
        """Forget a key's list (after an import, so the next search refills it)."""
        with self._lock:
            self._slots.pop(key, None)
