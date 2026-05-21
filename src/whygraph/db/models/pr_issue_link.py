"""SQLModel for the ``pr_issue_link`` join table."""

from __future__ import annotations

from sqlalchemy import Text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class PRIssueLink(WhygraphTable, table=True):
    """Many-to-many join between PRs and the issues they reference.

    The only ``link_kind`` populated by the scanner today is ``"closes"``
    (from GraphQL ``closingIssuesReferences``). The column exists so
    future kinds (``"mentions"``, ``"fixes"``, …) can coexist on the
    same row without schema churn.
    """

    pr_number: int = Field(primary_key=True)
    issue_number: int = Field(primary_key=True, index=True)
    link_kind: str = Field(primary_key=True, sa_type=Text)
