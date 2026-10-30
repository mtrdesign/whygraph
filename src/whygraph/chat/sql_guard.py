"""Authorizer-locked, aggregate-only SQL over an arbitrary read-only surface.

Raw SQL is a large capability to hand a model, so it is fenced by **four
independent layers**, in order of how much they can be trusted:

1. **The connection** is opened ``mode=ro``. No write can reach the file.
2. **An authorizer** (:func:`_make_authorizer`) runs inside SQLite's own VM. It
   permits ``SELECT``, function calls, and reads of the surface's
   ``allowed_tables`` only, and denies every other action code — so ``DROP``,
   ``ATTACH``, ``PRAGMA``, and reads of the chat transcripts are refused by
   SQLite itself, not by inspecting the query text.
3. **A shape check** (:func:`_check_shape`) requires one statement that starts
   with ``SELECT``/``WITH`` and contains an aggregate. This is what makes
   "statistics only" hold by construction rather than by prompt wording.
4. **Output caps** — :data:`_MAX_ROWS` and a progress-handler deadline, so
   neither a huge result nor a runaway join can hurt the caller.

Only layers 1 and 2 are a security boundary. Layer 3 is a *scope* boundary
enforced on a string, and a determined query could word its way around it; the
consequence of that is a boring record list, because layers 1–2 still hold.

**Why this module exists at all.** These layers were written once, for the
WhyGraph database (:mod:`whygraph.chat.stats_sql`). A second surface — CodeGraph's
own SQLite index (:mod:`whygraph.chat.graph_stats_sql`) — needs the identical
fence over a different file with a different table allowlist, and duplicating an
authorizer is how the two copies drift apart. So the layers live here, and each
surface contributes only a :class:`SqlSurface`: a label, a **frozenset literal**
of readable tables, and a way to find its file. There is one authorizer to
audit, not two.

The allowlist is a per-surface *parameter* but never a per-surface *derivation* —
see :class:`SqlSurface`.

Chat-only — never registered with MCP.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class SqlSurface:
    """One read-only aggregate surface: a database plus what may be read in it.

    Attributes
    ----------
    label : str
        Human name of the database — ``"WhyGraph"`` / ``"CodeGraph"``. Appears in
        refusal messages, so a model that queried the wrong tool is told which
        database it actually reached rather than only which tables it may not.
    allowed_tables : frozenset of str
        The read allowlist — **the** security boundary. Must be a literal at the
        call site. Deriving it from ``sqlite_master`` would silently widen the
        surface the next time a table appears, which for the WhyGraph DB means
        ``chat_message`` (the assistant's own transcripts) or
        ``rationale_cache``, and for CodeGraph means whatever the upstream tool
        adds on a version bump.
    db_path : callable
        Returns the database file. **Deferred on purpose** — resolved per call,
        never at import: config is memoized per process and path discovery walks
        up from ``cwd``, so binding a path at import time would freeze the wrong
        repository.
    missing_db_message : str
        Refusal text when the file is missing or unreadable, formatted with
        ``{db_path}``. Per-surface because the remedy differs: one says the scan
        has not run, the other that the index is absent.
    """

    label: str
    allowed_tables: frozenset[str]
    db_path: Callable[[], Path]
    missing_db_message: str

    def connect(
        self, db_path: Path | None = None, denials: list[str] | None = None
    ) -> sqlite3.Connection:
        """Open this surface's database read-only, authorizer already installed.

        Parameters
        ----------
        db_path : Path, optional
            Override the surface's own path. Used by tests to point at a fixture.
        denials : list of str, optional
            Sink for what the authorizer refused — see :func:`_make_authorizer`.

        Raises
        ------
        SqlNotAllowed
            With ``layer="connection"`` when the file cannot be opened.
        """
        if db_path is None:
            db_path = self.db_path()
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise SqlNotAllowed(
                self.missing_db_message.format(db_path=db_path),
                layer="connection",
            ) from exc
        conn.set_authorizer(
            _make_authorizer(
                denials if denials is not None else [], self.allowed_tables
            )
        )
        return conn


def _make_authorizer(denials: list[str], allowed_tables: frozenset[str]):
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
    allowed_tables : frozenset of str
        The surface's read allowlist. Passed in rather than closed over a module
        global so two surfaces cannot share one allowlist by accident.

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
        if action == sqlite3.SQLITE_READ and (arg1 or "").lower() not in allowed_tables:
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


def run_aggregate_query(
    sql: str, surface: SqlSurface, *, db_path: Path | None = None
) -> dict:
    """Execute one aggregate query against ``surface`` and return its rows.

    Parameters
    ----------
    sql : str
        A single ``SELECT`` / ``WITH`` statement that aggregates.
    surface : SqlSurface
        Which database, and what may be read in it.
    db_path : Path, optional
        Override the surface's own path. Defaults to ``surface.db_path()``, so
        this always follows the same ``whygraph.toml`` / project-root resolution
        as the rest of the package.

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

    denials: list[str] = []
    try:
        conn = surface.connect(db_path, denials)
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
                f"{', '.join(sorted(surface.allowed_tables))} "
                f"in the {surface.label} database, and only for reading"
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


__all__ = ["SqlNotAllowed", "SqlSurface", "run_aggregate_query"]
