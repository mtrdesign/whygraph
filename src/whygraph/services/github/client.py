"""High-level read-only view of a GitHub repository.

Exposes :class:`GitHubClient` — the entry point for the github
service. Mirrors :class:`whygraph.services.git.Repository` on the
github side: cheap construction, a bound :class:`Shell`, and
``cached_property`` collections (:attr:`pull_requests`, :attr:`issues`)
that yield typed value objects when iterated.

All network access goes through ``gh api graphql``, so authentication
and rate-limit handling are delegated to the ``gh`` CLI — no token
plumbing required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property

from whygraph.core import Shell
from whygraph.services.git import Repository

from .exceptions import GitHubError
from .issues import Issues
from .pull_requests import PullRequests

_GITHUB_URL_PATTERNS = (
    re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$"),
    re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$"),
    re.compile(r"^ssh://git@github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$"),
)


@dataclass
class GitHubClient:
    """Read-only client for a single GitHub repository.

    Construct directly when ``owner`` and ``name`` are known, or via
    :meth:`for_repository` to derive them from a local clone's ``origin``
    remote.

    Parameters
    ----------
    owner : str
        Repository owner (user or organization).
    name : str
        Repository name (without the ``.git`` suffix).
    shell : Shell, optional
        Shell used for every ``gh`` invocation. Defaults to a fresh
        :class:`whygraph.core.Shell`; inject a configured one to
        override (e.g. a longer timeout for slow networks).

    Attributes
    ----------
    owner : str
        Repository owner.
    name : str
        Repository name.
    shell : Shell
        The bound :class:`Shell` instance.
    pull_requests : PullRequests
        Reusable collection of every pull request in the repository
        (cached on first access; constructed lazily).
    issues : Issues
        Reusable collection of every issue in the repository (cached on
        first access; constructed lazily).
    """

    owner: str
    name: str
    shell: Shell = field(default_factory=Shell, repr=False)

    @classmethod
    def for_repository(cls, repo: Repository) -> "GitHubClient | None":
        """Build a client from a local clone's ``origin`` URL.

        Parameters
        ----------
        repo : Repository
            A :class:`whygraph.services.git.Repository` instance.

        Returns
        -------
        GitHubClient or None
            ``None`` if ``origin`` is unset or does not point at
            github.com; a configured client otherwise.
        """
        url = repo.origin_url
        if url is None:
            return None
        for pattern in _GITHUB_URL_PATTERNS:
            if m := pattern.match(url):
                return cls(owner=m.group(1), name=m.group(2))
        return None

    @staticmethod
    def check_auth() -> None:
        """Verify that ``gh`` is installed and authenticated.

        Raises
        ------
        GitHubError
            If ``gh`` is not on PATH, or ``gh auth status`` reports an
            unauthenticated session.
        """
        try:
            result = Shell().run(["gh", "auth", "status"], check=False)
        except FileNotFoundError as exc:
            raise GitHubError(
                "gh CLI is not installed. Install from https://cli.github.com/"
            ) from exc
        if result.returncode != 0:
            raise GitHubError(
                "gh CLI is not authenticated. Run `gh auth login` and retry."
            )

    @cached_property
    def pull_requests(self) -> PullRequests:
        """Reusable view of every pull request in the repository.

        Returns
        -------
        PullRequests
            A :class:`~collections.abc.Collection` over
            :class:`~whygraph.services.github.PullRequest` instances,
            bound to this client's ``owner/name`` and :attr:`shell`.
        """
        return PullRequests(self.owner, self.name, shell=self.shell)

    @cached_property
    def issues(self) -> Issues:
        """Reusable view of every issue in the repository.

        Returns
        -------
        Issues
            A :class:`~collections.abc.Collection` over
            :class:`~whygraph.services.github.Issue` instances, bound to
            this client's ``owner/name`` and :attr:`shell`.
        """
        return Issues(self.owner, self.name, shell=self.shell)
