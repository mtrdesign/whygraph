"""The shared SQL guard, tested for **surface isolation**.

``tests/test_chat_stats_sql.py`` already proves each of the four layers works;
those tests pass unchanged across the extraction, which is what makes it
behaviour-preserving. What they *cannot* prove is the property the extraction
newly creates: that two surfaces do not share one allowlist.

A cross-wired allowlist is the refactor's worst plausible outcome, and it is
invisible — every existing test still passes, because both surfaces would allow a
superset of what each needs. So the load-bearing test here is the negative one: an
authorizer built for surface A **refuses** a table only surface B allows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from whygraph.chat import graph_stats_sql, sql_guard, stats_sql
from whygraph.chat.sql_guard import SqlNotAllowed, SqlSurface

# ---------------------------------------------------------------------------
# Two toy surfaces over one file, differing only in their allowlist
# ---------------------------------------------------------------------------


@pytest.fixture
def two_table_db(tmp_path: Path) -> Path:
    """A database holding `alpha` and `beta`, so either can be the denied one."""
    path = tmp_path / "two.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE alpha (n INTEGER);
        CREATE TABLE beta (n INTEGER);
        INSERT INTO alpha (n) VALUES (1), (2);
        INSERT INTO beta (n) VALUES (3);
        """
    )
    conn.commit()
    conn.close()
    return path


def _surface(label: str, tables: set[str], path: Path) -> SqlSurface:
    return SqlSurface(
        label=label,
        allowed_tables=frozenset(tables),
        db_path=lambda: path,
        missing_db_message="{db_path} is missing",
    )


def test_a_surface_refuses_a_table_only_the_other_surface_allows(
    two_table_db: Path,
) -> None:
    """The cross-wiring guard, in both directions.

    Asserting only one direction would pass against an authorizer that had
    accidentally been given the *union* of both allowlists.
    """
    only_alpha = _surface("Alpha", {"alpha"}, two_table_db)
    only_beta = _surface("Beta", {"beta"}, two_table_db)

    ok = sql_guard.run_aggregate_query("SELECT count(*) AS n FROM alpha", only_alpha)
    assert ok["rows"] == [[2]]
    refused = sql_guard.run_aggregate_query(
        "SELECT count(*) AS n FROM beta", only_alpha
    )
    assert refused["layer"] == "authorizer"
    assert "table beta" in refused["error"]

    ok = sql_guard.run_aggregate_query("SELECT count(*) AS n FROM beta", only_beta)
    assert ok["rows"] == [[1]]
    refused = sql_guard.run_aggregate_query(
        "SELECT count(*) AS n FROM alpha", only_beta
    )
    assert refused["layer"] == "authorizer"
    assert "table alpha" in refused["error"]


def test_the_authorizer_closes_over_its_own_allowlist_not_a_global(
    two_table_db: Path,
) -> None:
    """Two live connections, two allowlists, at the same time.

    If the allowlist were still a module global the second ``connect`` would
    silently retune the first — the failure mode a sequential test cannot see.
    """
    conn_a = _surface("Alpha", {"alpha"}, two_table_db).connect()
    conn_b = _surface("Beta", {"beta"}, two_table_db).connect()
    try:
        assert conn_a.execute("SELECT count(*) FROM alpha").fetchone() == (2,)
        assert conn_b.execute("SELECT count(*) FROM beta").fetchone() == (1,)
        with pytest.raises(sqlite3.DatabaseError):
            conn_a.execute("SELECT count(*) FROM beta").fetchone()
        with pytest.raises(sqlite3.DatabaseError):
            conn_b.execute("SELECT count(*) FROM alpha").fetchone()
    finally:
        conn_a.close()
        conn_b.close()


def test_a_refusal_names_the_database_it_reached(two_table_db: Path) -> None:
    """`label` exists so a wrong-tool query is diagnosable.

    With two stats tools, "may read only nodes, edges, files" is ambiguous about
    *which* database refused; the label removes the guess.
    """
    result = sql_guard.run_aggregate_query(
        "SELECT count(*) AS n FROM beta", _surface("Alpha", {"alpha"}, two_table_db)
    )
    assert "Alpha database" in result["error"]


def test_the_missing_db_message_is_per_surface(tmp_path: Path) -> None:
    """The remedy differs per surface: run a scan, versus build an index."""
    surface = _surface("Alpha", {"alpha"}, tmp_path / "nope.db")
    result = sql_guard.run_aggregate_query("SELECT count(*) FROM alpha", surface)
    assert result["layer"] == "connection"
    assert result["error"].endswith("nope.db is missing")


# ---------------------------------------------------------------------------
# The layers are shared, so each is exercised once here too
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE alpha",
        "DELETE FROM alpha",
        "PRAGMA journal_mode = delete",
        "ATTACH DATABASE '/tmp/whygraph-guard-evil.db' AS evil",
    ],
)
def test_non_read_actions_are_refused_on_any_surface(
    two_table_db: Path, sql: str
) -> None:
    conn = _surface("Alpha", {"alpha"}, two_table_db).connect()
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute(sql)
    finally:
        conn.close()


def test_recursive_is_not_in_the_action_allowlist() -> None:
    """`WITH RECURSIVE` stays unavailable — three action codes, no more.

    Preserved verbatim across the move: the exclusion is easy to lose in a
    refactor and impossible to notice afterwards.
    """
    assert sqlite3.SQLITE_RECURSIVE not in sql_guard._ALLOWED_ACTIONS
    assert sql_guard._ALLOWED_ACTIONS == {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
    }


def test_the_shape_check_is_shared_and_still_demands_an_aggregate() -> None:
    with pytest.raises(SqlNotAllowed) as excinfo:
        sql_guard._check_shape("SELECT * FROM alpha")
    assert excinfo.value.layer == "shape"

    with pytest.raises(SqlNotAllowed) as excinfo:
        sql_guard._check_shape("SELECT count(*) FROM alpha; DROP TABLE alpha")
    assert excinfo.value.layer == "shape"

    assert sql_guard._check_shape("  SELECT count(*) FROM alpha ; ") == (
        "SELECT count(*) FROM alpha"
    )


# ---------------------------------------------------------------------------
# Both real surfaces, in one place
# ---------------------------------------------------------------------------


def test_both_shipped_allowlists_are_frozenset_literals() -> None:
    """Risk 16: a schema-derived allowlist widens itself on an upstream bump."""
    assert isinstance(stats_sql._ALLOWED_TABLES, frozenset)
    assert isinstance(graph_stats_sql._ALLOWED_TABLES, frozenset)
    # Disjoint, so a cross-wiring bug cannot hide behind an overlap.
    assert not (stats_sql._ALLOWED_TABLES & graph_stats_sql._ALLOWED_TABLES)
    # The names each surface must never reach.
    for denied in ("chat_message", "chat_session", "rationale_cache"):
        assert denied not in stats_sql._ALLOWED_TABLES
        assert denied not in graph_stats_sql._ALLOWED_TABLES


def test_the_shipped_surfaces_carry_their_own_labels_and_paths() -> None:
    assert stats_sql._SURFACE.label == "WhyGraph"
    assert graph_stats_sql._SURFACE.label == "CodeGraph"
    # `db_path` is deferred — a callable, not a resolved Path bound at import.
    assert callable(stats_sql._SURFACE.db_path)
    assert callable(graph_stats_sql._SURFACE.db_path)
