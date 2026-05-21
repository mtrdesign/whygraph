"""Tests for :class:`whygraph.scan.AnalyzeCrawler`.

Each test spins up a real on-disk git repo (same pattern as
``test_services_git_diff.py``) plus an isolated WhyGraph SQLite database
(same pattern as ``test_cli_analyze.py``). The LLM is stubbed — no
provider SDK is touched — via a thread-safe fake descriptor that records
the diffs it is handed and returns a canned :class:`Description`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from threading import Lock
from typing import Iterator

import pytest
from rich.progress import Progress
from sqlmodel import select

from whygraph import core
from whygraph.analyze import AnalyzeError, Description
from whygraph.core.config import Config
from whygraph.db import engine as db_engine
from whygraph.db import ensure_initialized, get_session
from whygraph.db.models.commit import Commit as CommitRow
from whygraph.scan import AnalyzeCrawler
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

    (root / "a.txt").write_text("hello updated\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "third")

    return root


@pytest.fixture
def repo_path(tmp_path: Path) -> Path:
    """A temp git repo with three commits."""
    return _make_repo(tmp_path)


@pytest.fixture
def isolated_db(repo_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point WhyGraph at a per-test SQLite file and initialize its schema."""
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
    """Thread-safe stand-in for ``LlmDescriptor``.

    Records every diff it is handed. When ``fail_on`` is set, raises
    :class:`AnalyzeError` for any diff that contains that substring.
    """

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


def _commits(root: Path) -> list:
    """All commits on ``main``, newest-first."""
    return list(Commits(root, "main"))


def _insert(commits: list, *, described: tuple[str, ...] = ()) -> None:
    """Insert a ``CommitRow`` per commit; SHAs in ``described`` start non-NULL."""
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


def _descriptions() -> dict[str, tuple[str | None, str | None]]:
    """Map every commit SHA to its (llm_description, llm_description_model)."""
    with get_session() as session:
        return {
            r.sha: (r.llm_description, r.llm_description_model)
            for r in session.exec(select(CommitRow)).all()
        }


def _run(repo_path: Path, descriptor: _StubDescriptor, max_workers: int = 2):
    with Progress() as progress:
        crawler = AnalyzeCrawler(
            progress,
            repository=Repository(repo_path),
            descriptor=descriptor,
            max_workers=max_workers,
        )
        crawler.start()
        crawler.join()
    return crawler


def test_describes_every_pending_commit(isolated_db: Path, repo_path: Path) -> None:
    commits = _commits(repo_path)
    _insert(commits)
    descriptor = _StubDescriptor()

    crawler = _run(repo_path, descriptor)

    assert crawler.error is None
    descs = _descriptions()
    for c in commits:
        assert descs[c.sha] == ("DESCRIPTION", "stub-provider:stub-model")
    assert len(descriptor.seen) == len(commits)


def test_skips_already_described_commits(
    isolated_db: Path, repo_path: Path
) -> None:
    commits = _commits(repo_path)
    already = commits[0].sha
    _insert(commits, described=(already,))
    descriptor = _StubDescriptor()

    crawler = _run(repo_path, descriptor)

    assert crawler.error is None
    descs = _descriptions()
    assert descs[already] == ("PRE-EXISTING", None)  # untouched
    for c in commits[1:]:
        assert descs[c.sha][0] == "DESCRIPTION"
    assert len(descriptor.seen) == len(commits) - 1


def test_skips_commit_with_empty_diff(
    isolated_db: Path, repo_path: Path
) -> None:
    _git(repo_path, "commit", "-q", "--allow-empty", "-m", "empty")
    commits = _commits(repo_path)
    empty_sha = commits[0].sha  # newest = the empty commit
    _insert(commits)
    descriptor = _StubDescriptor()

    crawler = _run(repo_path, descriptor)

    assert crawler.error is None
    descs = _descriptions()
    assert descs[empty_sha] == (None, None)  # nothing to describe — left NULL
    described = [sha for sha, v in descs.items() if v[0] == "DESCRIPTION"]
    assert len(described) == 3  # the three real commits


def test_one_failing_commit_does_not_block_the_rest(
    isolated_db: Path, repo_path: Path
) -> None:
    commits = _commits(repo_path)
    _insert(commits)
    # Only the "second" commit's diff adds b.txt with the line "world".
    descriptor = _StubDescriptor(fail_on="+world")

    crawler = _run(repo_path, descriptor)

    assert isinstance(crawler.error, AnalyzeError)
    descs = _descriptions()
    described = [sha for sha, v in descs.items() if v[0] == "DESCRIPTION"]
    failed = [sha for sha, v in descs.items() if v[0] is None]
    assert len(described) == 2  # the other two still completed and committed
    assert len(failed) == 1
