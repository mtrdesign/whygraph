"""Reusable, lazy view of every issue in a GitHub repository.

Symmetric to :mod:`whygraph.services.github.pull_requests`. GitHub's
GraphQL schema separates issues from pull requests, so this module
queries the ``repository.issues`` connection only.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator

from whygraph.core import Shell

from .commands import GhApiGraphqlCmd, floor_for, format_github_ts, paginate_graphql
from .exceptions import GitHubError
from .issue import Issue
from .token import GH_TOKEN_ENV, token_env

_LIST_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(
      first: 100
      after: $cursor
      states: [OPEN, CLOSED]
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body state url
        createdAt updatedAt closedAt
        author { login }
        labels(first: 20) { nodes { name } }
      }
    }
  }
}
"""

_SINCE_QUERY = """
query($owner: String!, $name: String!, $cursor: String, $since: DateTime!) {
  repository(owner: $owner, name: $name) {
    issues(
      first: 100
      after: $cursor
      states: [OPEN, CLOSED]
      filterBy: {since: $since}
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body state url
        createdAt updatedAt closedAt
        author { login }
        labels(first: 20) { nodes { name } }
      }
    }
  }
}
"""

_COUNT_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    issues(states: [OPEN, CLOSED]) {
      totalCount
    }
  }
}
"""


class Issues(Collection[Issue]):
    """All issues in a repository, as a reusable :class:`Collection`.

    Bound to one ``owner/name`` pair. Each ``__len__`` and ``__iter__``
    call re-issues a fresh ``gh api graphql`` request; the count is
    cached on the instance after the first call.

    The canonical entry point is
    :attr:`whygraph.services.github.GitHubClient.issues`. Direct
    construction is supported for callers that already hold an
    ``owner/name`` pair.

    Parameters
    ----------
    owner : str
        Repository owner (user or organization).
    name : str
        Repository name (without the ``.git`` suffix).
    shell : Shell, optional
        Override the subprocess wrapper used to run ``gh``. Defaults to
        a fresh :class:`Shell` per instance.

    Attributes
    ----------
    owner : str
        Repository owner.
    name : str
        Repository name.

    Raises
    ------
    GitHubError
        From :meth:`__len__` or :meth:`__iter__` if ``gh`` fails, the
        response is malformed, or GitHub reports GraphQL errors.
    """

    def __init__(
        self,
        owner: str,
        name: str,
        *,
        shell: Shell | None = None,
    ) -> None:
        self.owner = owner
        self.name = name
        self._shell = shell or Shell()
        self._len_cache: int | None = None

    def __repr__(self) -> str:
        return f"Issues(owner={self.owner!r}, name={self.name!r})"

    def __len__(self) -> int:
        if self._len_cache is None:
            data = self._shell.run(
                GhApiGraphqlCmd(
                    _COUNT_QUERY,
                    {"owner": self.owner, "name": self.name},
                ),
                env=token_env(GH_TOKEN_ENV),
            )
            try:
                self._len_cache = int(data["repository"]["issues"]["totalCount"])
            except (KeyError, TypeError, ValueError) as exc:
                raise GitHubError(
                    "GraphQL response missing repository.issues.totalCount"
                ) from exc
        return self._len_cache

    def __iter__(self) -> Iterator[Issue]:
        for node in paginate_graphql(
            self._shell,
            query=_LIST_QUERY,
            path=("repository", "issues"),
            variables={"owner": self.owner, "name": self.name},
        ):
            yield Issue.from_graphql_node(node)

    def updated_since(self, watermark: str) -> Iterator[Issue]:
        """Yield issues updated at or after ``watermark`` minus the lookback.

        The filter is server-side (``filterBy: {since: ...}``), so every
        issue GitHub returns is wanted and there is no client-side stop.

        Parameters
        ----------
        watermark : str
            A stored GitHub timestamp (``2026-10-26T12:00:00Z``).

        Yields
        ------
        Issue
            Each matching issue, newest update first.

        Raises
        ------
        GitHubError
            If ``gh`` fails or the response is malformed.
        """
        since = format_github_ts(floor_for(watermark))
        for node in paginate_graphql(
            self._shell,
            query=_SINCE_QUERY,
            path=("repository", "issues"),
            variables={"owner": self.owner, "name": self.name, "since": since},
        ):
            yield Issue.from_graphql_node(node)

    def __contains__(self, item: object) -> bool:
        if not isinstance(item, Issue):
            return False
        return any(issue.number == item.number for issue in self)
