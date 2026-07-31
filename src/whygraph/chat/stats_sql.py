"""Authorizer-locked, aggregate-only SQL over the WhyGraph database.

The statistics surface. ``get_repo_overview`` answers five counts; everything
else the schema already knows — velocity by month, churn, hotspot files,
contributor breakdowns, PR cycle time — had no tool at all, and the only route
left was paging commits one SHA at a time, which the tool-round budget makes
impossible.

Raw SQL is a large capability to hand a model, so it is fenced by **four
independent layers**, in order of how much they can be trusted:

1. **The connection** is opened ``mode=ro``. No write can reach the file.
2. **An authorizer** (:func:`_make_authorizer`) runs inside SQLite's own VM. It
   permits ``SELECT``, function calls, and reads of :data:`_ALLOWED_TABLES`
   only, and denies every other action code — so ``DROP``, ``ATTACH``,
   ``PRAGMA``, and reads of the chat transcripts are refused by SQLite
   itself, not by inspecting the query text.
3. **A shape check** (:func:`_check_shape`) requires one statement that starts
   with ``SELECT``/``WITH`` and contains an aggregate. This is what makes
   "statistics only" hold by construction rather than by prompt wording:
   record-fetching belongs to ``find_changes`` / ``get_area_history`` /
   ``get_commit``, which follow rename chains and git blame as raw SQL here
   cannot.
4. **Output caps** — :data:`_MAX_ROWS` and a progress-handler deadline, so
   neither a huge result nor a runaway join can hurt the caller.

Only layers 1 and 2 are a security boundary. Layer 3 is a *scope* boundary
enforced on a string, and a determined query could word its way around it; the
consequence of that is a boring record list, because layers 1–2 still hold.

Chat-only — never registered with MCP.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path

from whygraph.db import get_engine

_log = logging.getLogger(__name__)

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

_MAX_ROWS = 200
"""Row cap. A statistic that needs more rows than this is a record dump."""

_TIMEOUT_SEC = 5.0
"""Wall-clock budget per query, enforced by the progress handler."""

_PROGRESS_INTERVAL = 10_000
"""VM instructions between deadline checks.

An interval of 1,000 fires the callback ~72,000 times in 0.3s on a pathological
join — far more often than a 5s budget needs. 10,000 keeps abort granularity
well under a second at a fraction of the overhead.
"""

_AGGREGATE_TOKENS = (
    "count(",
    "sum(",
    "avg(",
    "min(",
    "max(",
    "total(",
    "group by",
)
"""One of these must appear for a query to count as a statistic."""

_ALLOWED_ACTIONS = frozenset(
    {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
)
"""Authorizer action codes that may proceed. Everything else is denied.

Notably absent: ``SQLITE_RECURSIVE``, so ``WITH RECURSIVE`` is unavailable. No
statistic here needs it, and denying it keeps the allowlist to the three codes
a plain aggregate actually issues.
"""

_DENIED_FUNCTIONS = frozenset({"load_extension", "readfile", "writefile", "edit"})
"""Functions denied by name even though ``SQLITE_FUNCTION`` is allowed.

Defence in depth. ``load_extension`` needs ``enable_load_extension`` (off by
default) and the file-I/O functions ship only with the ``sqlite3`` CLI, so none
of these should be reachable — which is the point: if a build ever makes one
reachable, this still refuses.
"""

_ACTION_NAMES = {
    sqlite3.SQLITE_INSERT: "INSERT",
    sqlite3.SQLITE_UPDATE: "UPDATE",
    sqlite3.SQLITE_DELETE: "DELETE",
    sqlite3.SQLITE_DROP_TABLE: "DROP TABLE",
    sqlite3.SQLITE_DROP_VIEW: "DROP VIEW",
    sqlite3.SQLITE_DROP_INDEX: "DROP INDEX",
    sqlite3.SQLITE_DROP_TRIGGER: "DROP TRIGGER",
    sqlite3.SQLITE_CREATE_TABLE: "CREATE TABLE",
    sqlite3.SQLITE_CREATE_VIEW: "CREATE VIEW",
    sqlite3.SQLITE_CREATE_INDEX: "CREATE INDEX",
    sqlite3.SQLITE_CREATE_TRIGGER: "CREATE TRIGGER",
    sqlite3.SQLITE_ALTER_TABLE: "ALTER TABLE",
    sqlite3.SQLITE_ATTACH: "ATTACH",
    sqlite3.SQLITE_DETACH: "DETACH",
    sqlite3.SQLITE_PRAGMA: "PRAGMA",
    sqlite3.SQLITE_TRANSACTION: "transaction control",
    sqlite3.SQLITE_REINDEX: "REINDEX",
    sqlite3.SQLITE_ANALYZE: "ANALYZE",
    sqlite3.SQLITE_RECURSIVE: "WITH RECURSIVE",
}
"""Human names for the denied action codes, so a refusal says what it refused.

Only used to build error text — the allow decision is
:data:`_ALLOWED_ACTIONS` alone, so a code missing from this map is still
denied (and reported by number).
"""


class SqlNotAllowed(Exception):
    """A query was refused before or during execution.

    Attributes
    ----------
    layer : str
        Which of the four layers rejected it. Surfaced to the model so it can
        correct the query itself rather than losing the turn.
    """

    def __init__(self, message: str, *, layer: str) -> None:
        super().__init__(message)
        self.layer = layer


def _make_authorizer(denials: list[str]):
    """Build the authorizer callback, recording what it refused into ``denials``.

    The recording exists for the error message. SQLite's own wording is
    ``"access to chat_message.content is prohibited"`` for a *column* read but a
    bare ``"not authorized"`` for a table-level one — so
    ``SELECT count(*) FROM chat_message`` would refuse without telling the model
    *what* it refused, which is the difference between a result it can correct
    and a dead end.

    Parameters
    ----------
    denials : list of str
        Mutable sink, appended to on each refusal. The caller reads it after the
        failed ``execute`` to name the offending target.

    Returns
    -------
    callable
        A five-argument callback for :meth:`sqlite3.Connection.set_authorizer`.
        ``arg1`` / ``arg2`` are action-dependent: table and column for a read,
        and the function name in ``arg2`` for a function call.

    Notes
    -----
    ``SQLITE_DENY`` is returned rather than ``SQLITE_IGNORE`` on purpose:
    ``IGNORE`` substitutes ``NULL`` for a denied column and lets the query
    *succeed* with silently wrong output — the worst possible outcome for a
    statistics tool.
    """

    def _authorizer(
        action: int,
        arg1: str | None,
        arg2: str | None,
        db_name: str | None,
        trigger: str | None,
    ) -> int:
        if action not in _ALLOWED_ACTIONS:
            denials.append(_ACTION_NAMES.get(action, f"action code {action}"))
            return sqlite3.SQLITE_DENY
        if (
            action == sqlite3.SQLITE_READ
            and (arg1 or "").lower() not in _ALLOWED_TABLES
        ):
            denials.append(f"table {arg1}")
            return sqlite3.SQLITE_DENY
        if (
            action == sqlite3.SQLITE_FUNCTION
            and (arg2 or "").lower() in _DENIED_FUNCTIONS
        ):
            denials.append(f"function {arg2}()")
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return _authorizer


def _check_shape(sql: str) -> str:
    """Validate that ``sql`` is a single read-only aggregate. Returns it trimmed.

    Raises
    ------
    SqlNotAllowed
        With ``layer="shape"`` and a message saying what to fix.
    """
    trimmed = sql.strip().rstrip(";").strip()
    if not trimmed:
        raise SqlNotAllowed("query is empty", layer="shape")
    if ";" in trimmed:
        raise SqlNotAllowed(
            "only one statement is allowed — remove the ';' and everything after it",
            layer="shape",
        )
    # Collapse whitespace and close the gap in `count (*)` so the token scan
    # cannot be defeated by formatting.
    normalized = re.sub(r"\s+", " ", trimmed.lower())
    normalized = re.sub(r"\s+\(", "(", normalized)
    if not normalized.startswith(("select", "with")):
        raise SqlNotAllowed(
            "only SELECT (or WITH ... SELECT) queries are allowed",
            layer="shape",
        )
    if not any(token in normalized for token in _AGGREGATE_TOKENS):
        raise SqlNotAllowed(
            "this tool answers STATISTICS only, so the query must aggregate: "
            "use COUNT/SUM/AVG/MIN/MAX or GROUP BY. To look up individual "
            "commits, PRs, or a file's history use find_changes, "
            "get_area_history, get_commit, or get_pr instead — those follow "
            "rename chains and git blame, which this tool does not.",
            layer="shape",
        )
    return trimmed


def _connect(db_path: Path, denials: list[str] | None = None) -> sqlite3.Connection:
    """Open the WhyGraph DB read-only with the authorizer already installed.

    Parameters
    ----------
    db_path : Path
        The database file. Opened ``mode=ro``, so the handle cannot write even
        if every other layer were removed.
    denials : list of str, optional
        Sink for what the authorizer refused — see :func:`_make_authorizer`.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise SqlNotAllowed(
            f"WhyGraph DB is missing or unreadable at {db_path} — "
            "run `whygraph scan` first",
            layer="connection",
        ) from exc
    conn.set_authorizer(_make_authorizer(denials if denials is not None else []))
    return conn


def run_stats_query(sql: str, *, db_path: Path | None = None) -> dict:
    """Execute one aggregate query and return its rows.

    Parameters
    ----------
    sql : str
        A single ``SELECT`` / ``WITH`` statement that aggregates.
    db_path : Path, optional
        The database to read. Defaults to whatever the SQLModel engine is
        bound to, so this always follows the same ``whygraph.toml`` /
        project-root resolution as the rest of the package.

    Returns
    -------
    dict
        ``{"sql", "columns", "rows", "row_count", "truncated"}`` on success, or
        ``{"error", "layer"}`` when a layer refused. Never raises for a
        query-level problem: a refusal the model can read and correct is worth
        more than an exception that ends the turn.
    """
    try:
        trimmed = _check_shape(sql)
    except SqlNotAllowed as exc:
        return {"error": str(exc), "layer": exc.layer}

    if db_path is None:
        db_path = Path(get_engine().url.database or "")

    denials: list[str] = []
    try:
        conn = _connect(db_path, denials)
    except SqlNotAllowed as exc:
        return {"error": str(exc), "layer": exc.layer}

    deadline = time.monotonic() + _TIMEOUT_SEC

    def _guard() -> int:
        """Abort the statement once the deadline passes.

        Returning non-zero from a progress handler is itself sufficient to
        interrupt — no worker thread and no ``interrupt()`` call. A signal
        handler would be outright wrong here: these run in FastAPI's
        threadpool, and Python installs signal handlers on the main thread
        only.
        """
        return 1 if time.monotonic() > deadline else 0

    try:
        conn.set_progress_handler(_guard, _PROGRESS_INTERVAL)
        cursor = conn.execute(trimmed)
        columns = [description[0] for description in cursor.description or ()]
        # One extra row is fetched purely to detect the cap honestly.
        fetched = cursor.fetchmany(_MAX_ROWS + 1)
    except sqlite3.OperationalError as exc:
        message = str(exc)
        if "interrupted" in message:
            _log.info("stats query exceeded %.0fs and was cancelled", _TIMEOUT_SEC)
            return {
                "error": (
                    f"query exceeded {_TIMEOUT_SEC:.0f}s and was cancelled — "
                    "narrow it with a WHERE filter or fewer joins"
                ),
                "layer": "timeout",
            }
        return {"error": f"SQL error: {message}", "layer": "sqlite"}
    except sqlite3.DatabaseError as exc:
        # `not authorized` arrives as this. SQLite's own text often omits *what*
        # it refused, so the recorded denial is what makes the result actionable.
        _log.debug("stats query refused: %s (denials=%s)", exc, denials)
        refused = f" ({denials[0]} is not permitted)" if denials else ""
        return {
            "error": (
                f"{exc}{refused} — this tool may read only "
                f"{', '.join(sorted(_ALLOWED_TABLES))}, and only for reading"
            ),
            "layer": "authorizer",
        }
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()

    truncated = len(fetched) > _MAX_ROWS
    rows = [list(row) for row in fetched[:_MAX_ROWS]]
    return {
        "sql": trimmed,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


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
  author_name, author_email TEXT   -- the reliable contributor identity
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
