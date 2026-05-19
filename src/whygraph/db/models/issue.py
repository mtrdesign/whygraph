"""SQLModel for the ``issue`` table."""

from __future__ import annotations

from sqlalchemy import REAL, Text, text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class Issue(WhygraphTable, table=True):
    """One row per issue fetched via ``gh api graphql`` (PRs excluded)."""

    number: int = Field(primary_key=True, nullable=True)
    title: str = Field(sa_type=Text)
    body: str | None = Field(default=None, sa_type=Text)
    state: str = Field(sa_type=Text, index=True)
    created_at: str = Field(sa_type=Text)
    updated_at: str = Field(sa_type=Text)
    closed_at: str | None = Field(default=None, sa_type=Text)
    author: str | None = Field(default=None, sa_type=Text)
    html_url: str = Field(sa_type=Text)
    labels: str = Field(sa_type=Text)
    fetched_at: str = Field(sa_type=Text)
    title_tfidf_score: float = Field(
        default=0.0, sa_type=REAL, sa_column_kwargs={"server_default": text("0")}
    )
    body_tfidf_score: float = Field(
        default=0.0, sa_type=REAL, sa_column_kwargs={"server_default": text("0")}
    )
