"""Tests for the ``whygraph analyze`` CLI command.

Each test spins up a real on-disk git repo (same pattern as
``test_services_git_diff.py``) plus an isolated WhyGraph SQLite database
(same pattern as ``test_db_plumbing.py``). The LLM is stubbed — no
provider SDK is touched — via a fake :class:`LlmDescriptor` that records
the diff it is handed and returns a canned :class:`Description`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterator

import pytest
from click.testing import CliRunner

from whygraph import core
from whygraph.analyze import Description
from whygraph.cli import main as whygraph_main
from whygraph.core.config import Config
from whygraph.db import ensure_initialized, get_session
from whygraph.db import engine as db_engine
from whygraph.db.models.commit import Commit as CommitRow
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


@pytest.fixture(autouse=True)
def _no_logging_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep CLI tests from reconfiguring process-wide logging.

    ``analyze`` calls ``_configure_logging_best_effort``, which attaches a
    Rich handler to the ``whygraph`` logger and sets ``propagate = False``
    behind a module-global guard that is never reset — a process-wide
    mutation that would otherwise bleed into unrelated tests' ``caplog``.
    """
    monkeypatch.setattr(
        "whygraph.cli._configure_logging_best_effort", lambda: None
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp git repo with three commits; cwd is moved into it."""
    root = _make_repo(tmp_path)
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def isolated_db(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point WhyGraph at a per-test SQLite file and initialize its schema."""
    db_path = repo / ".whygraph" / "whygraph.db"
    monkeypatch.setattr(core, "_config", Config(whygraph_db=db_path))
    db_engine._reset_engine()
    ensure_initialized()
    try:
        yield db_path
    finally:
        db_engine._reset_engine()
        core._reset_config()


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace ``LlmDescriptor`` with a fake; return the diffs it is handed."""
    seen: list[str] = []

    class _FakeDescriptor:
        @classmethod
        def from_config(cls, _config: object, **_kw: object) -> "_FakeDescriptor":
            return cls()

        def describe(self, diff: str) -> Description:
            seen.append(diff)
            return Description(
                text="CANNED DESCRIPTION TEXT",
                model="fake-model",
                provider="fake-provider",
                input_tokens=12,
                output_tokens=34,
                truncated=False,
            )

    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _FakeDescriptor)
    return seen


def _commits(root: Path) -> list:
    """All commits on ``main``, newest-first: ``[third, second, first]``."""
    return list(Commits(root, "main"))


def _insert(commits: list) -> None:
    """Insert minimal ``CommitRow``s so the analyze DB precondition is met."""
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
                )
            )
    db_engine._reset_engine()


# ---- happy paths ---------------------------------------------------------


def test_analyze_one_sha_describes_commit_against_parent(
    isolated_db: Path, stub_llm: list[str]
) -> None:
    commits = _commits(Path.cwd())
    second = commits[1]
    _insert(commits)

    result = CliRunner().invoke(whygraph_main, ["analyze", second.sha])

    assert result.exit_code == 0, result.output
    assert "CANNED DESCRIPTION TEXT" in result.output
    assert "fake-provider" in result.output
    assert "fake-model" in result.output
    assert "input tokens:  12" in result.output
    assert "output tokens: 34" in result.output
    # The actual diff is printed, under its section header.
    assert "git diff" in result.output
    assert "b.txt" in result.output
    # The model output is rendered in its own highlighted panel.
    assert "LLM description" in result.output
    # The diff handed to the LLM is `second` vs its parent — b.txt added.
    assert len(stub_llm) == 1
    assert "b.txt" in stub_llm[0]
    assert "+world" in stub_llm[0]


def test_analyze_two_shas_diffs_baseline_into_target(
    isolated_db: Path, stub_llm: list[str]
) -> None:
    commits = _commits(Path.cwd())
    third, first = commits[0], commits[2]
    _insert(commits)

    # analyze TARGET BASELINE -> git diff BASELINE..TARGET == first..third
    result = CliRunner().invoke(whygraph_main, ["analyze", third.sha, first.sha])

    assert result.exit_code == 0, result.output
    diff = stub_llm[0]
    # first -> third is the forward direction: the edit is applied, not undone.
    assert "+hello updated" in diff
    assert "+world" in diff
    assert "-hello updated" not in diff


# ---- error paths ---------------------------------------------------------


def test_analyze_errors_when_target_not_in_db(
    isolated_db: Path, stub_llm: list[str]
) -> None:
    commits = _commits(Path.cwd())
    third, first = commits[0], commits[2]
    _insert([first])  # `third` deliberately absent

    result = CliRunner().invoke(whygraph_main, ["analyze", third.sha])

    assert result.exit_code == 1
    assert third.sha[:12] in result.output
    assert "scan" in result.output
    assert stub_llm == []  # LLM never called


def test_analyze_errors_when_baseline_not_in_db(
    isolated_db: Path, stub_llm: list[str]
) -> None:
    commits = _commits(Path.cwd())
    third, first = commits[0], commits[2]
    _insert([third])  # `first` deliberately absent

    result = CliRunner().invoke(whygraph_main, ["analyze", third.sha, first.sha])

    assert result.exit_code == 1
    assert first.sha[:12] in result.output
    assert stub_llm == []


def test_analyze_rejects_unknown_ref(
    isolated_db: Path, stub_llm: list[str]
) -> None:
    result = CliRunner().invoke(whygraph_main, ["analyze", "nonexistentref"])

    assert result.exit_code == 1
    assert "nonexistentref" in result.output
    assert stub_llm == []


def test_analyze_requires_at_least_one_arg() -> None:
    result = CliRunner().invoke(whygraph_main, ["analyze"])
    assert result.exit_code == 2  # Click usage error


def test_analyze_rejects_more_than_two_args() -> None:
    result = CliRunner().invoke(whygraph_main, ["analyze", "aaa", "bbb", "ccc"])
    assert result.exit_code == 2
