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

import logging
from collections.abc import Collection, Iterator, Sequence

from whygraph.core import Shell, ShellError

from .commands import (
    GhApiGraphqlCmd,
    floor_for,
    forbidden_under,
    is_gateway_timeout,
    paginate_graphql,
    parse_github_ts,
    tolerated_data,
)
from .exceptions import GitHubError
from .pull_request import PullRequest, ReviewThread
from .token import GH_TOKEN_ENV, token_env

_log = logging.getLogger(__name__)

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
          totalCount
          nodes { author { __typename login } body createdAt isMinimized }
        }
        milestone { number }
        assignees(first: 10) { nodes { login } }
        reviewDecision
        latestOpinionatedReviews(first: 10) {
          nodes { author { login } state submittedAt }
        }
        reviewRequests(first: 10) {
          nodes { requestedReviewer { ... on User { login } ... on Bot { login } } }
        }
        reviews(first: 5, states: [APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED]) {
          nodes { author { login } submittedAt }
        }
        reviewSummaries: reviews(last: 30, states: [APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED]) {
          nodes { author { __typename login } state body submittedAt isMinimized }
        }
        reviewThreads { totalCount }
        files(first: 100) { nodes { path } }
        additions deletions changedFiles
"""


PR_PAGE_SIZES = (50, 25, 10, 5)
"""Page sizes of the PR list query, tried in turn on a 502 / 504."""


def _list_query(size: int = PR_PAGE_SIZES[0]) -> str:
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

_TOLERATE = forbidden_under("reviewRequests")
"""A team review request an installation token cannot read is not a failure."""

DETAILS_BATCH = 20
"""PRs per details request.

20 x (250 commits + 100 comments + 100 files + 50 refs + 20 labels + up
to 55 reviews and review requests) stays far under GitHub's 500k-node
limit; a 502 / 504 halves it (see :data:`MIN_DETAILS_BATCH`).
"""

MIN_DETAILS_BATCH = 2
"""Smallest details batch a 502 / 504 halves down to (20, 10, 5, 2)."""

_COUNT_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: [OPEN, CLOSED, MERGED]) {
      totalCount
    }
  }
}
"""

_THREAD_FIELDS = """\
        number
        reviewThreads(first: 20) {
          totalCount
          nodes { path originalLine isResolved isOutdated subjectType diffSide
            comments(first: 10) { totalCount nodes { author { __typename login } body createdAt isMinimized diffHunk } } }
        }
"""
"""Fields of the thread pass: the first 20 threads, 10 comments each.

Only the first comment's ``diffHunk`` is used (the anchor,
:meth:`ReviewThread.from_graphql_node`); the others are discarded at
parse. No follow-up pagination: the list pass's ``reviewThreads {
totalCount }`` reports the rest.
"""

THREAD_BATCH = 5
"""PRs per thread-pass request; a 502 / 504 halves it down to 1 (5, 2, 1).

5, not 10, because ``diffHunk`` adds bytes (measured on 5 heavy biome
PRs: 1 point, about 711 KB, 2.5 s per batch).
"""


def _not_found(payload: dict) -> bool:
    """Tolerate a batch whose only errors are unresolvable ``prN`` aliases.

    A deleted PR makes ``gh`` exit non-zero with ``data`` holding a null
    alias and a ``NOT_FOUND`` error whose ``path`` is exactly
    ``["repository", "prN"]``. Any other error, or a mix, is not tolerated.
    """
    errors = payload.get("errors")
    return (
        isinstance(payload.get("data"), dict)
        and isinstance(errors, list)
        and bool(errors)
        and all(
            isinstance(e, dict)
            and e.get("type") == "NOT_FOUND"
            and isinstance(e.get("path"), list)
            and len(e["path"]) == 2
            and e["path"][0] == "repository"
            and str(e["path"][1]).startswith("pr")
            for e in errors
        )
    )


ThreadResult = tuple[int, tuple[ReviewThread, ...] | None]
"""One thread-pass result: a PR number and its threads (``None`` = not found)."""


class ThreadPass(Iterator[ThreadResult]):
    """One run of the review-thread pass, iterated batch by batch.

    Returned by :meth:`PullRequests.review_threads`. Iterating sends
    aliased ``pullRequest(number:)`` requests of :data:`THREAD_BATCH`
    PRs and yields ``(number, threads)`` per PR, in the order asked, only
    after that PR's whole batch returned. The outcome is read from the
    attributes **after** iteration ends, normally or by a raised
    :class:`GitHubError`.

    Parameters
    ----------
    prs : PullRequests
        The repository's collection (its shell, owner and name).
    numbers : Sequence[int]
        PR numbers to fetch.
    point_floor : int
        Rate-limit points to leave unspent (see Notes).

    Attributes
    ----------
    stopped : bool
        ``True`` when the pass stopped at the rate-limit floor; no error
        is raised for it.
    pending : tuple[int, ...]
        PRs not fetched because of that stop (empty otherwise), in the
        order asked.
    skipped : tuple[int, ...]
        PRs that still failed with a 502 / 504 alone; skipped for this
        run (each logged), to be retried by the next one.
    remaining : int or None
        ``rateLimit.remaining`` of the last response that carried it.
    reset_at : str or None
        ``rateLimit.resetAt`` of that response.

    Notes
    -----
    Before each request after the first, the pass stops when the last
    response's ``remaining`` is below ``point_floor`` (no extra request:
    every request also asks ``rateLimit { cost remaining resetAt }``).
    A ``NOT_FOUND`` alias (a deleted PR) yields ``None`` for that PR; a
    502 / 504 halves the batch down to 1 and then skips that PR; any
    other failure raises :class:`GitHubError` after the batches already
    yielded.
    """

    def __init__(
        self, prs: PullRequests, numbers: Sequence[int], point_floor: int
    ) -> None:
        self.stopped = False
        self.pending: tuple[int, ...] = ()
        self.skipped: tuple[int, ...] = ()
        self.remaining: int | None = None
        self.reset_at: str | None = None
        self._items = self._run(prs, [int(n) for n in numbers], point_floor)

    def __next__(self) -> ThreadResult:
        return next(self._items)

    def _run(
        self, prs: PullRequests, pending: list[int], point_floor: int
    ) -> Iterator[ThreadResult]:
        """Drive the batches; see the class docstring for the rules."""
        size = THREAD_BATCH
        skipped: list[int] = []
        while pending:
            if self.remaining is not None and self.remaining < point_floor:
                self.stopped = True
                self.pending = tuple(pending)
                _log.info(
                    "Thread pass stopped at the rate-limit floor "
                    "(%d < %d points left, reset %s); %d PR(s) pending",
                    self.remaining,
                    point_floor,
                    self.reset_at,
                    len(pending),
                )
                return
            batch, rest = pending[:size], pending[size:]
            try:
                data = prs._threads(batch)
            except ShellError as exc:
                data = tolerated_data(exc, _not_found)
                if data is not None:
                    pass
                elif is_gateway_timeout(exc) and size > 1:
                    size = max(size // 2, 1)
                    continue
                elif is_gateway_timeout(exc):
                    _log.warning(
                        "Review threads of PR #%d timed out alone; skipped for "
                        "this run",
                        batch[0],
                    )
                    skipped.append(batch[0])
                    self.skipped = tuple(skipped)
                    pending = rest
                    continue
                else:
                    detail = exc.stderr.strip() or exc.stdout.strip()
                    raise GitHubError(f"gh api graphql failed: {detail}") from exc
            self._note_rate_limit(data)
            results = _parse_thread_batch(data, batch)
            pending = rest
            yield from results

    def _note_rate_limit(self, data: dict) -> None:
        """Record ``rateLimit`` from a response, when it carries one."""
        limit = data.get("rateLimit")
        if not isinstance(limit, dict):
            return
        try:
            self.remaining = int(limit["remaining"])
        except (KeyError, TypeError, ValueError):
            return
        self.reset_at = limit.get("resetAt")


def _parse_thread_batch(data: dict, batch: Sequence[int]) -> list[ThreadResult]:
    """Parse one thread-pass response for ``batch``, all or nothing.

    Raises
    ------
    GitHubError
        If ``repository``, an alias or a found PR's ``reviewThreads`` is
        missing or malformed; nothing of the batch is yielded then.
    """
    repo = data.get("repository")
    if not isinstance(repo, dict):
        raise GitHubError("GraphQL response missing repository")
    results: list[ThreadResult] = []
    for n in batch:
        alias = f"pr{n}"
        if alias not in repo:
            raise GitHubError(f"GraphQL response missing repository.{alias}")
        node = repo[alias]
        if node is None:
            results.append((n, None))
            continue
        conn = node.get("reviewThreads") if isinstance(node, dict) else None
        if not isinstance(conn, dict):
            raise GitHubError(f"GraphQL response missing {alias}.reviewThreads")
        threads = tuple(
            ReviewThread.from_graphql_node(t) for t in conn.get("nodes") or [] if t
        )
        results.append((n, threads))
    return results


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
            tolerate=_TOLERATE,
            page_sizes=PR_PAGE_SIZES,
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
                data = tolerated_data(exc, _TOLERATE)
                if data is not None:
                    pass
                elif is_gateway_timeout(exc) and size > MIN_DETAILS_BATCH:
                    size = max(size // 2, MIN_DETAILS_BATCH)
                    continue
                else:
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

    def review_threads(self, numbers: Sequence[int], *, point_floor: int) -> ThreadPass:
        """Fetch the first 20 review threads of each given PR (the thread pass).

        Sends :data:`THREAD_BATCH` PRs per request as aliased
        ``pullRequest(number:)`` fields, each request also asking
        ``rateLimit``. An empty sequence sends nothing. Nothing is sent
        until the result is iterated.

        Parameters
        ----------
        numbers : Sequence[int]
            PR numbers to fetch, in the order wanted.
        point_floor : int
            Stop, without error, before a request when the previous
            response left fewer rate-limit points than this.

        Returns
        -------
        ThreadPass
            An iterator of ``(number, threads)`` per PR, ``threads`` being
            ``None`` for a PR GitHub cannot resolve (deleted). After
            iteration, :attr:`ThreadPass.stopped` / ``pending`` report a
            floor stop and :attr:`ThreadPass.skipped` the PRs that timed
            out alone.

        Raises
        ------
        GitHubError
            While iterating, on any failure other than a tolerated
            ``NOT_FOUND`` alias or a 502 / 504, after the batches already
            yielded.
        """
        return ThreadPass(self, numbers, point_floor)

    def _threads(self, batch: Sequence[int]) -> dict:
        """Run one aliased thread-pass request for ``batch``."""
        aliases = "".join(
            f"    pr{n}: pullRequest(number: {n}) {{\n{_THREAD_FIELDS}    }}\n"
            for n in batch
        )
        query = (
            "query($owner: String!, $name: String!) {\n"
            "  rateLimit { cost remaining resetAt }\n"
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
