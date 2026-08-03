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

Branch membership is computed, not assumed. Every row records whether it
is reachable from the default branch (``on_default_branch``) and, when it
is not, the ref it was first seen on (``first_seen_ref``). A reconcile
pass at the end of each crawl recomputes the flag for *existing* rows too,
so the database self-heals as branches merge or get rewritten — see the
plan's §4.2 / §4.3.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from rich.progress import Progress
from sqlmodel import col, select, update

from whygraph.db import get_session
from whygraph.db.models.commit import Commit as CommitRow
from whygraph.db.models.commit_file_change import CommitFileChange
from whygraph.services.git import FileChange, Repository
from whygraph.services.git.commit import Commit as CommitDC

from .crawler import Crawler
from .refactor_score import compute_refactor_score

_log = logging.getLogger(__name__)

# SHAs per reconcile UPDATE. Well under SQLite's SQLITE_MAX_VARIABLE_NUMBER
# on every build (999 on older ones), so a bulk reflag never trips it.
_UPDATE_CHUNK = 500


class GitCrawler(Crawler):
    """Crawl every commit on the repository's current branch.

    Sizes the progress bar from ``len(repository.commits)`` and inserts
    one row per new commit. SHAs already present in the ``commit`` table
    are skipped without modification, so re-scans are idempotent — except
    for the branch-membership reconcile pass, which is the one place a
    re-scan deliberately rewrites existing rows.

    Parameters
    ----------
    progress : rich.progress.Progress
        Shared Progress instance owned by the orchestrator.
    repository : Repository
        The git repository to scan. Walks :attr:`Repository.current_branch`.

    Attributes
    ----------
    warning : str or None
        Message describing a bulk demotion, for the orchestrator to
        surface after the crawl. ``None`` when nothing was demoted.
        Mirrors :attr:`CodeGraphCrawler.warning`'s "surface after the
        crawl, don't fail it" contract.
    """

    def __init__(self, progress: Progress, *, repository: Repository) -> None:
        super().__init__("git", progress, total=None)
        self._repository = repository
        self.warning: str | None = None

    def work(self) -> None:
        commits = self._repository.commits
        self.set_total(len(commits))
        default_shas = self._repository.default_branch_shas
        branch = self._repository.current_branch

        with get_session() as session:
            existing_commits: set[str] = set(session.exec(select(CommitRow.sha)).all())
            existing_file_changes: set[str] = set(
                session.exec(select(CommitFileChange.commit_sha).distinct()).all()
            )
            scanned_at = datetime.now(timezone.utc).isoformat()
            inserted = 0
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
                    # An unresolvable default branch degrades to 1 for
                    # everything, which is exactly today's behaviour — no
                    # new failure mode for local-only or unborn repos.
                    on_default = (
                        1 if (not default_shas or dc.sha in default_shas) else 0
                    )
                    session.add(
                        _to_row(
                            dc,
                            scanned_at=scanned_at,
                            refactor_score=score,
                            on_default_branch=on_default,
                            first_seen_ref=None if on_default else branch,
                        )
                    )
                    inserted += 1
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

            promoted, demoted = _reconcile_branch_membership(
                session, default_shas, skip=self._repository.is_shallow
            )

        parts = [f"{inserted} new"]
        if promoted:
            parts.append(f"{promoted} promoted")
        if demoted:
            parts.append(f"{demoted} demoted")
            refs = ", ".join(self._repository.default_branch_refs)
            self.warning = (
                f"{demoted} commits are no longer reachable from {refs} — "
                "demoted to off-default-branch"
            )
        self.summary = f"{len(commits)} commits ({', '.join(parts)})"


def _reconcile_branch_membership(
    session, default_shas: frozenset[str], *, skip: bool
) -> tuple[int, int]:
    """Recompute ``on_default_branch`` for every existing ``commit`` row.

    This is what makes the database self-heal: a feature commit whose
    branch has since been merged is promoted, and a commit that was
    force-pushed away is demoted. Rows are never deleted — an unreachable
    commit is still valid evidence for why the code looks the way it does.

    ``first_seen_ref`` is deliberately **not** rewritten. On a demotion
    its ``NULL`` correctly reads as "was on the default branch, no longer
    reachable"; on a promotion the original ref stays as provenance.

    Parameters
    ----------
    session : Session
        The crawler's open session. Changes are staged, not committed —
        the caller's context manager owns the transaction.
    default_shas : frozenset[str]
        Every SHA reachable from the default branch. An **empty** set
        means the default branch could not be resolved, which is
        "cannot judge", not "nothing is on it".
    skip : bool
        Skip the pass entirely (shallow clone). A truncated view of the
        default branch would demote nearly every row.

    Returns
    -------
    tuple[int, int]
        ``(promoted, demoted)`` — the ``0 -> 1`` and ``1 -> 0`` counts.
        Both guards return ``(0, 0)`` without touching a single row.

    Notes
    -----
    The demoted SHAs are logged at ``INFO``, which
    :func:`whygraph.core.logger.scan_log_redirect` lands in
    ``.whygraph/scan.log``. The console warning stays a count — an
    unbounded SHA list has no place in a one-line-per-phase panel.
    """
    if not default_shas or skip:
        return (0, 0)

    promote: list[str] = []
    demoted_shas: list[str] = []
    rows = session.exec(select(CommitRow.sha, CommitRow.on_default_branch)).all()
    for sha, flag in rows:
        want = 1 if sha in default_shas else 0
        if want == flag:
            continue
        (promote if want == 1 else demoted_shas).append(sha)

    _set_flag(session, promote, 1)
    _set_flag(session, demoted_shas, 0)

    if demoted_shas:
        _log.info(
            "demoted %d commits off the default branch: %s",
            len(demoted_shas),
            " ".join(sorted(demoted_shas)),
        )
    return (len(promote), len(demoted_shas))


def _set_flag(session, shas: list[str], value: int) -> None:
    """Stage ``on_default_branch = value`` for ``shas``, chunked.

    Chunked because the SHA list is unbounded — a first scan against a
    pre-existing DB can move tens of thousands of rows — and SQLite caps
    the number of bound parameters per statement.
    """
    for start in range(0, len(shas), _UPDATE_CHUNK):
        chunk = shas[start : start + _UPDATE_CHUNK]
        session.exec(
            update(CommitRow)
            .where(col(CommitRow.sha).in_(chunk))
            .values(on_default_branch=value)
        )


def _to_row(
    dc: CommitDC,
    *,
    scanned_at: str,
    refactor_score: int = 0,
    on_default_branch: int = 1,
    first_seen_ref: str | None = None,
) -> CommitRow:
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
        on_default_branch=on_default_branch,
        first_seen_ref=first_seen_ref,
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
