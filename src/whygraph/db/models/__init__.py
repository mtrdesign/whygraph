"""WhyGraph SQLModel tables.

One class per module — the file name matches the snake_case table name.
Each model registers on :data:`whygraph.db.base.metadata` simply by being
imported (SQLModel populates ``SQLModel.metadata`` at class-definition
time), so for Alembic autogenerate to see a new table its module must be
imported from here.

Adding a new model:

1. Create ``whygraph/db/models/<table_name>.py`` defining
   ``class Foo(WhygraphTable, table=True): ...``.
2. Add the class to the imports + ``__all__`` below.

Conventions shared across these tables (so individual files stay terse):

* String columns use :class:`sqlalchemy.Text` (not the SQLModel default
  ``AutoString``/VARCHAR) so DDL emits ``TEXT`` — matching
  :mod:`whygraph.scan.db` and SQLite's natural affinity.
* Float columns use :class:`sqlalchemy.REAL` for the same reason.
* Several columns are typed ``str`` even though they hold JSON-encoded
  Python lists (e.g. ``Commit.parent_shas``, ``PullRequest.labels``,
  ``PullRequest.commit_titles``). Callers encode/decode with ``json`` at
  the boundary. Moving to a proper JSON column type is a follow-up that
  needs a real Alembic migration.

Current models cover the five tables whose auto-derived snake_case name
does *not* collide with the hand-rolled tables owned by
:mod:`whygraph.scan.db`: ``Author``, ``Commit``, ``Issue``,
``PullRequest``, ``PRIssueLink``. The remaining two scan-owned tables
(``rationale_cache``, ``scan_state``) intentionally have no SQLModel
yet — their natural snake_case names collide with scan/db.py, so they
will get models as part of the eventual scanner-side migration to ORM
rather than today.
"""

from __future__ import annotations

from whygraph.db.models.author import Author
from whygraph.db.models.commit import Commit
from whygraph.db.models.issue import Issue
from whygraph.db.models.pr_issue_link import PRIssueLink
from whygraph.db.models.pull_request import PullRequest

__all__ = ["Author", "Commit", "Issue", "PRIssueLink", "PullRequest"]
