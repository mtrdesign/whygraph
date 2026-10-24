"""One CodeGraph writer per project root.

CodeGraph keeps a single-writer lock of its own (``.codegraph/``'s
``FileLock``), but it fails a second writer instead of queueing it. The
portal has three writers that may meet on one repository - a scan child's
CodeGraph step, the MCP tools' re-sync and the bridge's freshness sync - so
each takes this lock around ``refresh_codegraph_index``, which itself stays
lock-free.

The lock is an in-process :class:`threading.Lock` keyed by the real path of
the root (``flock`` is per open file description, not per thread) **plus**
an exclusive ``flock`` on ``<lock dir>/<sha256(realpath)[:32]>.lock``, so it
also excludes across processes. The file lives in the portal's data dir,
never in the repository, so a repo cannot plant or symlink it. Lock files
are never deleted: unlinking a held ``flock`` file races a concurrent opener
(two holders on different inodes), and there is one small file per project
root, bounded by the number of projects.

The lock directory comes from :func:`configure_lock_dir` (the portal), else
``$WHYGRAPH_CODEGRAPH_LOCK_DIR`` (a scan child), else there is no file lock
and only the thread lock applies (tests).
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import anyio

from .exceptions import CodeGraphBootstrapError

LOCK_DIR_ENV: str = "WHYGRAPH_CODEGRAPH_LOCK_DIR"
"""Environment variable naming the lock directory for a process that did not configure one."""

POLL_SEC: float = 0.1
"""How often :func:`async_writer_lock` retries."""

_configured: Path | None = None
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def portal_lock_dir(data: Path) -> Path:
    """Return the lock directory under the portal data dir ``data``.

    Parameters
    ----------
    data : Path
        The portal data directory.

    Returns
    -------
    Path
        ``<data>/locks/codegraph``.
    """
    return data / "locks" / "codegraph"


def configure_lock_dir(path: Path | None) -> None:
    """Set (or clear, with ``None``) the process-wide lock directory.

    Parameters
    ----------
    path : Path or None
        The directory for the ``.lock`` files; created (mode 0700) on first
        use. ``None`` falls back to :data:`LOCK_DIR_ENV`.
    """
    global _configured
    _configured = path


def lock_dir() -> Path | None:
    """Return the lock directory in effect, or ``None`` when there is none."""
    if _configured is not None:
        return _configured
    value = os.environ.get(LOCK_DIR_ENV)
    return Path(value) if value else None


def _thread_lock(key: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


def _key(root: Path) -> str:
    return os.path.realpath(root)


def _open_lock_file(key: str) -> int | None:
    """Open (creating) the lock file for ``key``; ``None`` when no lock dir is set."""
    directory = lock_dir()
    if directory is None:
        return None
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".lock"
    try:
        return os.open(
            directory / name,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except OSError as exc:
        raise CodeGraphBootstrapError(
            f"cannot open the CodeGraph writer lock file: {exc}"
        ) from exc


def _unlock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class _Attempt:
    """A blocking ``flock`` running on a helper thread, abandonable by its caller."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.guard = threading.Lock()
        self.done = threading.Event()
        self.acquired = False
        self.abandoned = False
        self.error: OSError | None = None

    def run(self) -> None:
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        except OSError as exc:
            self.error = exc
            os.close(self.fd)
            self.done.set()
            return
        with self.guard:
            if self.abandoned:
                _unlock(self.fd)
                return
            self.acquired = True
        self.done.set()


def _flock_blocking(fd: int, wait: float | None) -> bool:
    """Take ``LOCK_EX`` on ``fd`` within ``wait`` seconds; closes ``fd`` on failure."""
    try:
        if wait is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return True
        if wait <= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return False
            return True
    except OSError:
        os.close(fd)
        raise
    attempt = _Attempt(fd)
    threading.Thread(
        target=attempt.run, name="codegraph-writer-lock", daemon=True
    ).start()
    attempt.done.wait(wait)
    with attempt.guard:
        if attempt.acquired:
            return True
        if not attempt.done.is_set():
            attempt.abandoned = True  # the helper releases and closes on acquire
            return False
    if attempt.error is not None:
        raise attempt.error
    return False  # pragma: no cover - done without acquired or error


@contextmanager
def writer_lock(root: Path, *, wait: float | None) -> Iterator[bool]:
    """Hold the CodeGraph writer lock for ``root`` (sync callers only).

    The blocking ``flock`` runs in a helper thread that the caller joins
    with a deadline; on timeout the attempt is abandoned and the helper,
    once it acquires, releases at once and exits. Never a signal. Never
    call this with ``wait > 0`` on an event loop - use
    :func:`async_writer_lock` there.

    Parameters
    ----------
    root : Path
        The repository root; the lock is per real path.
    wait : float or None
        Seconds to wait. ``0`` is a try-lock; ``None`` waits forever.

    Yields
    ------
    bool
        ``True`` when the lock is held (and released on exit), ``False``
        when it could not be taken within ``wait``.

    Raises
    ------
    CodeGraphBootstrapError
        If the lock file cannot be opened (e.g. it is a symbolic link).
    """
    key = _key(root)
    tlock = _thread_lock(key)
    deadline = None if wait is None else time.monotonic() + wait
    if not tlock.acquire(timeout=-1 if wait is None else max(0.0, wait)):
        yield False
        return
    fd: int | None = None
    try:
        file_fd = _open_lock_file(key)
        if file_fd is not None:
            remaining = None if deadline is None else deadline - time.monotonic()
            if not _flock_blocking(file_fd, remaining):
                yield False
                return
            fd = file_fd
        yield True
    finally:
        try:
            if fd is not None:
                _unlock(fd)
        finally:
            tlock.release()


@asynccontextmanager
async def async_writer_lock(root: Path, *, wait: float | None) -> AsyncIterator[bool]:
    """The bridge's form of :func:`writer_lock`: polls, never blocks the loop.

    Retries a non-blocking thread-lock acquire and ``flock(LOCK_EX |
    LOCK_NB)`` every :data:`POLL_SEC` with ``await anyio.sleep``. A plain
    lock (not an ``RLock``), so it may be released from another task.

    Parameters
    ----------
    root : Path
        The repository root.
    wait : float or None
        Seconds to wait; ``0`` tries once; ``None`` waits forever.

    Yields
    ------
    bool
        ``True`` when held, ``False`` when ``wait`` ran out.
    """
    key = _key(root)
    tlock = _thread_lock(key)
    deadline = None if wait is None else time.monotonic() + wait
    file_fd = _open_lock_file(key)
    held_fd: int | None = None
    held = False
    try:
        while True:
            if tlock.acquire(blocking=False):
                if file_fd is None:
                    held = True
                else:
                    try:
                        fcntl.flock(file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        held_fd = file_fd
                        held = True
                    except BlockingIOError:
                        tlock.release()
                if held:
                    break
            if deadline is not None and time.monotonic() >= deadline:
                break
            await anyio.sleep(POLL_SEC)
        yield held
    finally:
        if held:
            try:
                if held_fd is not None:
                    fcntl.flock(held_fd, fcntl.LOCK_UN)
            finally:
                tlock.release()
        if file_fd is not None:
            os.close(file_fd)


__all__ = [
    "LOCK_DIR_ENV",
    "async_writer_lock",
    "configure_lock_dir",
    "lock_dir",
    "portal_lock_dir",
    "writer_lock",
]
