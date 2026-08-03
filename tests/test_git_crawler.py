"""Tests for :class:`whygraph.scan.git_crawler.GitCrawler`."""

from __future__ import annotations

import logging
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from rich.progress import Progress
from sqlmodel import func, select

from whygraph import core
from whygraph.core.config import Config
from whygraph.db import engine as db_engine
from whygraph.db import get_session
from whygraph.db.bootstrap import ensure_initialized
from whygraph.db.models.commit import Commit as CommitRow
from whygraph.db.models.commit_file_change import CommitFileChange
from whygraph.scan.git_crawler import GitCrawler
from whygraph.services.git import Repository


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _git(tmp_path, "config", "tag.gpgsign", "false")

    (tmp_path / "a.txt").write_text("hello\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "first")

    (tmp_path / "b.txt").write_text("world\n")
    _git(tmp_path, "add", "b.txt")
    _git(tmp_path, "commit", "-q", "-m", "second")

    (tmp_path / "a.txt").write_text("hello updated\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "third")

    return tmp_path


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "repo")


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point WhyGraph at a per-test SQLite file and pre-create the schema."""
    db_path = tmp_path / "whygraph.db"
    monkeypatch.setattr(core, "_config", Config(whygraph_db=db_path))
    db_engine._reset_engine()
    ensure_initialized()
    try:
        yield db_path
    finally:
        db_engine._reset_engine()
        core._reset_config()


def _count_commits() -> int:
    with get_session() as session:
        return session.exec(select(func.count(CommitRow.sha))).one()


def test_summary_reports_commit_counts(repo_root: Path) -> None:
    repo = Repository(repo_root)

    crawler = GitCrawler(Progress(), repository=repo)
    crawler.run()
    assert crawler.summary == "3 commits (3 new)"

    # A rescan sees the same commits, none new.
    rescan = GitCrawler(Progress(), repository=repo)
    rescan.run()
    assert rescan.summary == "3 commits (0 new)"


def test_first_scan_persists_all_commits(repo_root: Path) -> None:
    repo = Repository(repo_root)
    expected_shas = {c.sha for c in repo.commits}

    crawler = GitCrawler(Progress(), repository=repo)
    crawler.run()

    assert crawler.error is None
    assert _count_commits() == 3

    with get_session() as session:
        subjects = set(session.exec(select(CommitRow.subject)).all())
        shas = set(session.exec(select(CommitRow.sha)).all())

    assert subjects == {"first", "second", "third"}
    assert shas == expected_shas


def test_rescan_is_idempotent(repo_root: Path) -> None:
    repo = Repository(repo_root)

    GitCrawler(Progress(), repository=repo).run()
    with get_session() as session:
        first_rows = {
            row.sha: row.scanned_at for row in session.exec(select(CommitRow)).all()
        }

    # Ensure any newly-generated scanned_at would differ if rows were
    # rewritten — guards against silent upsert.
    time.sleep(0.01)

    GitCrawler(Progress(), repository=repo).run()
    with get_session() as session:
        second_rows = {
            row.sha: row.scanned_at for row in session.exec(select(CommitRow)).all()
        }

    assert first_rows == second_rows
    assert _count_commits() == 3


def test_progress_total_matches_commit_count(repo_root: Path) -> None:
    repo = Repository(repo_root)
    progress = Progress()
    crawler = GitCrawler(progress, repository=repo)
    crawler.run()

    assert crawler.error is None
    assert len(progress.tasks) == 1
    task = progress.tasks[0]
    assert task.total == 3
    assert task.completed == 3


def test_first_scan_persists_per_file_changes(repo_root: Path) -> None:
    """The crawler records one ``commit_file_change`` row per touched file."""
    repo = Repository(repo_root)
    GitCrawler(Progress(), repository=repo).run()

    with get_session() as session:
        materialized = [
            {
                "path": r.path,
                "change_type": r.change_type,
                "renamed_from": r.renamed_from,
            }
            for r in session.exec(select(CommitFileChange)).all()
        ]

    # 3 commits, each touches exactly one file → 3 file-change rows.
    assert len(materialized) == 3
    assert {r["path"] for r in materialized} == {"a.txt", "b.txt"}
    # a.txt is added in the first commit and modified in the third.
    a_rows = [r for r in materialized if r["path"] == "a.txt"]
    assert {r["change_type"] for r in a_rows} == {"A", "M"}
    # No renames in this fixture.
    assert all(r["renamed_from"] is None for r in materialized)


def test_rescan_does_not_duplicate_file_changes(repo_root: Path) -> None:
    """File-change rows are keyed by commit_sha; re-running scan is a no-op."""
    repo = Repository(repo_root)
    GitCrawler(Progress(), repository=repo).run()
    GitCrawler(Progress(), repository=repo).run()

    with get_session() as session:
        count = session.exec(select(func.count(CommitFileChange.id))).one()
    assert count == 3


def test_scan_backfills_file_changes_for_pre_existing_commit_rows(
    repo_root: Path,
) -> None:
    """A repo where commits were scanned before Phase 2 (commit rows exist,
    but commit_file_change rows don't) gets backfilled on the next scan."""
    repo = Repository(repo_root)
    GitCrawler(Progress(), repository=repo).run()

    # Simulate an upgrade by deleting only the file-change rows.
    with get_session() as session:
        for row in session.exec(select(CommitFileChange)).all():
            session.delete(row)

    GitCrawler(Progress(), repository=repo).run()

    with get_session() as session:
        count = session.exec(select(func.count(CommitFileChange.id))).one()
    assert count == 3


def test_scan_computes_refactor_score_for_boring_commit(tmp_path: Path) -> None:
    """A ``refactor:``/``chore:``-prefixed mass-touch commit is flagged
    above :data:`BORING_THRESHOLD` at scan time."""
    from whygraph.scan.refactor_score import BORING_THRESHOLD

    repo = tmp_path / "boring_repo"
    repo.mkdir()

    def _g(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    _g("init", "-q", "-b", "main")
    _g("config", "user.email", "test@example.com")
    _g("config", "user.name", "Test User")
    _g("config", "commit.gpgsign", "false")
    # Twenty-plus files touched in one commit, ``refactor:`` subject ⇒
    # combined score crosses the threshold.
    for i in range(25):
        (repo / f"file{i}.txt").write_text(f"line {i}\n")
    _g("add", ".")
    _g("commit", "-q", "-m", "refactor: scaffolding sweep")
    boring_sha = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()

    GitCrawler(Progress(), repository=Repository(repo)).run()

    with get_session() as session:
        row = session.get(CommitRow, boring_sha)
        score = row.refactor_score if row else None
    assert score is not None
    assert score >= BORING_THRESHOLD


def test_scan_backfills_refactor_score_when_file_changes_arrive_late(
    tmp_path: Path,
) -> None:
    """A pre-Phase-3 ``commit`` row with default score 0 picks up its real
    score on the next scan once file-change rows are populated."""
    from whygraph.scan.refactor_score import BORING_THRESHOLD

    repo = tmp_path / "late_repo"
    repo.mkdir()

    def _g(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    _g("init", "-q", "-b", "main")
    _g("config", "user.email", "test@example.com")
    _g("config", "user.name", "Test User")
    _g("config", "commit.gpgsign", "false")
    for i in range(25):
        (repo / f"f{i}.txt").write_text("x\n")
    _g("add", ".")
    _g("commit", "-q", "-m", "chore: scaffolding")
    sha = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()

    repository = Repository(repo)
    GitCrawler(Progress(), repository=repository).run()

    # Simulate a pre-Phase-3 DB: file-change rows were generated, but
    # the score was never populated. Reset the score and the file-change
    # rows so the next scan reaches the backfill branch.
    with get_session() as session:
        for row in session.exec(select(CommitFileChange)).all():
            session.delete(row)
        existing = session.get(CommitRow, sha)
        existing.refactor_score = 0
        session.add(existing)

    GitCrawler(Progress(), repository=repository).run()

    with get_session() as session:
        score = session.get(CommitRow, sha).refactor_score
    assert score >= BORING_THRESHOLD


def test_persisted_fields_match_in_memory_commit(repo_root: Path) -> None:
    repo = Repository(repo_root)
    expected = {c.sha: c for c in repo.commits}

    GitCrawler(Progress(), repository=repo).run()

    with get_session() as session:
        materialized = [
            {
                "sha": r.sha,
                "author_name": r.author_name,
                "author_email": r.author_email,
                "subject": r.subject,
                "body": r.body,
                "authored_at": r.authored_at,
                "committed_at": r.committed_at,
                "parent_shas": r.parent_shas,
                "files_changed": r.files_changed,
                "insertions": r.insertions,
                "deletions": r.deletions,
                "scanned_at": r.scanned_at,
            }
            for r in session.exec(select(CommitRow)).all()
        ]

    assert {row["sha"] for row in materialized} == set(expected)
    for row in materialized:
        dc = expected[row["sha"]]
        assert row["author_name"] == dc.author_name
        assert row["author_email"] == dc.author_email
        assert row["subject"] == dc.subject
        assert row["body"] == dc.body
        assert row["authored_at"] == dc.authored_at
        assert row["committed_at"] == dc.committed_at
        assert row["parent_shas"] == " ".join(dc.parent_shas)
        assert row["files_changed"] == dc.stats.files_changed
        assert row["insertions"] == dc.stats.insertions
        assert row["deletions"] == dc.stats.deletions
        assert row["scanned_at"]  # set to a non-empty ISO string


# --------------------------------------------------------------------------
# Branch membership (plan §4.2) and the reconcile pass (§4.3).
# --------------------------------------------------------------------------


def _git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _commit_file(root: Path, name: str, body: str = "x\n") -> str:
    (root / name).write_text(body)
    _git(root, "add", name)
    _git(root, "commit", "-q", "-m", name)
    return _git_out(root, "rev-parse", "HEAD").strip()


def _configure(root: Path) -> Path:
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "commit.gpgsign", "false")
    return root


@pytest.fixture
def cloned(tmp_path: Path) -> Path:
    """A clone of a two-commit ``main`` upstream — ``origin/main`` resolves."""
    upstream = _make_repo(tmp_path / "upstream")
    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(work)],
        check=True,
        capture_output=True,
    )
    return _configure(work)


def _rows() -> dict[str, tuple[int, str | None]]:
    """``{sha: (on_default_branch, first_seen_ref)}`` for every commit row."""
    with get_session() as session:
        return {
            r.sha: (r.on_default_branch, r.first_seen_ref)
            for r in session.exec(select(CommitRow)).all()
        }


def test_feature_branch_commit_is_off_default_branch(cloned: Path) -> None:
    """Case 8 — unmerged work is flagged 0 and records the ref it came from."""
    _git(cloned, "switch", "-q", "-c", "feature/x")
    sha = _commit_file(cloned, "feature.txt")

    GitCrawler(Progress(), repository=Repository(cloned)).run()

    assert _rows()[sha] == (0, "feature/x")


def test_default_branch_commit_is_flagged_one(cloned: Path) -> None:
    """Case 9 — a commit reachable from origin/main is 1 with a NULL ref."""
    head = _git_out(cloned, "rev-parse", "HEAD").strip()

    GitCrawler(Progress(), repository=Repository(cloned)).run()

    assert _rows()[head] == (1, None)


def test_merge_promotes_feature_commits(cloned: Path) -> None:
    """Case 10 + 16 — a true merge promotes the rows on the next scan."""
    _git(cloned, "switch", "-q", "-c", "feature/x")
    sha = _commit_file(cloned, "feature.txt")
    GitCrawler(Progress(), repository=Repository(cloned)).run()
    assert _rows()[sha][0] == 0

    _git(cloned, "switch", "-q", "main")
    _git(cloned, "merge", "-q", "--no-ff", "-m", "merge feature/x", "feature/x")

    crawler = GitCrawler(Progress(), repository=Repository(cloned))
    crawler.run()

    # Promoted, but first_seen_ref is provenance and is never rewritten.
    assert _rows()[sha] == (1, "feature/x")
    assert "1 promoted" in crawler.summary
    assert "demoted" not in crawler.summary
    assert crawler.warning is None


def test_squash_merge_leaves_originals_off_branch(cloned: Path) -> None:
    """Case 11 — a squash creates a *new* commit; the originals stay 0."""
    _git(cloned, "switch", "-q", "-c", "feature/x")
    sha = _commit_file(cloned, "feature.txt")
    GitCrawler(Progress(), repository=Repository(cloned)).run()

    _git(cloned, "switch", "-q", "main")
    _git(cloned, "merge", "-q", "--squash", "feature/x")
    _git(cloned, "commit", "-q", "-m", "squashed feature/x")

    GitCrawler(Progress(), repository=Repository(cloned)).run()

    # Exactly what PROriginEnricher would have produced — offline and free.
    assert _rows()[sha] == (0, "feature/x")


def test_rewritten_history_demotes_and_warns(cloned: Path) -> None:
    """Cases 12, 16, 17 — a demotion is counted, warned about, and applied."""
    sha = _commit_file(cloned, "local.txt")
    GitCrawler(Progress(), repository=Repository(cloned)).run()
    assert _rows()[sha][0] == 1

    _git(cloned, "reset", "-q", "--hard", "HEAD~1")

    crawler = GitCrawler(Progress(), repository=Repository(cloned))
    crawler.run()

    # The row is retained as evidence, just excluded from default-branch queries.
    assert _rows()[sha] == (0, None)
    assert "1 demoted" in crawler.summary
    assert crawler.warning is not None
    assert "1 commits are no longer reachable from origin/main, main" in crawler.warning


@contextmanager
def _capture(logger_name: str) -> Iterator[list[str]]:
    """Capture a single logger's INFO records, independently of the root.

    Deliberately not ``caplog``: several CLI tests earlier in the session
    invoke the ``whygraph`` group, whose callback runs ``configure_logging``
    and replaces the root logger's handlers — taking pytest's capture handler
    with it. Attaching to the module logger asserts exactly what D9 promises
    (this logger emits the SHAs at INFO; ``scan_log_redirect`` does the rest)
    without depending on global logging state.
    """
    messages: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    logger = logging.getLogger(logger_name)
    handler = _Collect(level=logging.INFO)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def test_demoted_shas_are_logged(cloned: Path) -> None:
    """Case 47d (D9) — the SHAs land in the scan log, not the panel."""
    sha = _commit_file(cloned, "local.txt")
    GitCrawler(Progress(), repository=Repository(cloned)).run()
    _git(cloned, "reset", "-q", "--hard", "HEAD~1")

    with _capture("whygraph.scan.git_crawler") as messages:
        crawler = GitCrawler(Progress(), repository=Repository(cloned))
        crawler.run()

    assert any(sha in m for m in messages)
    # The console line stays a count — the SHA belongs in the log only.
    assert crawler.warning is not None
    assert sha not in crawler.warning


def test_shallow_clone_skips_reconcile(tmp_path: Path) -> None:
    """Case 13 — a truncated view must never mass-demote."""
    upstream = _make_repo(tmp_path / "upstream")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth=1", upstream.as_uri(), str(shallow)],
        check=True,
        capture_output=True,
    )
    _configure(shallow)
    assert Repository(shallow).is_shallow is True

    # A pre-existing row for a commit the shallow clone cannot see. Without
    # the guard the reconcile would demote it.
    with get_session() as session:
        session.add(
            CommitRow(
                sha="0" * 40,
                parent_shas="",
                author_name="A",
                author_email="a@example.com",
                authored_at="2026-01-01T00:00:00Z",
                committed_at="2026-01-01T00:00:00Z",
                subject="older than the graft point",
                body="",
                files_changed=0,
                insertions=0,
                deletions=0,
                scanned_at="2026-01-01T00:00:00Z",
            )
        )

    crawler = GitCrawler(Progress(), repository=Repository(shallow))
    crawler.run()

    assert _rows()["0" * 40] == (1, None)
    assert "demoted" not in crawler.summary
    assert crawler.warning is None


def test_unresolvable_default_branch_flags_everything_one(repo_root: Path) -> None:
    """Case 14 — no remote, no main/master override: today's behaviour exactly."""
    repo = Repository(repo_root)
    assert repo.default_branch_refs == ()

    GitCrawler(Progress(), repository=repo).run()

    assert all(row == (1, None) for row in _rows().values())


def test_detached_head_records_the_literal_head(cloned: Path) -> None:
    """Case 15 — a detached HEAD is stored verbatim; no special case."""
    _git(cloned, "switch", "-q", "-c", "feature/x")
    sha = _commit_file(cloned, "feature.txt")
    _git(cloned, "checkout", "-q", sha)

    repo = Repository(cloned)
    assert repo.current_branch == "HEAD"
    GitCrawler(Progress(), repository=repo).run()

    assert _rows()[sha] == (0, "HEAD")
