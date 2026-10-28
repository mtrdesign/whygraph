"""SQLModel for the ``tag`` table."""

from __future__ import annotations

from sqlalchemy import Text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class Tag(WhygraphTable, table=True):
    """One row per repository tag (the newest few hundred).

    Attributes
    ----------
    name : str
        The tag name. Primary key.
    sha : str
        The commit the tag points at (peeled for annotated tags).
    tagged_at : str or None
        When the tag was made, ISO timestamp; ``None`` when unknown.
    subject : str or None
        First line of the tag message or tagged commit.
    """

    name: str = Field(primary_key=True, sa_type=Text)
    sha: str = Field(sa_type=Text)
    tagged_at: str | None = Field(default=None, sa_type=Text)
    subject: str | None = Field(default=None, sa_type=Text)
