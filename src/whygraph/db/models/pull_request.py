"""SQLModel for the ``pull_request`` table."""

from __future__ import annotations

from sqlalchemy import Text, text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class PullRequest(WhygraphTable, table=True):
    """One row per pull request fetched via ``gh api graphql``.

    ``closing_issue_numbers`` from GraphQL is *not* stored here — it is
    flattened into :class:`whygraph.db.models.PRIssueLink` rows by the
    scan writer.

    Notes
    -----
    * ``draft`` is ``int`` (0/1), not ``bool``, to keep the declared
      SQLite affinity as INTEGER.

    Attributes
    ----------
    milestone : int or None
        The milestone number, or ``None`` when the PR has none.
    assignees : str
        JSON list of assignee logins.
    review_decision : str or None
        GitHub's ``reviewDecision`` (``APPROVED``, ``CHANGES_REQUESTED``,
        ``REVIEW_REQUIRED``), ``None`` when unset.
    latest_reviews : str
        JSON list of the latest opinionated review per reviewer.
    review_requests : str
        JSON list of requested reviewers (users and bots only).
    first_review_at : str or None
        Earliest submitted review by someone other than the author.
    files : str
        JSON list of changed file paths (first 100).
    additions, deletions, changed_files : int or None
        Diff totals as GitHub reports them.
    comments : str
        JSON list of the conversation comments (first 100); from forge
        fields version 2 on, human, non-minimized comments only.
    review_summaries : str
        JSON list of the review summaries with a body (from the last 30
        reviews); human, non-minimized reviews only.
    review_threads : str
        JSON list of the first 20 review threads, written only by the
        thread pass; a thread with no human, non-minimized comment is not
        stored.
    review_threads_total : int or None
        GitHub's review-thread count; ``None`` until a crawl re-reads the row.
    review_threads_synced : int
        1 once the thread pass stored this PR's threads, 0 while they are
        due (0/1, like ``draft``).
    comments_total : int or None
        GitHub's conversation-comment count (includes comments not stored);
        ``None`` until a crawl re-reads the row.
    """

    number: int = Field(primary_key=True, nullable=True)
    title: str = Field(sa_type=Text)
    body: str | None = Field(default=None, sa_type=Text)
    state: str = Field(sa_type=Text, index=True)
    draft: int = Field(default=0, sa_column_kwargs={"server_default": text("0")})
    created_at: str = Field(sa_type=Text)
    updated_at: str = Field(sa_type=Text)
    closed_at: str | None = Field(default=None, sa_type=Text)
    merged_at: str | None = Field(default=None, sa_type=Text)
    merge_commit_sha: str | None = Field(default=None, sa_type=Text, index=True)
    head_sha: str = Field(sa_type=Text)
    head_ref: str | None = Field(default=None, sa_type=Text)
    base_ref: str = Field(sa_type=Text)
    author: str | None = Field(default=None, sa_type=Text)
    html_url: str = Field(sa_type=Text)
    labels: str = Field(sa_type=Text)
    fetched_at: str = Field(sa_type=Text)
    commit_titles: str = Field(
        default="[]", sa_type=Text, sa_column_kwargs={"server_default": text("'[]'")}
    )
    comments: str = Field(
        default="[]", sa_type=Text, sa_column_kwargs={"server_default": text("'[]'")}
    )
    milestone: int | None = Field(default=None)
    assignees: str = Field(
        default="[]", sa_type=Text, sa_column_kwargs={"server_default": text("'[]'")}
    )
    review_decision: str | None = Field(default=None, sa_type=Text)
    latest_reviews: str = Field(
        default="[]", sa_type=Text, sa_column_kwargs={"server_default": text("'[]'")}
    )
    review_requests: str = Field(
        default="[]", sa_type=Text, sa_column_kwargs={"server_default": text("'[]'")}
    )
    first_review_at: str | None = Field(default=None, sa_type=Text)
    files: str = Field(
        default="[]", sa_type=Text, sa_column_kwargs={"server_default": text("'[]'")}
    )
    additions: int | None = Field(default=None)
    deletions: int | None = Field(default=None)
    changed_files: int | None = Field(default=None)
    review_summaries: str = Field(
        default="[]", sa_type=Text, sa_column_kwargs={"server_default": text("'[]'")}
    )
    review_threads: str = Field(
        default="[]", sa_type=Text, sa_column_kwargs={"server_default": text("'[]'")}
    )
    review_threads_total: int | None = Field(default=None)
    review_threads_synced: int = Field(
        default=0, sa_column_kwargs={"server_default": text("0")}
    )
    comments_total: int | None = Field(default=None)
