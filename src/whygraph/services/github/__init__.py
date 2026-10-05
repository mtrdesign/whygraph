"""GitHub service: typed access to PRs and Issues via the ``gh`` CLI.

Public API
----------
* :class:`GitHubClient` — the entry point for the github service.
  Holds the bound :class:`whygraph.core.Shell` and exposes
  :attr:`~GitHubClient.pull_requests` / :attr:`~GitHubClient.issues`
  collections.
* :class:`PullRequests`, :class:`Issues` — reusable
  :class:`collections.abc.Collection` views over a repository's PRs and
  issues. Iterate to paginate; ``len()`` runs a ``totalCount`` query.
* :class:`PullRequest`, :class:`Issue`, :class:`Comment` — value
  objects yielded by the collections.
* :class:`GitHubError` — raised on any ``gh`` failure (missing binary,
  unauthenticated session, malformed GraphQL response, GraphQL errors).
* :func:`github_token` — the token to use now: a portal scan child's token
  file (re-read on every call), else ``GH_TOKEN``.

``CommitSummary`` lives in :mod:`whygraph.services.git`; import it from
there when you need to construct or destructure PR commit lists.

Examples
--------
>>> from pathlib import Path
>>> from whygraph.services.git import Repository
>>> from whygraph.services.github import GitHubClient
>>> repo = Repository(Path.cwd())                       # doctest: +SKIP
>>> client = GitHubClient.for_repository(repo)          # doctest: +SKIP
>>> if client:                                          # doctest: +SKIP
...     print(len(client.pull_requests))
...     for pr in client.pull_requests:
...         print(pr.number, pr.title)
"""

from .access import RepoAccess, check_repo_access, github_env
from .client import GitHubClient, remote_identity
from .exceptions import GitHubError, RepoAccessError
from .issue import Issue
from .issues import Issues
from .pull_request import Comment, PullRequest
from .pull_requests import PullRequests
from .token import github_token

__all__ = [
    "Comment",
    "GitHubClient",
    "GitHubError",
    "Issue",
    "Issues",
    "PullRequest",
    "PullRequests",
    "RepoAccess",
    "RepoAccessError",
    "check_repo_access",
    "github_env",
    "github_token",
    "remote_identity",
]
