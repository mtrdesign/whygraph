"""SQLModel for the ``project_import`` table."""

from __future__ import annotations

from sqlalchemy import Text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class ProjectImport(WhygraphTable, table=True):
    """One applied project-transfer import. A log, not a gate.

    Re-importing a bundle is idempotent, so nothing refuses a second row
    for the same bundle; ``bundle_sha256`` is indexed, not unique.

    Attributes
    ----------
    id : int | None
        Primary key.
    bundle_sha256 : str
        sha256 of the imported bundle file.
    imported_by : str
        The importing user's uid.
    imported_at : str
        ISO-8601 UTC.
    source_mode : str
        ``local`` or ``production``, the mode that produced the bundle.
    source_label : str
        ``<org>/<slug>`` of the source project (at most 200 characters).
    whygraph_version : str
        WhyGraph version that wrote the bundle.
    counts : str
        JSON-encoded counts of what the import did.
    """

    id: int | None = Field(default=None, primary_key=True)
    bundle_sha256: str = Field(sa_type=Text, index=True)
    imported_by: str = Field(sa_type=Text)
    imported_at: str = Field(sa_type=Text)
    source_mode: str = Field(sa_type=Text)
    source_label: str = Field(sa_type=Text)
    whygraph_version: str = Field(sa_type=Text)
    counts: str = Field(sa_type=Text)
