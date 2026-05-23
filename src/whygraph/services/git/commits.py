"""Reusable, lazy view of every commit reachable from a ref.

Exposes :class:`Commits` — a :class:`collections.abc.Collection` of
:class:`Commit` bound to one working-tree root and one ref. Each
``len(commits)`` and ``for c in commits`` re-shells out to ``git``, so
the collection is safe to iterate more than once and cheap to size
up-front (the count comes from ``git rev-list --count``, which is fast).

The intended consumer is the scan layer's ``GitCrawler``: ask for
``len()`` to size the progress bar, then iterate. The repo is assumed
static for the duration of a scan; ``__len__`` caches its result on the
instance accordingly.

Per-commit format and parsing live on :class:`Commit`
(``Commit.LOG_FORMAT`` and ``Commit.from_git_log``). This module owns
only the outer concerns: shelling out and slicing stdout into chunks.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from pathlib import Path

from whygraph.core import Shell, ShellError

from .commands import GitLogShortstatCmd, GitRevListCountCmd
from .commit import Commit
from .exceptions import GitError


class Commits(Collection[Commit]):
    """All commits reachable from a ref, as a reusable :class:`Collection`.

    The collection is bound to one working-tree root and one ref (branch
    name, tag, or SHA). Each ``__len__`` and ``__iter__`` call re-invokes
    ``git``; the repo is assumed static during a scan, so the count is
    cached on the instance after the first call.

    No filters (``--first-parent``, ``--no-merges``, path or date
    filters) are exposed yet — add them when a second caller actually
    needs one.

    The canonical entry point is :meth:`Repository.commits`. Direct
    construction is supported for callers that already hold a :class:`Path`.

    Parameters
    ----------
    root : Path
        The repository working-tree root (used as ``cwd`` for every
        ``git`` invocation).
    ref : str
        Any commit-ish ``git`` accepts (branch, tag, sha, ``HEAD``).
    shell : Shell, optional
        Override the subprocess wrapper used to run ``git``. Defaults to
        a fresh :class:`Shell` per instance. Mostly useful for tests.

    Attributes
    ----------
    root : Path
        The bound working-tree root.
    ref : str
        The ref being walked.

    Raises
    ------
    GitError
        From :meth:`__len__` or :meth:`__iter__` if ``git`` itself fails
        (unknown ref, not a repo, etc.). The original
        :class:`whygraph.core.ShellError` is preserved as ``__cause__``.
    """

    def __init__(
        self,
        root: Path,
        ref: str,
        *,
        shell: Shell | None = None,
    ) -> None:
        self.root = root
        self.ref = ref
        self._shell = shell or Shell()
        self._len_cache: int | None = None

    def __repr__(self) -> str:
        return f"Commits(root={self.root!r}, ref={self.ref!r})"

    def __len__(self) -> int:
        if self._len_cache is None:
            try:
                self._len_cache = self._shell.run(
                    GitRevListCountCmd(self.ref), cwd=self.root
                )
            except ShellError as exc:
                raise GitError(f"failed to count commits on {self.ref!r}") from exc
        return self._len_cache

    def __iter__(self) -> Iterator[Commit]:
        try:
            return self._shell.run(GitLogShortstatCmd(self.ref), cwd=self.root)
        except ShellError as exc:
            raise GitError(f"failed to enumerate commits on {self.ref!r}") from exc

    def __contains__(self, item: object) -> bool:
        if not isinstance(item, Commit):
            return False
        return any(c.sha == item.sha for c in self)
