"""JsonProgress - a machine-readable stand-in for the scan's Rich progress bars.

``whygraph scan --progress json`` swaps the shared
:class:`rich.progress.Progress` for :class:`JsonProgress`, which emits one
JSON object per line on stdout instead of drawing bars. The portal's scan
runner (a parent process) reads that stream and re-serves it to the browser.

Event contract (one JSON object per line, ``type`` discriminates):

``start``
    Always the **first** event: ``{"type": "start", "phase_total": n,
    "phases": ["Structural crawl", ...]}``. The phase count is 2-4
    depending on the scan flags; ``phases`` lists the titles of the phases
    that will run, in order (``["Code index"]`` for ``--codegraph-only``).
    ``phases`` is optional for readers: events written by older runs lack it.
``phase``
    ``{"type": "phase", "phase": n, "title": "..."}`` when a phase begins.
``task``
    ``{"type": "task", "name", "completed", "total", "description"}`` on
    task registration and on (throttled) progress updates. ``name`` is the
    crawler's stable label; ``description`` is its current status text.
``usage``
    Only in a scan the portal manages (``--managed-by-portal`` with
    ``--progress json``; :class:`JsonUsageSink`): one per successful LLM
    call, always before ``result``: ``{"type": "usage", "task",
    "model_served", "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "reasoning_tokens", "provider_cost_usd",
    "subject", "duration_ms"}``. Counts only - the runner attributes and
    prices the call from its own spec (provider, model, project, actor),
    writes it to the usage ledger and keeps it out of the events file.
``result``
    Always the **last** event: outcome, timings and one entry per crawler.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any, TextIO

from rich.console import Console
from rich.progress import Progress, TaskID

from whygraph.core.usage import UsageRecord, UsageScope

_THROTTLE_SEC = 0.25
"""Minimum gap between two ``task`` events for the same task."""

USAGE_TEXT_MAX = 200
"""Longest ``subject`` / ``model_served`` a ``usage`` event carries (the runner refuses longer)."""


class JsonProgress(Progress):
    """A :class:`~rich.progress.Progress` that emits JSON lines, not bars.

    Crawlers drive it exactly like the Rich version (``add_task`` /
    ``update``), from several threads at once. Every emit is one
    ``write(line + "\\n")`` plus ``flush()`` under a lock, so two threads
    can never splice a line. ``update`` events are throttled per task; a
    task's finishing update, a total change and a description change are
    always emitted, and :meth:`flush` writes the latest state of any task
    whose update was dropped.

    Parameters
    ----------
    stream : TextIO, optional
        Destination for the JSON lines. ``None`` (default) resolves
        ``sys.stdout`` at emit time, so a redirected stdout is honoured.
    throttle_sec : float, optional
        Minimum seconds between ``task`` events for one task
        (default ``0.25``).
    """

    def __init__(
        self, *, stream: TextIO | None = None, throttle_sec: float = _THROTTLE_SEC
    ) -> None:
        # ``disable=True`` skips the live display but keeps task tracking;
        # the quiet console guarantees nothing is ever drawn.
        super().__init__(console=Console(quiet=True), disable=True)
        self._stream = stream
        self._throttle_sec = throttle_sec
        self._emit_lock = threading.Lock()
        self._names: dict[TaskID, str] = {}
        self._last_emit: dict[TaskID, float] = {}
        self._last_sent: dict[TaskID, tuple[Any, ...]] = {}
        self._dirty: set[TaskID] = set()

    # --- emitting -------------------------------------------------------

    def emit(self, event: dict[str, Any]) -> None:
        """Write one event as a single JSON line and flush.

        Parameters
        ----------
        event : dict
            A JSON-serializable mapping; its ``type`` key names the event.
        """
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._emit_lock:
            stream = self._stream
            if stream is None:
                stream = sys.stdout
            try:
                stream.write(line + "\n")
                stream.flush()
            except (OSError, ValueError):
                # The reader went away (closed pipe / stream); a crawler
                # thread must not fail because nobody is listening.
                pass

    def start_event(self, phase_total: int, phases: list[str]) -> None:
        """Emit the ``start`` event that opens the stream.

        Parameters
        ----------
        phase_total : int
            How many phases the scan will run.
        phases : list of str
            Their titles, in order, so a reader can name every phase before
            it begins.
        """
        self.emit({"type": "start", "phase_total": phase_total, "phases": phases})

    def phase(self, phase: int, title: str) -> None:
        """Emit a ``phase`` event for the phase that is beginning."""
        self.emit({"type": "phase", "phase": phase, "title": title})

    def flush(self) -> None:
        """Emit the latest state of every task whose update was throttled."""
        for task_id in sorted(self._dirty):
            self._emit_task(task_id)

    # --- Progress overrides ----------------------------------------------

    def add_task(self, description: str, *args: Any, **kwargs: Any) -> TaskID:
        """Register a task (as :meth:`Progress.add_task`) and announce it."""
        task_id = super().add_task(description, *args, **kwargs)
        self._names[task_id] = description
        self._emit_task(task_id)
        return task_id

    def update(self, task_id: TaskID, **kwargs: Any) -> None:
        """Update a task (as :meth:`Progress.update`), emitting throttled events."""
        with self._lock:
            before = self._snapshot(task_id)
            super().update(task_id, **kwargs)
            after = self._snapshot(task_id)
        if after == before:
            return
        _, total, description = after
        finished = total is not None and after[0] >= total
        structural = (total, description) != (before[1], before[2])
        now = time.monotonic()
        last = self._last_emit.get(task_id, 0.0)
        if finished or structural or now - last >= self._throttle_sec:
            self._emit_task(task_id)
        else:
            self._dirty.add(task_id)

    # --- internals ---------------------------------------------------------

    def _snapshot(self, task_id: TaskID) -> tuple[Any, ...]:
        task = self._tasks[task_id]
        completed = task.completed
        if isinstance(completed, float) and completed.is_integer():
            completed = int(completed)
        total = task.total
        if isinstance(total, float) and total.is_integer():
            total = int(total)
        return (completed, total, task.description)

    def _emit_task(self, task_id: TaskID) -> None:
        with self._lock:
            completed, total, description = self._snapshot(task_id)
        self._dirty.discard(task_id)
        self._last_emit[task_id] = time.monotonic()
        state = (completed, total, description)
        if self._last_sent.get(task_id) == state:
            return
        self._last_sent[task_id] = state
        self.emit(
            {
                "type": "task",
                "name": self._names.get(task_id, description),
                "completed": completed,
                "total": total,
                "description": description,
            }
        )


def _capped(text: str | None) -> str | None:
    """``text`` cut to :data:`USAGE_TEXT_MAX` characters."""
    return None if text is None else text[:USAGE_TEXT_MAX]


class JsonUsageSink:
    """A :class:`~whygraph.core.usage.UsageSink` that emits ``usage`` events.

    Bound by ``whygraph scan`` only when the portal manages the scan
    (``--managed-by-portal`` with ``--progress json``), before any crawler
    is built, so every metered call of the scan child becomes one ``usage``
    line on the progress stream. A headless scan binds nothing and records
    nothing.

    Parameters
    ----------
    progress : JsonProgress
        The stream the events are written to (one line each, never
        spliced with another thread's).

    Attributes
    ----------
    scope : UsageScope
        Always ``source="scan"``; unused by the child.
    """

    def __init__(self, progress: JsonProgress) -> None:
        self.scope = UsageScope(source="scan")
        self._progress = progress

    def record(self, rec: UsageRecord) -> None:
        """Emit one ``usage`` event (counts only; no provider or model requested).

        Parameters
        ----------
        rec : UsageRecord
            What the provider call reported.
        """
        self._progress.emit(
            {
                "type": "usage",
                "task": rec.task,
                "model_served": _capped(rec.model_served),
                "input_tokens": rec.input_tokens,
                "output_tokens": rec.output_tokens,
                "cache_read_tokens": rec.cache_read_tokens,
                "cache_write_tokens": rec.cache_write_tokens,
                "reasoning_tokens": rec.reasoning_tokens,
                "provider_cost_usd": rec.provider_cost_usd,
                "subject": _capped(rec.subject),
                "duration_ms": rec.duration_ms,
            }
        )

    def blocked_scope(self) -> str | None:
        """Always ``None``: the child never decides; the runner enforces budgets.

        Returns
        -------
        None
        """
        return None
