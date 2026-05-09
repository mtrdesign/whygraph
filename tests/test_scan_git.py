import subprocess
from pathlib import Path

import pytest

from whygraph.scan.git import (
    GitError,
    _parse_shortstat,
    default_branch,
    get_commit,
    repo_root,
    walk_first_parent,
)


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


def test_parse_shortstat_full() -> None:
    out = " 5 files changed, 47 insertions(+), 12 deletions(-)\n"
    assert _parse_shortstat(out) == (5, 47, 12)


def test_parse_shortstat_only_insertions() -> None:
    out = " 1 file changed, 3 insertions(+)\n"
    assert _parse_shortstat(out) == (1, 3, 0)


def test_parse_shortstat_only_deletions() -> None:
    out = " 1 file changed, 2 deletions(-)\n"
    assert _parse_shortstat(out) == (1, 0, 2)


def test_parse_shortstat_empty() -> None:
    assert _parse_shortstat("") == (0, 0, 0)


def test_repo_root_returns_top_level(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    sub = repo / "subdir"
    sub.mkdir()
    assert repo_root(sub).resolve() == repo.resolve()


def test_repo_root_raises_outside_repo(tmp_path: Path) -> None:
    with pytest.raises(GitError):
        repo_root(tmp_path)


def test_default_branch_main(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    assert default_branch(repo) == "main"


def test_walk_first_parent_in_chronological_order(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    shas = list(walk_first_parent(repo, "main"))
    assert len(shas) == 3
    subjects = [get_commit(repo, sha).subject for sha in shas]
    assert subjects == ["first", "second", "third"]


def test_get_commit_root_metadata(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    shas = list(walk_first_parent(repo, "main"))
    first = get_commit(repo, shas[0])
    assert first.subject == "first"
    assert first.author_name == "Test User"
    assert first.author_email == "test@example.com"
    assert first.parent_shas == []
    assert first.files_changed == 1
    assert first.insertions == 1
    assert first.deletions == 0


def test_get_commit_with_parent_diff_stats(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    shas = list(walk_first_parent(repo, "main"))
    third = get_commit(repo, shas[2])
    assert third.subject == "third"
    assert len(third.parent_shas) == 1
    assert third.files_changed == 1
    assert third.insertions == 1
    assert third.deletions == 1
