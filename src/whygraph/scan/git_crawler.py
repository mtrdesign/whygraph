"""GitCrawler — walk a repository's current branch and persist commits.

Replaces the earlier placeholder. The crawler reads commits via
:class:`whygraph.services.git.Repository.commits`, sizes the progress
bar from ``len(commits)``, and inserts one row per *new* commit into
the ``commit`` table. Existing SHAs are skipped, so re-scans on a
repository whose history only grows are no-ops.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.progress import Progress
from sqlmodel import select

from whygraph.db import get_session
from whygraph.db.models.commit import Commit as CommitRow
from whygraph.services.git import Repository
from whygraph.services.git.commit import Commit as CommitDC

from .crawler import Crawler


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
            existing: set[str] = set(session.exec(select(CommitRow.sha)).all())
            scanned_at = datetime.now(timezone.utc).isoformat()
            for dc in commits:
                if dc.sha not in existing:
                    session.add(_to_row(dc, scanned_at=scanned_at))
                self.advance(1)


def _to_row(dc: CommitDC, *, scanned_at: str) -> CommitRow:
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
    )
