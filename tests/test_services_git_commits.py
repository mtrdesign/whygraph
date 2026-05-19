"""Tests for :class:`whygraph.services.git.commits.Commits`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from whygraph.core import Shell
from whygraph.services.git.commit import Commit, DiffStats
from whygraph.services.git.commits import Commits
from whygraph.services.git.exceptions import GitError
from whygraph.services.git.repository import Repository


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


def test_len_returns_commit_count(tmp_path: Path) -> None:
    commits = Commits(_make_repo(tmp_path), "main")
    assert len(commits) == 3


def test_iter_yields_commits_newest_first(tmp_path: Path) -> None:
    commits = list(Commits(_make_repo(tmp_path), "main"))
    assert [c.subject for c in commits] == ["third", "second", "first"]


def test_iter_populates_all_fields(tmp_path: Path) -> None:
    commits = list(Commits(_make_repo(tmp_path), "main"))
    third = commits[0]
    assert isinstance(third, Commit)
    assert len(third.sha) == 40
    assert third.author_name == "Test User"
    assert third.author_email == "test@example.com"
    assert third.subject == "third"
    assert third.body == ""
    assert third.authored_at
    assert third.committed_at
    assert len(third.parent_shas) == 1
    assert third.stats == DiffStats(files_changed=1, insertions=1, deletions=1)


def test_root_commit_has_no_parents(tmp_path: Path) -> None:
    commits = list(Commits(_make_repo(tmp_path), "main"))
    first = commits[-1]
    assert first.parent_shas == ()
    assert first.subject == "first"
    assert first.stats == DiffStats(files_changed=1, insertions=1, deletions=0)


def test_reusable_iteration(tmp_path: Path) -> None:
    commits = Commits(_make_repo(tmp_path), "main")
    first_pass = [c.sha for c in commits]
    second_pass = [c.sha for c in commits]
    assert first_pass == second_pass
    assert len(first_pass) == 3


def test_len_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commits = Commits(_make_repo(tmp_path), "main")
    assert len(commits) == 3

    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("Shell.run should not be re-invoked after cache hit")

    monkeypatch.setattr(Shell, "run", boom)
    assert len(commits) == 3
    assert len(commits) == 3


def test_unknown_ref_raises_git_error_on_len(tmp_path: Path) -> None:
    commits = Commits(_make_repo(tmp_path), "does-not-exist")
    with pytest.raises(GitError):
        len(commits)


def test_unknown_ref_raises_git_error_on_iter(tmp_path: Path) -> None:
    commits = Commits(_make_repo(tmp_path), "does-not-exist")
    with pytest.raises(GitError):
        list(commits)


def test_repository_current_branch(tmp_path: Path) -> None:
    repo = Repository(_make_repo(tmp_path))
    assert repo.current_branch == "main"


def test_repository_commits_property(tmp_path: Path) -> None:
    repo = Repository(_make_repo(tmp_path))
    commits = repo.commits
    assert isinstance(commits, Commits)
    assert commits.root == repo.root
    assert commits.ref == "main"
    assert len(commits) == 3
    assert [c.subject for c in commits] == ["third", "second", "first"]


def test_merge_commit_has_two_parents(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    _git(root, "checkout", "-q", "-b", "feature")
    (root / "c.txt").write_text("feature\n")
    _git(root, "add", "c.txt")
    _git(root, "commit", "-q", "-m", "feature work")
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "--no-ff", "-q", "-m", "merge feature", "feature")

    commits = list(Commits(root, "main"))
    merge = commits[0]
    assert merge.subject == "merge feature"
    assert len(merge.parent_shas) == 2


def test_body_is_preserved_for_multiline_commit(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    (root / "d.txt").write_text("multi\n")
    _git(root, "add", "d.txt")
    _git(
        root,
        "commit",
        "-q",
        "-m",
        "subject line",
        "-m",
        "body paragraph one\n\nbody paragraph two",
    )
    commits = list(Commits(root, "main"))
    top = commits[0]
    assert top.subject == "subject line"
    assert "body paragraph one" in top.body
    assert "body paragraph two" in top.body


def test_contains_matches_by_sha(tmp_path: Path) -> None:
    commits = Commits(_make_repo(tmp_path), "main")
    materialized = list(commits)
    assert materialized[0] in commits
    assert "not-a-commit" not in commits  # type: ignore[operator]
