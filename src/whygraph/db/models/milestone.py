"""SQLModel for the ``milestone`` table."""

from __future__ import annotations

from sqlalchemy import REAL, Text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class Milestone(WhygraphTable, table=True):
    """One row per forge milestone, re-listed in full on every crawl.

    Attributes
    ----------
    number : int
        The milestone number. Primary key.
    title : str
        Milestone title.
    description : str or None
        Milestone description.
    state : str
        ``"open"`` or ``"closed"``.
    due_on : str or None
        Due date as the forge sent it.
    closed_at : str or None
        Close time as the forge sent it.
    updated_at : str
        Last update time as the forge sent it.
    progress_pct : float or None
        Percentage of the milestone's issues and PRs that are closed.
    html_url : str
        Web URL of the milestone.
    fetched_at : str
        ISO timestamp of the crawl that stored this row.
    """

    number: int = Field(primary_key=True, nullable=True)
    title: str = Field(sa_type=Text)
    description: str | None = Field(default=None, sa_type=Text)
    state: str = Field(sa_type=Text)
    due_on: str | None = Field(default=None, sa_type=Text)
    closed_at: str | None = Field(default=None, sa_type=Text)
    updated_at: str = Field(sa_type=Text)
    progress_pct: float | None = Field(default=None, sa_type=REAL)
    html_url: str = Field(sa_type=Text)
    fetched_at: str = Field(sa_type=Text)
