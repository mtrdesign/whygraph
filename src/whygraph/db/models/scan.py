"""SQLModel tables for scan-derived VCS data.

Tables created here are *new* — they do not mirror the hand-rolled
tables maintained by :mod:`whygraph.scan.db` (which use different,
plural names like ``commits``/``pull_requests``). The two layers live
side-by-side in the same SQLite file but own disjoint table sets; the
scanner has not been ported to write through these models yet.

Notes
-----
* String columns use :class:`sqlalchemy.Text` (not the SQLModel default
  ``AutoString``/VARCHAR) so DDL emits ``TEXT`` — matching ``scan/db.py``
  and SQLite's natural affinity.
* Float columns use :class:`sqlalchemy.REAL` for the same reason.
* Several columns are typed ``str`` even though they hold JSON-encoded
  Python lists (e.g. ``Commit.parent_shas``, ``PullRequest.labels``,
  ``PullRequest.commit_titles``). Callers encode/decode with ``json``
  at the boundary. Moving to a proper JSON column type is a follow-up
  that needs a real Alembic migration.
* ``PullRequest.draft`` is ``int`` (0/1), not ``bool``, to keep the
  declared SQLite affinity as INTEGER.
"""

from __future__ import annotations

from sqlalchemy import REAL, Text, text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class Commit(WhygraphTable, table=True):
    """One row per scanned Git commit (first-parent walk of the default branch)."""

    sha: str = Field(primary_key=True, nullable=True, sa_type=Text)
    parent_shas: str = Field(sa_type=Text)
    author_name: str = Field(sa_type=Text)
    author_email: str = Field(sa_type=Text)
    authored_at: str = Field(sa_type=Text, index=True)
    committed_at: str = Field(sa_type=Text)
    subject: str = Field(sa_type=Text)
    body: str = Field(sa_type=Text)
    files_changed: int
    insertions: int
    deletions: int
    scanned_at: str = Field(sa_type=Text)
    subject_tfidf_score: float = Field(
        default=0.0, sa_type=REAL, sa_column_kwargs={"server_default": text("0")}
    )
    body_tfidf_score: float = Field(
        default=0.0, sa_type=REAL, sa_column_kwargs={"server_default": text("0")}
    )
    llm_description: str | None = Field(default=None, sa_type=Text)
    llm_description_model: str | None = Field(default=None, sa_type=Text)


class PullRequest(WhygraphTable, table=True):
    """One row per pull request fetched via ``gh api graphql``.

    ``closing_issue_numbers`` from GraphQL is *not* stored here — it is
    flattened into :class:`PRIssueLink` rows by the scan writer.
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
    title_tfidf_score: float = Field(
        default=0.0, sa_type=REAL, sa_column_kwargs={"server_default": text("0")}
    )
    body_tfidf_score: float = Field(
        default=0.0, sa_type=REAL, sa_column_kwargs={"server_default": text("0")}
    )


class Issue(WhygraphTable, table=True):
    """One row per issue fetched via ``gh api graphql`` (PRs excluded)."""

    number: int = Field(primary_key=True, nullable=True)
    title: str = Field(sa_type=Text)
    body: str | None = Field(default=None, sa_type=Text)
    state: str = Field(sa_type=Text, index=True)
    created_at: str = Field(sa_type=Text)
    updated_at: str = Field(sa_type=Text)
    closed_at: str | None = Field(default=None, sa_type=Text)
    author: str | None = Field(default=None, sa_type=Text)
    html_url: str = Field(sa_type=Text)
    labels: str = Field(sa_type=Text)
    fetched_at: str = Field(sa_type=Text)
    title_tfidf_score: float = Field(
        default=0.0, sa_type=REAL, sa_column_kwargs={"server_default": text("0")}
    )
    body_tfidf_score: float = Field(
        default=0.0, sa_type=REAL, sa_column_kwargs={"server_default": text("0")}
    )


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
