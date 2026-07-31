"""The CodeGraph statistics surface.

Modelled on ``test_chat_stats_sql.py``'s layer structure, against a temporary
CodeGraph fixture rather than the live index — including its **byte-identical-DB**
assertion after every hostile query, which is the real proof of read-only. The
absence of an exception is not: a query can be refused and still have written.

The fixture carries the shadow tables CodeGraph actually creates (``nodes_fts``
and friends, ``project_metadata``, ``schema_versions``, ``unresolved_refs``) so
the deny-by-default property is tested against real table names rather than
invented ones.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from whygraph.chat import graph_stats_sql
from whygraph.chat.graph_stats_sql import _ALLOWED_TABLES, run_graph_query
from whygraph.chat.sql_guard import _MAX_ROWS

_SCHEMA = """
CREATE TABLE nodes (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
  qualified_name TEXT NOT NULL, file_path TEXT NOT NULL, language TEXT NOT NULL,
  start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
  start_column INTEGER NOT NULL, end_column INTEGER NOT NULL,
  docstring TEXT, signature TEXT, visibility TEXT,
  is_exported INTEGER DEFAULT 0, is_async INTEGER DEFAULT 0,
  is_static INTEGER DEFAULT 0, is_abstract INTEGER DEFAULT 0,
  decorators TEXT, type_parameters TEXT, return_type TEXT,
  updated_at INTEGER NOT NULL);
CREATE TABLE edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, target TEXT NOT NULL,
  kind TEXT NOT NULL, metadata TEXT, line INTEGER, col INTEGER, provenance TEXT);
CREATE TABLE files (
  path TEXT PRIMARY KEY, content_hash TEXT NOT NULL, language TEXT NOT NULL,
  size INTEGER NOT NULL, modified_at INTEGER NOT NULL, indexed_at INTEGER NOT NULL,
  node_count INTEGER DEFAULT 0, errors TEXT);
-- The tables the allowlist must deny by default, under their real names.
CREATE TABLE project_metadata (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE schema_versions (version INTEGER PRIMARY KEY);
CREATE TABLE unresolved_refs (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE name_segment_vocab (segment TEXT PRIMARY KEY);
CREATE VIRTUAL TABLE nodes_fts USING fts5(name, qualified_name, docstring);
"""


def _node(nid: str, kind: str, name: str, path: str, language: str = "python") -> tuple:
    return (
        nid,
        kind,
        name,
        f"pkg.{name}",
        path,
        language,
        1,
        10,
        0,
        0,
        None,
        None,
        None,
        0,
        0,
        0,
        0,
        None,
        None,
        None,
        1_750_000_000,
    )


@pytest.fixture
def graph_db(tmp_path: Path) -> Path:
    """A small but structurally honest CodeGraph index.

    Deliberately includes ``import`` and ``file`` rows: those are the two traps
    ``_GRAPH_SCHEMA_DOC`` rules 1 and 2 exist for, so a fixture without them
    could not show that an unfiltered count is wrong.
    """
    path = tmp_path / "codegraph.db"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            _node("n1", "function", "alpha", "src/pkg/a.py"),
            _node("n2", "function", "beta", "src/pkg/a.py"),
            _node("n3", "method", "gamma", "src/pkg/b.py"),
            _node("n4", "class", "Delta", "src/pkg/b.py"),
            _node("n5", "import", "os", "src/pkg/a.py"),
            _node("n6", "import", "sys", "src/pkg/b.py"),
            _node("n7", "file", "a.py", "src/pkg/a.py"),
            _node("n8", "function", "epsilon", "tests/test_a.py"),
        ],
    )
    conn.executemany(
        "INSERT INTO edges (source, target, kind) VALUES (?,?,?)",
        [("n1", "n2", "calls"), ("n1", "n3", "calls"), ("n4", "n3", "contains")],
    )
    conn.executemany(
        "INSERT INTO files VALUES (?,?,?,?,?,?,?,?)",
        [
            ("src/pkg/a.py", "h1", "python", 400, 1, 1, 4, None),
            ("src/pkg/b.py", "h2", "python", 300, 1, 1, 2, None),
            ("tests/test_a.py", "h3", "python", 100, 1, 1, 1, None),
        ],
    )
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# The statistics the tool exists to answer
# ---------------------------------------------------------------------------


def test_symbols_per_module(graph_db: Path) -> None:
    """The question `run_project_stats` structurally cannot answer."""
    result = run_graph_query(
        "SELECT substr(file_path, 1, instr(file_path, '/') - 1) AS module, "
        "count(*) AS n FROM nodes "
        "WHERE kind IN ('function','method','class') "
        "GROUP BY module ORDER BY n DESC",
        db_path=graph_db,
    )
    assert result["columns"] == ["module", "n"]
    assert result["rows"] == [["src", 4], ["tests", 1]]
    assert result["truncated"] is False


def test_the_kind_filter_is_what_makes_a_count_honest(graph_db: Path) -> None:
    """Rule 1, demonstrated rather than asserted on the doc text.

    Unfiltered, imports and the file-as-node inflate the count by half — the
    trap `_GRAPH_SCHEMA_DOC` rule 1 exists to name.
    """
    unfiltered = run_graph_query("SELECT count(*) AS n FROM nodes", db_path=graph_db)
    definitions = run_graph_query(
        "SELECT count(*) AS n FROM nodes WHERE kind IN "
        "('function','method','class','interface','route','constant',"
        "'type_alias','component')",
        db_path=graph_db,
    )
    assert unfiltered["rows"] == [[8]]
    assert definitions["rows"] == [[5]]


def test_call_fan_out_joins_edges_to_nodes(graph_db: Path) -> None:
    """Rule 4: `edges.source` is an id, so a readable answer needs the join."""
    result = run_graph_query(
        "SELECT n.name AS symbol, count(*) AS calls "
        "FROM edges e JOIN nodes n ON n.id = e.source "
        "WHERE e.kind = 'calls' GROUP BY n.id ORDER BY calls DESC",
        db_path=graph_db,
    )
    assert result["rows"] == [["alpha", 2]]


def test_language_mix_from_the_files_table(graph_db: Path) -> None:
    result = run_graph_query(
        "SELECT language, count(*) AS n FROM files GROUP BY language",
        db_path=graph_db,
    )
    assert result["rows"] == [["python", 3]]


# ---------------------------------------------------------------------------
# Layer 3 — the shape check
# ---------------------------------------------------------------------------


def test_a_non_aggregate_select_is_refused(graph_db: Path) -> None:
    """Aggregate-only is what stops this surface dumping source-derived text.

    `nodes.docstring` and `nodes.signature` hold source, so a record-shaped
    read here would be a second `read_file` without its clamps.
    """
    result = run_graph_query("SELECT * FROM nodes", db_path=graph_db)
    assert result["layer"] == "shape"
    assert "aggregate" in result["error"]


def test_docstrings_cannot_be_dumped_row_by_row(graph_db: Path) -> None:
    result = run_graph_query(
        "SELECT name, docstring FROM nodes LIMIT 50", db_path=graph_db
    )
    assert result["layer"] == "shape"


# ---------------------------------------------------------------------------
# Layer 2 — the authorizer, on this surface's allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table",
    ["nodes_fts", "project_metadata", "schema_versions", "unresolved_refs"],
)
def test_reads_outside_the_allowlist_are_refused_by_name(
    graph_db: Path, table: str
) -> None:
    """Deny-by-default: these are never listed anywhere, only omitted."""
    assert table not in _ALLOWED_TABLES
    result = run_graph_query(f"SELECT count(*) AS n FROM {table}", db_path=graph_db)
    assert result["layer"] == "authorizer"
    assert table in result["error"]
    # The refusal says which database it reached — with two stats tools, that is
    # the difference between "wrong table" and "wrong tool".
    assert "CodeGraph database" in result["error"]


def test_a_denied_table_cannot_be_smuggled_in_through_a_subquery(
    graph_db: Path,
) -> None:
    result = run_graph_query(
        "SELECT count(*) AS n FROM nodes WHERE id IN "
        "(SELECT value FROM project_metadata)",
        db_path=graph_db,
    )
    assert result["layer"] == "authorizer"
    assert "project_metadata" in result["error"]


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE nodes",
        "DELETE FROM edges",
        "UPDATE nodes SET name = 'x'",
        "INSERT INTO nodes (id) VALUES ('x')",
        "ALTER TABLE nodes RENAME TO gone",
        "CREATE TABLE t (x)",
        "ATTACH DATABASE '/tmp/whygraph-graph-evil.db' AS evil",
        "PRAGMA journal_mode = delete",
    ],
)
def test_non_read_actions_are_refused_inside_the_vm(graph_db: Path, sql: str) -> None:
    """Driven through `_connect` so the string shape check is not load-bearing."""
    conn = graph_stats_sql._connect(graph_db)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute(sql)
    finally:
        conn.close()


def test_with_recursive_is_refused(graph_db: Path) -> None:
    """`SQLITE_RECURSIVE` is not in the action allowlist, deliberately."""
    result = run_graph_query(
        "WITH RECURSIVE walk(id) AS ("
        "  SELECT 'n1' UNION SELECT e.target FROM edges e JOIN walk ON walk.id = e.source"
        ") SELECT count(*) AS n FROM walk",
        db_path=graph_db,
    )
    assert result["layer"] == "authorizer"


@pytest.mark.parametrize("fn", ["load_extension", "readfile", "writefile"])
def test_dangerous_functions_are_refused_by_name(graph_db: Path, fn: str) -> None:
    conn = graph_stats_sql._connect(graph_db)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(f"SELECT {fn}('/etc/passwd')").fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Layer 1 — the file is never written
# ---------------------------------------------------------------------------


def test_the_index_is_byte_identical_after_every_hostile_query(
    graph_db: Path,
) -> None:
    """The whole point, asserted on the bytes rather than on an exception."""
    before = hashlib.sha256(graph_db.read_bytes()).hexdigest()
    for sql in [
        "DROP TABLE nodes",
        "DELETE FROM edges",
        "UPDATE nodes SET name = 'x'",
        "INSERT INTO files (path) VALUES ('x')",
        "ATTACH DATABASE '/tmp/whygraph-graph-evil.db' AS evil",
        "PRAGMA journal_mode = delete",
        "SELECT count(*) FROM project_metadata",
        "SELECT * FROM nodes",
    ]:
        run_graph_query(sql, db_path=graph_db)
    assert hashlib.sha256(graph_db.read_bytes()).hexdigest() == before


# ---------------------------------------------------------------------------
# Layer 4 — output caps
# ---------------------------------------------------------------------------


def test_the_row_cap_is_enforced_and_flagged(graph_db: Path) -> None:
    conn = sqlite3.connect(graph_db)
    conn.executemany(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [_node(f"p{n}", "function", f"f{n}", f"src/pkg/p{n}.py") for n in range(250)],
    )
    conn.commit()
    conn.close()

    result = run_graph_query(
        "SELECT file_path, count(*) AS n FROM nodes GROUP BY file_path",
        db_path=graph_db,
    )
    assert result["truncated"] is True
    assert result["row_count"] == _MAX_ROWS


# ---------------------------------------------------------------------------
# Degradation, not failure
# ---------------------------------------------------------------------------


def test_a_missing_index_degrades_instead_of_raising(tmp_path: Path) -> None:
    """The WhyGraph and file tools still work without an index."""
    result = run_graph_query(
        "SELECT count(*) AS n FROM nodes", db_path=tmp_path / "nope.db"
    )
    assert result["layer"] == "connection"
    assert result["error"] == graph_stats_sql._NO_CODEGRAPH
    assert "whygraph scan" in result["error"]


# ---------------------------------------------------------------------------
# The allowlist and the schema doc
# ---------------------------------------------------------------------------


def test_the_allowlist_is_a_literal_not_derived_from_the_schema() -> None:
    """Risk 16: CodeGraph's schema is upstream and can gain tables.

    A derived allowlist would admit a new upstream table on a version bump,
    silently. This one is inert until someone edits the literal.
    """
    assert isinstance(_ALLOWED_TABLES, frozenset)
    assert _ALLOWED_TABLES == {"nodes", "edges", "files"}
    source = Path(graph_stats_sql.__file__).read_text()
    # The literal, verbatim — and no query that could reach the schema table.
    # ("sqlite_master" appears in the module docstring, saying not to use it.)
    assert '_ALLOWED_TABLES = frozenset({"nodes", "edges", "files"})' in source
    assert "FROM sqlite_master" not in source
    assert "sqlite_master" not in graph_stats_sql._ALLOWED_TABLES


def test_the_graph_schema_doc_carries_its_five_rules() -> None:
    """Each rule is a trap that produces a *plausible* wrong number."""
    doc = graph_stats_sql._GRAPH_SCHEMA_DOC
    # Rule 1 — imports outnumber functions, so an unfiltered count is inflated.
    assert "kind IN ('function','method','class','interface','route','constant'," in doc
    assert "imports OUTNUMBER functions" in doc
    # Rule 2 — files-as-nodes double-count.
    assert "kind = 'file'" in doc
    # Rule 3 — directory grouping produces junk buckets; '_' is a LIKE wildcard.
    assert "truncated fragment" in doc
    assert "'_' is a LIKE" in doc
    # Rule 4 — edges hold ids, not names.
    assert "are `nodes.id`, not names" in doc
    # Rule 5 — tests are the largest module, which is true and misleading.
    assert "TESTS ARE CODE TOO" in doc
    # The two-database boundary, which is the new failure mode.
    assert "DIFFERENT DATABASE from run_project_stats" in doc
    assert "no history" in doc
    # Every readable table is documented.
    for table in _ALLOWED_TABLES:
        assert table in doc
    # An empty table is an index gap, not a finding.
    assert "the index has not been built" in doc


def test_the_graph_schema_doc_ships_the_measured_cardinalities() -> None:
    """Measured, not assumed — so the model need not discover them by query."""
    doc = graph_stats_sql._GRAPH_SCHEMA_DOC
    assert "function 1288, import 1111" in doc
    assert "contains 3128, calls 2618" in doc
    assert "python 182, tsx 23" in doc
    # `updated_at` is an index timestamp, and reading it as a commit date is the
    # single most likely two-database confusion.
    assert "INDEX timestamp, NOT a commit date" in doc
