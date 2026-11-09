"""Tests for :meth:`whygraph.services.git.Repository.diff`.

Spins up a real git repo on disk — same pattern as
``test_services_git_commits.py`` — so we exercise the actual ``git``
binary rather than mocking out the shell.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from whygraph.services.git import DiffStats, GitError, Repository
from whygraph.services.git.commands import GitFirstParentStatsCmd
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


def _merge_feature(root: Path, files: dict[str, str], *, strategy: str = "") -> None:
    """Merge a ``feature`` branch writing ``files`` into ``main`` (``--no-ff``)."""
    _git(root, "checkout", "-q", "-b", "feature")
    for name, text in files.items():
        (root / name).write_text(text)
    _git(root, "add", *files)
    _git(root, "commit", "-q", "-m", "feature work")
    _git(root, "checkout", "-q", "main")
    args = ["-s", strategy] if strategy else []
    _git(root, "merge", "--no-ff", "-q", *args, "-m", "merge feature", "feature")


def test_first_parent_stats_sizes_a_merge(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    _merge_feature(root, {"c.txt": "one\ntwo\n", "d.txt": "three\n"})
    by_subject = {c.subject: c for c in _commits_newest_first(root)}
    merge, third = by_subject["merge feature"], by_subject["third"]
    # The history walk prints no stat line for a merge.
    assert merge.stats == DiffStats(0, 0, 0)

    stats = Repository(root).first_parent_stats([merge.sha, third.sha])

    assert stats == {merge.sha: DiffStats(2, 3, 0), third.sha: DiffStats(1, 1, 1)}


def test_first_parent_stats_of_an_empty_merge_is_zero(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    _merge_feature(root, {"c.txt": "x\n"}, strategy="ours")
    merge = _commits_newest_first(root)[0]

    assert Repository(root).first_parent_stats([merge.sha]) == {
        merge.sha: DiffStats(0, 0, 0)
    }


def test_first_parent_stats_counts_a_binary_file(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    (root / "blob.bin").write_bytes(b"\x00\x01\x02")
    _git(root, "add", "blob.bin")
    _git(root, "commit", "-q", "-m", "binary")
    head = _commits_newest_first(root)[0]

    assert Repository(root).first_parent_stats([head.sha]) == {
        head.sha: DiffStats(1, 0, 0)
    }


def test_first_parent_stats_of_nothing_runs_no_git(tmp_path: Path) -> None:
    assert Repository(tmp_path).first_parent_stats([]) == {}  # not even a repo


def test_first_parent_stats_batches_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shas = [f"{n:040x}" for n in range(1200)]
    calls: list[int] = []

    def _fake_run(cmd: GitFirstParentStatsCmd, **_kw: object) -> dict:
        calls.append(len(cmd.shas))
        return {sha: DiffStats(1, 1, 0) for sha in cmd.shas}

    repo = Repository(tmp_path)
    monkeypatch.setattr(repo._shell, "run", _fake_run)

    assert len(repo.first_parent_stats(shas)) == 1200
    assert calls == [500, 500, 200]


def test_first_parent_stats_raises_git_error_for_unknown_sha(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    with pytest.raises(GitError):
        Repository(root).first_parent_stats(["0" * 40])


def test_first_parent_stats_never_runs_an_external_diff(tmp_path: Path) -> None:
    """A repo's ``diff.external`` must not run a program (untrusted input)."""
    root = _make_repo(tmp_path)
    marker = tmp_path / "ran"
    _git(root, "config", "diff.external", f"touch {marker}")
    head = _commits_newest_first(root)[0]

    Repository(root).first_parent_stats([head.sha])

    assert not marker.exists()
