"""High-level read-only view of a git repository on disk."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from functools import cached_property
from pathlib import Path

from whygraph.core import Shell, ShellError

from .blame import BlameHunk
from .commands import (
    GitBlameCmd,
    GitCheckMailmapCmd,
    GitCheckoutResetCmd,
    GitCloneCmd,
    GitCurrentBranchCmd,
    GitDiffCmd,
    GitDiffTreeFileChangesCmd,
    GitFastForwardCmd,
    GitFetchDefaultCmd,
    GitFetchRefsCmd,
    GitHeadShaCmd,
    GitIsAncestorCmd,
    GitIsShallowCmd,
    GitLogCommitCmd,
    GitLsTreeNamesCmd,
    GitRefExistsCmd,
    GitRemoteUrlCmd,
    GitResolveCommitCmd,
    GitRevListShasCmd,
    GitSetRemoteHeadCmd,
    GitSetRemoteUrlCmd,
    GitSymbolicRefCmd,
)
from .commit import Commit
from .commits import Commits
from .credentials import (
    TOKEN_ENV_VAR,
    TOKEN_FILE_ENV,
    git_env,
    parse_github_url,
    redact_tokens,
)
from .exceptions import GitError
from .file_change import FileChange

# Git's well-known empty-tree object (SHA-1 repositories). Diffing a root
# commit against it yields exactly what that commit introduced. Note that
# ``git diff --root <sha>`` does NOT do this — ``--root`` is a no-op for
# plain ``git diff``, which then compares ``<sha>`` to the working tree.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Conventional filename for a project-level blame skip list. Git also reads
# it via the ``blame.ignoreRevsFile`` config setting, but WhyGraph wires it
# explicitly so behaviour does not depend on the user's local git config.
_BLAME_IGNORE_REVS_FILE = ".git-blame-ignore-revs"

# Contacts per ``git check-mailmap`` invocation. Keeps argv well under
# ARG_MAX on every platform even for a repository with thousands of
# distinct identities; the results are concatenated in input order.
_MAILMAP_CHUNK = 500

# Short branch names probed, in order, when ``refs/remotes/<remote>/HEAD``
# is unset — which is the common case, since a plain ``git clone`` sets it
# but a plain ``git fetch`` into an existing repo does not.
_DEFAULT_BRANCH_CANDIDATES = ("main", "master")

# Wall-clock cap for a clone or fetch. ``Shell.timeout`` defaults to 30 s,
# which kills a real clone of anything non-trivial.
_NETWORK_TIMEOUT = 900


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
    origin_remote : str, optional
        Name of the git remote :attr:`origin_url` reads. Default
        ``"origin"``; override to inspect a differently-named remote
        (e.g. ``"upstream"``).
    default_branch : str or None, optional
        Short name of the branch to treat as the default (e.g.
        ``"develop"``), overriding :attr:`default_branch_refs`'
        auto-resolution outright. ``None`` (default) auto-resolves, which
        is right for the overwhelming majority of repositories — see
        that property for the chain.

    Attributes
    ----------
    root : Path
        The repository working tree (as supplied at construction).
    """

    def __init__(
        self,
        root: Path,
        *,
        origin_remote: str = "origin",
        default_branch: str | None = None,
    ) -> None:
        self.root = root
        self._origin_remote = origin_remote
        self._default_branch = default_branch
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
        """The configured remote URL, or ``None`` if unset.

        Reads the remote named by ``origin_remote`` (default ``origin``,
        set at construction). Used downstream by
        :meth:`whygraph.services.github.GitHubClient.for_repository` to
        derive ``owner/name``. A missing remote is a normal state (forks,
        local-only repos), not an error — so the property returns ``None``
        instead of raising. Genuine git failures (the ``git`` binary is
        missing, ``self.root`` is not a repository at all) still surface
        as :class:`GitError`.

        Returns
        -------
        str or None
            The remote URL exactly as configured (no normalization), or
            ``None`` when the remote is not set.

        Raises
        ------
        GitError
            If ``git`` itself cannot be invoked.
        """
        try:
            return self._shell.run(
                GitRemoteUrlCmd(self._origin_remote), cwd=self.root, check=False
            )
        except ShellError as exc:
            raise GitError(f"failed to resolve origin URL at {self.root}") from exc

    @cached_property
    def default_branch_refs(self) -> tuple[str, ...]:
        """Refs whose union defines "on the default branch".

        Resolution order — the first step that yields a short branch
        name wins:

        1. ``default_branch`` from construction, when supplied. It
           **replaces** the probing below outright, for repos on
           ``develop`` / ``trunk``.
        2. ``git symbolic-ref refs/remotes/<remote>/HEAD``, which a
           plain ``git clone`` sets to the forge's real default branch.
        3. ``<remote>/main``, then ``<remote>/master``.

        The resulting short name is then expanded into **both** the
        remote-tracking ref and the same-named *local* branch, when each
        exists — so the answer is typically ``("origin/main", "main")``.
        The union matters in both directions: unpushed commits on local
        ``main`` are absent from ``origin/main``, and a colleague's
        fetched-but-unmerged work is absent from local ``main``. Judging
        against only one of the two would misclassify one of those
        populations.

        Returns
        -------
        tuple[str, ...]
            Refs to pass to ``git rev-list``, or an empty tuple when
            nothing resolves — an unborn HEAD, no remote, or exotic
            branch naming with no configured override. Callers must read
            the empty tuple as "cannot judge" and leave flags alone
            rather than treating every commit as off-branch.

        Notes
        -----
        Never raises: any ``git`` failure degrades to the empty tuple,
        because a wrong answer here would mass-reflag the database while
        no answer merely preserves the status quo.
        """
        short = self._resolve_default_branch_name()
        if short is None:
            return ()
        refs = []
        remote_ref = f"{self._origin_remote}/{short}"
        if self._ref_exists(remote_ref):
            refs.append(remote_ref)
        if self._ref_exists(short):
            refs.append(short)
        return tuple(refs)

    @cached_property
    def default_branch_shas(self) -> frozenset[str]:
        """Every SHA reachable from :attr:`default_branch_refs`.

        One ``git rev-list`` over the whole ref union, so membership
        testing is O(1) per commit afterwards. Full reachability, **not**
        ``--first-parent``: a commit merged into the default branch via a
        merge commit is on that branch and must be counted.

        Returns
        -------
        frozenset[str]
            The reachable SHAs, or an empty set when
            :attr:`default_branch_refs` is empty or ``git`` fails.
        """
        refs = self.default_branch_refs
        if not refs:
            return frozenset()
        try:
            return self._shell.run(GitRevListShasCmd(refs), cwd=self.root)
        except ShellError:
            return frozenset()

    @cached_property
    def is_shallow(self) -> bool:
        """``True`` for a shallow clone, whose reachability is truncated.

        In a ``--depth=1`` clone (the GitHub Actions default)
        ``git rev-list origin/main`` returns a single SHA, so any caller
        that judges branch membership from it would conclude that the
        entire history is off-branch. Such callers must skip the
        judgement when this is ``True``.

        Returns
        -------
        bool
            Whether the repository is shallow. A ``git`` failure returns
            ``True`` — an unreadable answer is treated as truncated,
            since skipping a reconcile is recoverable and a mass-reflag
            is not.
        """
        try:
            return self._shell.run(GitIsShallowCmd, cwd=self.root)
        except ShellError:
            return True

    def _resolve_default_branch_name(self) -> str | None:
        """Short name of the default branch, or ``None`` if unresolvable.

        Implements steps 1-3 of :attr:`default_branch_refs`' chain. The
        configured override is returned verbatim without probing — an
        unresolvable value then falls out as an empty ref tuple, which
        the scan panel reports.
        """
        if self._default_branch:
            return self._default_branch
        pointee = self._symbolic_ref(f"refs/remotes/{self._origin_remote}/HEAD")
        if pointee:
            prefix = f"refs/remotes/{self._origin_remote}/"
            if pointee.startswith(prefix):
                return pointee[len(prefix) :]
        for candidate in _DEFAULT_BRANCH_CANDIDATES:
            if self._ref_exists(f"{self._origin_remote}/{candidate}"):
                return candidate
        return None

    def _symbolic_ref(self, ref: str) -> str | None:
        """Resolve a symbolic ref, or ``None`` if unset or unreadable."""
        try:
            return self._shell.run(GitSymbolicRefCmd(ref), cwd=self.root, check=False)
        except ShellError:
            return None

    def tracked_paths(self, ref: str, *paths: str) -> tuple[str, ...]:
        """List the files ``ref`` tracks under ``paths`` (purely local).

        Parameters
        ----------
        ref : str
            The commit-ish to read, e.g. ``"HEAD"`` or ``"origin/main"``.
        *paths : str
            Directories or files relative to the root, e.g. ``".whygraph"``.

        Returns
        -------
        tuple of str
            The tracked file paths; empty when ``ref`` tracks nothing there.

        Raises
        ------
        GitError
            If ``git`` fails (an unknown ``ref``, not a repository).
        """
        try:
            return self._shell.run(GitLsTreeNamesCmd(ref, paths), cwd=self.root)
        except ShellError as exc:
            raise GitError(f"failed to list the files {ref} tracks") from exc

    def _ref_exists(self, ref: str) -> bool:
        """Whether ``ref`` resolves to a commit; ``False`` if git fails."""
        try:
            return self._shell.run(GitRefExistsCmd(ref), cwd=self.root, check=False)
        except ShellError:
            return False

    def diff(self, commit: Commit, *, pathspec: str | None = None) -> str:
        """Raw unified-diff text for ``commit`` against its first parent.

        Root commits (no parents) are diffed against git's empty-tree
        object, so the result is exactly what the commit introduced.
        Merge commits diff against their first parent, matching the
        convention already in use for :attr:`Commit.stats`.

        Parameters
        ----------
        commit : Commit
            The commit to diff. Only :attr:`Commit.sha` and
            :attr:`Commit.parent_shas` are read.
        pathspec : str or None, optional
            When set, limit the diff to a single path (``git diff … --
            <pathspec>``). ``None`` (default) returns the whole commit
            diff. Used by the per-file lazy description path to slice a
            bulk commit down to the one file being queried — for a root
            commit that slice is the file's full contents.

        Returns
        -------
        str
            The raw ``git diff`` output. May be empty for a commit that
            touched no files (e.g. an empty merge), or — when
            ``pathspec`` is set — a commit that did not touch that path;
            callers can treat ``""`` as "nothing to describe".

        Raises
        ------
        GitError
            If ``git`` itself fails (unknown sha, repository broken).
        """
        if not commit.parent_shas:
            argv = (f"{_EMPTY_TREE}..{commit.sha}",)
        else:
            argv = (f"{commit.parent_shas[0]}..{commit.sha}",)
        try:
            return self._shell.run(GitDiffCmd(*argv, pathspec=pathspec), cwd=self.root)
        except ShellError as exc:
            raise GitError(
                f"failed to diff {commit.sha[:7]} against its parent"
            ) from exc

    def blame(
        self,
        path: str,
        line_start: int,
        line_end: int,
        *,
        ignore_revs: tuple[str, ...] | None = None,
        rev: str | None = None,
    ) -> tuple[BlameHunk, ...]:
        """Blame a contiguous line range of one file.

        Reports which commit owns each line of ``path`` between
        ``line_start`` and ``line_end`` (both 1-based, inclusive),
        aggregated into one :class:`BlameHunk` per commit.

        The underlying ``git blame`` invocation always runs with
        whitespace-blind and move/copy detection enabled (see
        :class:`GitBlameCmd`). When the working tree contains a
        ``.git-blame-ignore-revs`` file at :attr:`root`, that file is
        passed through too — so checked-in skip lists work without
        requiring per-user ``blame.ignoreRevsFile`` config.

        Parameters
        ----------
        path : str
            File to blame, relative to :attr:`root`.
        line_start : int
            First line of the range (1-based, inclusive).
        line_end : int
            Last line of the range (1-based, inclusive).
        ignore_revs : tuple[str, ...] or None, optional
            Extra commit SHAs to walk past for this single call. The
            project-level ``.git-blame-ignore-revs`` file (when present)
            still applies on top.
        rev : str or None, optional
            Revision to blame against. ``None`` (default) blames the
            **working tree**, uncommitted edits included (they come back
            as git's all-zero "Not Committed Yet" SHA). Pass a commit SHA
            to blame the file as of that commit — used by the
            predecessor-blame bridge to reach commits that touched a file
            at its pre-rename name.

        Returns
        -------
        tuple[BlameHunk, ...]
            One hunk per commit owning lines in the range, in
            first-appearance order. Uncommitted lines surface as a hunk
            with the all-zero SHA — see :attr:`BlameHunk.is_uncommitted`.

        Raises
        ------
        GitError
            If ``git`` fails — unknown path, or a range outside the file.
        """
        ignore_revs_file: str | None = None
        if (self.root / _BLAME_IGNORE_REVS_FILE).is_file():
            ignore_revs_file = _BLAME_IGNORE_REVS_FILE
        try:
            return self._shell.run(
                GitBlameCmd(
                    path,
                    line_start,
                    line_end,
                    ignore_revs_file=ignore_revs_file,
                    ignore_revs=ignore_revs,
                    rev=rev,
                ),
                cwd=self.root,
            )
        except ShellError as exc:
            raise GitError(f"failed to blame {path}:{line_start}-{line_end}") from exc

    def commit_file_changes(self, commit: Commit) -> tuple[FileChange, ...]:
        """Per-file structural changes recorded by ``commit``.

        Powers WhyGraph's per-commit path index (``commit_file_change``
        rows). The underlying ``git diff-tree`` invocation enables
        rename and copy detection (``-M -C``) so renames surface with
        :attr:`FileChange.renamed_from` populated rather than collapsing
        into the artificial "delete + add" pair git emits without it.
        Merge commits are diffed against their first parent and root
        commits against the empty tree, matching the convention
        :meth:`diff` already uses.

        Parameters
        ----------
        commit : Commit
            The commit to inspect. Only its :attr:`Commit.sha` is used —
            the rest of the value object is accepted to keep the call
            site symmetric with :meth:`diff`.

        Returns
        -------
        tuple[FileChange, ...]
            One entry per touched file, empty for an empty merge commit.

        Raises
        ------
        GitError
            If ``git diff-tree`` fails (unknown sha, broken repo).
        """
        try:
            return self._shell.run(GitDiffTreeFileChangesCmd(commit.sha), cwd=self.root)
        except ShellError as exc:
            raise GitError(
                f"failed to inspect file changes for {commit.sha[:7]}"
            ) from exc

    def diff_range(self, base: str, head: str) -> str:
        """Raw unified-diff text for the range ``base..head``.

        Unlike :meth:`diff` — which always compares a commit to its first
        parent — this compares two arbitrary commit-ishes. Used by callers
        that want "what changed between these two commits" rather than
        "what this commit introduced".

        Parameters
        ----------
        base : str
            Commit-ish on the left of the ``..`` range — the state being
            compared *from*.
        head : str
            Commit-ish on the right — the state being compared *to*.

        Returns
        -------
        str
            The raw ``git diff base..head`` output. Empty when the two
            trees are identical (e.g. ``base == head``).

        Raises
        ------
        GitError
            If ``git`` itself fails (unknown commit-ish, broken repo).
        """
        try:
            return self._shell.run(GitDiffCmd(f"{base}..{head}"), cwd=self.root)
        except ShellError as exc:
            raise GitError(f"failed to diff {base[:7]}..{head[:7]}") from exc

    def fetch_refs(self, refspecs: list[str], *, remote: str | None = None) -> None:
        """Fetch one or more refspecs from ``remote`` in a single round-trip.

        The only network-touching method on :class:`Repository`. It writes
        refs and objects into the local store (not the working tree), so
        the squash-origin enricher can pin a batch of PR refs
        (``refs/pull/<N>/head:refs/whygraph/pull/<N>``) against local GC
        with one ``git fetch`` rather than one per PR — keeping later
        blame/diff offline. ``--no-tags`` keeps the fetch from dragging in
        the remote's tag set.

        Parameters
        ----------
        refspecs : list[str]
            ``<src>:<dst>`` refspecs to fetch. An empty list is a no-op
            (no ``git`` is invoked).
        remote : str or None, optional
            Remote to fetch from. ``None`` (default) uses the remote this
            repository was constructed with (``origin_remote``).

        Raises
        ------
        GitError
            If ``git fetch`` fails — an unknown remote, a missing/GC'd
            source ref, or no network. Callers that treat enrichment as
            best-effort should catch this.

        Notes
        -----
        In a portal scan child with a token file
        (:data:`~.credentials.TOKEN_FILE_ENV`), the file is re-read for
        this fetch and its token handed to the credential helper through
        this one process's environment.
        """
        if not refspecs:
            return
        target = remote or self._origin_remote
        env = None
        if os.environ.get(TOKEN_FILE_ENV):
            # Imported here: the github service imports this package.
            from whygraph.services.github.token import token_env

            env = token_env(TOKEN_ENV_VAR)
        try:
            self._shell.run(
                GitFetchRefsCmd(*refspecs, remote=target), cwd=self.root, env=env
            )
        except ShellError as exc:
            raise GitError(
                f"failed to fetch {len(refspecs)} refspec(s) from {target!r}"
            ) from exc

    @classmethod
    def clone(
        cls,
        url: str,
        dest: Path,
        *,
        env: Mapping[str, str] | None = None,
        timeout: int = _NETWORK_TIMEOUT,
    ) -> Repository:
        """Clone a GitHub repository into ``dest`` without persisting any credential.

        ``url`` must be a plain ``<GitHub URL>/<owner>/<repo>`` URL on the
        configured host (see :func:`~.credentials.parse_github_url`); it is stored as
        ``origin`` exactly as given, so no token can end up in
        ``.git/config``. Credentials come only from ``env`` (build it with
        :func:`~.credentials.git_env`) via a host-scoped inline helper.

        Parameters
        ----------
        url : str
            The GitHub clone URL.
        dest : Path
            Directory to clone into. Git creates it and refuses a non-empty one.
        env : Mapping[str, str], optional
            The complete child environment. ``None`` (default) means an
            anonymous, allowlisted one (:func:`~.credentials.git_env`).
        timeout : int, optional
            Seconds before the clone is killed. Default ``900``.

        Returns
        -------
        Repository
            A view of the fresh clone rooted at ``dest``.

        Raises
        ------
        InvalidRepoUrlError
            If ``url`` is not a plain GitHub URL (nothing is run).
        GitError
            If git fails, is missing, or the clone exceeds ``timeout``.
        """
        parse_github_url(url)
        _run_network(
            Shell(),
            GitCloneCmd(url, dest),
            cwd=None,
            env=env,
            timeout=timeout,
            what=f"clone of {url}",
        )
        return cls(dest)

    def fetch_default(
        self,
        *,
        env: Mapping[str, str] | None = None,
        timeout: int = _NETWORK_TIMEOUT,
    ) -> None:
        """Fetch the origin remote under the same protocol and credential rules as :meth:`clone`.

        Updates remote-tracking refs only; the working tree does not move
        (see :meth:`fast_forward`).

        Parameters
        ----------
        env : Mapping[str, str], optional
            The complete child environment. ``None`` (default) means an
            anonymous, allowlisted one.
        timeout : int, optional
            Seconds before the fetch is killed. Default ``900``.

        Raises
        ------
        GitError
            If git fails, is missing, or the fetch exceeds ``timeout``.
        """
        _run_network(
            self._shell,
            GitFetchDefaultCmd(self._origin_remote),
            cwd=self.root,
            env=env,
            timeout=timeout,
            what=f"fetch in {self.root}",
        )

    def fast_forward(self) -> bool:
        """Fast-forward the checked-out branch to its upstream, with git hooks disabled.

        Purely local (run it after :meth:`fetch_default`). A diverged
        branch is refused rather than merged.

        Returns
        -------
        bool
            ``True`` when ``HEAD`` moved.

        Raises
        ------
        GitError
            If the branch cannot fast-forward (diverged, detached, no upstream).
        """
        try:
            before = self._shell.run(GitHeadShaCmd, cwd=self.root)
            self._shell.run(GitFastForwardCmd, cwd=self.root)
            after = self._shell.run(GitHeadShaCmd, cwd=self.root)
        except ShellError as exc:
            raise GitError(f"failed to fast-forward {self.root}") from exc
        return before != after

    def set_remote_url(self, url: str) -> None:
        """Point the origin remote at ``url`` (purely local).

        ``url`` must be a plain ``<GitHub URL>/<owner>/<repo>`` URL on the
        configured host, as for :meth:`clone`, so no credential and no other
        host can end up in ``.git/config``.

        Parameters
        ----------
        url : str
            The new URL.

        Raises
        ------
        InvalidRepoUrlError
            If ``url`` is not a plain GitHub URL (nothing is run).
        GitError
            If git fails (e.g. no such remote).
        """
        parse_github_url(url)
        try:
            self._shell.run(GitSetRemoteUrlCmd(self._origin_remote, url), cwd=self.root)
        except ShellError as exc:
            raise GitError(f"failed to set the {self._origin_remote} URL") from exc

    def remote_branch_commit(self, branch: str) -> str | None:
        """The commit ``refs/remotes/<origin>/<branch>`` names, or ``None``.

        Parameters
        ----------
        branch : str
            The branch's short name.

        Returns
        -------
        str or None
            The full SHA, or ``None`` when the remote-tracking branch does
            not exist (or the name is unusable).
        """
        if not branch or branch.startswith("-"):
            return None
        try:
            return self._shell.run(
                GitResolveCommitCmd(f"refs/remotes/{self._origin_remote}/{branch}"),
                cwd=self.root,
                check=False,
            )
        except ShellError:
            return None

    def checkout_reset(self, branch: str, commit: str) -> None:
        """Create or reset ``branch`` to ``commit`` and check it out, hooks disabled.

        Purely local (run it after :meth:`fetch_default`). Unlike
        :meth:`fast_forward` it follows a rewritten history; git still
        refuses to overwrite untracked files in the way.

        Parameters
        ----------
        branch : str
            The local branch name.
        commit : str
            The commit to reset it to.

        Raises
        ------
        GitError
            If ``branch`` starts with ``-`` or git fails.
        """
        if not branch or branch.startswith("-"):
            raise GitError(f"refusing the branch name {branch!r}")
        try:
            self._shell.run(GitCheckoutResetCmd(branch, commit), cwd=self.root)
        except ShellError as exc:
            raise GitError(f"failed to check out {branch} at {commit[:9]}") from exc

    def set_remote_head(self, branch: str) -> None:
        """Record ``branch`` as the origin remote's default (``refs/remotes/<origin>/HEAD``).

        Parameters
        ----------
        branch : str
            The remote's default branch.

        Raises
        ------
        GitError
            If git fails (e.g. the remote-tracking branch does not exist).
        """
        try:
            self._shell.run(
                GitSetRemoteHeadCmd(self._origin_remote, branch), cwd=self.root
            )
        except ShellError as exc:
            raise GitError(f"failed to set {self._origin_remote}/HEAD") from exc

    def is_ancestor(self, old: str, new: str) -> bool | None:
        """Whether ``old`` is in ``new``'s history (purely local).

        Parameters
        ----------
        old, new : str
            Two commits.

        Returns
        -------
        bool or None
            ``None`` when git cannot tell (an unknown commit).
        """
        try:
            return self._shell.run(
                GitIsAncestorCmd(old, new), cwd=self.root, check=False
            )
        except ShellError:
            return None

    def check_mailmap(self, contacts: Sequence[str]) -> tuple[str, ...]:
        """Canonicalize ``Name <email>`` contacts through the repo's mailmap.

        Shelling out to ``git check-mailmap`` rather than parsing a file
        is deliberate: git honours ``./.mailmap``, ``mailmap.file`` and
        ``mailmap.blob`` alike, so a mailmap deliberately kept *outside*
        a public repository (the usual arrangement when a contributor
        wants their address private) is still applied. A ``./.mailmap``
        parser would silently see nothing.

        Batched via argv — one subprocess per :data:`_MAILMAP_CHUNK`
        contacts — because ``git check-mailmap A B C`` emits one line per
        contact **in input order**. Callers therefore zip the result back
        onto their inputs positionally, and should treat a ragged result
        as a no-op.

        Parameters
        ----------
        contacts : Sequence[str]
            ``Name <email>`` strings. An empty sequence is a no-op — no
            ``git`` is invoked.

        Returns
        -------
        tuple[str, ...]
            One canonicalized ``Name <email>`` line per input contact, in
            input order. A contact the mailmap does not mention is echoed
            back unchanged — no mailmap at all is therefore *not* a
            failure, just an identity mapping.

        Raises
        ------
        GitError
            If ``git`` itself fails — the binary is missing, ``root`` is
            not a work tree, or the mailmap is malformed.
        """
        if not contacts:
            return ()
        lines: list[str] = []
        for start in range(0, len(contacts), _MAILMAP_CHUNK):
            chunk = tuple(contacts[start : start + _MAILMAP_CHUNK])
            try:
                lines.extend(self._shell.run(GitCheckMailmapCmd(*chunk), cwd=self.root))
            except ShellError as exc:
                raise GitError(
                    f"failed to check-mailmap {len(chunk)} contact(s) at {self.root}"
                ) from exc
        return tuple(lines)

    def commit_metadata(self, ref: str) -> Commit:
        """Full :class:`Commit` value object for a single commit-ish.

        Reads one commit's message body and diff stats via
        ``git log -1 --shortstat`` and parses it with the same
        :meth:`Commit.from_git_log` the main history walk uses. The object
        must already be local (the enricher calls :meth:`fetch_refs`
        first); an unknown ``ref`` surfaces as :class:`GitError`.

        Parameters
        ----------
        ref : str
            Any commit-ish ``git`` accepts (typically a full oid).

        Returns
        -------
        Commit
            The parsed commit value object.

        Raises
        ------
        GitError
            If ``git`` fails — most commonly an unknown ``ref`` whose
            object is not in the local store.
        """
        try:
            return self._shell.run(GitLogCommitCmd(ref), cwd=self.root)
        except ShellError as exc:
            raise GitError(f"failed to read commit metadata for {ref[:7]}") from exc


def _run_network(
    shell: Shell,
    cmd: GitCloneCmd | GitFetchDefaultCmd,
    *,
    cwd: Path | None,
    env: Mapping[str, str] | None,
    timeout: int,
    what: str,
) -> None:
    """Run a network git command, mapping failures to :class:`GitError`.

    The message carries git's last stderr line with the token (if any) and
    anything shaped like a GitHub token scrubbed, never the argv or the
    environment.
    """
    effective_env = git_env() if env is None else env
    try:
        shell.run(cmd, cwd=cwd, env=effective_env, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"{what} timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise GitError(f"{what} failed: git is not installed") from exc
    except ShellError as exc:
        lines = (exc.stderr or exc.stdout).strip().splitlines()
        detail = lines[-1] if lines else f"exit {exc.returncode}"
        token = effective_env.get(TOKEN_ENV_VAR)
        if token:
            detail = detail.replace(token, "***")
        detail = redact_tokens(detail)
        raise GitError(f"{what} failed: {detail}") from exc
