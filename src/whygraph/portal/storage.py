"""The storage sampler (plan 59, section 4.10, D14, D25, D42).

Production only: it measures the server clone, the history directory and the
run files of every ``github`` project and keeps one sparse row per project and
UTC day in ``storage_samples``. Readers take the latest sample on or before a
day (:func:`latest_project_bytes`), never a plain ``GROUP BY day``.

A walk is iterative, follows no symlink, counts apparent size (``st_size``)
and never aborts on an error. No database session is open while one runs, and
only one walk runs at a time (``state.storage_lock``, taken inside
:func:`sample`).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col, select

from whygraph.core.safe_paths import UnsafePathError, check_inside

from .clones import clone_dir_is_safe
from .db import get_session
from .models import Organization, Project, StorageSample, _now
from .paths import project_data_dir

_log = logging.getLogger(__name__)

SAMPLE_MAX_AGE = timedelta(hours=24)
"""A project is due when its newest sample is older than this."""

RETENTION_DAYS = 400
"""Samples older than this are pruned, except each project's newest."""


class StorageBusy(Exception):
    """Another storage walk holds ``state.storage_lock`` (``409 busy``, D42)."""


@dataclass(frozen=True)
class ProjectSizes:
    """The five measured parts of one project, in bytes.

    Attributes
    ----------
    history : int
        Everything under ``<data>/projects/<org>/<slug>/``.
    git : int
        The clone's ``.git``.
    codegraph : int
        The clone's ``.codegraph``.
    worktree : int
        The rest of the clone.
    runs : int
        The project's run files under ``<data>/runs``.
    """

    history: int
    git: int
    codegraph: int
    worktree: int
    runs: int


@dataclass(frozen=True)
class DueProject:
    """A project to measure.

    Attributes
    ----------
    id, org_id : int
        The project.
    org_slug, slug : str
        Its org slug and slug.
    root : str
        ``Project.root``, relative to the data dir.
    """

    id: int
    org_id: int
    org_slug: str
    slug: str
    root: str


def _walk(
    root: Path, base: Path, stop: Callable[[], bool], *, split: bool = False
) -> dict[str, int]:
    """Walk ``root`` and return its apparent size, by bucket when ``split``.

    Without ``split`` there is one bucket, ``"all"``. With it the top-level
    entries of ``root`` go to ``git`` (``.git``), ``codegraph`` (``.codegraph``)
    or ``worktree``.
    """
    sizes = {"git": 0, "codegraph": 0, "worktree": 0} if split else {"all": 0}
    try:
        check_inside(base, root)
        os.lstat(root)
    except (OSError, UnsafePathError):
        return sizes
    if root.is_symlink() or not root.is_dir():
        return sizes
    stack: list[tuple[Path, str | None]] = [(root, None)]
    while stack:
        if stop():
            break
        path, bucket = stack.pop()
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                if bucket is None and split:
                    name = (
                        "git"
                        if entry.name == ".git"
                        else "codegraph"
                        if entry.name == ".codegraph"
                        else "worktree"
                    )
                else:
                    name = bucket or "all"
                if entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), name))
                elif entry.is_file(follow_symlinks=False):
                    sizes[name] += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return sizes


def tree_size(
    root: Path, base: Path, *, stop: Callable[[], bool] = lambda: False
) -> int:
    """Return the apparent size (``st_size``) of the regular files under ``root``.

    ``root`` is checked with ``check_inside(base, root)`` and ``lstat`` first:
    a symlinked or missing root counts ``0``. The walk is iterative, follows no
    symlink, counts symlinks, FIFOs, sockets and devices as ``0``, counts a
    hard link once per path and skips any entry that raises ``OSError``.

    Parameters
    ----------
    root : Path
        The directory to measure.
    base : Path
        The directory ``root`` must lie under with no symlink on the way.
    stop : callable, optional
        Called once per directory; the walk returns early when it is true.

    Returns
    -------
    int
        The size in bytes.
    """
    return _walk(root, base, stop)["all"]


def _run_bytes(data_dir: Path, project_id: int) -> int:
    from .runner import run_files

    runs_dir = data_dir / "runs"
    total = 0
    for run_id, events, log in run_files([project_id]):
        for rel in (events, log or f"runs/{run_id}.log"):
            if not rel:
                continue
            try:
                path = check_inside(runs_dir, data_dir / rel)
                total += os.lstat(path).st_size
            except (OSError, UnsafePathError):
                continue
    return total


def measure_project(
    state, project: DueProject, *, stop: Callable[[], bool] = lambda: False
) -> ProjectSizes:
    """Measure one project: history dir, clone (split three ways) and run files.

    Parameters
    ----------
    state : PortalState
        The portal state (only ``data_dir`` is read).
    project : DueProject
        The project; its root passed :func:`due_projects`.
    stop : callable, optional
        As for :func:`tree_size`.

    Returns
    -------
    ProjectSizes
        The five parts.
    """
    data = state.data_dir
    history = tree_size(
        project_data_dir(data, project.org_slug, project.slug), data, stop=stop
    )
    clone = _walk(data / project.root, data, stop, split=True)
    return ProjectSizes(
        history=history,
        git=clone["git"],
        codegraph=clone["codegraph"],
        worktree=clone["worktree"],
        runs=_run_bytes(data, project.id),
    )


def due_projects(
    db, state, *, project_id: int | None = None, now: str | None = None
) -> list[DueProject]:
    """The ``github`` projects to measure now.

    A project is due when it has no sample, its newest ``measured_at`` is older
    than 24 h, or older than ``last_scan_at``. When the project or its org is
    soft-deleted it is due only while no sample is at or after that timestamp
    (D25). With ``project_id`` the due rules are ignored. A root that fails
    ``clone_dir_is_safe`` is never returned.

    Parameters
    ----------
    db : Session
        An open session (the caller keeps it short).
    state : PortalState
        The portal state (``data_dir``).
    project_id : int, optional
        Measure just this project, due or not.
    now : str, optional
        The current ISO timestamp (tests).

    Returns
    -------
    list of DueProject
        In project id order.
    """
    moment = now or _now()
    cutoff = (
        datetime.fromisoformat(moment).astimezone(timezone.utc) - SAMPLE_MAX_AGE
    ).isoformat(timespec="seconds")
    newest = dict(
        db.exec(
            select(StorageSample.project_id, text("max(measured_at)"))
            .select_from(StorageSample)
            .group_by(col(StorageSample.project_id))
        ).all()
    )
    stmt = (
        select(
            Project.id,
            Project.org_id,
            Organization.slug,
            Project.slug,
            Project.root,
            Project.last_scan_at,
            Project.deleted_at,
            Organization.deleted_at,
        )
        .join(Organization, col(Organization.id) == col(Project.org_id))
        .where(Project.source == "github")
        .order_by(col(Project.id))
    )
    if project_id is not None:
        stmt = stmt.where(Project.id == project_id)
    out: list[DueProject] = []
    for pid, org_id, org_slug, slug, root, scanned, p_deleted, o_deleted in db.exec(
        stmt
    ).all():
        last = newest.get(pid)
        deleted = p_deleted or o_deleted
        if project_id is None:
            if deleted:
                due = last is None or last < deleted
            else:
                due = (
                    last is None
                    or last < cutoff
                    or (scanned is not None and last < scanned)
                )
            if not due:
                continue
        if not clone_dir_is_safe(state.data_dir / root, state.data_dir, depth=2):
            continue
        out.append(DueProject(pid, org_id, org_slug, slug, root))
    return out


def _upsert(project: DueProject, sizes: ProjectSizes) -> None:
    measured_at = _now()
    values = {
        "org_id": project.org_id,
        "project_id": project.id,
        "day": measured_at[:10],
        "history_bytes": sizes.history,
        "git_bytes": sizes.git,
        "codegraph_bytes": sizes.codegraph,
        "worktree_bytes": sizes.worktree,
        "runs_bytes": sizes.runs,
        "measured_at": measured_at,
    }
    stmt = pg_insert(StorageSample).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_storage_samples_project_day",
        set_={k: stmt.excluded[k] for k in values if k not in ("project_id", "day")},
    )
    with get_session() as db:
        db.execute(stmt)


def sample(state, *, project_id: int | None = None, blocking: bool = True) -> int:
    """Measure every due project and upsert one row per (project, UTC day).

    Takes ``state.storage_lock`` itself, per project (callers never hold it).
    The due list is read in one short session, each walk runs with no session
    open, and each row is written in its own short session. Stops between
    projects, and inside a walk, once ``state.shutdown_event`` is set.

    Parameters
    ----------
    state : PortalState
        The portal state.
    project_id : int, optional
        Measure only this project, whether or not it is due.
    blocking : bool, optional
        ``False`` raises :class:`StorageBusy` when another walk runs (the
        "Measure now" route); the loop waits.

    Returns
    -------
    int
        How many projects were measured.

    Raises
    ------
    StorageBusy
        ``blocking`` is false and the lock is held.
    """
    event = state.shutdown_event

    def stopped() -> bool:
        return event is not None and event.is_set()

    with get_session() as db:
        todo = due_projects(db, state, project_id=project_id)
    done = 0
    for project in todo:
        if stopped():
            break
        if not state.storage_lock.acquire(blocking=blocking):
            raise StorageBusy("a storage measurement is already running")
        try:
            sizes = measure_project(state, project, stop=stopped)
            if stopped():
                break  # a cut-short walk is not a measurement
            _upsert(project, sizes)
            done += 1
        finally:
            state.storage_lock.release()
    return done


def prune(days: int = RETENTION_DAYS, *, now: datetime | None = None) -> int:
    """Delete samples older than ``days`` days, except each project's newest.

    Without the exception a project deleted long ago would lose its only row
    and the D14 figure would read 0 (P18).

    Parameters
    ----------
    days : int, optional
        The retention.
    now : datetime, optional
        The current time (tests).

    Returns
    -------
    int
        How many rows were deleted.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = (moment - timedelta(days=days)).date().isoformat()
    with get_session() as db:
        result = db.execute(
            text(
                "DELETE FROM storage_samples s WHERE s.day < :cutoff AND EXISTS "
                "(SELECT 1 FROM storage_samples n "
                "WHERE n.project_id = s.project_id AND n.day > s.day)"
            ),
            {"cutoff": cutoff},
        )
        return int(result.rowcount or 0)


def latest_project_bytes(
    db, *, org_id: int | None = None
) -> dict[int, tuple[int, int]]:
    """The latest sample of each project, carried forward, as total bytes.

    Parameters
    ----------
    db : Session
        An open session.
    org_id : int, optional
        Only this org's projects.

    Returns
    -------
    dict
        ``project_id -> (org_id, total bytes)`` for projects with a sample;
        absent means no sample (the caller shows ``null``).
    """
    where = "WHERE org_id = :org_id" if org_id is not None else ""
    rows = db.execute(
        text(
            "SELECT DISTINCT ON (project_id) project_id, org_id, "
            "history_bytes + git_bytes + codegraph_bytes + worktree_bytes "
            "+ runs_bytes FROM storage_samples "
            f"{where} ORDER BY project_id, day DESC"
        ),
        {"org_id": org_id} if org_id is not None else {},
    ).all()
    return {int(p): (int(o), int(b)) for p, o, b in rows}


__all__ = [
    "DueProject",
    "ProjectSizes",
    "StorageBusy",
    "due_projects",
    "latest_project_bytes",
    "measure_project",
    "prune",
    "sample",
    "tree_size",
]
