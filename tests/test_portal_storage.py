"""The storage sampler (plan 59, section 4.10) and its two admin fields."""

# ruff: noqa: F811 -- pytest fixtures are imported from other test modules

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import anyio
import pytest
from sqlmodel import col, select

from test_portal_admin_routes import be_ada, url
from test_portal_app import env, production_env  # noqa: F401 -- fixtures
from test_portal_connect import cn  # noqa: F401 -- fixture
from whygraph.portal import app as portal_app
from whygraph.portal import db as portal_db
from whygraph.portal import storage
from whygraph.portal.models import Organization, Project, ScanRun, StorageSample, _now
from whygraph.portal.storage import StorageBusy, sample, tree_size


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


# ---------------------------------------------------------------------------
# tree_size
# ---------------------------------------------------------------------------


def test_tree_size_counts_regular_files_and_no_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside / "big", 5000)
    root = tmp_path / "root"
    _write(root / "a", 10)
    _write(root / "sub" / "b", 20)
    (root / "link_file").symlink_to(outside / "big")
    (root / "link_dir").symlink_to(outside)
    os.mkfifo(root / "fifo")
    assert tree_size(root, tmp_path) == 30


def test_a_symlinked_root_counts_zero(tmp_path: Path) -> None:
    real = tmp_path / "real"
    _write(real / "a", 10)
    (tmp_path / "root").symlink_to(real)
    assert tree_size(tmp_path / "root", tmp_path) == 0
    assert tree_size(tmp_path / "missing", tmp_path) == 0


def test_a_file_removed_mid_walk_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    _write(root / "a", 10)
    _write(root / "gone", 99)
    real = os.scandir

    class _Entry:
        def __init__(self, entry: os.DirEntry) -> None:
            self._e = entry
            self.name, self.path = entry.name, entry.path

        def is_dir(self, **kw):
            return self._e.is_dir(**kw)

        def is_file(self, **kw):
            return self._e.is_file(**kw)

        def stat(self, **kw):
            if self.name == "gone":
                raise FileNotFoundError(self.path)
            return self._e.stat(**kw)

    class _Scan:
        def __init__(self, path):
            self._it = real(path)

        def __enter__(self):
            return (_Entry(e) for e in self._it)

        def __exit__(self, *exc):
            self._it.close()

    monkeypatch.setattr(storage.os, "scandir", _Scan)
    assert tree_size(root, tmp_path) == 10


def test_a_two_thousand_level_tree_does_not_raise(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _write(root / "top", 7)
    fd = os.open(root, os.O_RDONLY)
    try:
        for _ in range(2000):
            os.mkdir("d", dir_fd=fd)
            nxt = os.open("d", os.O_RDONLY, dir_fd=fd)
            os.close(fd)
            fd = nxt
    finally:
        os.close(fd)
    assert tree_size(root, tmp_path) >= 7  # deep entries may hit PATH_MAX and skip


def test_the_walk_stops_when_asked(tmp_path: Path) -> None:
    _write(tmp_path / "root" / "a", 10)
    assert tree_size(tmp_path / "root", tmp_path, stop=lambda: True) == 0


# ---------------------------------------------------------------------------
# measure_project: the buckets
# ---------------------------------------------------------------------------


def test_worktree_excludes_git_and_codegraph(
    portal_database: str, tmp_path: Path
) -> None:
    clone = tmp_path / "repos" / "acme" / "api"
    _write(clone / ".git" / "obj", 100)
    _write(clone / ".codegraph" / "db", 40)
    _write(clone / "src" / "x.py", 5)
    _write(clone / "README", 3)
    _write(tmp_path / "projects" / "acme" / "api" / "whygraph.db", 1000)
    state = SimpleNamespace(data_dir=tmp_path)
    project = storage.DueProject(0, 0, "acme", "api", "repos/acme/api")
    sizes = storage.measure_project(state, project)
    assert (sizes.git, sizes.codegraph, sizes.worktree) == (100, 40, 8)
    assert sizes.history == 1000


# ---------------------------------------------------------------------------
# sample (Postgres)
# ---------------------------------------------------------------------------


@pytest.fixture
def world(portal_database: str, tmp_path: Path) -> Iterator[SimpleNamespace]:
    data = tmp_path / "data"
    data.mkdir()
    with portal_db.get_session() as db:
        org = Organization(slug="acme", name="Acme")
        db.add(org)
        db.flush()
        org_id = org.id
        ids = {}
        for slug, source, root in (
            ("api", "github", "repos/acme/api"),
            ("loc", "local", "repos/acme/loc"),
            ("bad", "github", "../elsewhere"),
        ):
            project = Project(
                org_id=org_id, slug=slug, name=slug, source=source, root=root
            )
            db.add(project)
            db.flush()
            ids[slug] = project.id
    _write(data / "repos" / "acme" / "api" / "f", 10)
    _write(data / "repos" / "acme" / "loc" / "f", 10)
    state = SimpleNamespace(
        data_dir=data, storage_lock=threading.Lock(), shutdown_event=threading.Event()
    )
    yield SimpleNamespace(state=state, org_id=org_id, ids=ids, data=data)


def _samples(pid: int) -> list[SimpleNamespace]:
    with portal_db.get_session() as db:
        return [
            SimpleNamespace(**r.model_dump())
            for r in db.exec(
                select(StorageSample)
                .where(StorageSample.project_id == pid)
                .order_by(col(StorageSample.day))
            )
        ]


def _set(pid: int, **fields) -> None:
    with portal_db.get_session() as db:
        row = db.get(Project, pid)
        for k, v in fields.items():
            setattr(row, k, v)
        db.add(row)


def test_sample_measures_github_projects_only_and_upserts_per_day(
    world: SimpleNamespace,
) -> None:
    assert sample(world.state) == 1  # the local and the unsafe row are skipped
    assert _samples(world.ids["loc"]) == [] and _samples(world.ids["bad"]) == []
    (row,) = _samples(world.ids["api"])
    assert row.worktree_bytes == 10 and row.day == row.measured_at[:10]
    # Forced, not due: upsert the same day.
    _write(world.data / "repos" / "acme" / "api" / "g", 5)
    assert sample(world.state, project_id=world.ids["api"]) == 1
    (row,) = _samples(world.ids["api"])
    assert row.worktree_bytes == 15


def test_the_due_rules(world: SimpleNamespace) -> None:
    pid = world.ids["api"]

    def due(now: str | None = None) -> list[int]:
        with portal_db.get_session() as db:
            return [p.id for p in storage.due_projects(db, world.state, now=now)]

    assert due() == [pid]  # no sample
    sample(world.state)
    assert due() == []  # fresh
    later = (datetime.now(timezone.utc) + timedelta(hours=25)).isoformat(
        timespec="seconds"
    )
    assert due(later) == [pid]  # older than 24 h
    _set(
        pid,
        last_scan_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(
            timespec="seconds"
        ),
    )
    assert due() == [pid]  # scanned since


def test_a_deleted_project_is_measured_once(world: SimpleNamespace) -> None:
    pid = world.ids["api"]
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(
        timespec="seconds"
    )
    _set(pid, deleted_at=stamp)
    assert sample(world.state) == 1
    assert sample(world.state) == 0  # a sample at or after deleted_at exists


def test_no_session_is_open_during_a_walk(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[int] = []
    real = portal_db.get_session

    from contextlib import contextmanager

    @contextmanager
    def tracked():
        opened.append(1)
        try:
            with real() as s:
                yield s
        finally:
            opened.pop()

    monkeypatch.setattr(storage, "get_session", tracked)
    seen: list[int] = []
    real_measure = storage.measure_project

    def measure(*a, **kw):
        seen.append(len(opened))
        return real_measure(*a, **kw)

    monkeypatch.setattr(storage, "measure_project", measure)
    assert sample(world.state) == 1
    assert seen == [0]


def test_it_stops_when_shutdown_is_set(world: SimpleNamespace) -> None:
    world.state.shutdown_event.set()
    assert sample(world.state) == 0
    assert _samples(world.ids["api"]) == []


def test_a_held_lock_raises_busy_when_not_blocking(world: SimpleNamespace) -> None:
    world.state.storage_lock.acquire()
    try:
        with pytest.raises(StorageBusy):
            sample(world.state, project_id=world.ids["api"], blocking=False)
    finally:
        world.state.storage_lock.release()
    assert sample(world.state, project_id=world.ids["api"], blocking=False) == 1


def test_a_run_file_counts_toward_runs(world: SimpleNamespace) -> None:
    with portal_db.get_session() as db:
        run = ScanRun(
            project_id=world.ids["api"],
            org_id=world.org_id,
            status="ok",
            trigger="sync",
            log_path="runs/1.log",
        )
        db.add(run)
    _write(world.data / "runs" / "1.log", 77)
    sample(world.state)
    assert _samples(world.ids["api"])[0].runs_bytes == 77


def _add_sample(world: SimpleNamespace, day: str, size: int) -> None:
    with portal_db.get_session() as db:
        db.add(
            StorageSample(
                org_id=world.org_id,
                project_id=world.ids["api"],
                day=day,
                history_bytes=size,
                git_bytes=0,
                codegraph_bytes=0,
                worktree_bytes=0,
                runs_bytes=0,
                measured_at=f"{day}T00:00:00+00:00",
            )
        )


def test_prune_keeps_each_projects_newest_row(world: SimpleNamespace) -> None:
    for day in ("2020-01-01", "2020-01-02"):
        _add_sample(world, day, 1)
    assert storage.prune() == 1
    assert [r.day for r in _samples(world.ids["api"])] == ["2020-01-02"]
    assert storage.prune() == 0  # the only row stays however old


def test_the_first_pass_waits_five_minutes_then_repeats(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    calls: list[str] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) > 2:
            raise anyio.get_cancelled_exc_class()()

    monkeypatch.setattr(portal_app.anyio, "sleep", fake_sleep)
    monkeypatch.setattr(storage, "sample", lambda state: calls.append("s") or 0)
    monkeypatch.setattr(storage, "prune", lambda: calls.append("p") or 0)

    async def run() -> None:
        with anyio.move_on_after(5):
            try:
                await portal_app._sample_storage(world.state)
            except BaseException as exc:  # noqa: BLE001 -- the cancel that ends the test
                if not isinstance(exc, anyio.get_cancelled_exc_class()):
                    raise

    anyio.run(run)
    assert sleeps[:2] == [300, 6 * 3600]
    assert calls[:2] == ["s", "p"]


# ---------------------------------------------------------------------------
# The two admin fields
# ---------------------------------------------------------------------------


def test_admin_storage_fields_use_the_latest_sample_carried_forward(
    cn: SimpleNamespace,
) -> None:
    be_ada(cn)
    rows = {r["slug"]: r for r in cn.client.get(url("/api/admin/orgs")).json()}
    assert rows["acme"]["storage_bytes"] is None  # no sample yet
    detail = cn.client.get(url("/api/admin/orgs/acme")).json()
    assert all(p["storage_bytes"] is None for p in detail["projects"])
    for day, size in (("2026-11-01", 100), ("2026-11-10", 300)):
        with portal_db.get_session() as db:
            db.add(
                StorageSample(
                    org_id=cn.orgs["acme"],
                    project_id=cn.projects["api"],
                    day=day,
                    history_bytes=size,
                    git_bytes=1,
                    codegraph_bytes=2,
                    worktree_bytes=3,
                    runs_bytes=4,
                    measured_at=_now(),
                )
            )
    rows = {r["slug"]: r for r in cn.client.get(url("/api/admin/orgs")).json()}
    assert rows["acme"]["storage_bytes"] == 310  # the newest row, not a sum of days
    assert rows["bravo"]["storage_bytes"] is None
    detail = cn.client.get(url("/api/admin/orgs/acme")).json()
    projects = {p["slug"]: p for p in detail["projects"]}
    assert projects["api"]["storage_bytes"] == 310
    assert projects["web"]["storage_bytes"] is None
