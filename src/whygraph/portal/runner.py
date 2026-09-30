"""The scan runner's interface to the rest of the portal.

The runner (plan section 4.6) is a small in-process queue that spawns
``whygraph scan`` child processes per project, with single-flight +
coalescing, a global concurrency cap, the events SSE, GitHub ``sync``
jobs and the poll / catch-up tick. It lands in rollout step 8.

This module fixes the seam the app and the endpoints call, so step 8
fills in :class:`ScanRunner` without touching them:

* the lifespan calls :meth:`ScanRunner.start` after the portal DB is up
  and :meth:`ScanRunner.shutdown` on exit (SIGTERM / SIGKILL of the child
  process groups, marking their runs ``interrupted``);
* ``POST .../scans``, ``GET .../scans/{id}/events`` and ``POST .../sync``
  delegate to :meth:`request_scan`, :meth:`events` and
  :meth:`request_sync`;
* project removal asks :meth:`is_busy`.

Until then those three request methods raise :class:`RunnerUnavailable`,
which the endpoints turn into ``501``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlmodel import select

from .db import get_session
from .models import ScanRun

if TYPE_CHECKING:  # pragma: no cover
    import anyio

    from .deps import BoundProject
    from .security import Principal


class RunnerUnavailable(RuntimeError):
    """The scan runner does not implement this operation yet (HTTP 501)."""


class ScanRunner:
    """Placeholder runner: lifecycle no-ops, request methods unavailable."""

    async def start(self) -> None:
        """Start the queue and poller (step 8). A no-op for now."""

    async def shutdown(self, *, grace: float = 5.0) -> None:
        """Stop children and mark their runs ``interrupted`` (step 8). A no-op now.

        Parameters
        ----------
        grace : float
            Seconds between SIGTERM and SIGKILL of each child group.
        """

    def is_busy(self, project_id: int) -> bool:
        """Return whether a scan or sync for the project is queued or running.

        Parameters
        ----------
        project_id : int
            ``projects.id``.

        Returns
        -------
        bool
            Read from ``scan_runs``, so it holds before step 8 too.
        """
        with get_session() as session:
            row = session.exec(
                select(ScanRun.id)
                .where(ScanRun.project_id == project_id)
                .where(ScanRun.status.in_(("queued", "running")))  # type: ignore[attr-defined]
            ).first()
        return row is not None

    async def request_scan(
        self,
        project: "BoundProject",
        *,
        trigger: str | None,
        analyze: bool | None,
        principal: "Principal | None",
    ) -> int:
        """Queue (or coalesce into) a scan and return its ``scan_runs.id``."""
        raise RunnerUnavailable("scans are not available in this build yet")

    async def request_sync(
        self, project: "BoundProject", *, principal: "Principal | None"
    ) -> int:
        """Queue a GitHub ``sync`` job and return its ``scan_runs.id``."""
        raise RunnerUnavailable("sync is not available in this build yet")

    async def events(
        self, project: "BoundProject", run_id: int, *, shutdown: "anyio.Event"
    ) -> Any:
        """Return the SSE response streaming a run's events."""
        raise RunnerUnavailable("scan events are not available in this build yet")


__all__ = ["RunnerUnavailable", "ScanRunner"]
