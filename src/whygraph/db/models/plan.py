"""SQLModel for the ``plan`` table."""

from __future__ import annotations

from sqlalchemy import Text, text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable

PLAN_MAX_BYTES = 512 * 1024
"""Cap on the stored (redacted) plan body, in bytes."""


class Plan(WhygraphTable, table=True):
    """One row per plan file found in the configured plans directory.

    Notes
    -----
    * ``stem`` (the file name without ``.md``) is the identity; ``path`` is
      the last seen path.
    * ``truncated`` / ``present`` are ``int`` (0/1) and timestamps are ISO
      8601 UTC ``str``, like the rest of the project DB.
    * There is no ``status_line`` column: the plan's own status text is never
      stored or served; status is derived at read time.
    * ``outline`` and ``why_index`` hold JSON-encoded text.
    * ``body`` is redacted and capped at :data:`PLAN_MAX_BYTES`. A row whose
      file left ``HEAD`` keeps ``present = 0`` and is never deleted.
    """

    stem: str = Field(primary_key=True)
    path: str = Field(sa_type=Text)
    blob_sha: str = Field(sa_type=Text)
    parser_version: int
    title: str = Field(sa_type=Text)
    line_count: int
    objective: str | None = Field(default=None, sa_type=Text)
    digest: str = Field(sa_type=Text)
    outline: str = Field(sa_type=Text)
    why_index: str = Field(
        default="{}", sa_type=Text, sa_column_kwargs={"server_default": text("'{}'")}
    )
    body: str = Field(sa_type=Text)
    redacted_lines: int = Field(
        default=0, sa_column_kwargs={"server_default": text("0")}
    )
    truncated: int = Field(default=0, sa_column_kwargs={"server_default": text("0")})
    present: int = Field(default=1, sa_column_kwargs={"server_default": text("1")})
    paths_head: str | None = Field(default=None, sa_type=Text)
    paths_hash: str | None = Field(default=None, sa_type=Text)
    first_seen_at: str = Field(sa_type=Text)
    updated_at: str = Field(sa_type=Text)
