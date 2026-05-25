"""Tests for ``git blame`` support in :mod:`whygraph.services.git`.

The porcelain parser is unit-tested against a crafted blob; the
:meth:`Repository.blame` integration is exercised against a real throwaway
repo (the ``temp_git_repo`` fixture).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from whygraph.services.git import BlameHunk, GitError, Repository
from whygraph.services.git.commands import GitBlameCmd


def _git(repo: Path, *args: str) -> str:
    """Run ``git`` in ``repo`` and return stdout (utility for refactor fixtures)."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _make_repo(tmp_path: Path) -> Path:
    """Bootstrap an empty repo configured for deterministic test commits."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "tester@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "commit.gpgsign", "false")
    return root


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


def test_blame_cmd_argv_includes_refactor_resilient_flags() -> None:
    argv = GitBlameCmd("src/foo.py", 10, 20).argv()
    # Order is part of the contract: flags come before the file separator.
    assert argv[:5] == ["git", "blame", "-w", "-M", "-C"]
    assert "-L10,20" in argv
    assert argv[-2:] == ["--", "src/foo.py"]
    assert all(not a.startswith("--ignore-revs-file=") for a in argv)


def test_blame_cmd_argv_threads_through_ignore_revs_file() -> None:
    argv = GitBlameCmd(
        "src/foo.py", 1, 1, ignore_revs_file=".git-blame-ignore-revs"
    ).argv()
    assert "--ignore-revs-file=.git-blame-ignore-revs" in argv
    # The flag must appear before the ``--`` separator so git treats it
    # as an option, not as a path.
    assert argv.index("--ignore-revs-file=.git-blame-ignore-revs") < argv.index("--")


def test_blame_w_flag_ignores_whitespace_only_refactor(tmp_path: Path) -> None:
    """A pure-whitespace reformat must not steal attribution from the author."""
    root = _make_repo(tmp_path)
    sample = root / "sample.py"
    # Commit A authors the line with two-space indentation.
    sample.write_text("def foo():\n  return 1\n")
    _git(root, "add", "sample.py")
    _git(root, "commit", "-m", "feat: add foo")
    commit_a = _git(root, "rev-parse", "HEAD").strip()
    # Commit B is a pure-whitespace reformat (4-space indent + trailing
    # whitespace). With ``-w`` (Phase 1 default), the line stays attributed
    # to commit A.
    sample.write_text("def foo():\n    return 1   \n")
    _git(root, "add", "sample.py")
    _git(root, "commit", "-m", "style: reformat")

    hunks = Repository(root).blame("sample.py", 2, 2)

    assert len(hunks) == 1
    assert hunks[0].sha == commit_a


def test_blame_respects_project_level_git_blame_ignore_revs(tmp_path: Path) -> None:
    """A checked-in ``.git-blame-ignore-revs`` makes blame skip listed commits."""
    root = _make_repo(tmp_path)
    sample = root / "sample.py"
    # Commit A: original content. Commit B: rewrites the line meaningfully
    # (non-whitespace), so ``-w`` alone wouldn't skip it.
    sample.write_text("x = 1\n")
    _git(root, "add", "sample.py")
    _git(root, "commit", "-m", "feat: add x")
    commit_a = _git(root, "rev-parse", "HEAD").strip()
    sample.write_text("x = 42\n")
    _git(root, "add", "sample.py")
    _git(root, "commit", "-m", "refactor: rename constant")
    commit_b = _git(root, "rev-parse", "HEAD").strip()

    # Sanity: without an ignore-revs file, blame attributes to commit B.
    without = Repository(root).blame("sample.py", 1, 1)
    assert without[0].sha == commit_b

    # Drop the skip list at the repo root, then re-blame.
    (root / ".git-blame-ignore-revs").write_text(f"{commit_b}\n")
    with_ignore = Repository(root).blame("sample.py", 1, 1)
    # Commit B is walked past; the attributed SHA is now commit A.
    assert with_ignore[0].sha == commit_a
