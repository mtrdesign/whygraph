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
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property

from whygraph.core import Shell
from whygraph.services.git import Repository

from .exceptions import GitHubError
from .issues import Issues
from .pull_requests import PullRequests
from .token import GH_TOKEN_ENV, token_env

_GITHUB_URL_PATTERNS = (
    re.compile(r"^https://([^/@:]+)(?::(\d+))?/([^/]+)/([^/]+?)(?:\.git)?/?$"),
    re.compile(r"^git@([^/@:]+)():([^/]+)/([^/]+?)(?:\.git)?$"),
    re.compile(r"^ssh://git@([^/@:]+)(?::(\d+))?/([^/]+)/([^/]+?)(?:\.git)?/?$"),
)
"""Remote URL forms: ``https://host[:port]/o/r``, ``git@host:o/r`` and
``ssh://git@host[:port]/o/r``, each with an optional ``.git``. Groups: host,
port (empty for the scp form), owner, name."""


def _parse_remote(url: str) -> tuple[str, str | None, str, str] | None:
    """``(host, port, owner, name)`` of a remote URL, exactly as written."""
    for pattern in _GITHUB_URL_PATTERNS:
        if m := pattern.match(url):
            return m.group(1), m.group(2) or None, m.group(3), m.group(4)
    return None


def remote_identity(url: str | None) -> tuple[str, str, str] | None:
    """The ``(host, owner, name)`` a git remote URL names, case-folded.

    Matches a checkout's ``origin`` against a platform project's clone URL
    (M2e plan section 4.8): ``https://host/o/r(.git)``, ``git@host:o/r(.git)``
    and ``ssh://git@host[:port]/o/r(.git)`` name the same repository. The
    port is not part of the identity (an ``https`` and an ``ssh`` remote of
    one host use different ports).

    Parameters
    ----------
    url : str or None
        A remote URL.

    Returns
    -------
    tuple[str, str, str] or None
        Host, owner and name, lower-cased; ``None`` for ``None`` and for a
        URL of another form (a local path, credentials in the URL, a
        nested group path).
    """
    if not url:
        return None
    parsed = _parse_remote(url.strip())
    if parsed is None:
        return None
    host, _port, owner, name = parsed
    return host.lower(), owner.lower(), name.lower()


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
        parsed = _parse_remote(url)
        if parsed is None:
            return None
        host, port, owner, name = parsed
        if host != "github.com" or port is not None:
            return None
        return cls(owner=owner, name=name)

    @staticmethod
    def check_auth(env: Mapping[str, str] | None = None) -> None:
        """Verify that ``gh`` is installed and authenticated.

        Parameters
        ----------
        env : Mapping[str, str], optional
            Complete environment for the ``gh`` call, so a candidate token
            can be validated (build it with
            :func:`~whygraph.services.github.access.github_env`). ``None``
            (default) inherits the current process environment, with the
            token of a portal token file when one is configured
            (:func:`~.token.token_env`).

        Raises
        ------
        GitHubError
            If ``gh`` is not on PATH, or ``gh auth status`` reports an
            unauthenticated session.
        """
        if env is None:
            env = token_env(GH_TOKEN_ENV)
        try:
            # `gh auth status` prints most of a token: never into the log.
            result = Shell().run(
                ["gh", "auth", "status"], check=False, env=env, log_output=False
            )
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
