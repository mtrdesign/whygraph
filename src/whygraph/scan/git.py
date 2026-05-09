"""Git plumbing wrappers for the scan pipeline."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

_LOG_FORMAT = "%H%x1f%P%x1f%an%x1f%ae%x1f%aI%x1f%cI%x1f%s%x1f%b"


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Commit:
    sha: str
    parent_shas: list[str]
    author_name: str
    author_email: str
    authored_at: str
    committed_at: str
    subject: str
    body: str
    files_changed: int
    insertions: int
    deletions: int


def _run_git(repo_root: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def repo_root(start: Path) -> Path:
    out = _run_git(start, ["rev-parse", "--show-toplevel"])
    return Path(out.strip())


def default_branch(repo_root: Path) -> str:
    try:
        out = _run_git(repo_root, ["symbolic-ref", "refs/remotes/origin/HEAD"])
        return out.strip().rsplit("/", 1)[-1]
    except GitError:
        pass
    for candidate in ("main", "master"):
        try:
            _run_git(repo_root, ["rev-parse", "--verify", "--quiet", candidate])
            return candidate
        except GitError:
            continue
    raise GitError("could not determine default branch (no origin/HEAD, no main, no master)")


def walk_first_parent(repo_root: Path, branch: str) -> Iterator[str]:
    out = _run_git(
        repo_root,
        ["log", "--first-parent", "--reverse", "--format=%H", branch],
    )
    for line in out.splitlines():
        sha = line.strip()
        if sha:
            yield sha


def get_commit(repo_root: Path, sha: str) -> Commit:
    out = _run_git(repo_root, ["log", "-1", f"--format={_LOG_FORMAT}", sha])
    fields = out.rstrip("\n").split("\x1f")
    if len(fields) != 8:
        raise GitError(f"unexpected log output for {sha}: got {len(fields)} fields")
    sha_, parents_raw, author_name, author_email, authored, committed, subject, body = fields
    parent_shas = parents_raw.split() if parents_raw else []
    files_changed, insertions, deletions = get_diff_stats(repo_root, sha_)
    return Commit(
        sha=sha_,
        parent_shas=parent_shas,
        author_name=author_name,
        author_email=author_email,
        authored_at=authored,
        committed_at=committed,
        subject=subject,
        body=body,
        files_changed=files_changed,
        insertions=insertions,
        deletions=deletions,
    )


def get_diff_stats(repo_root: Path, sha: str) -> tuple[int, int, int]:
    """Return (files_changed, insertions, deletions) for a commit vs. its first parent."""
    out = _run_git(repo_root, ["show", "--shortstat", "--format=", sha])
    return _parse_shortstat(out)


def _parse_shortstat(out: str) -> tuple[int, int, int]:
    files = insertions = deletions = 0
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.search(r"(\d+) files? changed", line)
        if m:
            files = int(m.group(1))
        m = re.search(r"(\d+) insertions?", line)
        if m:
            insertions = int(m.group(1))
        m = re.search(r"(\d+) deletions?", line)
        if m:
            deletions = int(m.group(1))
    return files, insertions, deletions
