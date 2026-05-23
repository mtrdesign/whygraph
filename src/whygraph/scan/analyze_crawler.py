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

from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.progress import Progress
from sqlmodel import select

from whygraph.analyze import AnalyzeError, LlmDescriptor
from whygraph.db import get_session
from whygraph.db.models.commit import Commit as CommitRow
from whygraph.services.git import Repository
from whygraph.services.git.commit import Commit as CommitDC

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

    Notes
    -----
    Each worker opens its own :func:`whygraph.db.get_session` —
    ``sqlmodel.Session`` is not thread-safe. Per-commit failures are
    collected rather than aborting the run; once the pool drains, a
    single aggregate :class:`~whygraph.analyze.AnalyzeError` is raised
    (and captured into :attr:`Crawler.error`) if any commit failed.
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
    ) -> None:
        super().__init__("analyze", progress, total=None)
        self._repository = repository
        self._descriptor = descriptor
        self._max_workers = max_workers

    def work(self) -> None:
        # Warm the `commits` cached_property single-threaded before the
        # pool starts, then read which commits still need a description.
        commits = self._repository.commits
        with get_session() as session:
            pending: set[str] = set(
                session.exec(
                    select(CommitRow.sha).where(CommitRow.llm_description.is_(None))
                ).all()
            )
        todo = [c for c in commits if c.sha in pending]
        self.set_total(len(todo))
        if not todo:
            return

        failures: list[tuple[str, BaseException]] = []
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {pool.submit(self._describe_commit, c): c.sha for c in todo}
            for future in as_completed(futures):
                self.advance(1)
                exc = future.exception()
                if exc is not None:
                    failures.append((futures[future], exc))

        if failures:
            sha, exc = failures[0]
            raise AnalyzeError(
                f"{len(failures)} of {len(todo)} commits failed to analyze; "
                f"first failure {sha[:12]}: {exc}"
            )

    def _describe_commit(self, commit: CommitDC) -> None:
        """Describe one commit and persist it. Runs in a worker thread.

        Opens its own DB session — sessions are not shareable across
        threads. Commits with an empty diff are skipped silently; any
        other failure propagates and is collected by :meth:`work`.
        """
        diff = self._repository.diff(commit)
        if not diff.strip():
            return
        description = self._descriptor.describe(diff)
        with get_session() as session:
            row = session.get(CommitRow, commit.sha)
            if row is not None:
                row.llm_description = description.text
                row.llm_description_model = (
                    f"{description.provider}:{description.model}"
                )
