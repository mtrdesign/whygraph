"""SQLModel for the ``project_purpose_run`` table."""

from __future__ import annotations

from sqlalchemy import Text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class ProjectPurposeRun(WhygraphTable, table=True):
    """One run of the project-purpose generator, kept for staleness and audit.

    Written once, at the end of a run that reached ``ok``, ``failed``,
    ``no_input`` or ``blocked``. Refusals that never reach the generator
    (``unchanged``, ``409 purpose_in_progress``, role, linked and validation
    errors) write nothing. The newest 50 rows per project are kept.

    Attributes
    ----------
    id : int | None
        Primary key.
    requested_by : str
        The requesting user's uid.
    started_at : str
        ISO-8601 UTC, captured before the provider call.
    finished_at : str
        ISO-8601 UTC, captured when the run ended.
    status : str
        ``ok``, ``failed``, ``no_input`` or ``blocked`` (validated in code).
    input_hash : str | None
        sha256 of the gathered inputs.
    input_bytes : int | None
        Size of the gathered inputs in bytes.
    sources : str | None
        JSON list of the repo-relative paths read (at most 20).
    provider : str | None
        Provider that served the call.
    model : str | None
        Model that served the call.
    tokens_in, tokens_out : int | None
        Token counts reported by the provider.
    note_id : int | None
        The ``project_note`` proposal this run made. A plain integer, not a
        foreign key, so a downgrade cannot trip over it.
    error_code : str | None
        Machine-readable error code; never a message with content.
    """

    id: int | None = Field(default=None, primary_key=True)
    requested_by: str = Field(sa_type=Text)
    started_at: str = Field(sa_type=Text)
    finished_at: str = Field(sa_type=Text)
    status: str = Field(sa_type=Text)
    input_hash: str | None = Field(default=None, sa_type=Text)
    input_bytes: int | None = Field(default=None)
    sources: str | None = Field(default=None, sa_type=Text)
    provider: str | None = Field(default=None, sa_type=Text)
    model: str | None = Field(default=None, sa_type=Text)
    tokens_in: int | None = Field(default=None)
    tokens_out: int | None = Field(default=None)
    note_id: int | None = Field(default=None)
    error_code: str | None = Field(default=None, sa_type=Text)
