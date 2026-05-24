"""Tests for :mod:`whygraph.analyze.backfill`.

Same on-disk-repo + isolated-DB pattern as
``test_scan_analyze_crawler.py`` — exercises the lazy backfill helper
that ``whygraph_evidence_for`` and ``whygraph_rationale_brief`` rely on
when a commit's ``llm_description`` is ``NULL``.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from threading import Lock
from typing import Iterator

import pytest
from sqlmodel import select

from whygraph import core
from whygraph.analyze import (
    AnalyzeError,
    Description,
    backfill_all,
    backfill_commit_description,
)
from whygraph.core.config import Config
from whygraph.db import engine as db_engine
from whygraph.db import ensure_initialized, get_session
from whygraph.db.models.commit import Commit as CommitRow
from whygraph.services.git import Repository
from whygraph.services.git.commits import Commits


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _make_repo(root: Path) -> Path:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "commit.gpgsign", "false")

    (root / "a.txt").write_text("hello\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "first")

    (root / "b.txt").write_text("world\n")
    _git(root, "add", "b.txt")
    _git(root, "commit", "-q", "-m", "second")

    return root


@pytest.fixture
def repo_path(tmp_path: Path) -> Path:
    """A temp git repo with two commits."""
    return _make_repo(tmp_path)


@pytest.fixture
def isolated_db(repo_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Per-test SQLite DB, migrated, with the WhyGraph config rebound."""
    db_path = repo_path / ".whygraph" / "whygraph.db"
    monkeypatch.setattr(core, "_config", Config(whygraph_db=db_path))
    db_engine._reset_engine()
    ensure_initialized()
    try:
        yield db_path
    finally:
        db_engine._reset_engine()
        core._reset_config()


class _StubDescriptor:
    """Records every diff it is handed; configurable failure substring."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self._fail_on = fail_on
        self._lock = Lock()
        self.seen: list[str] = []

    def describe(self, diff: str) -> Description:
        with self._lock:
            self.seen.append(diff)
        if self._fail_on is not None and self._fail_on in diff:
            raise AnalyzeError("stub failure")
        return Description(
            text="DESCRIPTION",
            model="stub-model",
            provider="stub-provider",
            input_tokens=1,
            output_tokens=2,
        )


def _insert(commits: list, *, described: tuple[str, ...] = ()) -> None:
    """Insert one ``CommitRow`` per commit; SHAs in ``described`` start non-NULL."""
    with get_session() as session:
        for c in commits:
            session.add(
                CommitRow(
                    sha=c.sha,
                    parent_shas=" ".join(c.parent_shas),
                    author_name="Test User",
                    author_email="test@example.com",
                    authored_at="2026-01-01T00:00:00+00:00",
                    committed_at="2026-01-01T00:00:00+00:00",
                    subject=c.subject,
                    body="",
                    files_changed=0,
                    insertions=0,
                    deletions=0,
                    scanned_at="2026-01-01T00:00:00+00:00",
                    llm_description=("PRE-EXISTING" if c.sha in described else None),
                )
            )
    db_engine._reset_engine()


def _load_detached(sha: str) -> CommitRow:
    """Load a CommitRow and detach it (mirrors the MCP read path)."""
    with get_session() as session:
        row = session.get(CommitRow, sha)
        assert row is not None
        session.expunge(row)
    return row


def _persisted(sha: str) -> tuple[str | None, str | None]:
    with get_session() as session:
        row = session.get(CommitRow, sha)
        assert row is not None
        return row.llm_description, row.llm_description_model


def test_backfill_persists_and_mutates_in_place(
    isolated_db: Path, repo_path: Path
) -> None:
    commits = list(Commits(repo_path, "main"))
    _insert(commits)

    detached = _load_detached(commits[0].sha)
    descriptor = _StubDescriptor()

    did_backfill = backfill_commit_description(
        detached, repository=Repository(repo_path), descriptor=descriptor
    )

    assert did_backfill is True
    # In-place mutation so the caller's already-detached row reads the new text.
    assert detached.llm_description == "DESCRIPTION"
    assert detached.llm_description_model == "stub-provider:stub-model"
    # Persisted to the DB for the next call.
    assert _persisted(commits[0].sha) == ("DESCRIPTION", "stub-provider:stub-model")
    assert len(descriptor.seen) == 1


def test_backfill_noop_when_already_described(
    isolated_db: Path, repo_path: Path
) -> None:
    commits = list(Commits(repo_path, "main"))
    already = commits[0].sha
    _insert(commits, described=(already,))

    detached = _load_detached(already)
    descriptor = _StubDescriptor()

    did_backfill = backfill_commit_description(
        detached, repository=Repository(repo_path), descriptor=descriptor
    )

    assert did_backfill is False
    # Pre-existing value untouched, descriptor never invoked.
    assert detached.llm_description == "PRE-EXISTING"
    assert _persisted(already) == ("PRE-EXISTING", None)
    assert descriptor.seen == []


def test_backfill_noop_on_empty_diff(isolated_db: Path, repo_path: Path) -> None:
    _git(repo_path, "commit", "-q", "--allow-empty", "-m", "empty")
    commits = list(Commits(repo_path, "main"))
    empty_sha = commits[0].sha  # newest = the empty commit
    _insert(commits)

    detached = _load_detached(empty_sha)
    descriptor = _StubDescriptor()

    did_backfill = backfill_commit_description(
        detached, repository=Repository(repo_path), descriptor=descriptor
    )

    assert did_backfill is False
    assert detached.llm_description is None
    assert _persisted(empty_sha) == (None, None)
    # Empty diff is rejected before the descriptor is called.
    assert descriptor.seen == []


def test_backfill_all_swallows_per_commit_failure(
    isolated_db: Path,
    repo_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    commits = list(Commits(repo_path, "main"))
    _insert(commits)

    rows = [_load_detached(c.sha) for c in commits]
    # The "second" commit's diff is the one that adds "world".
    descriptor = _StubDescriptor(fail_on="+world")

    with caplog.at_level(logging.WARNING, logger="whygraph.analyze.backfill"):
        succeeded = backfill_all(
            rows, repository=Repository(repo_path), descriptor=descriptor
        )

    # Only the non-failing commit was backfilled; the other was logged.
    assert succeeded == 1
    persisted = {c.sha: _persisted(c.sha)[0] for c in commits}
    described = [sha for sha, text in persisted.items() if text == "DESCRIPTION"]
    failed = [sha for sha, text in persisted.items() if text is None]
    assert len(described) == 1
    assert len(failed) == 1
    assert any(
        "lazy LLM description backfill failed" in rec.getMessage()
        for rec in caplog.records
    )
