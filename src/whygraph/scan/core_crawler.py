"""CoreCrawler - the scan's "Core commits" phase (plan 49 section 5.3).

Finds the **core set**: the commits a rationale card on a **core file** can
show by the evidence collector's blame, predecessor and area rules. Core
files are the files at ``HEAD`` the rest of the code depends on most,
ranked from the CodeGraph index (:func:`select_core_files`); with no usable
index every text file is core instead.

The pass is incremental and resumable. Per-file and per-rename blame
results are cached in ``core_file`` / ``core_rename`` and recomputed only
when a file's blob or history (or the blame rules) change; ``core_commit``
and ``core_bulk_pair`` are replaced only by a pass that has a current row
for every selected file and rename event, so a pass cut short by its wall
keeps the previous set. Best-effort: any failure lands on
:attr:`CoreCrawler.warning` and the previous rows stay.

The index is untrusted input: its paths only choose among the ``HEAD``
tree entries the pass listed itself, and are never opened, blamed or
passed to git on their own.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import math
import os
import sqlite3
import time
from collections.abc import Collection, Iterable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from rich.progress import Progress
from sqlalchemy import insert, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import col, delete, select

from whygraph.core.safe_paths import UnsafePathError, check_inside
from whygraph.db import get_session
from whygraph.db.models import (
    Commit,
    CommitFileChange,
    CoreBulkPair,
    CoreCommit,
    CoreFile,
    CoreRename,
    CoreState,
)
from whygraph.services.codegraph import CodeGraph, CodeGraphError
from whygraph.services.codegraph.paths import CODEGRAPH_DB_RELPATH
from whygraph.services.git import GitError, Repository

from .crawler import Crawler

if TYPE_CHECKING:
    from whygraph.mcp.evidence import _Blamer

_log = logging.getLogger(__name__)

CORE_RULES_VERSION = 1
"""Bumped when the per-file rules change; part of ``core_state.rules_key``."""

CORE_AREA_DEPTH = 20
"""Newest default-branch commits per core file's alias set (the card's ``limit``)."""

CORE_MAX_BYTES = 1 << 20
"""Blobs larger than this are not listed, and predecessors larger are skipped."""

CORE_MAX_LINES = 20_000
"""Files and predecessors with more lines than this are skipped."""

CORE_BLAME_TIMEOUT_SEC = 30
"""Seconds one whole-file blame may take before it is recorded ``failed``."""

CORE_PHASE_WALL_SEC = 900.0
"""Seconds a walled pass (a first scan, a Quick rescan) takes new blame work for."""

CORE_WORKERS = max(1, min(4, (os.cpu_count() or 2) // 2))
"""Blames run at once."""

CORE_REFERENCE_SHARE = 0.95
"""Share of the index's cross-file references the core files must cover (D6)."""

CORE_EDGE_KINDS: tuple[str, ...] = (
    "calls",
    "references",
    "imports",
    "instantiates",
    "implements",
    "extends",
)
"""Index edge kinds that count as one file depending on another."""

CORE_INDEX_QUERY_SEC = 60.0
"""Seconds the in-degree query may run before it is aborted (a hostile index)."""

_PROGRESS_STEPS = 10_000
"""SQLite VM instructions between two deadline checks of the index query."""

_SKIP_ERRORCODES = frozenset({5, 6, 14})
"""Primary SQLite result codes that skip the phase: BUSY, LOCKED, CANTOPEN."""

_CODEGRAPH_DIR = ".codegraph/"
_SYMLINK_MODE = "120000"
_SUBMODULE_MODE = "160000"
_BATCH = 500
"""Rows per write, and paths per ``IN`` list."""


@dataclass(frozen=True)
class CoreSelection:
    """The files one core pass works on (:func:`select_core_files`).

    Attributes
    ----------
    paths : frozenset of str
        The core files, a subset of the eligible ``HEAD`` blobs.
    selection : str
        ``"codegraph"`` (ranked from the index) or ``"all_text"`` (the
        fallback: every eligible file).
    fallback_reason : str or None
        ``"no_index"`` when no index file existed, ``"unusable"`` for any
        other fallback, ``None`` with ``"codegraph"``.
    """

    paths: frozenset[str]
    selection: str
    fallback_reason: str | None


class _UnusableIndex(Exception):
    """The index exists but must not be used (a view, the query deadline)."""


def index_file_exists(root: Path) -> bool:
    """Whether ``root``'s CodeGraph index file exists, reached without a symlink.

    Parameters
    ----------
    root : Path
        The repository root.

    Returns
    -------
    bool
        ``False`` when the file is missing, or the path passes through a
        symlink or leaves ``root``.
    """
    try:
        return check_inside(root, CODEGRAPH_DB_RELPATH).is_file()
    except UnsafePathError:
        return False


def select_core_files(
    eligible_paths: Collection[str], repository: Repository
) -> CoreSelection | None:
    """Choose the core files among ``eligible_paths`` (plan 49 section 5.1).

    Ranks the eligible files by the cross-file references the CodeGraph
    index records into them (edge kinds :data:`CORE_EDGE_KINDS`, source
    node in another file), in ``(-in_degree, path)`` order, and takes them
    from the top until the running sum reaches
    ``ceil(CORE_REFERENCE_SHARE * total)``; the file that crosses the line
    is included. Paths only the index names are never selected.

    The index is opened read-only through :func:`check_inside` and is
    ignored when git tracks anything under ``.codegraph/``. Before the one
    fixed query, ``PRAGMA trusted_schema=OFF`` is set, ``nodes`` and
    ``edges`` must be tables (not views), and a progress handler aborts the
    query after :data:`CORE_INDEX_QUERY_SEC`.

    Parameters
    ----------
    eligible_paths : Collection of str
        The ``HEAD`` blobs that passed the listing filter.
    repository : Repository
        The repository whose ``.codegraph/codegraph.db`` is read.

    Returns
    -------
    CoreSelection or None
        The ranked selection; the fallback (every eligible file,
        ``selection = "all_text"``) when there is no index file
        (``"no_index"``) or the index is unusable (``"unusable"``); ``None``
        when the index is busy, locked or cannot be opened right now
        (SQLite ``BUSY`` / ``LOCKED`` / ``CANTOPEN``), so this scan skips
        the phase.
    """
    eligible = frozenset(eligible_paths)

    def fallback(reason: str, why: str) -> CoreSelection:
        _log.info("core files: every text file (%s: %s)", reason, why)
        return CoreSelection(eligible, "all_text", reason)

    root = repository.root
    try:
        db = check_inside(root, CODEGRAPH_DB_RELPATH)
    except UnsafePathError as exc:
        return fallback("unusable", str(exc))
    if not db.is_file():
        return fallback("no_index", "no index file")
    try:
        if repository.index_paths(_CODEGRAPH_DIR):
            return fallback("unusable", "git tracks .codegraph/")
    except GitError as exc:
        return fallback("unusable", str(exc))
    try:
        in_degree = _read_in_degree(db)
    except (sqlite3.Error, CodeGraphError) as exc:
        if _skip_code(exc):
            _log.warning("core files: the code index is busy (%s)", exc)
            return None
        return fallback("unusable", str(exc))
    except _UnusableIndex as exc:
        return fallback("unusable", str(exc))

    kept = sorted(
        (
            (n, path)
            for path, n in in_degree.items()
            if path in eligible and isinstance(n, int) and n >= 1
        ),
        key=lambda item: (-item[0], item[1]),
    )
    if not kept:
        return fallback("unusable", "no cross-file reference to a HEAD file")
    target = math.ceil(CORE_REFERENCE_SHARE * sum(n for n, _ in kept))
    chosen: list[str] = []
    running = 0
    for n, path in kept:
        chosen.append(path)
        running += n
        if running >= target:
            break
    return CoreSelection(frozenset(chosen), "codegraph", None)


def _skip_code(exc: BaseException) -> bool:
    """Whether ``exc`` (or the SQLite error behind it) is BUSY, LOCKED or CANTOPEN."""
    current: BaseException | None = exc
    while current is not None:
        code = getattr(current, "sqlite_errorcode", None)
        if isinstance(code, int) and code & 0xFF in _SKIP_ERRORCODES:
            return True
        current = current.__cause__
    return False


def _read_in_degree(db: Path) -> dict[str, int]:
    """The index's per-file cross-file in-degree, read with the rule-6 hardening."""
    with CodeGraph(db) as graph:
        conn = graph.connection
        conn.execute("PRAGMA trusted_schema=OFF")
        rows = conn.execute(
            "SELECT name, type FROM sqlite_master WHERE name IN ('nodes', 'edges')"
        ).fetchall()
        types = {row[0]: row[1] for row in rows}
        if len(rows) != 2 or types != {"nodes": "table", "edges": "table"}:
            raise _UnusableIndex(f"nodes / edges are not plain tables: {types}")
        started = time.monotonic()
        fired = False

        def deadline() -> int:
            nonlocal fired
            if time.monotonic() - started > CORE_INDEX_QUERY_SEC:
                fired = True
                return 1
            return 0

        conn.set_progress_handler(deadline, _PROGRESS_STEPS)
        try:
            return graph.file_in_degree(CORE_EDGE_KINDS)
        except sqlite3.OperationalError as exc:
            if fired:
                # Our own abort: treat the index as hostile, not as busy.
                raise _UnusableIndex(
                    f"the in-degree query ran past {CORE_INDEX_QUERY_SEC:g} s"
                ) from exc
            raise


def _rules_key(ignore_revs_file: Path | None) -> str:
    """sha256 of the validated ignore-revs entries and :data:`CORE_RULES_VERSION`."""
    entries: list[str] = []
    if ignore_revs_file is not None:
        for line in ignore_revs_file.read_bytes().decode("utf-8").splitlines():
            entry = line.split("#", 1)[0].strip().lower()
            if entry:
                entries.append(entry)
    material = "\n".join([f"rules={CORE_RULES_VERSION}", *entries])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _chunks(items: Sequence, size: int = _BATCH) -> Iterable[Sequence]:
    """``items`` in slices of ``size``."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _split(shas: str) -> set[str]:
    return set(shas.split()) if shas else set()


@dataclass(frozen=True)
class _Item:
    """One unit of blame work: a file (``pred_path`` is ``None``) or a rename."""

    key: str
    pred_path: str | None = None

    @property
    def is_file(self) -> bool:
        return self.pred_path is None


class CoreCrawler(Crawler):
    """The "Core commits" phase: compute ``core_commit`` and ``core_bulk_pair``.

    Its progress task (``"core"``) has no total until the core files are
    chosen; the scan child constructs it before it waits for a first
    CodeGraph build, so the UI can show the wait.

    Parameters
    ----------
    progress : rich.progress.Progress
        Shared Progress instance owned by the orchestrator.
    repository : Repository
        The repository to list and blame.
    large_commit_file_count : int
        Non-merge commits touching more files than this are bulk: they are
        left out of ``core_commit`` and described per file instead
        (``core_bulk_pair``).
    wall_sec : float or None
        Seconds the pass takes new blame work for, from the start of
        :meth:`work`; ``None`` for no wall (a Describe core run). Work left
        when it is spent waits for the next eligible scan.

    Attributes
    ----------
    warning : str or None
        Why the pass did not complete (a busy index, the wall, an
        exception); ``None`` when it did.
    selection : CoreSelection or None
        The files this pass chose; ``None`` until chosen, or when skipped.
    reblamed, skipped, failed, left : int
        This pass's counts: items blamed, items skipped by the filter,
        items whose blame failed, and selected files and rename events
        still without a current row.
    completed : bool
        Whether this pass replaced ``core_commit``.
    """

    def __init__(
        self,
        progress: Progress,
        *,
        repository: Repository,
        large_commit_file_count: int,
        wall_sec: float | None,
    ) -> None:
        super().__init__("core", progress, total=None)
        self._repository = repository
        self._large_commit_file_count = large_commit_file_count
        self._wall_sec = wall_sec
        self.warning: str | None = None
        self.selection: CoreSelection | None = None
        self.reblamed = 0
        self.skipped = 0
        self.failed = 0
        self.left = 0
        self.completed = False

    def work(self) -> None:
        try:
            self._pass()
        except Exception as exc:  # noqa: BLE001 - best effort (step 10)
            _log.warning("core commits not refreshed", exc_info=True)
            self.warning = f"core commits not refreshed - {exc}"

    # --- the pass --------------------------------------------------------

    def _pass(self) -> None:
        # Lazy imports (plan 49, I16): whygraph.mcp.evidence imports
        # whygraph.scan, so importing it at module level would be a cycle.
        from whygraph.mcp.evidence import (
            _Blamer,
            _rename_events_for,
        )
        from whygraph.mcp.path_history import (
            area_history_shas,
            current_branch_scope,
            resolve_path_aliases,
        )

        started = time.monotonic()
        deadline = None if self._wall_sec is None else started + self._wall_sec
        repo = self._repository
        head = repo.head_commit()
        if not head:
            raise GitError("HEAD names no commit")

        # Step 1 - list and select.
        self.advance(0, description="listing files")
        entries = repo.tree_entries(head, recursive=True)
        live = {entry.name for entry in entries}
        eligible: dict[str, str] = {}
        for entry in entries:
            if entry.type != "blob" or entry.mode in (_SYMLINK_MODE, _SUBMODULE_MODE):
                continue
            if entry.size is None or entry.size > CORE_MAX_BYTES:
                continue
            if "�" in entry.name:
                continue
            eligible[entry.name] = entry.sha
        self.advance(0, description="choosing core files")
        selection = select_core_files(eligible, repo)
        if selection is None:
            self.warning = "code index busy, core not refreshed"
            self.summary = "skipped: code index busy"
            return
        self.selection = selection
        selected = sorted(selection.paths)
        self.set_total(len(selected))

        # Step 2 - state; a new rules key drops every blame cache row.
        blamer = _Blamer(repo)
        rules_key = _rules_key(blamer.ignore_revs_file)
        with get_session() as session:
            state = session.get(CoreState, 1)
            if state is None:
                session.add(CoreState(id=1, rules_key=rules_key))
            elif state.rules_key != rules_key:
                session.exec(delete(CoreFile))
                session.exec(delete(CoreRename))
                state.rules_key = rules_key

        # Step 3 - choose work.
        self.advance(0, description="checking core files")
        stale = self._stale_files(selected, eligible, live, head)
        branch = current_branch_scope()
        events = sorted(
            {
                event
                for path in selected
                for event in _rename_events_for(path, current_branch=branch)
            }
        )
        with get_session() as session:
            have = {
                (sha, pred)
                for sha, pred in session.exec(
                    select(CoreRename.rename_sha, CoreRename.pred_path)
                )
            }
        todo_events = [event for event in events if tuple(event) not in have]
        files_total = len(selected) + len(events)
        done = files_total - len(stale) - len(todo_events)
        self._write_progress(files_total=files_total, files_done=done)
        self.set_total(files_total)
        self.advance(done, description="core files")

        # Step 4 - filter: missing, binary and oversized are skipped.
        items, skipped_rows = self._filter(stale, todo_events, eligible)
        self.skipped = len(skipped_rows)

        # Steps 5 and 6 - blame, in batches, until the work or the wall ends.
        done += self._blame_all(
            items,
            skipped_rows,
            blamer=blamer,
            head=head,
            eligible=eligible,
            deadline=deadline,
            done=done,
        )
        self.left = files_total - done
        if self.left:
            self.warning = f"{self.left} of {files_total} core files left for the next Quick rescan"
            self.summary = self._counts()
            return

        # Step 7 - area: the newest commits of each core file's aliases.
        self.advance(0, description="reading area history")
        area: dict[str, list[str]] = {}
        with get_session() as session:
            for path in selected:
                aliases = resolve_path_aliases(session, path, current_branch=branch)
                area[path] = area_history_shas(session, aliases, limit=CORE_AREA_DEPTH)

        # Step 8 - write the set, only from a complete pass.
        core, pairs = self._write_set(selection, selected, events, area, head)
        self.completed = True

        # Step 9 - summary.
        summary = (
            f"{core} core commits and {pairs} bulk file slices in "
            f"{len(selected)} core files · {self._counts()}"
        )
        if selection.selection == "all_text":
            summary += " · no usable code index, every text file used"
        self.summary = summary

    def _counts(self) -> str:
        return (
            f"{self.reblamed} re-blamed · {self.skipped} skipped · "
            f"{self.failed} failed · {self.left} left"
        )

    def _stale_files(
        self,
        selected: list[str],
        eligible: dict[str, str],
        live: set[str],
        head: str,
    ) -> set[str]:
        """Selected files to blame; drop rows of dead paths, touch unaffected ones.

        A file needs blame when it has no row, its blob changed, or a commit
        between its row's ``computed_head`` and ``head`` touched it (or that
        head is gone, or the log fails). Rows of live but unselected paths
        are left as they are.
        """
        repo = self._repository
        with get_session() as session:
            rows = {
                path: (blob, computed)
                for path, blob, computed in session.exec(
                    select(CoreFile.path, CoreFile.blob_sha, CoreFile.computed_head)
                ).all()
            }
            gone = [path for path in rows if path not in live]
            for chunk in _chunks(gone):
                session.exec(delete(CoreFile).where(col(CoreFile.path).in_(chunk)))

        stale: set[str] = set()
        by_head: dict[str, list[str]] = {}
        for path in selected:
            row = rows.get(path)
            if row is None or row[0] != eligible[path]:
                stale.add(path)
            elif row[1] != head:
                by_head.setdefault(row[1], []).append(path)
        unaffected: list[str] = []
        for computed, paths in by_head.items():
            if not repo.has_commit(computed):
                stale.update(paths)
                continue
            try:
                changed = repo.paths_changed_between(computed, head)
            except GitError:
                stale.update(paths)
                continue
            for path in paths:
                if path in changed:
                    stale.add(path)
                else:
                    unaffected.append(path)
        if unaffected:
            with get_session() as session:
                for chunk in _chunks(unaffected):
                    session.exec(
                        update(CoreFile)
                        .where(col(CoreFile.path).in_(chunk))
                        .values(computed_head=head)
                    )
        return stale

    def _filter(
        self,
        stale: set[str],
        todo_events: list[tuple[str, str]],
        eligible: dict[str, str],
    ) -> tuple[list[_Item], list[tuple[_Item, str]]]:
        """One ``blob_info`` call: the items to blame, and the skipped ones."""
        parents: dict[str, str] = {}
        rename_shas = sorted({sha for sha, _ in todo_events})
        with get_session() as session:
            for chunk in _chunks(rename_shas):
                for sha, parent_shas in session.exec(
                    select(Commit.sha, Commit.parent_shas).where(
                        col(Commit.sha).in_(chunk)
                    )
                ).all():
                    first = parent_shas.split()[:1] if parent_shas else []
                    if first:
                        parents[sha] = first[0]

        objects: dict[_Item, str] = {}
        skipped: list[tuple[_Item, str]] = []
        for path in sorted(stale):
            objects[_Item(path)] = eligible[path]
        for sha, pred_path in todo_events:
            item = _Item(sha, pred_path)
            parent = parents.get(sha)
            if parent is None or "\n" in pred_path:
                skipped.append((item, "skipped"))
            else:
                objects[item] = f"{parent}:{pred_path}"
        info = self._repository.blob_info(list(objects.values())) if objects else {}

        items: list[_Item] = []
        for item, name in objects.items():
            blob = info.get(name)
            if (
                blob is None
                or blob.binary
                or blob.size > CORE_MAX_BYTES
                or blob.lines > CORE_MAX_LINES
            ):
                skipped.append((item, "skipped"))
            else:
                items.append(item)
        return items, skipped

    def _blame_all(
        self,
        items: list[_Item],
        skipped_rows: list[tuple[_Item, str]],
        *,
        blamer: _Blamer,
        head: str,
        eligible: dict[str, str],
        deadline: float | None,
        done: int,
    ) -> int:
        """Blame ``items`` with :data:`CORE_WORKERS`; return how many got a row.

        Rows are written in batches of :data:`_BATCH`, each with the
        running ``files_done``. No new item starts once ``deadline`` passed.
        """
        written = 0
        batch: list[tuple[_Item, str, str]] = [
            (item, status, "") for item, status in skipped_rows
        ]

        def flush() -> None:
            nonlocal batch, written
            if not batch:
                return
            written += len(batch)
            self._write_rows(batch, head=head, eligible=eligible)
            self._write_progress(files_done=done + written)
            batch = []

        if len(batch) >= _BATCH:
            flush()
        self.advance(len(skipped_rows))
        queue = iter(items)
        with ThreadPoolExecutor(max_workers=CORE_WORKERS) as pool:
            inflight: dict[Future[tuple[str, str]], _Item] = {}

            def submit() -> bool:
                if deadline is not None and time.monotonic() >= deadline:
                    return False
                item = next(queue, None)
                if item is None:
                    return False
                # A fresh context copy per submit: pool workers do not
                # inherit ContextVars, and predecessor_shas reads the DB.
                future = pool.submit(
                    contextvars.copy_context().run,
                    self._blame_item,
                    item,
                    blamer,
                    head,
                )
                inflight[future] = item
                return True

            for _ in range(CORE_WORKERS):
                if not submit():
                    break
            while inflight:
                finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
                for future in finished:
                    item = inflight.pop(future)
                    status, shas = future.result()
                    if status == "ok":
                        self.reblamed += 1
                    else:
                        self.failed += 1
                    batch.append((item, status, shas))
                    self.advance(1)
                    if len(batch) >= _BATCH:
                        flush()
                    submit()
        flush()
        return written

    def _blame_item(self, item: _Item, blamer: _Blamer, head: str) -> tuple[str, str]:
        """Blame one file or predecessor; any exception is a ``failed`` row."""
        from whygraph.mcp.evidence import predecessor_shas, reachable_shas_for_file

        try:
            if item.is_file:
                shas = reachable_shas_for_file(
                    self._repository,
                    item.key,
                    blamer=blamer,
                    rev=head,
                    timeout=CORE_BLAME_TIMEOUT_SEC,
                )
            else:
                assert item.pred_path is not None
                shas = predecessor_shas(
                    self._repository,
                    item.key,
                    item.pred_path,
                    blamer=blamer,
                    timeout=CORE_BLAME_TIMEOUT_SEC,
                )
        except Exception as exc:  # noqa: BLE001 - a per-item failure is a row
            _log.info("core: blame of %s failed: %s", item, exc)
            return "failed", ""
        return "ok", " ".join(sorted(shas))

    def _write_rows(
        self,
        batch: list[tuple[_Item, str, str]],
        *,
        head: str,
        eligible: dict[str, str],
    ) -> None:
        """Upsert one batch of ``core_file`` / ``core_rename`` rows."""
        files = [
            {
                "path": item.key,
                "blob_sha": eligible[item.key],
                "computed_head": head,
                "status": status,
                "shas": shas,
            }
            for item, status, shas in batch
            if item.is_file
        ]
        renames = [
            {
                "rename_sha": item.key,
                "pred_path": item.pred_path,
                "status": status,
                "shas": shas,
            }
            for item, status, shas in batch
            if not item.is_file
        ]
        with get_session() as session:
            conn = session.connection()
            if files:
                stmt = sqlite_insert(CoreFile)
                conn.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["path"],
                        set_={
                            name: stmt.excluded[name]
                            for name in ("blob_sha", "computed_head", "status", "shas")
                        },
                    ),
                    files,
                )
            if renames:
                stmt = sqlite_insert(CoreRename)
                conn.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["rename_sha", "pred_path"],
                        set_={name: stmt.excluded[name] for name in ("status", "shas")},
                    ),
                    renames,
                )

    def _write_progress(self, **values: int) -> None:
        """Set ``files_total`` / ``files_done`` on the state row."""
        with get_session() as session:
            state = session.get(CoreState, 1)
            if state is not None:
                for name, value in values.items():
                    setattr(state, name, value)

    def _write_set(
        self,
        selection: CoreSelection,
        selected: list[str],
        events: list[tuple[str, str]],
        area: dict[str, list[str]],
        head: str,
    ) -> tuple[int, int]:
        """Replace ``core_commit`` and ``core_bulk_pair`` in one transaction (step 8).

        Returns
        -------
        tuple of int
            The core commits and the bulk pairs written.

        Raises
        ------
        RuntimeError
            If a selected file or a current rename event has no row (the
            completeness gate; the previous set stays).
        """
        chosen = set(selected)
        wanted = {tuple(event) for event in events}
        with get_session() as session:
            file_shas = {
                path: _split(shas)
                for path, shas in session.exec(select(CoreFile.path, CoreFile.shas))
                if path in chosen
            }
            rename_shas = {
                (sha, pred): _split(shas)
                for sha, pred, shas in session.exec(
                    select(CoreRename.rename_sha, CoreRename.pred_path, CoreRename.shas)
                )
                if (sha, pred) in wanted
            }
            if len(file_shas) != len(chosen) or len(rename_shas) != len(wanted):
                raise RuntimeError("a core file or rename event has no row")

            commits = {
                sha: (parent_shas, files_changed)
                for sha, parent_shas, files_changed in session.exec(
                    select(Commit.sha, Commit.parent_shas, Commit.files_changed)
                )
            }
            bulk = {
                sha
                for sha, (parent_shas, files_changed) in commits.items()
                if len(parent_shas.split()) <= 1
                and files_changed > self._large_commit_file_count
            }
            reached: set[str] = set()
            for shas in rename_shas.values():
                reached |= shas
            bulk_by_path: dict[str, set[str]] = {}
            for path in selected:
                shas = file_shas[path] | set(area.get(path, ()))
                reached |= shas
                if shas & bulk:
                    bulk_by_path[path] = shas & bulk
            core = sorted((reached & commits.keys()) - bulk)

            pairs: set[tuple[str, str]] = set()
            for chunk in _chunks(sorted(bulk_by_path)):
                for sha, path in session.exec(
                    select(CommitFileChange.commit_sha, CommitFileChange.path).where(
                        col(CommitFileChange.path).in_(chunk)
                    )
                ):
                    if sha in bulk_by_path.get(path, ()):
                        pairs.add((sha, path))

            conn = session.connection()
            conn.execute(delete(CoreCommit))
            conn.execute(delete(CoreBulkPair))
            if core:
                conn.execute(insert(CoreCommit), [{"sha": sha} for sha in core])
            if pairs:
                conn.execute(
                    insert(CoreBulkPair),
                    [{"sha": sha, "path": path} for sha, path in sorted(pairs)],
                )
            state = session.get(CoreState, 1)
            assert state is not None
            state.selection = selection.selection
            state.fallback_reason = selection.fallback_reason
            state.files_selected = len(selected)
            state.files_total = len(selected) + len(events)
            state.files_done = state.files_total
            state.completed_at = datetime.now(timezone.utc).isoformat()
            state.completed_head = head
        with get_session() as session:
            session.connection().exec_driver_sql("PRAGMA wal_checkpoint(PASSIVE)")
        return len(core), len(pairs)
