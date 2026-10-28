"""SQLModel for the ``forge_crawl_state`` table."""

from __future__ import annotations

from sqlalchemy import Text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class ForgeCrawlState(WhygraphTable, table=True):
    """Where the last complete GitHub crawl of one kind left off.

    One row per kind (``"pull_request"`` or ``"issue"``). The crawler
    reads it to decide between a full walk and an incremental one, and
    writes it only after every row of that crawl is stored.

    Attributes
    ----------
    kind : str
        ``"pull_request"`` or ``"issue"``. Primary key.
    repo : str
        ``owner/name``, case-folded. A different repo forces a full walk.
    watermark : str or None
        The newest ``updatedAt`` seen, as GitHub sent it. ``None`` when
        the repository had no items of this kind.
    crawled_at : str
        ISO timestamp of the crawl. The chat's ``get_work_in_flight`` and
        ``list_recent_activity`` report the older of the two kinds' stamps
        as ``forge_data_as_of``, so a reader can tell how old the PR and
        issue data is.
    """

    kind: str = Field(primary_key=True, sa_type=Text)
    repo: str = Field(sa_type=Text)
    watermark: str | None = Field(default=None, sa_type=Text)
    crawled_at: str = Field(sa_type=Text)
