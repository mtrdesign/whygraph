from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommitMeta:
    sha: str
    author_time: int
    author: str


def _git(repo_root: Path, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def head_sha(repo_root: Path) -> str:
    out = _git(repo_root, ["rev-parse", "HEAD"])
    return (out or "").strip()


def commits_touching_file(repo_root: Path, file_path: str) -> list[str]:
    """Shas of non-merge commits whose diff touches `file_path`. Newest first.

    Uses `--follow` so a renamed file's history walks back through the rename.
    """
    out = _git(
        repo_root,
        ["log", "--no-merges", "--follow", "--pretty=format:%H", "--", file_path],
    )
    if not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def files_in_commit(repo_root: Path, sha: str) -> list[str]:
    """Files touched by a single commit (non-merge). Empty list on failure."""
    out = _git(
        repo_root,
        ["show", "--no-merges", "--name-only", "--pretty=format:", sha],
    )
    if not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def commits_with_metadata_for_file(
    repo_root: Path, file_path: str
) -> list[CommitMeta]:
    """Per-commit metadata for volatility — sha, author timestamp, author name.

    Excludes merges (same rationale as `commits_touching_file`). Uses a NUL
    record separator and tab field separator so commit messages with embedded
    newlines can't corrupt the parse.
    """
    fmt = "--pretty=format:%H%x09%at%x09%an%x00"
    out = _git(
        repo_root,
        ["log", "--no-merges", "--follow", fmt, "--", file_path],
    )
    if not out:
        return []
    records: list[CommitMeta] = []
    for raw in out.split("\x00"):
        line = raw.strip("\n").strip()
        if not line:
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        sha, ts_str, author = parts
        try:
            ts = int(ts_str)
        except ValueError:
            continue
        records.append(CommitMeta(sha=sha, author_time=ts, author=author))
    return records
