"""SQLModel for the ``pullrequest`` table."""

from __future__ import annotations

from sqlalchemy import REAL, Text, text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class PullRequest(WhygraphTable, table=True):
    """One row per pull request fetched via ``gh api graphql``.

    ``closing_issue_numbers`` from GraphQL is *not* stored here — it is
    flattened into :class:`whygraph.db.models.PRIssueLink` rows by the
    scan writer.

    Notes
    -----
    * ``draft`` is ``int`` (0/1), not ``bool``, to keep the declared
      SQLite affinity as INTEGER.
    """

    number: int = Field(primary_key=True, nullable=True)
    title: str = Field(sa_type=Text)
    body: str | None = Field(default=None, sa_type=Text)
    state: str = Field(sa_type=Text, index=True)
    draft: int = Field(default=0, sa_column_kwargs={"server_default": text("0")})
    created_at: str = Field(sa_type=Text)
    updated_at: str = Field(sa_type=Text)
    closed_at: str | None = Field(default=None, sa_type=Text)
    merged_at: str | None = Field(default=None, sa_type=Text)
    merge_commit_sha: str | None = Field(default=None, sa_type=Text, index=True)
    head_sha: str = Field(sa_type=Text)
    head_ref: str | None = Field(default=None, sa_type=Text)
    base_ref: str = Field(sa_type=Text)
    author: str | None = Field(default=None, sa_type=Text)
    html_url: str = Field(sa_type=Text)
    labels: str = Field(sa_type=Text)
    fetched_at: str = Field(sa_type=Text)
    commit_titles: str = Field(
        default="[]", sa_type=Text, sa_column_kwargs={"server_default": text("'[]'")}
    )
    comments: str = Field(
        default="[]", sa_type=Text, sa_column_kwargs={"server_default": text("'[]'")}
    )
    title_tfidf_score: float = Field(
        default=0.0, sa_type=REAL, sa_column_kwargs={"server_default": text("0")}
    )
    body_tfidf_score: float = Field(
        default=0.0, sa_type=REAL, sa_column_kwargs={"server_default": text("0")}
    )
