"""GitCrawler — walk a repository's current branch and persist commits.

Replaces the earlier placeholder. The crawler reads commits via
:class:`whygraph.services.git.Repository.commits`, sizes the progress
bar from ``len(commits)``, and inserts one row per *new* commit into
the ``commit`` table. Existing SHAs are skipped, so re-scans on a
repository whose history only grows are no-ops.

Phase 2 of the layered evidence pipeline added the
``commit_file_change`` index on top: for every commit the crawler
sees, ``git diff-tree -M -C`` produces per-file structural records
(``A``/``M``/``D``/``R``/``C``, ``renamed_from``, line counts) that
become rows keyed by ``commit_sha``. Existence of file-change rows is
checked independently of the commit row, so upgrading from a pre-Phase-2
WhyGraph DB and re-running ``whygraph scan`` backfills the index without
needing a separate command.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.progress import Progress
from sqlmodel import select

from whygraph.db import get_session
from whygraph.db.models.commit import Commit as CommitRow
from whygraph.db.models.commit_file_change import CommitFileChange
from whygraph.services.git import FileChange, Repository
from whygraph.services.git.commit import Commit as CommitDC

from .crawler import Crawler
from .refactor_score import compute_refactor_score


class GitCrawler(Crawler):
    """Crawl every commit on the repository's current branch.

    Sizes the progress bar from ``len(repository.commits)`` and inserts
    one row per new commit. SHAs already present in the ``commit`` table
    are skipped without modification, so re-scans are idempotent.

    Parameters
    ----------
    progress : rich.progress.Progress
        Shared Progress instance owned by the orchestrator.
    repository : Repository
        The git repository to scan. Walks :attr:`Repository.current_branch`.
    """

    def __init__(self, progress: Progress, *, repository: Repository) -> None:
        super().__init__("git", progress, total=None)
        self._repository = repository

    def work(self) -> None:
        commits = self._repository.commits
        self.set_total(len(commits))

        with get_session() as session:
            existing_commits: set[str] = set(session.exec(select(CommitRow.sha)).all())
            existing_file_changes: set[str] = set(
                session.exec(select(CommitFileChange.commit_sha).distinct()).all()
            )
            scanned_at = datetime.now(timezone.utc).isoformat()
            for dc in commits:
                if dc.sha not in existing_file_changes:
                    file_changes = self._repository.commit_file_changes(dc)
                    for change in file_changes:
                        session.add(_to_file_change_row(dc.sha, change))
                else:
                    file_changes = ()

                score = compute_refactor_score(
                    subject=dc.subject, file_changes=file_changes
                )

                if dc.sha not in existing_commits:
                    session.add(
                        _to_row(dc, scanned_at=scanned_at, refactor_score=score)
                    )
                elif file_changes:
                    # Existing commit row but we just computed its file
                    # changes for the first time — backfill the score so
                    # an upgrade from a pre-Phase-3 DB picks up the
                    # heuristic without needing a separate command.
                    existing = session.get(CommitRow, dc.sha)
                    if existing is not None and existing.refactor_score == 0:
                        existing.refactor_score = score
                        session.add(existing)
                self.advance(1)


def _to_row(dc: CommitDC, *, scanned_at: str, refactor_score: int = 0) -> CommitRow:
    return CommitRow(
        sha=dc.sha,
        parent_shas=" ".join(dc.parent_shas),
        author_name=dc.author_name,
        author_email=dc.author_email,
        authored_at=dc.authored_at,
        committed_at=dc.committed_at,
        subject=dc.subject,
        body=dc.body,
        files_changed=dc.stats.files_changed,
        insertions=dc.stats.insertions,
        deletions=dc.stats.deletions,
        scanned_at=scanned_at,
        refactor_score=refactor_score,
    )


def _to_file_change_row(commit_sha: str, change: FileChange) -> CommitFileChange:
    return CommitFileChange(
        commit_sha=commit_sha,
        path=change.path,
        change_type=change.change_type,
        renamed_from=change.renamed_from,
        similarity=change.similarity,
        lines_added=change.lines_added,
        lines_deleted=change.lines_deleted,
    )
