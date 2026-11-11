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


# ---- plan 49 section 5.2: whole-file blame, ignore-revs, the core helpers ----


def _commit(root: Path, message: str, files: dict[str, str | bytes]) -> str:
    """Write ``files`` (text or raw bytes), commit them, return the SHA."""
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)
        _git(root, "add", "--", rel)
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD").strip()


def test_blame_cmd_whole_file_has_no_line_range() -> None:
    argv = GitBlameCmd("src/foo.py", whole_file=True, rev="HEAD").argv()
    assert not any(a.startswith("-L") for a in argv)
    assert argv[-3:] == ["HEAD", "--", "src/foo.py"]
    with pytest.raises(ValueError):
        GitBlameCmd("src/foo.py", 1, 2, whole_file=True)
    with pytest.raises(ValueError):
        GitBlameCmd("src/foo.py", ranges=[(1, 2)], whole_file=True)


def test_whole_file_blame_returns_every_owner(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    first = _commit(root, "one", {"f.py": "a\nb\n"})
    second = _commit(root, "two", {"f.py": "a\nb\nc\n"})
    (root / "f.py").write_text("a\nb\nc\nuncommitted\n")

    hunks = Repository(root).blame("f.py", whole_file=True, rev="HEAD")

    assert {h.sha for h in hunks} == {first, second}
    assert sum(h.lines_owned for h in hunks) == 3


def test_a_latin1_file_blames(tmp_path: Path) -> None:
    """Undecodable content is replaced, not raised (``UnicodeDecodeError``)."""
    root = _make_repo(tmp_path)
    sha = _commit(root, "latin-1", {"legacy.txt": b"caf\xe9\nna\xefve\n"})
    repo = Repository(root)

    assert {h.sha for h in repo.blame("legacy.txt", 1, 2)} == {sha}
    assert {h.sha for h in repo.blame("legacy.txt", whole_file=True)} == {sha}


def test_a_blame_timeout_is_a_git_error(
    temp_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def times_out(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise subprocess.TimeoutExpired(cmd="git blame", timeout=1)

    monkeypatch.setattr(subprocess, "run", times_out)
    with pytest.raises(GitError, match="timed out"):
        Repository(temp_git_repo).blame("sample.py", whole_file=True, timeout=1)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("not-a-sha\n", id="malformed"),
        pytest.param("abc123\n", id="abbreviated"),
        pytest.param("".join(f"{i:040x}\n" for i in range(1, 1002)), id="too-many"),
        pytest.param(f"{'a' * 40}\n# {'x' * 70_000}\n", id="oversized"),
        pytest.param(b"\xff\xfe\n", id="not-utf8"),
    ],
)
def test_an_unusable_ignore_revs_file_is_ignored(
    tmp_path: Path, content: str | bytes
) -> None:
    """Git would exit 128 on such a file: it is dropped and blame still works."""
    root = _make_repo(tmp_path)
    sha = _commit(root, "one", {"f.py": "x = 1\n"})
    ignore = root / ".git-blame-ignore-revs"
    if isinstance(content, bytes):
        ignore.write_bytes(content)
    else:
        ignore.write_text(content)
    repo = Repository(root)

    assert repo.blame_ignore_revs_file() is None
    assert repo.blame("f.py", 1, 1)[0].sha == sha


def test_a_valid_ignore_revs_file_keeps_comments_and_both_hash_sizes(
    tmp_path: Path,
) -> None:
    root = _make_repo(tmp_path)
    _commit(root, "one", {"f.py": "x = 1\n"})
    ignore = root / ".git-blame-ignore-revs"
    ignore.write_text(
        "# formatting sweeps\n"
        f"{'a' * 40}  # black\n"
        "\n"
        f"   {'B' * 64}\n" + "".join(f"{i:040x}\n" for i in range(1, 999))
    )

    assert Repository(root).blame_ignore_revs_file() == ignore


def test_tree_entries_recursive_lists_nested_blobs(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    _commit(root, "one", {"top.txt": "t\n", "pkg/sub/deep.py": "d\n"})
    repo = Repository(root)

    flat = {e.name: e.type for e in repo.tree_entries("HEAD")}
    deep = {e.name: e.type for e in repo.tree_entries("HEAD", recursive=True)}

    assert flat == {"pkg": "tree", "top.txt": "blob"}
    assert deep == {"pkg/sub/deep.py": "blob", "top.txt": "blob"}


def test_paths_changed_between_covers_both_sides_and_non_ascii(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    base = _commit(root, "base", {"a.txt": "a\n", "old.txt": "o\n" * 20})
    _git(root, "checkout", "-q", "-b", "side")
    side = _commit(root, "side", {"b.txt": "b\n"})
    _git(root, "checkout", "-q", base)
    _git(root, "mv", "old.txt", "new.txt")
    _commit(root, "main", {"ünï cödé.txt": "u\n"})
    repo = Repository(root)

    changed = repo.paths_changed_between(side, "HEAD")

    # ``a...b`` is symmetric; ``--no-renames`` names both ends of a move.
    assert changed == {"b.txt", "old.txt", "new.txt", "ünï cödé.txt"}
    assert repo.paths_changed_between("HEAD", "HEAD") == set()
    with pytest.raises(GitError):
        repo.paths_changed_between("0" * 40, "HEAD")
    with pytest.raises(GitError):
        repo.paths_changed_between("--all", "HEAD")


def test_blob_info_reports_size_binary_and_lines(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    sha = _commit(
        root,
        "one",
        {
            "text.txt": "a\nb\nc\n",
            "no_eol.txt": "a\nb",
            "empty.txt": "",
            "bin.dat": b"\x00\x01\x02\n",
        },
    )
    repo = Repository(root)

    info = repo.blob_info(
        [
            f"{sha}:text.txt",
            "HEAD:no_eol.txt",
            "HEAD:empty.txt",
            "HEAD:bin.dat",
            "HEAD:missing.txt",
            "HEAD",
            "HEAD:bad\nname",
        ]
    )

    text = info[f"{sha}:text.txt"]
    assert text is not None
    assert (text.size, text.binary, text.lines) == (6, False, 3)
    assert len(text.sha) == 40
    assert info["HEAD:no_eol.txt"].lines == 2
    assert (info["HEAD:empty.txt"].size, info["HEAD:empty.txt"].lines) == (0, 0)
    assert info["HEAD:bin.dat"].binary is True
    assert info["HEAD:missing.txt"] is None
    assert info["HEAD"] is None  # a commit, not a blob
    assert info["HEAD:bad\nname"] is None
    assert repo.blob_info([]) == {}


def test_blob_info_streams_a_batch_larger_than_the_pipes(tmp_path: Path) -> None:
    """Names and content both far beyond a pipe buffer: no deadlock."""
    root = _make_repo(tmp_path)
    big = "".join(f"line {i}\n" for i in range(50_000))
    _commit(root, "big", {"big.txt": big})
    repo = Repository(root)
    names = ["HEAD:big.txt"] * 1 + [f"HEAD:nope{i}.txt" for i in range(5_000)]

    info = repo.blob_info(names)

    assert info["HEAD:big.txt"].lines == 50_000
    assert info["HEAD:big.txt"].size == len(big)
    assert sum(1 for v in info.values() if v is None) == 5_000
