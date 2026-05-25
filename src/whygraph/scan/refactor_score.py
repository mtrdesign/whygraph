"""Heuristic "is this commit a refactor?" score, 0–100.

The score combines three signals that experience says correlate with
"this commit's lines should not be trusted as authorship attribution":

* **Diff breadth** — a commit touching dozens of files is usually a
  mass rename, formatter sweep, or scaffolding move, not a single
  author writing one feature.
* **Rename ratio** — when most of the diff is ``R`` / ``C`` rows, the
  commit moved code rather than authored it.
* **Conventional-commit prefix** — ``style:`` / ``chore:`` /
  ``refactor:`` / ``format:`` / ``lint:`` subjects are usually authored
  with "this is mechanical, please ignore" intent.

The :data:`BORING_THRESHOLD` is the value the evidence collector uses to
decide whether to walk past a blame hit. Numbers and weights here are
deliberately conservative — a commit needs *two* signals before it
crosses the threshold, so a single 30-file commit (which might still be
real authorship) is not flagged on its own.
"""

from __future__ import annotations

from whygraph.services.git import FileChange

# A commit with score >= this is treated as a "refactor" for the
# purposes of blame walk-past. Exposed as a module constant so tests
# and downstream tunings reference the same number.
BORING_THRESHOLD = 50

_BORING_PREFIXES = (
    "style:",
    "chore:",
    "refactor:",
    "format:",
    "lint:",
)


def compute_refactor_score(
    *,
    subject: str,
    file_changes: tuple[FileChange, ...] | list[FileChange],
) -> int:
    """Score how likely ``subject`` + ``file_changes`` describe a refactor.

    Parameters
    ----------
    subject : str
        The commit subject line. Conventional-commit prefixes are
        matched case-insensitively against :data:`_BORING_PREFIXES`.
    file_changes : Sequence[FileChange]
        Per-file structural records for the commit (i.e. the same rows
        the crawler is about to persist to ``commit_file_change``).

    Returns
    -------
    int
        Clamped to ``[0, 100]``.
    """
    score = 0

    n_files = len(file_changes)
    if n_files >= 50:
        score += 60
    elif n_files >= 20:
        score += 30
    elif n_files >= 10:
        score += 10

    if n_files > 0:
        renames = sum(
            1 for ch in file_changes if ch.change_type in ("R", "C")
        )
        ratio = renames / n_files
        if ratio >= 0.8:
            score += 40
        elif ratio >= 0.5:
            score += 20

    head = subject.strip().lower()
    if any(head.startswith(p) for p in _BORING_PREFIXES):
        score += 30

    return min(score, 100)
