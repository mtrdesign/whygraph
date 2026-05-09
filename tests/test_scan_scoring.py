from pathlib import Path

from rich.console import Console
from rich.progress import Progress

from whygraph.scan import db as db_module
from whygraph.scan.git import Commit
from whygraph.scan.github import Issue, PullRequest
from whygraph.scan.scoring import (
    FieldRef,
    ValueGate,
    collect_corpus,
    percentile_threshold,
    run_scoring_phase,
    score_documents,
    write_scores,
)


def _silent_progress() -> Progress:
    return Progress(console=Console(quiet=True))


def _make_commit(sha: str, subject: str, body: str = "") -> Commit:
    return Commit(
        sha=sha,
        parent_shas=[],
        author_name="A",
        author_email="a@x.com",
        authored_at="2026-01-01T00:00:00+00:00",
        committed_at="2026-01-01T00:00:00+00:00",
        subject=subject,
        body=body,
        files_changed=1,
        insertions=1,
        deletions=0,
    )


def _make_pr(number: int, title: str, body: str | None) -> PullRequest:
    return PullRequest(
        number=number,
        title=title,
        body=body,
        state="open",
        draft=False,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        closed_at=None,
        merged_at=None,
        merge_commit_sha=None,
        head_sha="a" * 40,
        head_ref="branch",
        base_ref="main",
        author="alice",
        html_url=f"https://github.com/o/r/pull/{number}",
        labels=[],
    )


def _make_issue(number: int, title: str, body: str | None) -> Issue:
    return Issue(
        number=number,
        title=title,
        body=body,
        state="open",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        closed_at=None,
        author="alice",
        html_url=f"https://github.com/o/r/issues/{number}",
        labels=[],
    )


def test_collect_corpus_skips_empty_fields(tmp_path: Path) -> None:
    with db_module.Database(tmp_path / "whygraph.db") as db:
        db.upsert_commit(_make_commit("a" * 40, "subject text", body=""))
        db.upsert_pull_request(_make_pr(1, "PR title", body=None))
        db.upsert_issue(_make_issue(7, "issue title", body="   "))
        refs = collect_corpus(db)
    keys = {(r.table, r.pk, r.field) for r in refs}
    assert keys == {
        ("commits", "a" * 40, "subject"),
        ("pull_requests", 1, "title"),
        ("issues", 7, "title"),
    }


def test_score_documents_higher_for_distinctive() -> None:
    refs = [
        FieldRef("commits", "a" * 40, "subject", "wip"),
        FieldRef(
            "commits",
            "b" * 40,
            "subject",
            "implement Levenshtein distance with memoization fallback",
        ),
        FieldRef("commits", "c" * 40, "subject", "wip"),
        FieldRef("commits", "d" * 40, "subject", "wip"),
    ]
    scores = score_documents(refs)
    distinctive = scores[("commits", "b" * 40, "subject")]
    common = scores[("commits", "a" * 40, "subject")]
    assert distinctive > common
    assert common > 0  # 'wip' is not a stopword, gets a positive score


def test_score_documents_zero_for_stopwords_only() -> None:
    refs = [
        FieldRef("commits", "a" * 40, "subject", "the and a of with"),
        FieldRef("commits", "b" * 40, "subject", "real content here"),
    ]
    scores = score_documents(refs)
    assert scores[("commits", "a" * 40, "subject")] == 0.0
    assert scores[("commits", "b" * 40, "subject")] > 0


def test_score_documents_empty_input_returns_empty() -> None:
    assert score_documents([]) == {}


def test_write_scores_resets_then_updates(tmp_path: Path) -> None:
    with db_module.Database(tmp_path / "whygraph.db") as db:
        db.upsert_commit(_make_commit("a" * 40, "first", body="alpha beta"))
        db.upsert_commit(_make_commit("b" * 40, "second", body="gamma"))
        # Pre-populate scores to verify the reset.
        cur = db._conn.cursor()
        cur.execute("UPDATE commits SET subject_tfidf_score = 9.9, body_tfidf_score = 9.9")
        db._conn.commit()

        scores = {
            ("commits", "a" * 40, "subject"): 1.5,
            ("commits", "a" * 40, "body"): 2.5,
            # 'b' rows intentionally absent — they should reset to 0.
        }
        write_scores(db, scores)

        cur.execute(
            "SELECT sha, subject_tfidf_score, body_tfidf_score FROM commits ORDER BY sha"
        )
        rows = dict((sha, (s, b)) for sha, s, b in cur.fetchall())
    assert rows["a" * 40] == (1.5, 2.5)
    assert rows["b" * 40] == (0, 0)


def test_run_scoring_phase_writes_back_to_all_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "whygraph.db"
    with db_module.Database(db_path) as db:
        db.upsert_commit(
            _make_commit("a" * 40, "implement memoization", body="caches results")
        )
        db.upsert_pull_request(
            _make_pr(1, "rationale layer", body="explains why code exists")
        )
        db.upsert_issue(_make_issue(7, "wip", body="placeholder"))

    with _silent_progress() as progress:
        task_id = progress.add_task("score", total=None, start=False)
        progress.start_task(task_id)
        summary = run_scoring_phase(db_path, progress, task_id)

    assert "commits" in summary
    with db_module.Database(db_path) as db:
        cur = db._conn.cursor()
        cur.execute("SELECT subject_tfidf_score, body_tfidf_score FROM commits")
        s, b = cur.fetchone()
        assert s > 0 and b > 0
        cur.execute("SELECT title_tfidf_score, body_tfidf_score FROM pull_requests")
        t, b2 = cur.fetchone()
        assert t > 0 and b2 > 0
        cur.execute("SELECT title_tfidf_score, body_tfidf_score FROM issues")
        t3, b3 = cur.fetchone()
        assert t3 > 0 and b3 > 0


def test_run_scoring_phase_resets_empty_to_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "whygraph.db"
    with db_module.Database(db_path) as db:
        db.upsert_commit(_make_commit("a" * 40, "real content here", body=""))

    with _silent_progress() as progress:
        task_id = progress.add_task("score", total=None, start=False)
        progress.start_task(task_id)
        run_scoring_phase(db_path, progress, task_id)

    with db_module.Database(db_path) as db:
        cur = db._conn.cursor()
        cur.execute(
            "SELECT subject_tfidf_score, body_tfidf_score FROM commits WHERE sha = ?",
            ("a" * 40,),
        )
        subject_score, body_score = cur.fetchone()
    assert body_score == 0
    # subject_score may be 0 too if the corpus is too small for IDF to differ;
    # the contract is "empty body stays at 0", which is what we're verifying.


def _seed_subject_scores(db: db_module.Database, scores: list[float]) -> None:
    """Insert one commit per score with that score on subject_tfidf_score."""
    for i, score in enumerate(scores):
        sha = f"{i:040x}"
        db.upsert_commit(_make_commit(sha, subject=f"row {i}"))
        cur = db._conn.cursor()
        cur.execute(
            "UPDATE commits SET subject_tfidf_score = ? WHERE sha = ?",
            (score, sha),
        )
    db._conn.commit()


def test_percentile_threshold_returns_score_at_fractional_rank(tmp_path: Path) -> None:
    with db_module.Database(tmp_path / "whygraph.db") as db:
        _seed_subject_scores(db, [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0])
        # fraction=0.2 over 10 rows → offset = int(0.2 * 9) = 1 → score 1.0
        assert percentile_threshold(
            db, "commits", "subject", fraction=0.2
        ) == 1.0
        # rows above 1.0 are 8 of 10 → matches "drop bottom 20%"
        cur = db._conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM commits WHERE subject_tfidf_score > 1.0"
        )
        assert cur.fetchone()[0] == 8


def test_percentile_threshold_empty_table_returns_zero(tmp_path: Path) -> None:
    with db_module.Database(tmp_path / "whygraph.db") as db:
        assert percentile_threshold(db, "commits", "subject") == 0.0


def test_percentile_threshold_rejects_unknown_table_or_field(tmp_path: Path) -> None:
    import pytest

    with db_module.Database(tmp_path / "whygraph.db") as db:
        with pytest.raises(ValueError):
            percentile_threshold(db, "bogus", "subject")
        with pytest.raises(ValueError):
            percentile_threshold(db, "commits", "bogus")
        with pytest.raises(ValueError):
            percentile_threshold(db, "commits", "subject", fraction=2.0)


def test_value_gate_is_above(tmp_path: Path) -> None:
    with db_module.Database(tmp_path / "whygraph.db") as db:
        _seed_subject_scores(db, [0.0, 1.0, 2.0, 3.0, 4.0])
        gate = ValueGate.percentile(db, fraction=0.2)
        # threshold for 5 rows at fraction=0.2 → offset 0 → score 0.0
        assert gate.threshold_for("commits", "subject") == 0.0
        assert gate.is_above("commits", "subject", 0.5) is True
        assert gate.is_above("commits", "subject", 0.0) is False


def test_value_gate_caches_thresholds_for_all_scored_fields(tmp_path: Path) -> None:
    with db_module.Database(tmp_path / "whygraph.db") as db:
        gate = ValueGate.percentile(db, fraction=0.2)
    expected = {
        ("commits", "subject"),
        ("commits", "body"),
        ("pull_requests", "title"),
        ("pull_requests", "body"),
        ("issues", "title"),
        ("issues", "body"),
    }
    assert set(gate.thresholds) == expected


def test_run_scoring_phase_handles_empty_db(tmp_path: Path) -> None:
    db_path = tmp_path / "whygraph.db"
    with db_module.Database(db_path):
        pass  # just create schema
    with _silent_progress() as progress:
        task_id = progress.add_task("score", total=None, start=False)
        progress.start_task(task_id)
        summary = run_scoring_phase(db_path, progress, task_id)
    assert "0 commits" in summary
