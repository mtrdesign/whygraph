"""SQLModel for the ``plan_link`` join table."""

from __future__ import annotations

from sqlalchemy import Index, Text, text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class PlanLink(WhygraphTable, table=True):
    """Link from a plan to a commit, a pull request or a repo path.

    Notes
    -----
    * ``stem`` is deliberately not a foreign key: a commit trailer may name a
      plan that is not indexed yet.
    * ``kind`` is ``"commit"``, ``"pr"`` or ``"path"``; ``ref`` is the sha,
      the PR number as text, or the repo path.
    * ``sections`` and ``declared`` are set for ``kind = "path"`` only.
    """

    __table_args__ = (Index("ix_plan_link_kind_ref", "kind", "ref"),)

    stem: str = Field(primary_key=True)
    kind: str = Field(primary_key=True)
    ref: str = Field(primary_key=True)
    sections: str | None = Field(default=None, sa_type=Text)
    declared: int = Field(default=0, sa_column_kwargs={"server_default": text("0")})
