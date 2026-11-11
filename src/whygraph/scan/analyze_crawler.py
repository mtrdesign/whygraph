"""AnalyzeCrawler — describe each scanned commit's diff with an LLM.

Runs after :class:`~whygraph.scan.git_crawler.GitCrawler` has populated
the ``commit`` table. For every commit whose ``llm_description`` is still
``NULL`` it computes the first-parent diff
(:meth:`whygraph.services.git.Repository.diff`), sends it through a
:class:`whygraph.analyze.LlmDescriptor`, and writes the result back to
``commit.llm_description`` / ``commit.llm_description_model``.

Commits are processed concurrently — a thread pool sized by
``[scan] max_workers`` — because each LLM round-trip dominates the wall
clock. Already-described commits are skipped, so re-scans are idempotent
and a partially-completed run resumes cleanly.
"""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.progress import Progress
from sqlmodel import select

from whygraph.analyze import AnalyzeError, LlmDescriptor, bulk_commit_stub
from whygraph.db import get_session
from whygraph.analyze.backfill import backfill_file_description
from whygraph.db.models.commit import Commit as CommitRow
from whygraph.db.models.commit_file_change import CommitFileChange
from whygraph.db.models.core import CoreBulkPair, CoreCommit, CoreState
from whygraph.services.git import Repository
from whygraph.services.git.commit import Commit as CommitDC
from whygraph.services.llm import LlmAuthError

from .crawler import Crawler


class AnalyzeCrawler(Crawler):
    """Crawl undescribed commits and fill in their LLM descriptions.

    Sizes the progress bar from the number of commits whose
    ``llm_description`` is ``NULL`` and advances one unit per commit as
    each completes. Commits with an empty diff (e.g. empty merges) are
    skipped — there is nothing to describe.

    Parameters
    ----------
    progress : rich.progress.Progress
        Shared Progress instance owned by the orchestrator.
    repository : Repository
        The git repository to diff commits against.
    descriptor : LlmDescriptor
        A pre-built descriptor. Its ``describe`` is called concurrently
        from worker threads — each call is independent and stateless.
    max_workers : int
        Size of the thread pool running the LLM round-trips.
    large_commit_file_count : int
        Commits touching strictly more files than this are *bulk*
        commits: their whole-diff description is skipped (it would be one
        expensive LLM pass yielding a vague repo-wide summary) and a
        cheap :func:`~whygraph.analyze.bulk_commit_stub` is written
        instead. Real per-file descriptions are filled in lazily on the
        MCP read path. Comes from ``analyze.large_commit_file_count``.
    core_only : bool
        Describe only the commits of ``core_commit`` (Describe core). The
        core bulk pairs (``core_bulk_pair``) are described by every run
        whatever this is, so "all" always covers "core".

    Notes
    -----
    Each worker opens its own :func:`whygraph.db.get_session` —
    ``sqlmodel.Session`` is not thread-safe. Each submitted commit runs in
    its own copy of the crawler thread's :mod:`contextvars` context, so a
    bound :func:`whygraph.core.context.use_project` project reaches the
    workers. Per-commit failures are collected rather than aborting the
    run; once the pool drains, a single aggregate
    :class:`~whygraph.analyze.AnalyzeError` is raised (and captured into
    :attr:`Crawler.error`) if any commit failed.
    Commits that succeeded are committed as they finish, so the failed
    ones are simply retried on the next scan.
    """

    def __init__(
        self,
        progress: Progress,
        *,
        repository: Repository,
        descriptor: LlmDescriptor,
        max_workers: int,
        large_commit_file_count: int,
        core_only: bool = False,
    ) -> None:
        super().__init__("analyze", progress, total=None)
        self._repository = repository
        self._descriptor = descriptor
        self._max_workers = max_workers
        self._large_commit_file_count = large_commit_file_count
        self._core_only = core_only

    def work(self) -> None:
        # Warm the `commits` cached_property single-threaded before the
        # pool starts, then read which commits still need a description.
        commits = self._repository.commits
        reachable = {c.sha for c in commits}
        with get_session() as session:
            query = select(CommitRow.sha).where(CommitRow.llm_description.is_(None))
            if self._core_only:
                query = query.where(CommitRow.sha.in_(select(CoreCommit.sha)))
            pending: set[str] = set(session.exec(query).all())
            pairs: list[tuple[CommitRow, str]] = []
            state = session.get(CoreState, 1)
            if state is not None and state.completed_at is not None:
                pair_rows = session.exec(
                    select(CoreBulkPair.sha, CoreBulkPair.path)
                    .join(
                        CommitFileChange,
                        (CommitFileChange.commit_sha == CoreBulkPair.sha)
                        & (CommitFileChange.path == CoreBulkPair.path),
                    )
                    .where(CommitFileChange.llm_description.is_(None))
                ).all()
                wanted = {sha for sha, _ in pair_rows if sha in reachable}
                rows = {
                    r.sha: r
                    for r in session.exec(
                        select(CommitRow).where(CommitRow.sha.in_(wanted))
                    ).all()
                }
                for r in rows.values():
                    session.expunge(r)
                pairs = [(rows[sha], path) for sha, path in pair_rows if sha in rows]
        todo = [c for c in commits if c.sha in pending]
        self.set_total(len(todo) + len(pairs))
        if not todo and not pairs:
            self.summary = "up to date"
            return

        failures: list[tuple[str, BaseException]] = []
        described = bulk = slices = 0
        auth: BaseException | None = None
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            # A fresh context copy per submit: pool workers do not inherit
            # ContextVars, and one shared Context cannot be entered by two
            # threads at once (Context.run raises RuntimeError).
            futures = {
                pool.submit(
                    contextvars.copy_context().run, self._describe_commit, c
                ): c.sha
                for c in todo
            }
            for row, path in pairs:
                futures[
                    pool.submit(
                        contextvars.copy_context().run,
                        self._describe_pair,
                        row,
                        path,
                    )
                ] = f"{row.sha}:{path}"
            for future in as_completed(futures):
                if future.cancelled():
                    continue
                self.advance(1)
                exc = future.exception()
                if exc is not None:
                    failures.append((futures[future], exc))
                    if auth is None and _auth_failure(exc):
                        # Rejected credentials fail every call the same way:
                        # stop here rather than spend one call per commit.
                        auth = exc
                        for pending in futures:
                            pending.cancel()
                elif future.result() == "bulk":
                    bulk += 1
                elif future.result() == "described":
                    described += 1
                elif future.result() == "slice":
                    slices += 1

        parts = [f"{described} described"]
        if bulk:
            parts.append(f"{bulk} bulk-stubbed")
        if slices:
            parts.append(f"{slices} bulk file slices")
        self.summary = " · ".join(parts)

        if auth is not None:
            total = len(todo) + len(pairs)
            skipped = total - described - bulk - slices - len(failures)
            raise AnalyzeError(
                f"stopped after the provider rejected the credentials "
                f"({skipped} commits or file slices not attempted): {_auth_failure(auth)}"
            )
        if failures:
            sha, exc = failures[0]
            raise AnalyzeError(
                f"{len(failures)} of {len(todo) + len(pairs)} commits or file "
                f"slices failed to analyze; "
                f"first failure {sha[:12]}: {exc}"
            )

    def _describe_commit(self, commit: CommitDC) -> str:
        """Describe one commit and persist it. Runs in a worker thread.

        Opens its own DB session — sessions are not shareable across
        threads. *Bulk* commits (more files than
        ``large_commit_file_count``; a merge counts the files of its
        first-parent diff) skip the whole-diff LLM call and get a cheap
        stub instead; their real per-file descriptions are filled in
        lazily on read. Commits with an empty diff are skipped
        silently; any other failure propagates and is collected by
        :meth:`work`.

        Returns
        -------
        str
            The outcome kind — ``"bulk"`` (stub written), ``"empty"``
            (empty diff, skipped), or ``"described"`` (LLM description
            written) — tallied by :meth:`work` for its summary.
        """
        files_changed = commit.stats.files_changed
        if len(commit.parent_shas) > 1:
            # A merge's walk stats are zero (``git log --shortstat`` prints
            # none), but its diff is the whole first-parent diff.
            counts = self._repository.first_parent_file_counts([commit.sha])
            files_changed = counts[commit.sha]
        if files_changed > self._large_commit_file_count:
            with get_session() as session:
                row = session.get(CommitRow, commit.sha)
                if row is not None:
                    # Leave llm_description_model NULL — the stub is not
                    # LLM-generated, and a NULL model is the signal the
                    # lazy read path uses to know a real per-file
                    # description still needs generating.
                    row.llm_description = bulk_commit_stub(files_changed)
            return "bulk"
        diff = self._repository.diff(commit)
        if not diff.strip():
            return "empty"
        with get_session() as session:
            row = session.get(CommitRow, commit.sha)
            if row is not None and row.llm_description is not None:
                # Described meanwhile (the portal's lazy backfill): no call.
                return "described"
        description = self._descriptor.describe(diff, subject=commit.sha)
        with get_session() as session:
            row = session.get(CommitRow, commit.sha)
            if row is not None:
                row.llm_description = description.text
                row.llm_description_model = (
                    f"{description.provider}:{description.model}"
                )
        return "described"

    def _describe_pair(self, commit: CommitRow, path: str) -> str:
        """Describe one core bulk pair's file slice. Runs in a worker thread.

        Returns
        -------
        str
            ``"slice"`` (described, or an empty slice counted as done).
        """
        backfill_file_description(
            commit,
            path,
            repository=self._repository,
            descriptor=self._descriptor,
        )
        return "slice"


def _auth_failure(exc: BaseException) -> LlmAuthError | None:
    """The :class:`LlmAuthError` behind ``exc`` (it arrives wrapped), if any."""
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, LlmAuthError):
            return current
        current = current.__cause__
    return None
