"""Tests for :mod:`whygraph.services.codegraph.writer_lock` (plan section 4.2b)."""

from __future__ import annotations

import fcntl
import importlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import anyio
import pytest

from whygraph.services.codegraph.exceptions import CodeGraphBootstrapError

# The package re-exports the function under the module's name.
wl = importlib.import_module("whygraph.services.codegraph.writer_lock")


@pytest.fixture(autouse=True)
def _lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv(wl.LOCK_DIR_ENV, raising=False)
    directory = tmp_path / "locks"
    wl.configure_lock_dir(directory)
    yield directory
    wl.configure_lock_dir(None)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


def _lock_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.lock"))


def test_the_lock_dir_and_file_are_private(root: Path, _lock_dir: Path) -> None:
    with wl.writer_lock(root, wait=0) as held:
        assert held
    (lock_file,) = _lock_files(_lock_dir)
    assert len(lock_file.stem) == 32
    assert lock_file.stat().st_mode & 0o777 == 0o600
    assert _lock_dir.stat().st_mode & 0o777 == 0o700


def test_excludes_across_threads(root: Path) -> None:
    order: list[str] = []
    entered = threading.Event()

    def other() -> None:
        with wl.writer_lock(root, wait=5) as held:
            assert held
            order.append("other")

    with wl.writer_lock(root, wait=0) as held:
        assert held
        thread = threading.Thread(target=other)
        thread.start()
        entered.wait(0.2)
        order.append("first")
    thread.join(5)

    assert order == ["first", "other"]


def test_try_lock_fails_while_held_and_succeeds_after(root: Path) -> None:
    with wl.writer_lock(root, wait=0) as held:
        assert held
        with wl.writer_lock(root, wait=0) as again:
            assert again is False
    with wl.writer_lock(root, wait=0) as held:
        assert held


def test_a_blocking_wait_returns_false_at_the_deadline(root: Path) -> None:
    with wl.writer_lock(root, wait=0):
        started = time.monotonic()
        with wl.writer_lock(root, wait=0.3) as held:
            assert held is False
        assert 0.25 <= time.monotonic() - started < 3


_HOLDER = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
time.sleep(float(sys.argv[2]))
"""


def _hold_in_subprocess(lock_file: Path, seconds: float) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(lock_file), str(seconds)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
    return proc


def _file_for(root: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    with wl.writer_lock(root, wait=0):
        pass
    (lock_file,) = _lock_files(directory)
    return lock_file


def test_excludes_across_processes(root: Path, _lock_dir: Path) -> None:
    holder = _hold_in_subprocess(_file_for(root, _lock_dir), 1.0)
    try:
        with wl.writer_lock(root, wait=0) as held:
            assert held is False
        with wl.writer_lock(root, wait=10) as held:  # waits for the holder to exit
            assert held
    finally:
        holder.kill()
        holder.wait()


def test_an_abandoned_attempt_releases_the_lock_once_it_acquires(
    root: Path, _lock_dir: Path
) -> None:
    holder = _hold_in_subprocess(_file_for(root, _lock_dir), 0.6)
    try:
        with wl.writer_lock(root, wait=0.1) as held:
            assert held is False
        holder.wait(10)
        time.sleep(0.2)  # the helper thread acquires, sees the abandonment, releases
        with wl.writer_lock(root, wait=0) as held:
            assert held
    finally:
        holder.kill()
        holder.wait()


def test_a_symlinked_lock_file_is_refused(
    root: Path, _lock_dir: Path, tmp_path: Path
) -> None:
    lock_file = _file_for(root, _lock_dir)
    lock_file.unlink()
    target = tmp_path / "elsewhere"
    target.write_text("")
    lock_file.symlink_to(target)

    with pytest.raises(CodeGraphBootstrapError, match="lock file"):
        with wl.writer_lock(root, wait=0):
            pytest.fail("must not be held")
    # and the thread lock was released
    lock_file.unlink()
    with wl.writer_lock(root, wait=0) as held:
        assert held


def test_no_lock_dir_means_only_the_thread_lock(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wl.configure_lock_dir(None)
    monkeypatch.delenv(wl.LOCK_DIR_ENV, raising=False)

    with wl.writer_lock(root, wait=0) as held:
        assert held
        with wl.writer_lock(root, wait=0) as again:
            assert again is False
    assert not (tmp_path / "locks").exists()


def test_the_env_variable_names_the_lock_dir(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wl.configure_lock_dir(None)
    monkeypatch.setenv(wl.LOCK_DIR_ENV, str(tmp_path / "from-env"))

    with wl.writer_lock(root, wait=0) as held:
        assert held
    assert len(_lock_files(tmp_path / "from-env")) == 1


def test_different_roots_do_not_exclude_each_other(root: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    with wl.writer_lock(root, wait=0) as a, wl.writer_lock(other, wait=0) as b:
        assert a and b


def test_a_symlinked_path_to_the_root_is_the_same_lock(
    root: Path, tmp_path: Path
) -> None:
    link = tmp_path / "link"
    link.symlink_to(root)
    with wl.writer_lock(root, wait=0):
        with wl.writer_lock(link, wait=0) as held:
            assert held is False


def test_the_async_form_excludes_and_does_not_block_the_loop(root: Path) -> None:
    ticks: list[float] = []

    async def ticker(stop: anyio.Event) -> None:
        while not stop.is_set():
            ticks.append(time.monotonic())
            await anyio.sleep(0.02)

    async def main() -> tuple[bool, bool]:
        stop = anyio.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(ticker, stop)
            async with wl.async_writer_lock(root, wait=0) as first:
                async with wl.async_writer_lock(root, wait=0.3) as second:
                    pass
            stop.set()
        return first, second

    first, second = anyio.run(main)

    assert first is True and second is False
    assert len(ticks) >= 8  # the loop kept running during the 0.3 s wait


def test_the_async_form_takes_the_lock_when_it_frees(root: Path) -> None:
    async def main() -> bool:
        release = threading.Timer(0.25, lambda: None)

        def hold_then_release() -> None:
            with wl.writer_lock(root, wait=0):
                time.sleep(0.25)

        thread = threading.Thread(target=hold_then_release)
        thread.start()
        await anyio.sleep(0.05)
        try:
            async with wl.async_writer_lock(root, wait=5) as held:
                return held
        finally:
            thread.join()
            release.cancel()

    assert anyio.run(main) is True


def test_the_async_form_excludes_a_sync_holder_in_another_process(
    root: Path, _lock_dir: Path
) -> None:
    holder = _hold_in_subprocess(_file_for(root, _lock_dir), 5.0)

    async def main() -> bool:
        async with wl.async_writer_lock(root, wait=0.3) as held:
            return held

    try:
        assert anyio.run(main) is False
    finally:
        holder.kill()
        holder.wait()
    # the failed attempt left nothing held
    with wl.writer_lock(root, wait=0) as held:
        assert held


def test_the_lock_is_released_when_the_body_raises(root: Path) -> None:
    with pytest.raises(RuntimeError):
        with wl.writer_lock(root, wait=0):
            raise RuntimeError("boom")
    with wl.writer_lock(root, wait=0) as held:
        assert held


def test_a_held_lock_is_a_real_flock_on_the_file(root: Path, _lock_dir: Path) -> None:
    with wl.writer_lock(root, wait=0):
        (lock_file,) = _lock_files(_lock_dir)
        fd = os.open(lock_file, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
