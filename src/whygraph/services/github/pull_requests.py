"""Reusable, lazy view of every pull request in a GitHub repository.

Exposes :class:`PullRequests` — a :class:`collections.abc.Collection`
of :class:`PullRequest` bound to one ``owner/name`` pair. Each
``len(prs)`` and ``for pr in prs`` re-issues a fresh ``gh api graphql``
call, so the collection is safe to iterate more than once and cheap to
size up-front (the count comes from ``repository.pullRequests.totalCount``,
which is a single small request).

Mirrors :class:`whygraph.services.git.Commits` on the github side.
Per-node parsing lives on :class:`PullRequest` (``from_graphql_node``);
this module owns only the outer concerns: query strings, pagination,
and the ``totalCount`` count query.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator

from whygraph.core import Shell

from .commands import GhApiGraphqlCmd, paginate_graphql
from .exceptions import GitHubError
from .pull_request import PullRequest

_LIST_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(
      first: 100
      after: $cursor
      states: [OPEN, CLOSED, MERGED]
      orderBy: {field: CREATED_AT, direction: ASC}
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body state isDraft url
        createdAt updatedAt closedAt mergedAt
        mergeCommit { oid }
        headRefOid headRefName baseRefName
        author { login }
        labels(first: 20) { nodes { name } }
        commits(first: 250) {
          nodes {
            commit {
              oid
              messageHeadline
              author {
                name
                email
                user { login }
              }
            }
          }
        }
        closingIssuesReferences(first: 50) { nodes { number } }
        comments(first: 100) {
          nodes { author { login } body createdAt }
        }
      }
    }
  }
}
"""

_COUNT_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: [OPEN, CLOSED, MERGED]) {
      totalCount
    }
  }
}
"""


class PullRequests(Collection[PullRequest]):
    """All pull requests in a repository, as a reusable :class:`Collection`.

    The collection is bound to one ``owner/name`` pair. Each ``__len__``
    and ``__iter__`` call re-issues a fresh ``gh api graphql`` request;
    repositories are assumed static for the duration of a scan, so the
    count is cached on the instance after the first call.

    No filters (state, label, author, date range) are exposed yet — add
    them when a second caller actually needs one.

    The canonical entry point is
    :attr:`whygraph.services.github.GitHubClient.pull_requests`. Direct
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
        return f"PullRequests(owner={self.owner!r}, name={self.name!r})"

    def __len__(self) -> int:
        if self._len_cache is None:
            data = self._shell.run(
                GhApiGraphqlCmd(
                    _COUNT_QUERY,
                    {"owner": self.owner, "name": self.name},
                )
            )
            try:
                self._len_cache = int(data["repository"]["pullRequests"]["totalCount"])
            except (KeyError, TypeError, ValueError) as exc:
                raise GitHubError(
                    "GraphQL response missing repository.pullRequests.totalCount"
                ) from exc
        return self._len_cache

    def __iter__(self) -> Iterator[PullRequest]:
        for node in paginate_graphql(
            self._shell,
            query=_LIST_QUERY,
            path=("repository", "pullRequests"),
            variables={"owner": self.owner, "name": self.name},
        ):
            yield PullRequest.from_graphql_node(node)

    def __contains__(self, item: object) -> bool:
        if not isinstance(item, PullRequest):
            return False
        return any(pr.number == item.number for pr in self)
