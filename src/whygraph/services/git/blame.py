"""In-memory value object for ``git blame`` output — one hunk per commit.

Exposes :class:`BlameHunk` plus the parser that builds a tuple of them from
``git blame --porcelain`` stdout. The parser lives here (not on
:class:`~whygraph.services.git.Repository`) so that "what blame output looks
like" is owned by the class that represents it — the same pattern
:meth:`whygraph.services.git.Commit.from_git_log` follows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# Git's sentinel SHA for lines that are not yet committed (local edits).
_UNCOMMITTED_SHA = "0" * 40


@dataclass(frozen=True, slots=True)
class BlameHunk:
    """The lines a single commit owns within a blamed range.

    ``git blame --porcelain`` reports one record per source line; this
    aggregates every line attributed to the same commit into one hunk, so a
    blamed range of N lines spanning M commits yields M hunks.

    Attributes
    ----------
    sha : str
        Full commit SHA. The all-zero SHA marks lines that are not yet
        committed (uncommitted local edits) — see :attr:`is_uncommitted`.
    lines_owned : int
        How many lines of the blamed range this commit is responsible for.
    author_name : str or None
        Commit author display name, when git reported it.
    author_email : str or None
        Commit author email, when git reported it.
    summary : str or None
        First line of the commit message, when git reported it.
    committed_at : str or None
        ISO 8601 UTC timestamp of when the commit was applied, derived from
        the porcelain ``committer-time`` epoch.
    """

    sha: str
    lines_owned: int
    author_name: str | None
    author_email: str | None
    summary: str | None
    committed_at: str | None

    @property
    def is_uncommitted(self) -> bool:
        """``True`` when this hunk covers uncommitted local edits."""
        return self.sha == _UNCOMMITTED_SHA

    @classmethod
    def from_porcelain(cls, stdout: str) -> tuple["BlameHunk", ...]:
        """Parse ``git blame --porcelain`` output into per-commit hunks.

        In porcelain format every source line emits a header line —
        ``<sha> <orig-line> <final-line> [<group-size>]`` — but the metadata
        block (``author``, ``summary``, ``committer-time``, …) is emitted
        only the *first* time a SHA appears. This parser carries the
        per-SHA metadata forward and tallies one owned line per header.

        Parameters
        ----------
        stdout : str
            Raw stdout of ``git blame --porcelain``.

        Returns
        -------
        tuple[BlameHunk, ...]
            One hunk per distinct commit, in first-appearance order.
        """
        hunks: dict[str, dict] = {}
        order: list[str] = []
        current: str | None = None
        for line in stdout.splitlines():
            if line.startswith("\t"):
                # Source-line content, not metadata.
                continue
            parts = line.split(" ")
            if (
                len(parts) >= 3
                and len(parts[0]) == 40
                and parts[1].isdigit()
                and parts[2].isdigit()
            ):
                current = parts[0]
                entry = hunks.get(current)
                if entry is None:
                    entry = {
                        "sha": current,
                        "lines_owned": 0,
                        "author_name": None,
                        "author_email": None,
                        "summary": None,
                        "committed_at": None,
                    }
                    hunks[current] = entry
                    order.append(current)
                entry["lines_owned"] += 1
                continue
            if current is None:
                continue
            entry = hunks[current]
            if line.startswith("author "):
                entry["author_name"] = line[len("author ") :].strip() or None
            elif line.startswith("author-mail "):
                mail = line[len("author-mail ") :].strip()
                if mail.startswith("<") and mail.endswith(">"):
                    mail = mail[1:-1]
                entry["author_email"] = mail or None
            elif line.startswith("summary "):
                entry["summary"] = line[len("summary ") :].strip() or None
            elif line.startswith("committer-time "):
                epoch = line[len("committer-time ") :].strip()
                if epoch.isdigit():
                    entry["committed_at"] = datetime.fromtimestamp(
                        int(epoch), tz=timezone.utc
                    ).isoformat()
        return tuple(BlameHunk(**hunks[sha]) for sha in order)
