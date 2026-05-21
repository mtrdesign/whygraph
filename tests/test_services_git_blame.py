"""Tests for ``git blame`` support in :mod:`whygraph.services.git`.

The porcelain parser is unit-tested against a crafted blob; the
:meth:`Repository.blame` integration is exercised against a real throwaway
repo (the ``temp_git_repo`` fixture).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whygraph.services.git import BlameHunk, GitError, Repository


def _porcelain(sha1: str, sha2: str) -> str:
    """A two-commit ``git blame --porcelain`` blob, sha1 owning lines 1-2."""
    return (
        "\n".join(
            [
                f"{sha1} 1 1 2",
                "author Alice",
                "author-mail <alice@example.com>",
                "author-time 1700000000",
                "author-tz +0000",
                "committer Alice",
                "committer-mail <alice@example.com>",
                "committer-time 1700000000",
                "committer-tz +0000",
                "summary first commit",
                "filename sample.py",
                "\tline one",
                # Repeat header for the same SHA — no metadata block.
                f"{sha1} 2 2",
                "\tline two",
                f"{sha2} 3 3 1",
                "author Bob",
                "author-mail <bob@example.com>",
                "author-time 1700003600",
                "author-tz +0000",
                "committer Bob",
                "committer-mail <bob@example.com>",
                "committer-time 1700003600",
                "committer-tz +0000",
                "summary second commit",
                "filename sample.py",
                "\tline three",
            ]
        )
        + "\n"
    )


def test_from_porcelain_aggregates_and_carries_metadata_forward() -> None:
    sha1, sha2 = "a" * 40, "b" * 40
    hunks = BlameHunk.from_porcelain(_porcelain(sha1, sha2))

    assert [h.sha for h in hunks] == [sha1, sha2]
    first, second = hunks
    # The repeat `{sha1} 2 2` line counts even though it carries no metadata.
    assert first.lines_owned == 2
    assert first.author_name == "Alice"
    assert first.author_email == "alice@example.com"
    assert first.summary == "first commit"
    assert first.committed_at is not None
    assert second.lines_owned == 1
    assert second.author_name == "Bob"


def test_from_porcelain_empty_input_yields_no_hunks() -> None:
    assert BlameHunk.from_porcelain("") == ()


def test_repository_blame_attributes_lines_to_commits(temp_git_repo: Path) -> None:
    hunks = Repository(temp_git_repo).blame("sample.py", 1, 3)

    assert sum(h.lines_owned for h in hunks) == 3
    assert {h.lines_owned for h in hunks} == {1, 2}
    assert all(h.author_name == "Test User" for h in hunks)
    assert all(h.author_email == "tester@example.com" for h in hunks)
    assert all(h.committed_at is not None for h in hunks)
    assert not any(h.is_uncommitted for h in hunks)


def test_repository_blame_flags_uncommitted_lines(temp_git_repo: Path) -> None:
    sample = temp_git_repo / "sample.py"
    sample.write_text(sample.read_text() + "uncommitted line\n")

    hunks = Repository(temp_git_repo).blame("sample.py", 4, 4)

    assert len(hunks) == 1
    assert hunks[0].is_uncommitted


def test_repository_blame_raises_git_error_for_unknown_path(
    temp_git_repo: Path,
) -> None:
    with pytest.raises(GitError, match="failed to blame"):
        Repository(temp_git_repo).blame("does_not_exist.py", 1, 1)
