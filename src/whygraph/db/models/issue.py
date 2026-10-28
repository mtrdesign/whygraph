"""SQLModel for the ``issue`` table."""

from __future__ import annotations

from sqlalchemy import Text, text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class Issue(WhygraphTable, table=True):
    """One row per issue fetched via ``gh api graphql`` (PRs excluded).

    Attributes
    ----------
    milestone : int or None
        The milestone number, or ``None`` when the issue has none.
    assignees : str
        JSON list of assignee logins.
    state_reason : str or None
        GitHub's ``stateReason`` (``COMPLETED``, ``NOT_PLANNED``, ...).
    """

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
    milestone: int | None = Field(default=None)
    assignees: str = Field(
        default="[]", sa_type=Text, sa_column_kwargs={"server_default": text("'[]'")}
    )
    state_reason: str | None = Field(default=None, sa_type=Text)
