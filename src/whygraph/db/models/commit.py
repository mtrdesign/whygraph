"""SQLModel for the ``commit`` table."""

from __future__ import annotations

from sqlalchemy import Text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class Commit(WhygraphTable, table=True):
    """One row per scanned Git commit (first-parent walk of the default branch)."""

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
