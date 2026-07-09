"""Concurrent scan crawlers with shared-progress reporting.

Exposes :class:`Crawler`, a :class:`threading.Thread` subclass that owns
one task on a shared :class:`rich.progress.Progress` instance. Subclasses
override :meth:`Crawler.work` (not :meth:`run`) and call
:meth:`Crawler.advance` / :meth:`Crawler.set_total` to drive the bar.
The base class captures any exception raised by ``work`` so that one
failing crawler does not crash a multi-crawler scan; callers inspect
:attr:`Crawler.error` after :meth:`threading.Thread.join`.
"""

from __future__ import annotations

import abc
import threading

from rich.progress import Progress, TaskID


class Crawler(threading.Thread, abc.ABC):
    """Abstract base for scan crawlers.

    Subclasses MUST implement :meth:`work` (NOT :meth:`run`). The base
    class handles thread plumbing, progress-task registration, and
    exception capture so one failing crawler does not crash the
    orchestrator.

    Notes
    -----
    The MRO combines :class:`threading.Thread` (metaclass ``type``) with
    :class:`abc.ABC` (metaclass :class:`abc.ABCMeta`). Because
    ``ABCMeta`` is a subclass of ``type``, Python selects ``ABCMeta`` as
    the resolved metaclass automatically — no explicit ``metaclass=`` is
    required.

    Parameters
    ----------
    name : str
        Short label shown in the progress UI (e.g. ``"git"``).
    progress : rich.progress.Progress
        Shared Progress instance owned by the orchestrator. The crawler
        registers a task on it at construction time.
    total : int or None, optional
        Initial total for the progress task. Pass ``None`` when the
        total is not known up front and call :meth:`set_total` later.

    Attributes
    ----------
    error : BaseException or None
        Exception raised by :meth:`work`, captured so callers can
        inspect it after :meth:`threading.Thread.join`. ``None`` on
        success.
    summary : str or None
        One-line outcome delta set by :meth:`work` at the end of a
        successful crawl (e.g. ``"45 commits"``), for the orchestrator's
        closing results panel. Written from the worker thread and read
        only after :meth:`threading.Thread.join`, mirroring the safety
        model of :attr:`error`. ``None`` until set.
    """

    def __init__(
        self,
        name: str,
        progress: Progress,
        *,
        total: int | None = None,
    ) -> None:
        super().__init__(name=name, daemon=True)
        self._progress = progress
        self._task_id: TaskID = progress.add_task(name, total=total)
        self.error: BaseException | None = None
        self.summary: str | None = None

    # --- subclass hooks ------------------------------------------------

    @abc.abstractmethod
    def work(self) -> None:
        """Perform the crawl. Concrete subclasses must implement this.

        Implementations should drive progress via :meth:`advance` (and
        :meth:`set_total` when the total is not known up front). Any
        exception raised here is captured into :attr:`error` by
        :meth:`run` and surfaced to the orchestrator after
        :meth:`threading.Thread.join`.

        Raises
        ------
        NotImplementedError
            If reached at runtime — either via ``super().work()`` from a
            subclass that did not provide its own implementation, or
            because the abstract guard was bypassed at instantiation
            (e.g. by clearing ``__abstractmethods__``).
        """
        raise NotImplementedError(
            f"{type(self).__name__}.work() is abstract and must be "
            "implemented by a concrete subclass."
        )

    # --- progress helpers ----------------------------------------------

    def advance(self, n: int = 1, description: str | None = None) -> None:
        """Advance this crawler's progress task by ``n`` units.

        Parameters
        ----------
        n : int, optional
            Units to add to the task's completed count (default ``1``).
        description : str, optional
            If given, also updates the task's description label.
        """
        if description is not None:
            self._progress.update(self._task_id, advance=n, description=description)
        else:
            self._progress.update(self._task_id, advance=n)

    def set_total(self, total: int) -> None:
        """Set or update this crawler's progress task total."""
        self._progress.update(self._task_id, total=total)

    # --- Thread entry point --------------------------------------------

    def run(self) -> None:
        """Thread entry point — invokes :meth:`work` with safety wrapping.

        Captures any exception (including :class:`KeyboardInterrupt` /
        :class:`SystemExit`, which can propagate inside a worker thread)
        into :attr:`error` and marks the progress task complete so its
        bar stops animating regardless of outcome.
        """
        try:
            self.work()
        except BaseException as exc:  # noqa: BLE001 — captured for caller
            self.error = exc
        finally:
            total = self._progress.tasks[self._task_id].total or 0
            self._progress.update(self._task_id, completed=total)
