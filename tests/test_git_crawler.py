"""Tests for :class:`whygraph.scan.git_crawler.GitCrawler`."""

from __future__ import annotations

import subprocess
import time
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
