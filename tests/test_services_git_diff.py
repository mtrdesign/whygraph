"""Tests for :meth:`whygraph.services.git.Repository.diff`.

Spins up a real git repo on disk — same pattern as
``test_services_git_commits.py`` — so we exercise the actual ``git``
binary rather than mocking out the shell.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from whygraph.services.git import GitError, Repository
from whygraph.services.git.commits import Commits


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
    )


def _make_repo(tmp_path: Path) -> Path:
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


def _commits_newest_first(root: Path) -> list:
    return list(Commits(root, "main"))


def test_diff_against_first_parent_contains_added_file(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    repo = Repository(root)
    commits = _commits_newest_first(root)
    second = commits[1]  # the "second" commit added b.txt

    diff = repo.diff(second)

    assert "b.txt" in diff
    assert "+world" in diff


def test_diff_for_root_commit_shows_only_its_introduction(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    repo = Repository(root)
    initial = _commits_newest_first(root)[-1]  # oldest
    assert initial.parent_shas == ()

    diff = repo.diff(initial)

    # Exactly what the root commit introduced: a.txt as a new file.
    assert "a.txt" in diff
    assert "+hello" in diff
    # b.txt is added by a *later* commit; diffing the root against the
    # working tree (the old `--root` no-op bug) would wrongly include it.
    assert "b.txt" not in diff


def test_diff_captures_modification_not_addition(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    repo = Repository(root)
    third = _commits_newest_first(root)[0]  # newest: edits a.txt

    diff = repo.diff(third)

    assert "a.txt" in diff
    assert "-hello" in diff
    assert "+hello updated" in diff


def test_diff_for_merge_commit_uses_first_parent(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    _git(root, "checkout", "-q", "-b", "feature")
    (root / "c.txt").write_text("feature\n")
    _git(root, "add", "c.txt")
    _git(root, "commit", "-q", "-m", "feature work")
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "--no-ff", "-q", "-m", "merge feature", "feature")

    repo = Repository(root)
    merge = _commits_newest_first(root)[0]
    assert len(merge.parent_shas) == 2

    diff = repo.diff(merge)

    # First-parent diff = the changes brought in by feature relative to
    # the main tip — c.txt's addition.
    assert "c.txt" in diff
    assert "+feature" in diff


def test_diff_raises_git_error_for_unknown_sha(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    repo = Repository(root)
    real = _commits_newest_first(root)[0]
    # Build a Commit-shaped object with a bogus sha to force a git failure.
    bogus = real.__class__(
        sha="deadbeef" * 5,
        parent_shas=("0" * 40,),
        author_name=real.author_name,
        author_email=real.author_email,
        authored_at=real.authored_at,
        committed_at=real.committed_at,
        subject=real.subject,
        body=real.body,
        stats=real.stats,
    )

    with pytest.raises(GitError):
        repo.diff(bogus)


# ---- diff(pathspec=...) --------------------------------------------------


def test_diff_pathspec_limits_to_one_file(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    repo = Repository(root)
    second = _commits_newest_first(root)[1]  # added b.txt

    diff = repo.diff(second, pathspec="b.txt")

    assert "b.txt" in diff
    assert "+world" in diff


def test_diff_pathspec_for_untouched_path_is_empty(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    repo = Repository(root)
    second = _commits_newest_first(root)[1]  # touched b.txt only, not a.txt

    # The "second" commit did not touch a.txt, so scoping to it yields
    # nothing — callers treat "" as "this commit has nothing for path".
    assert repo.diff(second, pathspec="a.txt") == ""


def test_diff_pathspec_on_root_commit_shows_file_contents(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    repo = Repository(root)
    initial = _commits_newest_first(root)[-1]  # oldest, added a.txt
    assert initial.parent_shas == ()

    diff = repo.diff(initial, pathspec="a.txt")

    # For a root commit the per-file slice against the empty tree is the
    # file's full introduction.
    assert "a.txt" in diff
    assert "+hello" in diff


# ---- diff_range ----------------------------------------------------------


def test_diff_range_between_two_commits(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    repo = Repository(root)
    third, _second, first = _commits_newest_first(root)

    diff = repo.diff_range(first.sha, third.sha)

    # first -> third: b.txt added, a.txt edited.
    assert "b.txt" in diff
    assert "+world" in diff
    assert "+hello updated" in diff


def test_diff_range_is_directional(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    repo = Repository(root)
    third, _second, first = _commits_newest_first(root)

    forward = repo.diff_range(first.sha, third.sha)
    reverse = repo.diff_range(third.sha, first.sha)

    assert "+hello updated" in forward
    # The reverse range shows that edit being undone, not applied.
    assert "+hello updated" not in reverse


def test_diff_range_of_commit_against_itself_is_empty(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    repo = Repository(root)
    head = _commits_newest_first(root)[0]

    assert repo.diff_range(head.sha, head.sha) == ""


def test_diff_range_raises_git_error_for_unknown_ref(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    repo = Repository(root)
    real = _commits_newest_first(root)[0]

    with pytest.raises(GitError):
        repo.diff_range("0" * 40, real.sha)
