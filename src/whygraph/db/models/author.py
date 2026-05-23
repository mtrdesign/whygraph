"""SQLModel for the ``author`` table.

Deduplicated identity rows built by :mod:`whygraph.scan.authors` from
the union of commit author email/name pairs and GitHub PR/issue
authors. Conceptually rebuilt per scan, so the ``id`` column is
ephemeral — do not reference it from external systems.
"""

from __future__ import annotations

from sqlalchemy import Text, text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class Author(WhygraphTable, table=True):
    """One row per identity resolved from commits and GitHub activity."""

    id: int | None = Field(
        default=None,
        primary_key=True,
        nullable=True,
        sa_column_kwargs={"autoincrement": True},
    )
    primary_login: str | None = Field(default=None, sa_type=Text, index=True)
    primary_name: str | None = Field(default=None, sa_type=Text)
    primary_email: str | None = Field(default=None, sa_type=Text, index=True)
    emails: str = Field(sa_type=Text)
    logins: str = Field(sa_type=Text)
    names: str = Field(sa_type=Text)
    first_seen: str | None = Field(default=None, sa_type=Text)
    last_seen: str | None = Field(default=None, sa_type=Text)
    commit_count: int = Field(default=0, sa_column_kwargs={"server_default": text("0")})
    pr_count: int = Field(default=0, sa_column_kwargs={"server_default": text("0")})
    issue_count: int = Field(default=0, sa_column_kwargs={"server_default": text("0")})
