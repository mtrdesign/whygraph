"""Git service: low-level client plus typed repository view.

Public API
----------
* :class:`GitClient` — the entry point for running git commands. Holds
  subprocess config and constructs :class:`Repository` instances.
* :class:`Repository` — typed read-only view of a working tree, with
  semantic methods (head, history, diff stats, branches/tags, remotes).
* :class:`Commit`, :class:`DiffStats`, :class:`CommitSummary`,
  :class:`BlameHunk` — value objects returned by :class:`Repository` and
  consumed by the github service (the commit ones, for PR commit lists).
* :class:`GitError` — raised on any git failure (missing binary,
  non-zero exit, malformed output).
"""

from .blame import BlameHunk
from .commit import Commit, CommitSummary, DiffStats
from .commits import Commits
from .exceptions import GitError
from .file_change import FileChange
from .repository import Repository

__all__ = [
    "BlameHunk",
    "Commit",
    "CommitSummary",
    "Commits",
    "DiffStats",
    "FileChange",
    "GitError",
    "Repository",
]
