"""High-level read-only view of a git repository on disk."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

from whygraph.core import Shell, ShellError

from .commands import GitCurrentBranchCmd, GitDiffCmd, GitOriginUrlCmd
from .commit import Commit
from .commits import Commits
from .exceptions import GitError


class Repository:
    """A git repository rooted at a specific working tree on disk.

    All methods shell out to ``git`` via the bound :class:`Shell`
    instance (each command supplies its own argv; ``cwd`` is :attr:`root`).
    Instances are cheap and effectively stateless beyond ``root`` plus
    a per-instance cache for stable attributes (:attr:`current_branch`,
    :attr:`commits`). If you need fresh values for those, construct a
    new instance — discarding instances is the supported invalidation
    strategy.

    Notes
    -----
    Cached properties are not thread-safe: simultaneous first access from
    multiple threads may run the underlying ``git`` subprocess more than
    once. The result is still correct; only one of the racing computations
    is retained in the cache.

    Parameters
    ----------
    root : Path
        The repository working tree.

    Attributes
    ----------
    root : Path
        The repository working tree (as supplied at construction).
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._shell = Shell()

    def __repr__(self) -> str:
        return f"Repository(root={self.root!r})"

    @cached_property
    def current_branch(self) -> str:
        """The name of the currently checked-out branch.

        Returns the literal string ``"HEAD"`` when the working tree is
        in a detached-HEAD state — that's still a valid commit-ish for
        :class:`Commits`, so callers rarely need to special-case it.

        Returns
        -------
        str
            The branch name (e.g. ``"main"``) or ``"HEAD"`` if detached.

        Raises
        ------
        GitError
            If ``git`` fails (not a repository, etc.).
        """
        try:
            return self._shell.run(GitCurrentBranchCmd, cwd=self.root)
        except ShellError as exc:
            raise GitError(f"failed to resolve current branch at {self.root}") from exc

    @cached_property
    def commits(self) -> Commits:
        """Reusable view of every commit reachable from :attr:`current_branch`.

        For commits on a different ref, construct :class:`Commits`
        directly: ``Commits(repo.root, "other-ref")``.

        Returns
        -------
        Commits
            A :class:`~collections.abc.Collection` over
            :class:`~whygraph.services.git.commit.Commit` instances,
            bound to this repository's :attr:`root` and current branch.
        """
        return Commits(self.root, self.current_branch)

    @cached_property
    def origin_url(self) -> str | None:
        """The configured ``origin`` remote URL, or ``None`` if unset.

        Used downstream by
        :meth:`whygraph.services.github.GitHubClient.for_repository` to
        derive ``owner/name``. A missing ``origin`` remote is a normal
        state (forks, local-only repos), not an error — so the property
        returns ``None`` instead of raising. Genuine git failures (the
        ``git`` binary is missing, ``self.root`` is not a repository at
        all) still surface as :class:`GitError`.

        Returns
        -------
        str or None
            The ``origin`` URL exactly as configured (no normalization),
            or ``None`` when no ``origin`` remote is set.

        Raises
        ------
        GitError
            If ``git`` itself cannot be invoked.
        """
        try:
            return self._shell.run(GitOriginUrlCmd, cwd=self.root, check=False)
        except ShellError as exc:
            raise GitError(f"failed to resolve origin URL at {self.root}") from exc

    def diff(self, commit: Commit) -> str:
        """Raw unified-diff text for ``commit`` against its first parent.

        Root commits (no parents) are diffed against the empty tree via
        ``git diff --no-color --root <sha>`` — git's documented form for
        "what does this commit introduce". Merge commits diff against
        their first parent, matching the convention already in use for
        :attr:`Commit.stats`.

        Returns
        -------
        str
            The raw ``git diff`` output. May be empty for a commit that
            touched no files (e.g. an empty merge); callers can treat
            ``""`` as "nothing to describe".

        Raises
        ------
        GitError
            If ``git`` itself fails (unknown sha, repository broken).
        """
        if not commit.parent_shas:
            argv = ("--root", commit.sha)
        else:
            argv = (f"{commit.parent_shas[0]}..{commit.sha}",)
        try:
            return self._shell.run(GitDiffCmd(*argv), cwd=self.root)
        except ShellError as exc:
            raise GitError(
                f"failed to diff {commit.sha[:7]} against its parent"
            ) from exc
