"""Unit tests for the import's repository list cache (M2f-3 plan sections 4.8, 6.1 #2)."""

from __future__ import annotations

import threading
import time

import pytest

from whygraph.portal.github_app import GitHubRepo, RepoPage
from whygraph.portal.repo_cache import RepoListCache


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def repo(n: int) -> GitHubRepo:
    return GitHubRepo(
        id=n, full_name=f"acme/repo-{n}", private=True, default_branch="main"
    )


class GitHub:
    """``fetch(page)`` over ``count`` repositories, ``per_page`` a page; counts calls."""

    def __init__(self, count: int, per_page: int = 10) -> None:
        self.repos = [repo(n) for n in range(count)]
        self.per_page = per_page
        self.pages: list[int] = []

    def fetch(self, page: int) -> RepoPage:
        self.pages.append(page)
        start = (page - 1) * self.per_page
        return RepoPage(
            repos=self.repos[start : start + self.per_page],
            total_count=len(self.repos),
        )


def make(**kw) -> tuple[RepoListCache, Clock]:
    clock = Clock()
    return RepoListCache(clock=clock, **kw), clock


def test_a_fill_pages_until_a_short_page() -> None:
    cache, _ = make()
    gh = GitHub(25)
    listed = cache.get(("u", 1), gh.fetch, per_page=10)
    assert gh.pages == [1, 2, 3]
    assert [r.id for r in listed.repos] == list(range(25))
    assert (listed.total_count, listed.truncated) == (25, False)


def test_an_exact_multiple_reads_one_empty_page() -> None:
    cache, _ = make()
    gh = GitHub(20)
    assert len(cache.get(("u", 1), gh.fetch, per_page=10).repos) == 20
    assert gh.pages == [1, 2, 3]


def test_the_page_cap_truncates() -> None:
    cache, _ = make(max_pages=3)
    gh = GitHub(45)
    listed = cache.get(("u", 1), gh.fetch, per_page=10)
    assert gh.pages == [1, 2, 3]
    assert len(listed.repos) == 30 and listed.total_count == 45
    assert listed.truncated is True
    # Exactly the cap's worth is not truncated.
    exact = GitHub(30)
    cache2, _ = make(max_pages=3)
    assert cache2.get(("u", 1), exact.fetch, per_page=10).truncated is False


def test_ttl_and_per_key_isolation() -> None:
    cache, clock = make(ttl=60)
    ben, ada = GitHub(5), GitHub(3)
    assert len(cache.get(("ben", 7), ben.fetch, per_page=10).repos) == 5
    # Another user of the same installation never sees Ben's list.
    assert len(cache.get(("ada", 7), ada.fetch, per_page=10).repos) == 3
    clock.now += 59
    assert len(cache.get(("ben", 7), ben.fetch, per_page=10).repos) == 5
    assert ben.pages == [1]  # served from memory
    clock.now += 2
    cache.get(("ben", 7), ben.fetch, per_page=10)
    assert ben.pages == [1, 1]  # expired: filled again


def test_refresh_is_limited_per_key() -> None:
    cache, clock = make(refresh_interval=10)
    gh = GitHub(5)
    cache.get(("u", 1), gh.fetch, per_page=10)
    clock.now += 5
    cache.get(("u", 1), gh.fetch, per_page=10, refresh=True)
    assert gh.pages == [1]  # within 10 s of the fill: the cached list
    clock.now += 6
    cache.get(("u", 1), gh.fetch, per_page=10, refresh=True)
    assert gh.pages == [1, 1]
    cache.get(("u", 1), gh.fetch, per_page=10, refresh=True)
    assert gh.pages == [1, 1]


def test_drop_and_the_key_cap() -> None:
    cache, _ = make(max_keys=2)
    gh = GitHub(1)
    cache.get(("u", 1), gh.fetch, per_page=10)
    cache.drop(("u", 1))
    cache.get(("u", 1), gh.fetch, per_page=10)
    assert gh.pages == [1, 1]
    cache.get(("u", 2), gh.fetch, per_page=10)
    cache.get(("u", 3), gh.fetch, per_page=10)  # evicts ("u", 1)
    cache.get(("u", 1), gh.fetch, per_page=10)
    assert gh.pages == [1, 1, 1, 1, 1]


def test_a_failed_fill_caches_nothing() -> None:
    cache, _ = make()
    calls = []

    def broken(page: int) -> RepoPage:
        calls.append(page)
        raise RuntimeError("GitHub is down")

    with pytest.raises(RuntimeError):
        cache.get(("u", 1), broken, per_page=10)
    gh = GitHub(2)
    assert len(cache.get(("u", 1), gh.fetch, per_page=10).repos) == 2


def test_concurrent_callers_fill_once() -> None:
    cache = RepoListCache()
    gh = GitHub(3)
    entered = threading.Event()

    def slow(page: int) -> RepoPage:
        entered.set()
        time.sleep(0.2)
        return gh.fetch(page)

    results: list[int] = []

    def call() -> None:
        results.append(len(cache.get(("u", 1), slow, per_page=10).repos))

    threads = [threading.Thread(target=call) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert results == [3, 3, 3, 3]
    assert gh.pages == [1]
