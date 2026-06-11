"""SQLModel for the ``commit`` table."""

from __future__ import annotations

from sqlalchemy import Text, text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class Commit(WhygraphTable, table=True):
    """One row per scanned Git commit (first-parent walk of the default branch).

    Notes
    -----
    * ``on_default_branch`` is ``int`` (0/1), not ``bool``, to keep the
      declared SQLite affinity as INTEGER (same rationale as
      :attr:`whygraph.db.models.PullRequest.draft`). ``1`` (default) marks a
      commit on the first-parent main walk; ``0`` marks a PR-origin commit
      recovered from a squash-merged PR (see ``scan/pr_origin_enricher.py``)
      that must stay out of the main-walk-only queries (area-history,
      refactor-walk).
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
    # 0 = PR-origin commit recovered from a squash-merged PR (not on the
    # first-parent main walk); 1 = on the default-branch walk (the norm).
    on_default_branch: int = Field(
        default=1, sa_column_kwargs={"server_default": text("1")}
    )
