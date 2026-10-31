"""SQLModel for the ``project_note`` table."""

from __future__ import annotations

from sqlalchemy import Text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class ProjectNote(WhygraphTable, table=True):
    """A code-anchored team note: a decision, constraint or gotcha.

    Values are validated in :mod:`whygraph.mcp.notes`; the table carries no
    CHECK constraints, like the rest of the project DB.

    Attributes
    ----------
    id : int | None
        Primary key.
    kind : str
        One of ``NOTE_KINDS``.
    body : str
        The note text (at most 1000 characters).
    proposed_body : str | None
        The body as proposed; set only when confirm edited it.
    status : str
        ``proposed``, ``confirmed`` or ``retired``.
    anchor_kind : str
        One of ``ANCHOR_KINDS``.
    anchor_path : str | None
        Repo-relative file or directory.
    line_start, line_end : int | None
        Line range for a range anchor.
    qualified_name : str | None
        Symbol the note is anchored to.
    area : str | None
        Configured area label.
    content_hash : str | None
        sha256 of the anchored lines at (re)confirmation; symbol and range
        anchors only.
    source : str
        ``human``, ``chat`` or ``mcp``.
    origin : str | None
        ``chat:<session_id>``, ``mcp`` or ``ui``.
    refs : str | None
        JSON list of SHAs / ``#N`` references.
    supersedes_id : int | None
        The note this one replaces.
    proposed_by, proposed_at : str
        Proposer uid and ISO-8601 UTC timestamp.
    confirmed_by, confirmed_at : str | None
        Confirmation actor and time.
    reconfirmed_by, reconfirmed_at : str | None
        Latest re-confirmation actor and time.
    retired_by, retired_at : str | None
        Retirement actor and time.
    retire_reason : str | None
        One of ``RETIRE_REASONS``.
    """

    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(sa_type=Text)
    body: str = Field(sa_type=Text)
    proposed_body: str | None = Field(default=None, sa_type=Text)
    status: str = Field(sa_type=Text, index=True)
    anchor_kind: str = Field(sa_type=Text)
    anchor_path: str | None = Field(default=None, sa_type=Text, index=True)
    line_start: int | None = Field(default=None)
    line_end: int | None = Field(default=None)
    qualified_name: str | None = Field(default=None, sa_type=Text, index=True)
    area: str | None = Field(default=None, sa_type=Text)
    content_hash: str | None = Field(default=None, sa_type=Text)
    source: str = Field(sa_type=Text)
    origin: str | None = Field(default=None, sa_type=Text)
    refs: str | None = Field(default=None, sa_type=Text)
    supersedes_id: int | None = Field(default=None, foreign_key="project_note.id")
    proposed_by: str = Field(sa_type=Text)
    proposed_at: str = Field(sa_type=Text)
    confirmed_by: str | None = Field(default=None, sa_type=Text)
    confirmed_at: str | None = Field(default=None, sa_type=Text)
    reconfirmed_by: str | None = Field(default=None, sa_type=Text)
    reconfirmed_at: str | None = Field(default=None, sa_type=Text)
    retired_by: str | None = Field(default=None, sa_type=Text)
    retired_at: str | None = Field(default=None, sa_type=Text)
    retire_reason: str | None = Field(default=None, sa_type=Text)
