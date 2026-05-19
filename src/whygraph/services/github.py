"""GitHub service: typed access to PRs and Issues via the ``gh`` CLI.

Wraps GitHub's GraphQL API behind a :class:`GitHubClient`. All requests
are issued via ``gh api graphql`` (subprocess) so authentication and
rate-limit handling are delegated to the ``gh`` CLI — no token plumbing
required.

Examples
--------
>>> from whygraph.services.git import Repository
>>> from whygraph.services.github import GitHubClient
>>> repo = Repository.discover(Path.cwd())            # doctest: +SKIP
>>> client = GitHubClient.for_repository(repo)        # doctest: +SKIP
>>> if client:                                        # doctest: +SKIP
...     for pr in client.iter_pull_requests():
...         print(pr.number, pr.title)
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from whygraph.core import Shell, ShellError
from whygraph.services.git import Repository


class GitHubError(RuntimeError):
    """Raised when a GitHub API call fails or returns a malformed payload.

    Wraps the underlying :class:`whygraph.core.ShellError` (when the
    failure originates in the ``gh`` subprocess) with semantic context.
    """


@dataclass(frozen=True, slots=True)
class CommitSummary:
    """A single commit referenced by a pull request.

    Attributes
    ----------
    oid : str
        Full commit SHA.
    headline : str
        First line of the commit message.
    author_login : str or None
        GitHub username of the commit author, when available.
    author_name : str or None
        Display name from the commit metadata.
    author_email : str or None
        Email from the commit metadata.
    """

    oid: str
    headline: str
    author_login: str | None
    author_name: str | None
    author_email: str | None


@dataclass(frozen=True, slots=True)
class Comment:
    """A comment on a pull request or issue.

    Attributes
    ----------
    author : str or None
        GitHub login of the commenter; ``None`` for deleted users.
    body : str
        Markdown body of the comment.
    created_at : str
        ISO 8601 timestamp of comment creation.
    """

    author: str | None
    body: str
    created_at: str


@dataclass(frozen=True, slots=True)
class PullRequest:
    """A GitHub pull request with embedded commits, labels, and comments.

    The ``state`` field collapses GitHub's ``MERGED`` and ``CLOSED`` into
    ``"closed"`` — use ``merged_at`` to distinguish.

    Attributes
    ----------
    number : int
        PR number within the repository.
    title : str
        Title of the PR.
    body : str or None
        Markdown body; ``None`` if empty.
    state : str
        Lowercased state: ``"open"`` or ``"closed"``.
    draft : bool
        Whether the PR is in draft mode.
    created_at, updated_at : str
        ISO 8601 timestamps.
    closed_at, merged_at : str or None
        ISO 8601 timestamps, ``None`` until the event occurs.
    merge_commit_sha : str or None
        SHA of the merge commit, set only after merge.
    head_sha : str
        SHA of the PR's HEAD commit.
    head_ref : str or None
        Name of the head branch (``None`` if the branch was deleted).
    base_ref : str
        Name of the base branch.
    author : str or None
        Login of the PR author; ``None`` for deleted users.
    html_url : str
        Browser URL.
    labels : tuple[str, ...]
        Label names applied to the PR.
    commits : tuple[CommitSummary, ...]
        Commits in the PR (capped server-side at 250).
    closing_issue_numbers : tuple[int, ...]
        Issue numbers this PR claims to close.
    comments : tuple[Comment, ...]
        Issue-style comments on the PR (capped server-side at 100).
    """

    number: int
    title: str
    body: str | None
    state: str
    draft: bool
    created_at: str
    updated_at: str
    closed_at: str | None
    merged_at: str | None
    merge_commit_sha: str | None
    head_sha: str
    head_ref: str | None
    base_ref: str
    author: str | None
    html_url: str
    labels: tuple[str, ...]
    commits: tuple[CommitSummary, ...]
    closing_issue_numbers: tuple[int, ...]
    comments: tuple[Comment, ...]


@dataclass(frozen=True, slots=True)
class Issue:
    """A GitHub issue (excludes pull requests — those have their own type).

    Attributes
    ----------
    number : int
        Issue number within the repository.
    title : str
        Issue title.
    body : str or None
        Markdown body; ``None`` if empty.
    state : str
        Lowercased state: ``"open"`` or ``"closed"``.
    created_at, updated_at : str
        ISO 8601 timestamps.
    closed_at : str or None
        ISO 8601 timestamp; ``None`` while open.
    author : str or None
        Login of the issue author; ``None`` for deleted users.
    html_url : str
        Browser URL.
    labels : tuple[str, ...]
        Label names applied to the issue.
    """

    number: int
    title: str
    body: str | None
    state: str
    created_at: str
    updated_at: str
    closed_at: str | None
    author: str | None
    html_url: str
    labels: tuple[str, ...]


_GITHUB_URL_PATTERNS = (
    re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$"),
    re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$"),
    re.compile(r"^ssh://git@github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$"),
)


_PRS_QUERY = """
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


_ISSUES_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(
      first: 100
      after: $cursor
      states: [OPEN, CLOSED]
      orderBy: {field: CREATED_AT, direction: ASC}
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


@dataclass
class GitHubClient:
    """Read-only client for a single GitHub repository.

    Construct directly when ``owner`` and ``name`` are known, or via
    :meth:`for_repository` to derive them from a local clone's ``origin``
    remote. All network access goes through ``gh api graphql``, so
    authentication is whatever ``gh auth status`` reports.

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
    """

    owner: str
    name: str
    shell: Shell = field(default_factory=Shell, repr=False)

    @classmethod
    def for_repository(cls, repo: Repository) -> GitHubClient | None:
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
        url = repo.origin_url()
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

    def iter_pull_requests(self) -> Iterator[PullRequest]:
        """Stream all pull requests, oldest first.

        Yields
        ------
        PullRequest
            One PR per yielded value, fully populated with commits,
            labels, closing-issue refs, and comments.

        Raises
        ------
        GitHubError
            On ``gh`` subprocess failure or malformed GraphQL response.
        """
        for node in self._paginate(_PRS_QUERY, ("repository", "pullRequests")):
            yield _parse_pr_node(node)

    def iter_issues(self) -> Iterator[Issue]:
        """Stream all issues, oldest first.

        GitHub's GraphQL schema separates issues from PRs (the REST
        ``/issues`` endpoint mixes them); this method returns issues only.

        Yields
        ------
        Issue
            One issue per yielded value.

        Raises
        ------
        GitHubError
            On ``gh`` subprocess failure or malformed GraphQL response.
        """
        for node in self._paginate(_ISSUES_QUERY, ("repository", "issues")):
            yield _parse_issue_node(node)

    def _paginate(self, query: str, path: tuple[str, ...]) -> Iterator[dict]:
        """Yield ``nodes`` from a paginated GraphQL connection.

        Parameters
        ----------
        query : str
            The GraphQL query (must include ``$cursor`` and a connection
            with ``pageInfo`` and ``nodes``).
        path : tuple[str, ...]
            Key path through the response to the connection object.
        """
        cursor: str | None = None
        while True:
            data = self._graphql(query, cursor=cursor)
            conn: Any = data
            for key in path:
                conn = (conn or {}).get(key)
            if conn is None:
                raise GitHubError(f"GraphQL response missing {'.'.join(path)}")
            for node in conn.get("nodes") or []:
                yield node
            page_info = conn.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return
            cursor = page_info.get("endCursor")

    def _graphql(self, query: str, *, cursor: str | None) -> dict:
        """Run a GraphQL query and return the ``data`` payload."""
        args = [
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-f",
            f"owner={self.owner}",
            "-f",
            f"name={self.name}",
        ]
        if cursor is not None:
            args.extend(["-f", f"cursor={cursor}"])
        out = self._gh(args)
        try:
            payload = json.loads(out)
        except json.JSONDecodeError as exc:
            raise GitHubError(f"gh returned non-JSON output: {exc}") from exc
        if "errors" in payload:
            msgs = "; ".join(e.get("message", "") for e in payload["errors"])
            raise GitHubError(f"GraphQL errors: {msgs}")
        return payload.get("data") or {}

    def _gh(self, args: list[str]) -> str:
        """Run ``gh <args>`` and return stdout, rewrapping failures.

        Raises
        ------
        GitHubError
            If ``gh`` is missing or exits non-zero.
        """
        try:
            result = self.shell.run(["gh", *args])
        except FileNotFoundError as exc:
            raise GitHubError(
                "gh CLI is not installed. Install from https://cli.github.com/"
            ) from exc
        except ShellError as exc:
            summary = " ".join(args[:2])
            detail = exc.stderr.strip() or exc.stdout.strip()
            raise GitHubError(f"gh {summary} failed: {detail}") from exc
        return result.stdout


def _parse_pr_node(node: dict) -> PullRequest:
    """Convert a GraphQL PR node into a :class:`PullRequest`."""
    merge_commit = node.get("mergeCommit") or {}
    author = node.get("author") or {}
    label_nodes = ((node.get("labels") or {}).get("nodes")) or []
    commit_nodes = ((node.get("commits") or {}).get("nodes")) or []
    closing_nodes = ((node.get("closingIssuesReferences") or {}).get("nodes")) or []
    comment_nodes = ((node.get("comments") or {}).get("nodes")) or []

    state_raw = str(node.get("state", "")).lower()
    # GraphQL distinguishes MERGED; collapse to CLOSED — merged-ness is
    # preserved via merged_at.
    state = "closed" if state_raw == "merged" else state_raw

    return PullRequest(
        number=int(node["number"]),
        title=node.get("title", ""),
        body=node.get("body"),
        state=state,
        draft=bool(node.get("isDraft", False)),
        created_at=node["createdAt"],
        updated_at=node["updatedAt"],
        closed_at=node.get("closedAt"),
        merged_at=node.get("mergedAt"),
        merge_commit_sha=merge_commit.get("oid"),
        head_sha=node.get("headRefOid") or "",
        head_ref=node.get("headRefName"),
        base_ref=node.get("baseRefName") or "main",
        author=author.get("login") if author else None,
        html_url=node["url"],
        labels=tuple(lbl["name"] for lbl in label_nodes if lbl.get("name")),
        commits=tuple(
            _parse_commit_summary(c["commit"]) for c in commit_nodes if c.get("commit")
        ),
        closing_issue_numbers=tuple(int(n["number"]) for n in closing_nodes),
        comments=tuple(
            Comment(
                author=(c.get("author") or {}).get("login"),
                body=c.get("body") or "",
                created_at=c.get("createdAt") or "",
            )
            for c in comment_nodes
        ),
    )


def _parse_commit_summary(commit: dict) -> CommitSummary:
    """Convert a GraphQL commit node into a :class:`CommitSummary`."""
    author = commit.get("author") or {}
    user = author.get("user") or {}
    return CommitSummary(
        oid=commit.get("oid") or "",
        headline=commit.get("messageHeadline", ""),
        author_login=user.get("login"),
        author_name=author.get("name"),
        author_email=author.get("email"),
    )


def _parse_issue_node(node: dict) -> Issue:
    """Convert a GraphQL issue node into an :class:`Issue`."""
    author = node.get("author") or {}
    label_nodes = ((node.get("labels") or {}).get("nodes")) or []
    return Issue(
        number=int(node["number"]),
        title=node.get("title", ""),
        body=node.get("body"),
        state=str(node.get("state", "")).lower(),
        created_at=node["createdAt"],
        updated_at=node["updatedAt"],
        closed_at=node.get("closedAt"),
        author=author.get("login") if author else None,
        html_url=node["url"],
        labels=tuple(lbl["name"] for lbl in label_nodes if lbl.get("name")),
    )
