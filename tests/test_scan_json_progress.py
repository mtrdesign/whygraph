"""Tests for :class:`whygraph.scan.JsonProgress`, the scan child's event stream.

Pins the JSONL ``task`` events (registration, throttled updates, the
finishing / total-change updates that are never throttled), the lock that
keeps lines from concurrent crawler threads from splicing, and that a
closed stream is swallowed.
"""

from __future__ import annotations

import io
import json
import threading

from whygraph.scan import JsonProgress


def _events(text: str) -> list[dict]:
    """Parse every line as JSON - a spliced or stray line raises."""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class _RecordingStream:
    """Records each ``write`` call whole, so a splice would be visible."""

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.flushes = 0

    def write(self, text: str) -> int:
        self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        self.flushes += 1


def test_json_progress_emits_add_task_and_final_update() -> None:
    stream = _RecordingStream()
    progress = JsonProgress(stream=stream)

    task = progress.add_task("git", total=4)
    progress.update(task, advance=4, description="git done")

    events = _events("".join(stream.writes))
    assert events[0] == {
        "type": "task",
        "name": "git",
        "completed": 0,
        "total": 4,
        "description": "git",
    }
    assert events[-1] == {
        "type": "task",
        "name": "git",
        "completed": 4,
        "total": 4,
        "description": "git done",
    }


def test_json_progress_throttles_updates_and_flush_sends_latest() -> None:
    stream = _RecordingStream()
    progress = JsonProgress(stream=stream, throttle_sec=3600)
    task = progress.add_task("git", total=100)

    for _ in range(50):
        progress.update(task, advance=1)

    # add_task + nothing else: every update fell inside the throttle window.
    assert len(stream.writes) == 1
    progress.flush()
    latest = _events(stream.writes[-1])[0]
    assert latest["completed"] == 50
    # Idempotent: nothing left to flush.
    progress.flush()
    assert len(stream.writes) == 2


def test_json_progress_always_emits_finish_and_total_change() -> None:
    stream = _RecordingStream()
    progress = JsonProgress(stream=stream, throttle_sec=3600)
    task = progress.add_task("codegraph", total=None)

    progress.update(task, total=1)  # structural change - not throttled
    progress.update(task, advance=1)  # finishing - not throttled

    events = _events("".join(stream.writes))
    assert [(e["completed"], e["total"]) for e in events] == [
        (0, None),
        (0, 1),
        (1, 1),
    ]


def test_json_progress_lines_from_concurrent_threads_never_splice() -> None:
    stream = _RecordingStream()
    progress = JsonProgress(stream=stream, throttle_sec=0)
    n_threads, n_updates = 8, 200

    def work(idx: int) -> None:
        task = progress.add_task(f"crawler-{idx}", total=n_updates)
        for _ in range(n_updates):
            progress.update(task, advance=1)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert stream.writes, "expected events"
    for chunk in stream.writes:
        # One write == exactly one complete JSON line.
        assert chunk.endswith("\n") and chunk.count("\n") == 1
        json.loads(chunk)
    assert stream.flushes == len(stream.writes)
    # Every crawler's final state made it out.
    finals = {e["name"]: e["completed"] for e in _events("".join(stream.writes))}
    assert finals == {f"crawler-{i}": n_updates for i in range(n_threads)}


def test_json_progress_swallows_a_closed_stream() -> None:
    stream = io.StringIO()
    progress = JsonProgress(stream=stream)
    stream.close()

    task = progress.add_task("git", total=1)  # must not raise
    progress.update(task, advance=1)
