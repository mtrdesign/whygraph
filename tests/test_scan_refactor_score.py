"""Tests for the refactor-score heuristic."""

from __future__ import annotations

from whygraph.scan.refactor_score import BORING_THRESHOLD, compute_refactor_score
from whygraph.services.git import FileChange


def _change(
    path: str,
    change_type: str = "M",
    renamed_from: str | None = None,
) -> FileChange:
    return FileChange(
        path=path,
        change_type=change_type,
        renamed_from=renamed_from,
        similarity=100 if change_type in ("R", "C") else None,
        lines_added=1,
        lines_deleted=0,
    )


def test_small_modification_is_not_boring() -> None:
    score = compute_refactor_score(
        subject="feat: add login form",
        file_changes=[_change(f"src/file{i}.py") for i in range(3)],
    )

    assert score < BORING_THRESHOLD


def test_many_files_alone_is_below_threshold() -> None:
    """Twenty-plus files is suspicious but not enough on its own."""
    score = compute_refactor_score(
        subject="feat: big feature",
        file_changes=[_change(f"src/file{i}.py") for i in range(25)],
    )

    assert score < BORING_THRESHOLD


def test_refactor_subject_plus_many_files_crosses_threshold() -> None:
    score = compute_refactor_score(
        subject="refactor: split auth module",
        file_changes=[_change(f"src/file{i}.py") for i in range(25)],
    )

    assert score >= BORING_THRESHOLD


def test_pure_rename_commit_with_boring_subject_is_boring() -> None:
    score = compute_refactor_score(
        subject="chore: rename modules",
        file_changes=[
            _change(f"src/new{i}.py", change_type="R", renamed_from=f"src/old{i}.py")
            for i in range(5)
        ],
    )

    assert score >= BORING_THRESHOLD


def test_mass_formatter_run_is_boring() -> None:
    score = compute_refactor_score(
        subject="style: prettier sweep",
        file_changes=[_change(f"src/file{i}.py") for i in range(60)],
    )

    assert score >= BORING_THRESHOLD
    assert score <= 100
