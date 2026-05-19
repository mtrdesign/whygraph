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
