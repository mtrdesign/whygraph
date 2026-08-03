"""SQLModel for the ``commit`` table."""

from __future__ import annotations

from sqlalchemy import Text, text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class Commit(WhygraphTable, table=True):
    """One row per scanned Git commit (reachable from the default branch).

    Notes
    -----
    * ``on_default_branch`` is ``int`` (0/1), not ``bool``, to keep the
      declared SQLite affinity as INTEGER (same rationale as
      :attr:`whygraph.db.models.PullRequest.draft`). ``1`` marks a commit
      reachable from the default branch; ``0`` marks one that is not —
      either unmerged local work scanned off a feature branch, or a
      PR-origin commit recovered from a squash-merged PR (see
      ``scan/pr_origin_enricher.py``). Both must stay out of the
      default-branch-only queries (area-history, refactor-walk).
    * ``first_seen_ref`` discriminates those two populations; see the
      field comment.
    """

    sha: str = Field(primary_key=True, nullable=True, sa_type=Text)
    parent_shas: str = Field(sa_type=Text)
    author_name: str = Field(sa_type=Text)
    author_email: str = Field(sa_type=Text)
    authored_at: str = Field(sa_type=Text, index=True)
    committed_at: str = Field(sa_type=Text)
    subject: str = Field(sa_type=Text)
    body: str = Field(sa_type=Text)
    files_changed: int
    insertions: int
    deletions: int
    scanned_at: str = Field(sa_type=Text)
    llm_description: str | None = Field(default=None, sa_type=Text)
    llm_description_model: str | None = Field(default=None, sa_type=Text)
    # Phase 3 bridge — heuristic 0–100 score indicating how likely this
    # commit is a refactor/formatter sweep. Phase 3's evidence collector
    # uses it to drive ``git blame --ignore-rev`` walk-past so older
    # authorship surfaces through commits that would otherwise mask it.
    refactor_score: int = Field(default=0)
    # 0 = not reachable from the default branch (unmerged local work, or a
    # PR-origin commit recovered from a squash-merged PR); 1 = reachable
    # (the norm). Recomputed on every scan by GitCrawler's reconcile pass.
    on_default_branch: int = Field(
        default=1, sa_column_kwargs={"server_default": text("1")}
    )
    # Ref this commit was first seen on when it was NOT on the default
    # branch: a local branch name, or refs/pull/<N>/head for a PR-origin
    # recovery. NULL means it was on the default branch when first scanned.
    # Written once at insert and never rewritten — including by a 1 -> 0
    # demotion, where NULL correctly reads as "was on the default branch,
    # no longer reachable".
    first_seen_ref: str | None = Field(default=None, sa_type=Text)
