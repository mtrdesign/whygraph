from __future__ import annotations

import threading

import pytest
from rich.progress import Progress

from whygraph.scan import Crawler


class _CountingCrawler(Crawler):
    def __init__(self, name: str, progress: Progress, units: int) -> None:
        super().__init__(name, progress, total=units)
        self._units = units

    def work(self) -> None:
        for _ in range(self._units):
            self.advance(1)


class _BoomCrawler(Crawler):
    def work(self) -> None:
        self.advance(1)
        raise RuntimeError("boom")


def test_crawler_happy_path_completes_task() -> None:
    with Progress(disable=True) as progress:
        crawler = _CountingCrawler("demo", progress, units=5)
        crawler.start()
        crawler.join(timeout=5)

        assert not crawler.is_alive()
        assert crawler.error is None
        task = progress.tasks[0]
        assert task.total == 5
        assert task.completed == 5


def test_crawler_captures_exception_and_finalizes_task() -> None:
    with Progress(disable=True) as progress:
        crawler = _BoomCrawler("boomy", progress, total=3)
        crawler.start()
        crawler.join(timeout=5)

        assert not crawler.is_alive()
        assert isinstance(crawler.error, RuntimeError)
        assert str(crawler.error) == "boom"
        # finally-block marks the bar fully done even on error
        task = progress.tasks[0]
        assert task.completed == task.total == 3


def test_two_crawlers_run_concurrently_with_independent_tasks() -> None:
    with Progress(disable=True) as progress:
        a = _CountingCrawler("a", progress, units=10)
        b = _CountingCrawler("b", progress, units=4)

        a.start()
        b.start()
        a.join(timeout=5)
        b.join(timeout=5)

        assert a.error is None and b.error is None
        # Distinct task IDs were registered on the same Progress.
        assert a._task_id != b._task_id  # noqa: SLF001 — verifying isolation
        # Totals stayed independent.
        task_a = progress.tasks[a._task_id]  # noqa: SLF001
        task_b = progress.tasks[b._task_id]  # noqa: SLF001
        assert (task_a.total, task_a.completed) == (10, 10)
        assert (task_b.total, task_b.completed) == (4, 4)


def test_crawler_cannot_be_instantiated_directly() -> None:
    with Progress(disable=True) as progress:
        with pytest.raises(TypeError, match="abstract"):
            Crawler("bare", progress)  # type: ignore[abstract]


def test_crawler_work_raises_not_implemented_when_super_invoked() -> None:
    class _PassThroughCrawler(Crawler):
        def work(self) -> None:
            super().work()

    with Progress(disable=True) as progress:
        crawler = _PassThroughCrawler("pt", progress, total=1)
        crawler.start()
        crawler.join(timeout=5)

        assert isinstance(crawler.error, NotImplementedError)
        assert "work() is abstract" in str(crawler.error)


def test_set_total_can_be_called_after_construction() -> None:
    class _LateTotalCrawler(Crawler):
        def work(self) -> None:
            self.set_total(7)
            for _ in range(7):
                self.advance(1)

    with Progress(disable=True) as progress:
        crawler = _LateTotalCrawler("late", progress)  # total=None initially
        crawler.start()
        crawler.join(timeout=5)

        assert crawler.error is None
        task = progress.tasks[0]
        assert task.total == 7
        assert task.completed == 7


@pytest.mark.parametrize("units", [0, 1, 50])
def test_crawler_handles_various_unit_counts(units: int) -> None:
    with Progress(disable=True) as progress:
        crawler = _CountingCrawler("n", progress, units=units)
        crawler.start()
        crawler.join(timeout=5)

        assert crawler.error is None
        task = progress.tasks[0]
        assert task.total == units
        assert task.completed == units


def test_crawler_is_a_thread() -> None:
    # Sanity: subclassing threading.Thread is part of the public contract.
    with Progress(disable=True) as progress:
        c = _CountingCrawler("t", progress, units=1)
        assert isinstance(c, threading.Thread)
