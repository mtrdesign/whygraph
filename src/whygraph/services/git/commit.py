"""In-memory value objects for a parsed Git commit.

Exposes :class:`DiffStats` and :class:`Commit` plus the ``git log``
format string and per-record parser that produce them. The parser
lives here (not in the :mod:`Commits` collection) so that "what one
git-log record looks like" is owned by the class that represents it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar

_COMMIT_SEP = "\x1e"
_FIELD_SEP = "\x1f"

_SHORTSTAT_FILES_RE = re.compile(r"(\d+)\s+files?\s+changed")
_SHORTSTAT_INS_RE = re.compile(r"(\d+)\s+insertions?\(\+\)")
_SHORTSTAT_DEL_RE = re.compile(r"(\d+)\s+deletions?\(-\)")


@dataclass(frozen=True, slots=True)
class DiffStats:
    """Aggregate diff statistics for a single commit.

    Attributes
    ----------
    files_changed : int
        Number of files touched by the commit.
    insertions : int
        Lines added.
    deletions : int
        Lines removed.
    """

    files_changed: int
    insertions: int
    deletions: int


@dataclass(frozen=True, slots=True)
class Commit:
    """A single Git commit with diff statistics.

    Attributes
    ----------
    sha : str
        Full commit hash.
    parent_shas : tuple[str, ...]
        Parent commit hashes (empty for the root commit, multiple for merges).
    author_name : str
        Author display name.
    author_email : str
        Author email.
    authored_at : str
        ISO 8601 timestamp of when the change was written. Survives
        rebase/cherry-pick unchanged.
    committed_at : str
        ISO 8601 timestamp of when the commit was applied to the repo.
        Updated on rebase/cherry-pick.
    subject : str
        First line of the commit message.
    body : str
        Everything after the subject's blank-line separator (may be empty).
    stats : DiffStats
        Diff totals against the first parent.
    """

    sha: str
    parent_shas: tuple[str, ...]
    author_name: str
    author_email: str
    authored_at: str
    committed_at: str
    subject: str
    body: str
    stats: DiffStats

    LOG_FORMAT: ClassVar[str] = (
        f"{_COMMIT_SEP}%H{_FIELD_SEP}%P{_FIELD_SEP}"
        f"%an{_FIELD_SEP}%ae{_FIELD_SEP}"
        f"%aI{_FIELD_SEP}%cI{_FIELD_SEP}"
        f"%s{_FIELD_SEP}%b{_FIELD_SEP}"
    )
    """``--pretty=format`` string consumed by :meth:`from_git_log`.

    Each commit's record starts with the ASCII record-separator
    (``\\x1e``) and consists of nine fields delimited by the unit
    separator (``\\x1f``). The trailing ``_FIELD_SEP`` after ``%b``
    makes the shortstat line (added by ``--shortstat``) land as the
    9th split token, so a single ``str.split(_FIELD_SEP)`` recovers
    every field plus stats in one pass.
    """

    @classmethod
    def from_git_log(cls, block: str) -> "Commit":
        """Parse one ``git log`` record into a :class:`Commit`.

        ``block`` is one chunk of the output of
        ``git log --shortstat --pretty=format:Commit.LOG_FORMAT``,
        obtained by splitting the full stdout on :data:`_COMMIT_SEP`
        and discarding any empty leading chunk.

        Parameters
        ----------
        block : str
            A single commit's record: the format-block followed by an
            optional shortstat line. Must contain the full set of
            fields produced by :attr:`LOG_FORMAT`.

        Returns
        -------
        Commit
            The parsed commit.

        Raises
        ------
        ValueError
            If ``block`` does not contain the expected number of
            field-separated tokens. The format is owned by this class,
            so malformed input indicates a bug rather than user input.
        """
        parts = block.split(_FIELD_SEP)
        if len(parts) < 9:
            raise ValueError(f"malformed git log block: {block!r}")
        sha, parents, an, ae, authored, committed, subject, body, tail = parts[:9]
        return cls(
            sha=sha,
            parent_shas=tuple(p for p in parents.split() if p),
            author_name=an,
            author_email=ae,
            authored_at=authored,
            committed_at=committed,
            subject=subject,
            body=body,
            stats=_parse_shortstat(tail),
        )


def _parse_shortstat(line: str) -> DiffStats:
    """Parse a single ``git diff/log --shortstat`` summary line into :class:`DiffStats`.

    Accepts the trailing newline that git emits. Missing pieces (e.g.
    insertions-only or deletions-only lines, merge commits with no stats)
    default to zero. An empty or whitespace-only input maps to
    ``DiffStats(0, 0, 0)`` — matches git's behaviour for root commits and
    merge commits under the default ``git log --shortstat``.

    Parameters
    ----------
    line : str
        The shortstat line as captured from ``git`` (with or without
        surrounding whitespace and newlines).

    Returns
    -------
    DiffStats
        Parsed counts; zero for any component the line omits.
    """
    if not line.strip():
        return DiffStats(0, 0, 0)
    files = _SHORTSTAT_FILES_RE.search(line)
    ins = _SHORTSTAT_INS_RE.search(line)
    dels = _SHORTSTAT_DEL_RE.search(line)
    return DiffStats(
        files_changed=int(files.group(1)) if files else 0,
        insertions=int(ins.group(1)) if ins else 0,
        deletions=int(dels.group(1)) if dels else 0,
    )
