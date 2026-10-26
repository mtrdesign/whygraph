"""SQLModel for the ``pr_origin_skip`` table."""

from __future__ import annotations

from sqlalchemy import Text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class PROriginSkip(WhygraphTable, table=True):
    """A squash PR whose origin recovery could not complete at this PR state.

    Written by :class:`~whygraph.scan.pr_origin_enricher.PROriginEnricher`
    only on proof (a ref ``git ls-remote`` showed to be gone, or a commit
    that stayed unreadable after a good fetch), never on a network error
    alone. The PR is skipped until its ``updated_at`` differs from
    ``pr_updated_at``.

    Attributes
    ----------
    pr_number : int
        The PR number. Primary key.
    pr_updated_at : str
        The PR's ``updated_at`` when recovery gave up, as GitHub sent it.
    reason : str
        ``"missing_ref"`` or ``"unreadable_commit"``.
    recorded_at : str
        ISO timestamp, for debugging only.
    """

    pr_number: int = Field(primary_key=True)
    pr_updated_at: str = Field(sa_type=Text)
    reason: str = Field(sa_type=Text)
    recorded_at: str = Field(sa_type=Text)
