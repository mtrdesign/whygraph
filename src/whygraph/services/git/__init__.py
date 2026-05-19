"""Git service: low-level client plus typed repository view.

Public API
----------
* :class:`GitClient` — the entry point for running git commands. Holds
  subprocess config and constructs :class:`Repository` instances.
* :class:`Repository` — typed read-only view of a working tree, with
  semantic methods (head, history, diff stats, branches/tags, remotes).
* :class:`Commit`, :class:`DiffStats` — value objects returned by
  :class:`Repository`.
* :class:`GitError` — raised on any git failure (missing binary,
  non-zero exit, malformed output).
"""

from .commit import Commit, DiffStats
from .commits import Commits
from .exceptions import GitError
from .repository import Repository

__all__ = [
    "Commit",
    "Commits",
    "DiffStats",
    "GitError",
    "Repository",
]
