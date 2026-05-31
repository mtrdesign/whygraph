"""Tests for the ``FileChange`` parser and the per-commit file-change command.

The parser is unit-tested against synthetic ``git diff-tree --raw
--numstat`` blobs; :meth:`Repository.commit_file_changes` is exercised
against a real throwaway repo so the integration of flags, rename
detection, and parsing all run end to end.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


from whygraph.services.git import FileChange, Repository
from whygraph.services.git.file_change import (
    _collapse_rename_arrow,
    _parse_raw_line,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "tester@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "commit.gpgsign", "false")
    return root


def test_parse_raw_line_modification() -> None:
    rec = _parse_raw_line(":100644 100644 abc1234 def5678 M\tsrc/foo.py")
    assert rec == {
        "change_type": "M",
        "renamed_from": None,
        "similarity": None,
        "path": "src/foo.py",
    }


def test_parse_raw_line_rename_with_similarity() -> None:
    rec = _parse_raw_line(":100644 100644 abc1234 def5678 R98\tsrc/old.py\tsrc/new.py")
    assert rec == {
        "change_type": "R",
        "renamed_from": "src/old.py",
        "similarity": 98,
        "path": "src/new.py",
    }


def test_collapse_rename_arrow_plain_form() -> None:
    assert _collapse_rename_arrow("old.py => new.py") == "new.py"


def test_collapse_rename_arrow_brace_form_preserves_surrounding_dirs() -> None:
    assert _collapse_rename_arrow("src/{old.py => new.py}") == "src/new.py"


def test_collapse_rename_arrow_unchanged_path_passes_through() -> None:
    assert _collapse_rename_arrow("src/foo.py") == "src/foo.py"


def test_from_diff_tree_pairs_raw_with_numstat() -> None:
    stdout = (
        ":100644 100644 abc1234 def5678 M\tsrc/foo.py\n"
        ":000000 100644 0000000 1234567 A\tsrc/bar.py\n"
        "\n"
        "10\t5\tsrc/foo.py\n"
        "3\t0\tsrc/bar.py\n"
    )

    changes = FileChange.from_diff_tree(stdout)

    by_path = {c.path: c for c in changes}
    assert by_path["src/foo.py"] == FileChange(
        path="src/foo.py",
        change_type="M",
        renamed_from=None,
        similarity=None,
        lines_added=10,
        lines_deleted=5,
    )
    assert by_path["src/bar.py"] == FileChange(
        path="src/bar.py",
        change_type="A",
        renamed_from=None,
        similarity=None,
        lines_added=3,
        lines_deleted=0,
    )


def test_from_diff_tree_resolves_rename_numstat_via_brace_form() -> None:
    stdout = (
        ":100644 100644 abc1234 def5678 R96\tsrc/old.py\tsrc/new.py\n"
        "\n"
        "2\t1\tsrc/{old.py => new.py}\n"
    )

    changes = FileChange.from_diff_tree(stdout)

    assert changes == (
        FileChange(
            path="src/new.py",
            change_type="R",
            renamed_from="src/old.py",
            similarity=96,
            lines_added=2,
            lines_deleted=1,
        ),
    )


def test_from_diff_tree_treats_binary_numstat_as_zero() -> None:
    stdout = (
        ":100644 100644 abc1234 def5678 M\tassets/img.png\n\n-\t-\tassets/img.png\n"
    )

    changes = FileChange.from_diff_tree(stdout)

    assert changes[0].lines_added == 0
    assert changes[0].lines_deleted == 0


def test_from_diff_tree_empty_input_yields_no_changes() -> None:
    assert FileChange.from_diff_tree("") == ()


def test_repository_commit_file_changes_detects_rename(tmp_path: Path) -> None:
    """A real rename in a throwaway repo surfaces with renamed_from populated."""
    root = _make_repo(tmp_path)
    (root / "old.py").write_text("import sys\n\nprint('hello')\n")
    _git(root, "add", "old.py")
    _git(root, "commit", "-m", "feat: add module")
    _git(root, "mv", "old.py", "new.py")
    _git(root, "commit", "-m", "refactor: rename module")

    repo = Repository(root)
    rename_commit = next(iter(repo.commits))  # newest first

    changes = repo.commit_file_changes(rename_commit)

    assert len(changes) == 1
    change = changes[0]
    assert change.change_type == "R"
    assert change.path == "new.py"
    assert change.renamed_from == "old.py"
    # `git mv` produces a 100% similarity match.
    assert change.similarity == 100


def test_repository_commit_file_changes_records_root_commit(tmp_path: Path) -> None:
    """The very first commit's adds surface as ``A`` against the empty tree."""
    root = _make_repo(tmp_path)
    (root / "main.py").write_text("print('hi')\n")
    _git(root, "add", "main.py")
    _git(root, "commit", "-m", "initial")

    repo = Repository(root)
    initial = next(iter(repo.commits))

    changes = repo.commit_file_changes(initial)

    assert changes[0].change_type == "A"
    assert changes[0].path == "main.py"
    assert changes[0].lines_added == 1
