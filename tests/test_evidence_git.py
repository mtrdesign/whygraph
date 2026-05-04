from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from whygraph.backend import SymbolNode
from whygraph.evidence.git import (
    GitEvidenceCollector,
    _parse_line_porcelain,
    collect_git_evidence,
)


def _node(file_path: str, start: int = 1, end: int = 3) -> SymbolNode:
    return SymbolNode(
        id="n_test",
        kind="function",
        name="test",
        qualified_name="pkg.test",
        file_path=file_path,
        language="python",
        start_line=start,
        end_line=end,
        docstring=None,
        signature=None,
    )


def test_parse_line_porcelain_extracts_blame_entries() -> None:
    porcelain = (
        "abc1234567 1 1 1\n"
        "author Alice\n"
        "author-mail <alice@example.com>\n"
        "author-time 1700000000\n"
        "summary fix bug\n"
        "filename src/foo.py\n"
        "\tcontent line 1\n"
        "abc1234567 2 2 1\n"
        "author Alice\n"
        "author-mail <alice@example.com>\n"
        "author-time 1700000000\n"
        "summary fix bug\n"
        "filename src/foo.py\n"
        "\tcontent line 2\n"
    )
    entries = _parse_line_porcelain(porcelain)
    assert len(entries) == 1
    assert entries[0].commit == "abc1234567"
    assert entries[0].author == "Alice"
    assert entries[0].author_email == "alice@example.com"
    assert entries[0].author_time == 1700000000
    assert entries[0].summary == "fix bug"
    assert entries[0].line_count == 2


def test_parse_line_porcelain_aggregates_per_sha() -> None:
    porcelain = (
        "aaa0000000 1 1 1\n"
        "author A\n"
        "author-mail <a@x>\n"
        "author-time 1\n"
        "summary one\n"
        "\tcontent\n"
        "bbb1111111 2 2 1\n"
        "author B\n"
        "author-mail <b@x>\n"
        "author-time 2\n"
        "summary two\n"
        "\tcontent\n"
        "bbb1111111 3 3 1\n"
        "author B\n"
        "author-mail <b@x>\n"
        "author-time 2\n"
        "summary two\n"
        "\tcontent\n"
    )
    entries = _parse_line_porcelain(porcelain)
    by_sha = {e.commit: e for e in entries}
    assert by_sha["aaa0000000"].line_count == 1
    assert by_sha["bbb1111111"].line_count == 2
    # Sorted desc by line_count
    assert entries[0].commit == "bbb1111111"


def test_parse_line_porcelain_returns_empty_for_no_headers() -> None:
    assert _parse_line_porcelain("") == []
    assert _parse_line_porcelain("garbage\nmore garbage\n") == []


def test_blame_returns_entries_for_committed_lines(init_git_repo, git_commit) -> None:
    repo = init_git_repo()
    git_commit(repo, "a.py", "line1\nline2\nline3\n")
    git = GitEvidenceCollector(repo)
    entries = git.blame_line_range("a.py", 1, 3)
    assert len(entries) == 1
    assert entries[0].author == "Test"
    assert entries[0].author_email == "test@example.com"
    assert entries[0].line_count == 3


def test_blame_returns_empty_for_uncommitted_file(init_git_repo) -> None:
    repo = init_git_repo()
    (repo / "a.py").write_text("only on disk\n")
    git = GitEvidenceCollector(repo)
    assert git.blame_line_range("a.py", 1, 1) == []


def test_blame_returns_empty_when_not_a_repo(tmp_path: Path) -> None:
    git = GitEvidenceCollector(tmp_path)
    assert git.blame_line_range("nope.py", 1, 1) == []


def test_blame_returns_empty_when_end_before_start(init_git_repo, git_commit) -> None:
    repo = init_git_repo()
    git_commit(repo, "a.py", "x\n")
    git = GitEvidenceCollector(repo)
    assert git.blame_line_range("a.py", 5, 1) == []


def test_commit_info_round_trip(init_git_repo, git_commit) -> None:
    repo = init_git_repo()
    sha = git_commit(repo, "a.py", "x\n", message="add a")
    git = GitEvidenceCollector(repo)
    info = git.commit_info(sha)
    assert info is not None
    assert info.sha == sha
    assert info.author == "Test"
    assert info.author_email == "test@example.com"
    assert info.subject == "add a"
    assert info.parents == ()


def test_commit_info_returns_none_for_unknown_sha(init_git_repo, git_commit) -> None:
    repo = init_git_repo()
    git_commit(repo, "a.py", "x\n")
    git = GitEvidenceCollector(repo)
    assert git.commit_info("0" * 40) is None


def test_commit_info_caches(
    init_git_repo, git_commit, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_git_repo()
    sha = git_commit(repo, "a.py", "x\n")
    git = GitEvidenceCollector(repo)
    git.commit_info(sha)

    calls = {"n": 0}
    real_run = subprocess.run

    def counting_run(*args, **kwargs):
        calls["n"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr("whygraph.evidence.git.subprocess.run", counting_run)
    git.commit_info(sha)
    assert calls["n"] == 0  # served from cache, no subprocess


def test_collect_git_evidence_returns_blame_and_commit_rows(
    init_git_repo, git_commit
) -> None:
    repo = init_git_repo()
    sha = git_commit(repo, "src/a.py", "l1\nl2\nl3\n", message="initial")
    git = GitEvidenceCollector(repo)
    rows = collect_git_evidence(git, _node("src/a.py", start=1, end=3))

    blames = [r for r in rows if r.source == "git_blame"]
    commits = [r for r in rows if r.source == "git_commit"]
    assert len(blames) == 1
    assert blames[0].ref == sha
    assert blames[0].payload["line_count"] == 3
    assert blames[0].payload["line_total"] == 3
    assert blames[0].payload["author"] == "Test"

    assert len(commits) == 1
    assert commits[0].ref == sha
    assert commits[0].payload["subject"] == "initial"
    assert commits[0].payload["parents"] == []


def test_collect_git_evidence_returns_empty_when_no_blame(
    init_git_repo,
) -> None:
    repo = init_git_repo()
    git = GitEvidenceCollector(repo)
    # File doesn't exist → no blame.
    assert collect_git_evidence(git, _node("missing.py")) == []
