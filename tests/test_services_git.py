import subprocess
from pathlib import Path

import pytest

from whygraph.services.git import (
    Commit,
    DiffStats,
    GitClient,
    GitError,
    Repository,
)
from whygraph.services.git.commands import _parse_shortstat


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


# ---- _parse_shortstat ---------------------------------------------------


def test_parse_shortstat_full() -> None:
    out = " 5 files changed, 47 insertions(+), 12 deletions(-)\n"
    assert _parse_shortstat(out) == DiffStats(
        files_changed=5, insertions=47, deletions=12
    )


def test_parse_shortstat_only_insertions() -> None:
    out = " 1 file changed, 3 insertions(+)\n"
    assert _parse_shortstat(out) == DiffStats(
        files_changed=1, insertions=3, deletions=0
    )


def test_parse_shortstat_only_deletions() -> None:
    out = " 1 file changed, 2 deletions(-)\n"
    assert _parse_shortstat(out) == DiffStats(
        files_changed=1, insertions=0, deletions=2
    )


def test_parse_shortstat_empty() -> None:
    assert _parse_shortstat("") == DiffStats(
        files_changed=0, insertions=0, deletions=0
    )


# ---- GitClient.run ------------------------------------------------------


def test_client_run_returns_stdout(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    out = GitClient().run(repo, "rev-parse", "--show-toplevel")
    assert Path(out.strip()).resolve() == repo.resolve()


def test_client_run_raises_outside_repo(tmp_path: Path) -> None:
    with pytest.raises(GitError):
        GitClient().run(tmp_path, "rev-parse", "--show-toplevel")


def test_client_run_raises_on_bad_command(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    with pytest.raises(GitError):
        GitClient().run(repo, "rev-parse", "no-such-ref")


# ---- GitClient.discover -------------------------------------------------


def test_discover_returns_repository_at_toplevel(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    sub = repo / "subdir"
    sub.mkdir()
    client = GitClient()
    found = client.discover(sub)
    assert isinstance(found, Repository)
    assert found.root.resolve() == repo.resolve()
    assert found.client is client


def test_discover_raises_outside_repo(tmp_path: Path) -> None:
    with pytest.raises(GitError):
        GitClient().discover(tmp_path)


# ---- Repository: per-commit reads ---------------------------------------


def test_head_sha_resolves(tmp_path: Path) -> None:
    repo = GitClient().discover(_make_repo(tmp_path))
    sha = repo.head_sha()
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_walk_first_parent_in_chronological_order(tmp_path: Path) -> None:
    repo = GitClient().discover(_make_repo(tmp_path))
    shas = list(repo.walk_first_parent("main"))
    assert len(shas) == 3
    subjects = [repo.get_commit(sha).subject for sha in shas]
    assert subjects == ["first", "second", "third"]


def test_get_commit_root_metadata(tmp_path: Path) -> None:
    repo = GitClient().discover(_make_repo(tmp_path))
    shas = list(repo.walk_first_parent("main"))
    first = repo.get_commit(shas[0])
    assert isinstance(first, Commit)
    assert first.subject == "first"
    assert first.author_name == "Test User"
    assert first.author_email == "test@example.com"
    assert first.parent_shas == ()
    assert first.stats == DiffStats(files_changed=1, insertions=1, deletions=0)


def test_get_commit_with_parent_diff_stats(tmp_path: Path) -> None:
    repo = GitClient().discover(_make_repo(tmp_path))
    shas = list(repo.walk_first_parent("main"))
    third = repo.get_commit(shas[2])
    assert third.subject == "third"
    assert len(third.parent_shas) == 1
    assert third.stats == DiffStats(files_changed=1, insertions=1, deletions=1)


def test_diff_stats_matches_get_commit(tmp_path: Path) -> None:
    repo = GitClient().discover(_make_repo(tmp_path))
    shas = list(repo.walk_first_parent("main"))
    assert repo.diff_stats(shas[2]) == repo.get_commit(shas[2]).stats


# ---- Repository: refs & remotes -----------------------------------------


def test_commit_count(tmp_path: Path) -> None:
    repo = GitClient().discover(_make_repo(tmp_path))
    assert repo.commit_count() == 3
    assert repo.commit_count("main") == 3


def test_branches_lists_local_branches(tmp_path: Path) -> None:
    raw = _make_repo(tmp_path)
    _git(raw, "branch", "feature/x")
    repo = GitClient().discover(raw)
    assert repo.branches() == ("feature/x", "main")


def test_tags_lists_tags(tmp_path: Path) -> None:
    raw = _make_repo(tmp_path)
    _git(raw, "tag", "v0.1.0")
    _git(raw, "tag", "v0.2.0")
    repo = GitClient().discover(raw)
    assert repo.tags() == ("v0.1.0", "v0.2.0")


def test_default_branch_main(tmp_path: Path) -> None:
    repo = GitClient().discover(_make_repo(tmp_path))
    assert repo.default_branch == "main"


def test_origin_url_none_when_no_remote(tmp_path: Path) -> None:
    repo = GitClient().discover(_make_repo(tmp_path))
    assert repo.origin_url is None


def test_origin_url_when_set(tmp_path: Path) -> None:
    raw = _make_repo(tmp_path)
    _git(raw, "remote", "add", "origin", "https://example.com/repo.git")
    repo = GitClient().discover(raw)
    assert repo.origin_url == "https://example.com/repo.git"


def test_remotes_mapping(tmp_path: Path) -> None:
    raw = _make_repo(tmp_path)
    _git(raw, "remote", "add", "origin", "https://example.com/o.git")
    _git(raw, "remote", "add", "upstream", "https://example.com/u.git")
    repo = GitClient().discover(raw)
    assert repo.remotes == {
        "origin": "https://example.com/o.git",
        "upstream": "https://example.com/u.git",
    }


def test_remotes_empty_when_none_configured(tmp_path: Path) -> None:
    repo = GitClient().discover(_make_repo(tmp_path))
    assert repo.remotes == {}
