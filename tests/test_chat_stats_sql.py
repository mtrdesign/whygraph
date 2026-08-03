"""Tests for ``run_project_stats`` — the authorizer-locked statistics tool.

This is the one chat tool where a wrong answer is undetectable by the reader,
and the one that hands a model raw SQL. So the suite is organised by *layer*,
and deliberately proves each one **on its own**:

1. The connection (``mode=ro``) refuses a write with no authorizer installed.
2. The authorizer refuses writes, DDL, ``ATTACH``, ``PRAGMA``, and reads of
   the chat transcripts — with the shape check bypassed entirely, because a bug
   in that string check must not be the only thing standing between the model
   and a ``DROP TABLE``.
3. The shape check keeps the tool to statistics.
4. The caps bound the output and the runtime.

Refusals must arrive as tool *results* naming the layer, never as exceptions:
a model that can read why it was refused corrects itself inside the same turn.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from whygraph.chat import sql_guard, stats_sql
from whygraph.chat.stats_sql import _ALLOWED_TABLES, _MAX_ROWS, run_stats_query
from whygraph.db import get_session
from whygraph.db.models import Commit, CommitFileChange, PullRequest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _commit(sha: str, day: str, *, on_main: int = 1, insertions: int = 10) -> Commit:
    return Commit(
        sha=sha,
        parent_shas="",
        author_name="dev",
        author_email="dev@example.com",
        # A UTC offset, as git writes it — see rule 2 of the schema doc.
        authored_at=f"2026-{day}T12:00:00+03:00",
        committed_at=f"2026-{day}T12:00:00+03:00",
        subject=f"subject {sha}",
        body="",
        files_changed=1,
        insertions=insertions,
        deletions=1,
        scanned_at="2026-07-30T00:00:00.123456+00:00",
        llm_description="A diff-derived summary.",
        on_default_branch=on_main,
    )


@pytest.fixture
def stats_db(whygraph_db_initialized: Path) -> Path:
    """A migrated DB with commits across two months, a PR, and no issues."""
    with get_session() as session:
        session.add_all(
            [
                _commit("a1", "05-10"),
                _commit("a2", "05-20", insertions=20),
                _commit("b1", "06-01", insertions=5),
                # Off the main walk — rule 1. Would double-count May.
                _commit("dup", "05-10", on_main=0, insertions=999),
            ]
        )
        session.add_all(
            [
                CommitFileChange(
                    commit_sha=sha,
                    path=path,
                    change_type="M",
                    lines_added=1,
                    lines_deleted=0,
                )
                for sha, path in [
                    ("a1", "README.md"),
                    ("a2", "README.md"),
                    ("b1", "src/whygraph/core/config.py"),
                ]
            ]
        )
        session.add(
            PullRequest(
                number=1,
                title="a pull request",
                body="",
                # A merged PR is 'closed' — rule 4.
                state="closed",
                created_at="2026-05-10T00:00:00Z",  # a `Z`, not an offset — rule 3
                updated_at="2026-05-12T00:00:00Z",
                merged_at="2026-05-12T00:00:00Z",
                merge_commit_sha="a1",
                head_sha="a1",
                base_ref="main",
                author="dev",
                html_url="https://example.invalid/pr/1",
                labels="[]",
                commit_titles="[]",
                comments="[]",
                fetched_at="2026-07-30T00:00:00+00:00",
            )
        )
        session.commit()
    return whygraph_db_initialized


# ---------------------------------------------------------------------------
# Layer 1 — the connection is read-only
# ---------------------------------------------------------------------------


def test_mode_ro_alone_refuses_a_write(stats_db: Path) -> None:
    """The file-level guarantee, with no authorizer in play."""
    conn = sqlite3.connect(f"file:{stats_db}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly database"):
            conn.execute('UPDATE "commit" SET subject = "x"')
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Layer 2 — the authorizer, proven with the shape check bypassed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        'UPDATE "commit" SET subject = "x"',
        'DELETE FROM "commit"',
        'INSERT INTO "commit" (sha) VALUES ("z")',
        "DROP TABLE issue",
        "CREATE TABLE t (x)",
        "ALTER TABLE issue RENAME TO gone",
        "ATTACH DATABASE '/tmp/whygraph-test-evil.db' AS evil",
        "PRAGMA journal_mode = delete",
        "CREATE TRIGGER t AFTER INSERT ON issue BEGIN SELECT 1; END",
    ],
)
def test_authorizer_refuses_every_non_read_action(stats_db: Path, sql: str) -> None:
    """Enforced inside SQLite's VM, not by inspecting the query string.

    :func:`stats_sql._check_shape` would reject all of these first, which is
    exactly why they are tested through :func:`stats_sql._connect` instead — a
    bug in that string check must not be load-bearing.
    """
    conn = stats_sql._connect(stats_db)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute(sql)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("chat_message", "content"),
        ("chat_session", "provider"),
        ("rationale_cache", "purpose"),
        ("sqlite_master", "sql"),
    ],
)
def test_authorizer_refuses_reads_outside_the_allowlist(
    stats_db: Path, table: str, column: str
) -> None:
    """The assistant must not be able to read its own transcripts."""
    assert table not in _ALLOWED_TABLES
    conn = stats_sql._connect(stats_db)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="prohibited|not authorized"):
            conn.execute(f"SELECT {column} FROM {table}").fetchone()
    finally:
        conn.close()


def test_transcripts_are_unreachable_through_a_subquery(stats_db: Path) -> None:
    """A well-shaped aggregate must not smuggle a denied table in via a subquery."""
    result = run_stats_query(
        'SELECT count(*) FROM "commit" '
        "WHERE sha IN (SELECT session_id FROM chat_message)",
        db_path=stats_db,
    )
    assert result["layer"] == "authorizer"
    assert "chat_message" in result["error"]


@pytest.mark.parametrize(
    ("sql", "named"),
    [
        ("SELECT count(*) FROM chat_message", "table chat_message"),
        ("SELECT count(*) FROM rationale_cache", "table rationale_cache"),
    ],
)
def test_a_refusal_names_what_it_refused(stats_db: Path, sql: str, named: str) -> None:
    """SQLite says a bare "not authorized" for a table-level read.

    Without the recorded denial the model is told no and not told why, which
    costs it the round it would have spent correcting itself.
    """
    result = run_stats_query(sql, db_path=stats_db)
    assert result["layer"] == "authorizer"
    assert named in result["error"]


def test_the_allowlist_is_a_literal_not_derived() -> None:
    """Deriving it from the schema would widen the surface silently."""
    assert isinstance(_ALLOWED_TABLES, frozenset)
    assert _ALLOWED_TABLES == {
        "commit",
        "commit_file_change",
        "pull_request",
        "issue",
        "pr_issue_link",
        "author",
    }
    # The two that must never be readable, named explicitly.
    assert "chat_message" not in _ALLOWED_TABLES
    assert "rationale_cache" not in _ALLOWED_TABLES


def test_the_database_is_byte_identical_after_every_hostile_query(
    stats_db: Path,
) -> None:
    """AC 12/13: the whole point, asserted on the bytes."""
    before = hashlib.sha256(stats_db.read_bytes()).hexdigest()
    for sql in [
        'UPDATE "commit" SET subject = "x"',
        "DROP TABLE issue",
        'DELETE FROM "commit" WHERE 1=1',
        "PRAGMA writable_schema = 1",
        "ATTACH DATABASE '/tmp/whygraph-test-evil.db' AS evil",
        'SELECT count(*) FROM "commit"; DROP TABLE issue',
    ]:
        assert "error" in run_stats_query(sql, db_path=stats_db)
    assert hashlib.sha256(stats_db.read_bytes()).hexdigest() == before


# ---------------------------------------------------------------------------
# Layer 3 — the shape check keeps the tool to statistics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "layer", "hint"),
    [
        ('SELECT sha FROM "commit" LIMIT 5', "shape", "aggregate"),
        ('SELECT * FROM "commit"', "shape", "aggregate"),
        ('UPDATE "commit" SET subject = "x"', "shape", "only SELECT"),
        ("DROP TABLE issue", "shape", "only SELECT"),
        ("ATTACH DATABASE '/tmp/x.db' AS x", "shape", "only SELECT"),
        ("PRAGMA table_list", "shape", "only SELECT"),
        ('SELECT count(*) FROM "commit"; DROP TABLE issue', "shape", "one statement"),
        ("   ", "shape", "empty"),
        ("SELECT count(*) FROM chat_message", "authorizer", "chat_message"),
    ],
)
def test_rejections_are_results_that_name_their_layer(
    stats_db: Path, sql: str, layer: str, hint: str
) -> None:
    """AC 12: a refusal the model can read and route around, never an exception."""
    result = run_stats_query(sql, db_path=stats_db)
    assert result["layer"] == layer
    assert hint in result["error"]
    assert "rows" not in result


def test_the_aggregate_rejection_points_at_the_right_tools(stats_db: Path) -> None:
    """Risk 1: the fix for misuse is redirection, not a bare refusal."""
    error = run_stats_query('SELECT sha FROM "commit"', db_path=stats_db)["error"]
    for tool in ("find_changes", "get_area_history", "get_commit", "get_pr"):
        assert tool in error


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT count(*) FROM "commit" WHERE on_default_branch = 1',
        'SELECT COUNT (*) FROM "commit"',  # whitespace before the paren
        'select\n  sum(insertions)\nfrom "commit"',
        'SELECT author_name, count(*) FROM "commit" GROUP BY author_name',
        'WITH m AS (SELECT insertions FROM "commit") SELECT avg(insertions) FROM m',
        'SELECT count(*) FROM "commit";',  # a trailing semicolon is fine
    ],
)
def test_well_formed_aggregates_are_accepted(stats_db: Path, sql: str) -> None:
    result = run_stats_query(sql, db_path=stats_db)
    assert "error" not in result, result
    assert result["row_count"] >= 1


# ---------------------------------------------------------------------------
# Layer 4 — output and runtime caps
# ---------------------------------------------------------------------------


def test_row_cap_is_enforced_and_flagged(stats_db: Path) -> None:
    """A capped result must say so — a silent cut reads as a complete answer."""
    # A self-join grouped per row generator, so the row count exceeds the cap.
    sql = (
        'SELECT a.sha, b.sha, count(*) FROM "commit" a, "commit" b, "commit" c, '
        '"commit" d GROUP BY a.sha, b.sha, c.sha, d.sha'
    )
    result = run_stats_query(sql, db_path=stats_db)
    assert result["row_count"] == _MAX_ROWS
    assert result["truncated"] is True


def test_a_runaway_query_is_aborted_and_returns_an_error_result(
    stats_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 13: not a hang, and not a raised exception.

    Returning non-zero from the progress handler is sufficient to interrupt the
    statement — no worker thread, no ``interrupt()`` call, and no signal handler
    (which would be wrong inside FastAPI's threadpool).
    """
    # Layer 4 lives in `sql_guard` (shared with the CodeGraph surface), so that
    # is where the deadline is read from — patching a re-export here would not
    # reach the code under test.
    monkeypatch.setattr(sql_guard, "_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(sql_guard, "_PROGRESS_INTERVAL", 100)
    # Enough rows that a 6-way cartesian product cannot finish in 50ms.
    with get_session() as session:
        for n in range(60):
            session.add(_commit(f"pad{n:03d}", "07-01"))
        session.commit()

    sql = (
        'SELECT count(*) FROM "commit" a, "commit" b, "commit" c, "commit" d, '
        '"commit" e, "commit" f'
    )
    result = run_stats_query(sql, db_path=stats_db)
    assert result["layer"] == "timeout"
    assert "cancelled" in result["error"]


# ---------------------------------------------------------------------------
# The statistics the tool exists to answer (§2.4)
# ---------------------------------------------------------------------------


def test_velocity_by_month(stats_db: Path) -> None:
    """AC 11, and rule 1: the off-main duplicate must not inflate May."""
    result = run_stats_query(
        "SELECT strftime('%Y-%m', authored_at) AS month, count(*) AS commits, "
        'sum(insertions) AS ins FROM "commit" WHERE on_default_branch = 1 '
        "GROUP BY month ORDER BY month",
        db_path=stats_db,
    )
    assert result["columns"] == ["month", "commits", "ins"]
    assert result["rows"] == [["2026-05", 2, 30], ["2026-06", 1, 5]]
    assert result["truncated"] is False


def test_hotspot_files(stats_db: Path) -> None:
    result = run_stats_query(
        "SELECT f.path, count(DISTINCT f.commit_sha) AS n FROM commit_file_change f "
        'JOIN "commit" c ON c.sha = f.commit_sha WHERE c.on_default_branch = 1 '
        "GROUP BY f.path ORDER BY n DESC",
        db_path=stats_db,
    )
    assert result["rows"][0] == ["README.md", 2]


def test_pr_cycle_time_in_days(stats_db: Path) -> None:
    """Rule 3: a duration must come from julianday, not string arithmetic."""
    result = run_stats_query(
        "SELECT avg(julianday(merged_at) - julianday(created_at)) AS days, "
        "count(*) AS merged FROM pull_request WHERE merged_at IS NOT NULL",
        db_path=stats_db,
    )
    assert result["rows"] == [[2.0, 1]]


def test_an_unscanned_source_returns_zero_rather_than_erroring(stats_db: Path) -> None:
    """Risk 7: issues were never fetched here. The tool reports, the model explains."""
    result = run_stats_query("SELECT count(*) AS n FROM issue", db_path=stats_db)
    assert result["rows"] == [[0]]
    assert "error" not in result


def test_a_missing_database_is_an_error_result(tmp_path: Path) -> None:
    result = run_stats_query(
        'SELECT count(*) FROM "commit"', db_path=tmp_path / "nope.db"
    )
    assert result["layer"] == "connection"
    assert "whygraph scan" in result["error"]


# ---------------------------------------------------------------------------
# The schema description (§4.4.1) — risk 2 and risk 8's only automatable guard
# ---------------------------------------------------------------------------


def test_schema_doc_carries_all_five_silent_corruption_rules() -> None:
    """The rules cannot be paraphrased or trimmed to save tokens.

    Each guards a trap that produces a *plausible* wrong number, which no other
    test can catch because the tool did exactly what it was asked.
    """
    doc = stats_sql._SCHEMA_DOC
    # Rule 1 — commits that are not on the default branch.
    assert "on_default_branch = 1" in doc
    # Rule 2 — mixed ISO-8601 forms; substr() does not normalise, strftime does.
    assert "strftime" in doc
    assert "substr" in doc
    assert "NEVER substr" in doc or "never substr" in doc.lower()
    # Rule 3 — cross-format string comparison is lexicographic.
    assert "julianday" in doc
    # Rule 4 — a merged PR is 'closed'.
    assert "merged_at IS NOT NULL" in doc
    assert "NOT 'merged'" in doc
    # Rule 5 — raw git identities split one human across several rows.
    assert "NEVER GROUP DEVELOPERS BY" in doc
    assert "one row per human" in doc
    # The aggregate-only contract and the redirection away from record lookups.
    assert "Aggregates only" in doc
    for tool in ("get_commit", "get_area_history", "find_changes"):
        assert tool in doc
    # Every readable table is documented — an undocumented table is unusable.
    for table in _ALLOWED_TABLES:
        assert table in doc
    # An empty table is a scan gap, not a finding.
    assert "has not been scanned" in doc


def test_schema_doc_documents_the_non_obvious_value_domains() -> None:
    """A field name alone is not enough to write a correct aggregate."""
    doc = stats_sql._SCHEMA_DOC
    # change_type is git's single letter, not a word.
    assert "'M' modified" in doc
    assert "NOT words" in doc
    # refactor_score is a heuristic, not a quality score.
    assert "Not a quality measure" in doc
    # The author table resolves identity, and its list columns are JSON.
    assert "ONE ROW PER HUMAN" in doc
    assert "JSON ARRAYS" in doc
    # llm_description's provenance, which is why it beats subject/body.
    assert "DIFF ALONE" in doc


def test_schema_doc_routes_developer_grouping_through_the_author_table() -> None:
    """Rule 5, and the two join forms it must never teach.

    Both alternatives look right and are not: ``json_each`` is refused by the
    authorizer, and a ``LIKE`` match against ``author.emails`` treats ``_`` as a
    wildcard, so it can attribute one person's commits to another. That is the
    one failure mode invisible in the output, so the wording is a contract — the
    eval greps the emitted SQL for this exact form.
    """
    doc = stats_sql._SCHEMA_DOC
    assert "author" in doc
    # The all-time form must aggregate: reading `commit_count` bare is refused
    # by `_check_shape`, so the rule ships the SUM/GROUP BY wrapper.
    assert "SUM(commit_count) AS commits" in doc
    assert "FROM author GROUP BY id" in doc
    # The time-sliced form, verbatim.
    assert "instr(a.emails, '\"' || c.author_email || '\"') > 0" in doc
    # Neither wrong form may appear as advice.
    assert "lower(author_name)" not in doc
    assert "json_each" in doc and "Do NOT use json_each" in doc
    assert "do NOT use LIKE for this match" in doc
    # `author.commit_count` already excludes on_default_branch = 0.
    assert "do NOT also apply rule 1" in doc
    # Rule 1 is not a caveat on this path, and the count is not a scoreboard.
    assert "Never present a commit count as a measure of productivity" in doc


def test_schema_doc_describes_flag_zero_as_both_populations() -> None:
    """Case 47e (audit A5) — rule 1's *reason* must match reality.

    Flag-0 used to mean only "PR-origin recovery, already on the main walk".
    It now also means unmerged local work, which is emphatically *not* on the
    main walk — a model reasoning from the old premise could decide to union
    flag-0 rows back in when asked for "all work including squashed PRs".

    Asserts on the rule's substance, not its wording, so a future rewording
    does not break the test.
    """
    doc = stats_sql._SCHEMA_DOC
    rule_one = doc.split("2. For dates")[0].split("1. ALWAYS")[1]

    # The instruction is unchanged and must stay.
    assert "on_default_branch = 1" in rule_one
    # Both populations named; the false "already on the main walk" claim gone.
    assert "unmerged" in rule_one.lower()
    assert "squash" in rule_one.lower()
    assert "already on the main walk" not in rule_one.lower()
    # The discriminator is documented in the commit schema block.
    assert "first_seen_ref" in doc
