"""Named ``git`` invocations as :class:`ShellCommand` argv+parser pairs.

Each command pairs an argv list with a typed parser so call sites can
do ``shell.run(GitFooCmd(...), cwd=...)`` and get a typed result back.
This is the single place to look for "which git commands does whygraph
run?" — the per-class docstring documents the underlying ``git`` syntax.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from subprocess import CompletedProcess

from whygraph.core import ShellCommand

from .blame import BlameHunk
from .commit import Commit

GitRevParseCmd = ShellCommand(
    argv=["git", "rev-parse", "--show-toplevel"],
    parse=lambda r: Path(r.stdout.strip()),
)
"""``git rev-parse --show-toplevel`` — the absolute path of the working tree root."""


GitCurrentBranchCmd = ShellCommand(
    argv=["git", "rev-parse", "--abbrev-ref", "HEAD"],
    parse=lambda r: r.stdout.strip(),
)
"""``git rev-parse --abbrev-ref HEAD`` — the current branch name, or ``"HEAD"`` if detached."""


def _parse_origin_url(result: CompletedProcess[str]) -> str | None:
    """Parse ``git remote get-url origin`` into a URL or ``None``.

    Returns ``None`` for a non-zero exit (no ``origin`` remote, not a
    repo) or empty stdout; the trimmed URL otherwise. Designed to be
    paired with ``check=False`` at the call site so a missing remote is
    a value, not an exception.
    """
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


GitOriginUrlCmd = ShellCommand(
    argv=["git", "remote", "get-url", "origin"],
    parse=_parse_origin_url,
)
"""``git remote get-url origin`` — the configured origin URL, or ``None`` if unset.

Must be run with ``check=False`` so the "no such remote" exit collapses
to ``None`` rather than raising :class:`whygraph.core.ShellError`.
"""


class GitRevListCountCmd(ShellCommand[int]):
    """``git rev-list --count <ref>`` — total commits reachable from ``ref``.

    Parameters
    ----------
    ref : str
        Any commit-ish ``git`` accepts (branch, tag, sha, ``HEAD``).
    """

    def __init__(self, ref: str) -> None:
        self.ref = ref

    def argv(self) -> list[str]:
        return ["git", "rev-list", "--count", self.ref]

    def parse(self, result: CompletedProcess[str]) -> int:
        return int(result.stdout.strip() or "0")


class GitDiffCmd(ShellCommand[str]):
    """``git diff --no-color <revspec...>`` — raw unified diff text.

    The argv after ``--no-color`` is passed verbatim; callers own the
    revspec semantics (``A..B``, ``<sha>^!``, ``--root <sha>``, …). The
    parser returns captured stdout unchanged — diff text *is* the
    interface, and any further structuring is the consumer's job.

    Parameters
    ----------
    revspec : tuple[str, ...]
        One or more arguments appended after ``--no-color``. Supplied
        as separate tokens so the call site does not have to worry
        about shell quoting (e.g. ``("--root", sha)`` for a root commit).
    """

    def __init__(self, *revspec: str) -> None:
        self.revspec = revspec

    def argv(self) -> list[str]:
        return ["git", "diff", "--no-color", *self.revspec]

    def parse(self, result: CompletedProcess[str]) -> str:
        return result.stdout


class GitBlameCmd(ShellCommand[tuple[BlameHunk, ...]]):
    """``git blame -L<a>,<b> --porcelain -- <path>`` — line ownership.

    Blames a contiguous line range of one file and parses the porcelain
    output into per-commit :class:`BlameHunk` records.

    Parameters
    ----------
    path : str
        File to blame, relative to the repository root.
    line_start : int
        First line of the range (1-based, inclusive).
    line_end : int
        Last line of the range (1-based, inclusive).
    """

    def __init__(self, path: str, line_start: int, line_end: int) -> None:
        self.path = path
        self.line_start = line_start
        self.line_end = line_end

    def argv(self) -> list[str]:
        return [
            "git",
            "blame",
            f"-L{self.line_start},{self.line_end}",
            "--porcelain",
            "--",
            self.path,
        ]

    def parse(self, result: CompletedProcess[str]) -> tuple[BlameHunk, ...]:
        return BlameHunk.from_porcelain(result.stdout)


class GitLogShortstatCmd(ShellCommand[Iterator[Commit]]):
    """``git log --shortstat --pretty=format:Commit.LOG_FORMAT <ref>``.

    Yields one :class:`Commit` per record in the captured stdout, newest
    first. Records are separated by :data:`Commit.LOG_FORMAT`'s leading
    ``\\x1e`` so ``str.split`` recovers them in one pass.

    Parameters
    ----------
    ref : str
        Any commit-ish ``git`` accepts.
    """

    def __init__(self, ref: str) -> None:
        self.ref = ref

    def argv(self) -> list[str]:
        return [
            "git",
            "log",
            f"--pretty=format:{Commit.LOG_FORMAT}",
            "--shortstat",
            self.ref,
        ]

    def parse(self, result: CompletedProcess[str]) -> Iterator[Commit]:
        for block in result.stdout.split("\x1e"):
            if block.strip():
                yield Commit.from_git_log(block)
