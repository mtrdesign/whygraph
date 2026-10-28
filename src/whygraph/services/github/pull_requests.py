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

from collections.abc import Collection, Iterator, Sequence

from whygraph.core import Shell, ShellError

from .commands import (
    GhApiGraphqlCmd,
    floor_for,
    is_gateway_timeout,
    paginate_graphql,
    parse_github_ts,
)
from .exceptions import GitHubError
from .pull_request import PullRequest
from .token import GH_TOKEN_ENV, token_env

_PR_FIELDS = """\
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
"""


def _list_query(size: int) -> str:
    """Return the full PR listing query for a page of ``size`` PRs."""
    return (
        f"""
query($owner: String!, $name: String!, $cursor: String) {{
  repository(owner: $owner, name: $name) {{
    pullRequests(
      first: {size}
      after: $cursor
      states: [OPEN, CLOSED, MERGED]
      orderBy: {{field: UPDATED_AT, direction: DESC}}
    ) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
"""
        + _PR_FIELDS
        + """      }
    }
  }
}
"""
    )


_UPDATES_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(
      first: 100
      after: $cursor
      states: [OPEN, CLOSED, MERGED]
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      pageInfo { hasNextPage endCursor }
      nodes { number updatedAt }
    }
  }
}
"""

DETAILS_BATCH = 20
"""PRs per details request.

20 x (250 commits + 100 comments + 50 refs + 20 labels) stays far under
GitHub's 500k-node limit and its 10 s query timeout.
"""

MIN_DETAILS_BATCH = 5
"""Smallest details batch a 502 / 504 halves down to (20, 10, 5)."""

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
                ),
                env=token_env(GH_TOKEN_ENV),
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
            query="",
            path=("repository", "pullRequests"),
            variables={"owner": self.owner, "name": self.name},
            query_for_size=_list_query,
        ):
            yield PullRequest.from_graphql_node(node)

    def updates_since(self, watermark: str) -> Iterator[tuple[int, str]]:
        """Yield ``(number, updatedAt)`` of PRs updated since ``watermark``.

        Lists PRs newest-update first, light (``number updatedAt`` only),
        and stops paginating at the first node older than ``watermark``
        minus the lookback.

        Parameters
        ----------
        watermark : str
            A stored GitHub timestamp (``2026-10-26T12:00:00Z``).

        Yields
        ------
        tuple[int, str]
            The PR number and its ``updatedAt`` as GitHub sent it.

        Raises
        ------
        GitHubError
            If ``gh`` fails or the response is malformed.
        """
        floor = floor_for(watermark)
        for node in paginate_graphql(
            self._shell,
            query=_UPDATES_QUERY,
            path=("repository", "pullRequests"),
            variables={"owner": self.owner, "name": self.name},
        ):
            if parse_github_ts(node["updatedAt"]) < floor:
                return
            yield int(node["number"]), node["updatedAt"]

    def by_numbers(self, numbers: Sequence[int]) -> Iterator[PullRequest]:
        """Yield the full details of the given PRs, in the order asked.

        Sends :data:`DETAILS_BATCH` PRs per request as aliased
        ``pullRequest(number:)`` fields. An empty sequence sends nothing.

        Parameters
        ----------
        numbers : Sequence[int]
            PR numbers to fetch.

        Yields
        ------
        PullRequest
            One per number; a null alias is skipped.

        Raises
        ------
        GitHubError
            If ``gh`` fails (including a number GitHub cannot resolve,
            which makes ``gh`` exit non-zero) or the response is malformed.
        """
        pending = [int(n) for n in numbers]
        size = DETAILS_BATCH
        while pending:
            batch, rest = pending[:size], pending[size:]
            try:
                data = self._details(batch)
            except ShellError as exc:
                if is_gateway_timeout(exc) and size > MIN_DETAILS_BATCH:
                    size = max(size // 2, MIN_DETAILS_BATCH)
                    continue
                detail = exc.stderr.strip() or exc.stdout.strip()
                raise GitHubError(f"gh api graphql failed: {detail}") from exc
            pending = rest
            repo = (data or {}).get("repository") or {}
            for n in batch:
                node = repo.get(f"pr{n}")
                if node is not None:
                    yield PullRequest.from_graphql_node(node)

    def _details(self, batch: Sequence[int]) -> dict:
        """Run one aliased details request for ``batch``."""
        aliases = "".join(
            f"    pr{n}: pullRequest(number: {n}) {{\n{_PR_FIELDS}    }}\n"
            for n in batch
        )
        query = (
            "query($owner: String!, $name: String!) {\n"
            "  repository(owner: $owner, name: $name) {\n"
            f"{aliases}  }}\n}}\n"
        )
        return self._shell.run(
            GhApiGraphqlCmd(query, {"owner": self.owner, "name": self.name}),
            env=token_env(GH_TOKEN_ENV),
        )

    def __contains__(self, item: object) -> bool:
        if not isinstance(item, PullRequest):
            return False
        return any(pr.number == item.number for pr in self)
