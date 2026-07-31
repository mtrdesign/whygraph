"""The CodeGraph statistics surface — a table allowlist and a schema doc.

The second aggregate surface, and the reason :mod:`whygraph.chat.sql_guard` exists.
``run_project_stats`` reads whatever the SQLModel engine is bound to — the
WhyGraph database — so *code structure* was unreachable from it no matter how
clever the SQL: CodeGraph writes a **separate file**, ``.codegraph/codegraph.db``.
Questions like "which modules hold the most functions", "which files are largest",
"what is the distribution of symbol kinds" had no tool at all.

Both surfaces run through the identical fence — same authorizer, same action
allowlist, same aggregate-only shape check, same read-only connection, same row
cap and deadline. Sharing the implementation *is* the security argument: there is
one authorizer to audit, not two that drift.

What is genuinely different is this module's two constants. The database belongs
to `CodeGraph <https://github.com/colbymchenry/codegraph>`_ upstream and can gain
tables on a version bump, so :data:`_ALLOWED_TABLES` is a frozenset **literal**
and the authorizer denies every table it does not recognize — a new upstream table
is inert until someone edits that literal.

Chat-only — never registered with MCP.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from whygraph.core import get_config
from whygraph.mcp.targets import repo_root
from whygraph.services.codegraph import CODEGRAPH_DB_RELPATH

from . import sql_guard
from .sql_guard import SqlNotAllowed, SqlSurface, run_aggregate_query

_log = logging.getLogger(__name__)

_MAX_ROWS = sql_guard._MAX_ROWS
"""Re-export of the shared row cap — see :mod:`whygraph.chat.stats_sql`."""

_ALLOWED_TABLES = frozenset({"nodes", "edges", "files"})
"""The read allowlist — **the** security boundary of this module.

A literal, **never** derived from ``sqlite_master``. CodeGraph's schema is
upstream (``colbymchenry/codegraph``) and can gain tables on a version bump; a
derived allowlist would widen silently on the next `codegraph` release, which is
the whole failure mode this constant exists to prevent.

Denied by default, because the authorizer refuses what it does not recognize:
``nodes_fts`` and its four shadow tables, ``name_segment_vocab``,
``project_metadata``, ``schema_versions``, ``unresolved_refs``.
"""

_NO_CODEGRAPH = "CodeGraph index unavailable — run `whygraph scan`"
"""Refusal text when the index is absent.

Deliberately the same sentence the other CodeGraph-backed tools use
(``tools.py``): the WhyGraph and file tools still work without an index, so a
missing index degrades the conversation rather than ending it.
"""


def _codegraph_db_path() -> Path:
    """Resolve ``<project_root>/.codegraph/codegraph.db``.

    Mirrors ``tools.py``'s ``_open_graph`` so the two cannot disagree about which
    index the assistant is reading: a ``codegraph_db`` entry in ``whygraph.toml``
    wins, otherwise the project-relative default. Resolved **per call** — config
    is memoized per process and root discovery walks up from ``cwd``.
    """
    configured = get_config().codegraph_db
    return configured if configured is not None else repo_root() / CODEGRAPH_DB_RELPATH


_SURFACE = SqlSurface(
    label="CodeGraph",
    allowed_tables=_ALLOWED_TABLES,
    db_path=_codegraph_db_path,
    # No `{db_path}` here: a missing index is a "run the scan" problem, not a
    # path problem, and the path is an implementation detail of the container
    # layout the shim mounts.
    missing_db_message=_NO_CODEGRAPH,
)
"""This module's binding of the shared guard."""


def _connect(db_path: Path, denials: list[str] | None = None) -> sqlite3.Connection:
    """Open the CodeGraph DB read-only with the authorizer already installed.

    A thin binding of :meth:`sql_guard.SqlSurface.connect` to this surface, kept
    as a module-level name so the layer-2 tests can drive the authorizer with the
    shape check out of the way — mirroring ``stats_sql._connect``.
    """
    return _SURFACE.connect(db_path, denials)


def run_graph_query(sql: str, *, db_path: Path | None = None) -> dict:
    """Execute one aggregate query against CodeGraph's index and return its rows.

    Parameters
    ----------
    sql : str
        A single ``SELECT`` / ``WITH`` statement that aggregates.
    db_path : Path, optional
        The database to read. Defaults to :func:`_codegraph_db_path`.

    Returns
    -------
    dict
        See :func:`sql_guard.run_aggregate_query` — ``{"sql", "columns", "rows",
        "row_count", "truncated"}`` on success, ``{"error", "layer"}`` when a
        layer refused. A missing index is ``{"error": _NO_CODEGRAPH, "layer":
        "connection"}``: it degrades, it never raises.
    """
    return run_aggregate_query(sql, _SURFACE, db_path=db_path)


_GRAPH_SCHEMA_DOC = """\
Run a read-only aggregate SQL query over the CODE GRAPH — the structure of the
codebase as it stands right now. How many functions per module, which files hold
the most symbols, the distribution of symbol kinds, call fan-in and fan-out.
Aggregates only: the query MUST use COUNT/SUM/AVG/MIN/MAX or GROUP BY, and
returns at most 200 rows.

This is a DIFFERENT DATABASE from run_project_stats. It has no history in it —
nothing here changes over time. Anything about WHEN something happened, who
changed it, or how it evolved belongs to run_project_stats.

For one symbol's callers, callees, or source, use search_symbols / get_symbol /
get_area_outline — they return readable, related detail that a bare count cannot.

=== FIVE REQUIRED RULES (each of these silently corrupts results) ===

1. `nodes` is NOT one row per definition. It also holds `import` and `variable`
   rows, and imports OUTNUMBER functions. For "how much code is here", filter to
   real definitions:
     kind IN ('function','method','class','interface','route','constant',
              'type_alias','component')
   Without that filter every count is dominated by import statements.

2. `kind = 'file'` rows are files-as-nodes, not code. Counting them alongside
   functions double-counts the file. Use the `files` table for per-file facts.

3. `file_path` is repo-relative ('src/whygraph/chat/tools.py'). To group by
   directory, take a fixed number of leading segments — e.g.
     substr(file_path, 1, instr(file_path, '/') - 1)
   for the top level — and CHECK THE RESULT: a path with no '/' at the expected
   offset yields a truncated fragment that looks like a real module. Drop or
   floor tiny buckets rather than plotting a fragment. Remember '_' is a LIKE
   wildcard and appears in almost every path here, so escape it or use instr().

4. `edges.source` and `edges.target` are `nodes.id`, not names. Join to `nodes`
   for anything readable. kind='contains' is structural nesting (a class
   containing its methods); kind='calls' is the call graph. Fan-out is counting
   edges by source, fan-in by target.

5. TESTS ARE CODE TOO, and here they are the largest module. Grouping symbols by
   directory puts `tests` on top. That is truthful and usually not what the
   asker meant, so when you answer "how big is the codebase" SAY whether tests
   are in or out, and filter explicitly if they should be out.

=== TABLES ===

nodes — one row per symbol, import, or file-as-node
  id TEXT PK -- opaque; join target for edges, never shown to a user
  kind TEXT -- see the cardinalities below
  name TEXT -- the bare identifier · qualified_name TEXT -- dotted path
  file_path TEXT -- repo-relative, see rule 3 · language TEXT
  start_line, end_line, start_column, end_column INTEGER
  docstring, signature TEXT NULL · visibility TEXT NULL
  is_exported, is_async, is_static, is_abstract INTEGER 0/1
  decorators, type_parameters, return_type TEXT NULL
  updated_at INTEGER -- INDEX timestamp, NOT a commit date. See rule 5 of the
                     -- project-stats tool for anything temporal.
  Measured on this repo: function 1288, import 1111, method 268, variable 238,
  file 212, class 163, interface 38, route 22, constant 15, type_alias 4,
  component 3.

edges — one row per relationship
  id INTEGER PK · source TEXT (nodes.id) · target TEXT (nodes.id)
  kind TEXT · metadata TEXT NULL · line, col INTEGER NULL · provenance TEXT NULL
  Measured: contains 3128, calls 2618, imports 1566, instantiates 972,
  references 470, extends 35.

files — one row per indexed file
  path TEXT PK (repo-relative) · content_hash TEXT · language TEXT
  size INTEGER (bytes) · modified_at, indexed_at INTEGER
  node_count INTEGER -- symbols found in this file; cheaper than counting nodes
  errors TEXT NULL -- set when the parse partially failed
  Measured languages: python 182, tsx 23, typescript 5, yaml 5, javascript 2.

If a table returns nothing the index has not been built — say so rather than
reporting zero as a finding.\
"""
"""The tool description, and the most important asset in this module.

Same reasoning as ``stats_sql._SCHEMA_DOC``: this is the one tool class whose
wrong answers are undetectable by the reader, because a plausible number arrives
with no way to tell it apart from a right one. So every value domain quoted was
**measured on this repository**, and each rule is a trap that was actually hit
while probing the schema — rule 1 (imports outnumber functions, so an unfiltered
count reports 3,362 "symbols"), rule 3 (a `substr` directory grouping produced a
junk bucket from a path with no separator at the expected offset), and rule 5
(`tests` is the largest module by symbol count, which is true and misleading).

Guarded by a test — the rules must not be paraphrased or trimmed to save tokens.
"""


__all__ = ["SqlNotAllowed", "run_graph_query"]
