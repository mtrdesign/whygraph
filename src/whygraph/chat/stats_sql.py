"""The WhyGraph statistics surface — a table allowlist and a schema doc.

``get_repo_overview`` answers five counts; everything else the schema already
knows — velocity by month, churn, hotspot files, contributor breakdowns, PR cycle
time — had no tool at all, and the only route left was paging commits one SHA at
a time, which the tool-round budget makes impossible.

The **four security layers** that fence that capability live in
:mod:`whygraph.chat.sql_guard` — read its docstring for the security model; it is
shared with CodeGraph's surface (:mod:`whygraph.chat.graph_stats_sql`) so there is
one authorizer to audit rather than two that drift. What stays here is the part
that is genuinely per-database: :data:`_ALLOWED_TABLES` (a frozenset **literal**)
and :data:`_SCHEMA_DOC` (the tool description).

Chat-only — never registered with MCP.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from whygraph.db import get_engine

from . import sql_guard
from .sql_guard import SqlNotAllowed, SqlSurface, run_aggregate_query

_log = logging.getLogger(__name__)

_MAX_ROWS = sql_guard._MAX_ROWS
"""Re-export of the shared row cap, for callers that document the limit.

The cap itself is layer 4 and belongs to :mod:`sql_guard`; this name exists so
that "how many rows can this tool return" is answerable from the surface the
caller actually uses. Do not shadow it with a different value — the guard reads
its own.
"""

_ALLOWED_TABLES = frozenset(
    {
        "commit",
        "commit_file_change",
        "pull_request",
        "issue",
        "pr_issue_link",
        "author",
    }
)
"""The read allowlist — **the** security boundary of this module.

Must stay a literal. Deriving it from the schema would silently widen the
surface the next time a table is added, which is exactly how ``chat_message``
(the assistant's own transcripts) or ``rationale_cache`` would leak in.
``rationale_cache`` is excluded deliberately: coverage is already reported by
``get_repo_overview``, so the tool gains nothing by reaching it.
"""

_SURFACE = SqlSurface(
    label="WhyGraph",
    allowed_tables=_ALLOWED_TABLES,
    db_path=lambda: Path(get_engine().url.database or ""),
    missing_db_message=(
        "WhyGraph DB is missing or unreadable at {db_path} — run `whygraph scan` first"
    ),
)
"""This module's binding of the shared guard. ``db_path`` is deferred so it
resolves per call, following the same ``whygraph.toml`` / project-root discovery
as the rest of the package rather than freezing whichever repo was current at
import."""


def _connect(db_path: Path, denials: list[str] | None = None) -> sqlite3.Connection:
    """Open the WhyGraph DB read-only with the authorizer already installed.

    A thin binding of :meth:`sql_guard.SqlSurface.connect` to this surface. Kept
    as a module-level name because the layer-2 tests drive the authorizer through
    it directly, with the shape check out of the way — a bug in that string check
    must not be load-bearing.
    """
    return _SURFACE.connect(db_path, denials)


def run_stats_query(sql: str, *, db_path: Path | None = None) -> dict:
    """Execute one aggregate query against the WhyGraph DB and return its rows.

    Parameters
    ----------
    sql : str
        A single ``SELECT`` / ``WITH`` statement that aggregates.
    db_path : Path, optional
        The database to read. Defaults to whatever the SQLModel engine is bound
        to.

    Returns
    -------
    dict
        See :func:`sql_guard.run_aggregate_query` — ``{"sql", "columns", "rows",
        "row_count", "truncated"}`` on success, ``{"error", "layer"}`` when a
        layer refused. Never raises for a query-level problem.
    """
    return run_aggregate_query(sql, _SURFACE, db_path=db_path)


_SCHEMA_DOC = """\
Run a read-only aggregate SQL query for PROJECT STATISTICS — velocity, churn,
hotspots, contributor and PR-cycle-time breakdowns. Aggregates only: the query
MUST use COUNT/SUM/AVG/MIN/MAX or GROUP BY, and returns at most 200 rows.

Do NOT use this to look up individual commits, PRs, or a file's history —
get_commit / get_pr / get_area_history / find_changes handle those, and they
follow rename chains and git blame, which raw SQL here does NOT.

=== FIVE REQUIRED RULES (each of these silently corrupts results) ===

1. ALWAYS filter `on_default_branch = 1` on the commit table. Rows with 0 are
   PR-origin commits recovered from squash merges; counting them double-counts
   work that is already on the main walk.

2. For dates ALWAYS use SQLite's date functions — strftime(), date(),
   julianday() — and NEVER substr() on a timestamp. Timestamps are TEXT in
   mixed ISO-8601 forms (commits carry a UTC offset like +03:00, GitHub rows
   end in Z). The date functions normalise every form to UTC; substr() silently
   uses whatever offset the author's machine had. On this repo the two methods
   disagree for 12 of 219 commits and produce a whole extra month bucket.
   Month grouping: strftime('%Y-%m', authored_at).

3. NEVER compare two timestamps as strings across tables. '...+03:00' vs
   '...Z' compares lexicographically, not chronologically. Use
   julianday(a) - julianday(b) for durations (result is in DAYS).

4. A merged PR has state = 'closed', NOT 'merged'. Count merges with
   `merged_at IS NOT NULL`. `state` is only 'open' or 'closed'.

5. NEVER GROUP DEVELOPERS BY `commit.author_name` OR `author_email`. Those
   are raw git identities and one human routinely has several — a work
   email, a GitHub noreply address, a second machine. Grouping by either
   reports one person as two or three, and the number looks authoritative.
   The `author` table has already resolved this, one row per human.

   For an ALL-TIME ranking, do not join at all — the counts are already
   columns. This tool still requires an aggregate, so wrap them in SUM() and
   group by the author's id (one row per group, so SUM is an identity):

     SELECT COALESCE(primary_login, primary_name, primary_email) AS developer,
            SUM(commit_count) AS commits, SUM(pr_count) AS prs
     FROM author GROUP BY id ORDER BY commits DESC

   (commit_count is already default-branch-only — do NOT also apply rule 1.)
   Selecting commit_count bare, without SUM and GROUP BY, is REFUSED as a
   non-aggregate query.

   For anything TIME-SLICED ("lately", per month, since a date), join
   commit to author on the emails array with instr():

     SELECT COALESCE(a.primary_login, a.primary_name, a.primary_email)
              AS developer,
            COUNT(*) AS commits
     FROM "commit" c
     JOIN author a ON instr(a.emails, '"' || c.author_email || '"') > 0
     WHERE c.on_default_branch = 1
       AND c.authored_at >= date('now', '-90 days')
     GROUP BY a.id ORDER BY commits DESC

   Use instr() exactly as written. Do NOT use json_each (not permitted here)
   and do NOT use LIKE for this match: '_' is a LIKE wildcard and appears in
   ordinary addresses, so a LIKE join can attribute one person's commits to
   a different person. The surrounding double quotes matter — they stop a
   short address matching a longer one.

   Never present a commit count as a measure of productivity.

=== TABLES ===

commit  — one row per scanned commit (first-parent walk of the default branch)
  NOTE: `commit` is a SQL keyword — quote it as "commit".
  sha TEXT PK · parent_shas TEXT (space-delimited, not JSON)
  author_name, author_email TEXT   -- RAW git identity, one human may have
                       -- several. Never group developers by these — see rule 5.
  authored_at TEXT   -- when written; use THIS for velocity
  committed_at TEXT  -- when committed; differs after rebase/cherry-pick
  subject, body TEXT -- developer-written; may be terse or wrong
  llm_description TEXT -- generated from the DIFF ALONE; authoritative for
                       -- what changed. NULL for ~23% (unbackfilled).
  files_changed, insertions, deletions INTEGER  -- per-commit totals
  refactor_score INTEGER 0-100 -- heuristic likelihood this is a refactor /
                       -- formatter sweep. Observed: 201 commits 0-24,
                       -- 13 at 25-49, 5 at 50-74. Not a quality measure.
  on_default_branch INTEGER 0/1 -- see rule 1
  scanned_at TEXT

commit_file_change — one row per (commit, path AT THAT COMMIT)
  commit_sha TEXT (indexed, FK commit.sha) · path TEXT (indexed)
  change_type TEXT — single git letter: 'M' modified, 'A' added, 'D' deleted,
                     'R' renamed, 'C' copied. NOT words.
  renamed_from TEXT NULL -- previous path when change_type='R'
  similarity INTEGER NULL -- rename similarity %, only on renames
  lines_added, lines_deleted INTEGER
  NOTE: `path` does not follow renames. A file moved mid-history appears under
  both names and a path filter under-reports it.

pull_request
  number INTEGER PK · title TEXT · body TEXT · state TEXT ('open'|'closed')
  draft INTEGER 0/1 · author TEXT · base_ref TEXT · head_sha, merge_commit_sha
  created_at, updated_at, closed_at, merged_at TEXT (merged_at NULL if unmerged)
  Cycle time: julianday(merged_at) - julianday(created_at) WHERE merged_at
  IS NOT NULL  -- days

issue
  number INTEGER PK · title, body, state, author TEXT
  created_at, updated_at, closed_at TEXT

pr_issue_link — pr_number, issue_number, link_kind ('closes')

author — resolved contributor identities, ONE ROW PER HUMAN. Built by the
  scan's author phase from evidence only (git mailmap, GitHub's own
  login/name/email triples, noreply parsing, byte-equal emails) — never from
  a display name, so two people who share a name are never merged.
  id INTEGER PK · primary_login TEXT NULL (GitHub login; NULL if they never
    appeared in a PR/issue) · primary_name, primary_email TEXT
  emails, logins, names TEXT -- JSON ARRAYS of every known value, sorted
  commit_count INTEGER -- default-branch commits only (rule 1 already applied)
  pr_count, issue_count INTEGER · first_seen, last_seen TEXT
  Rebuilt from scratch each scan, so it is current or absent, never stale.
  If it is EMPTY the author phase has not run — fall back to
  commit.author_email and say the identities are unresolved.

If a table returns nothing, that source has not been scanned — say so rather
than reporting zero as a finding.\
"""
"""The tool description — and the most important asset in this module.

A bare ``table(col, col)`` list is not enough to write a *correct* aggregate,
and this is the one tool whose wrong answers are undetectable by the reader: a
plausible number arrives with no way to tell it apart from a right one. So every
field whose meaning or value domain is non-obvious carries a note, and every
value domain quoted was **measured on this repository** rather than assumed.

The five rules exist because each one is a silent-corruption trap that was hit
in practice while probing the schema. They are guarded by a test — they must not
be paraphrased or trimmed to save tokens. Rule 5's ``instr`` join is quoted
verbatim on purpose: the two obvious alternatives are both wrong here.
``json_each`` is denied by the authorizer (a table-valued function registers as a
table read, and it is not in :data:`_ALLOWED_TABLES`), and a ``LIKE`` match
against the JSON array treats ``_`` as a single-character wildcard — so one
person's commits can be attributed to a different person whose address differs
only at that position. That false merge is undetectable from the output.
"""


__all__ = ["SqlNotAllowed", "run_stats_query"]
