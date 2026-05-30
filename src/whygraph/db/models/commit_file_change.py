"""SQLModel for the ``commit_file_change`` table.

One row per (commit, path-at-that-commit). Persists the per-commit
structural information :class:`~whygraph.services.git.FileChange`
captures from ``git diff-tree`` so WhyGraph can answer area-history
questions (and traverse rename chains) without re-shelling out to git
on every query.

A path can have multiple rows over time — one per commit that touched
it — and one historical path can map to today's name via successive
``renamed_from`` edges. Rename chains are derived in SQL (recursive
CTE) rather than materialised in a separate table.
"""

from __future__ import annotations

from sqlalchemy import Text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class CommitFileChange(WhygraphTable, table=True):
    """One file's change inside a single scanned commit.

    For *bulk* commits (more than ``analyze.large_commit_file_count``
    files) the per-commit ``Commit.llm_description`` is only a stub, and
    the real description for an individual file is generated lazily and
    cached on this row's ``llm_description`` — keyed by ``(commit_sha,
    path)`` so a second query for the same file is a cache hit. For
    normal commits these columns stay ``NULL`` and the whole-diff
    ``Commit.llm_description`` is used instead.
    """

    id: int | None = Field(default=None, primary_key=True)
    commit_sha: str = Field(sa_type=Text, index=True, foreign_key="commit.sha")
    path: str = Field(sa_type=Text, index=True)
    change_type: str = Field(sa_type=Text)
    renamed_from: str | None = Field(default=None, sa_type=Text, index=True)
    similarity: int | None = Field(default=None)
    lines_added: int = 0
    lines_deleted: int = 0
    llm_description: str | None = Field(default=None, sa_type=Text)
    llm_description_model: str | None = Field(default=None, sa_type=Text)
