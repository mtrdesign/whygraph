"""In-memory value objects for a parsed GitHub pull request.

Exposes :class:`PullRequest` and its embedded :class:`Comment`, plus
the per-node parser that produces them from a GitHub GraphQL response.
The parser lives here (not in the :mod:`PullRequests` collection) so
that "what one pull-request node looks like" is owned by the class
that represents it — same pattern :class:`Commit.from_git_log` follows
on the git side.
"""

from __future__ import annotations

from dataclasses import dataclass

from whygraph.services.git import CommitSummary


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

    @classmethod
    def from_graphql_node(cls, node: dict) -> "Comment":
        """Build a :class:`Comment` from a GitHub GraphQL ``comment`` node.

        Expects ``author { login }``, ``body``, and ``createdAt``.
        Missing fields degrade to ``None`` (author) or ``""`` (body,
        timestamp) to match the underlying GraphQL nullability.
        """
        author = node.get("author") or {}
        return cls(
            author=author.get("login"),
            body=node.get("body") or "",
            created_at=node.get("createdAt") or "",
        )


@dataclass(frozen=True, slots=True)
class PullRequest:
    """A GitHub pull request with embedded commits, labels, and comments.

    The ``state`` field collapses GitHub's ``MERGED`` and ``CLOSED`` into
    ``"closed"`` — use :attr:`merged_at` to distinguish.

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

    @classmethod
    def from_graphql_node(cls, node: dict) -> "PullRequest":
        """Parse one GraphQL pull-request node into a :class:`PullRequest`.

        Expects the shape produced by the query in
        :mod:`whygraph.services.github.pull_requests`: ``commits.nodes``
        entries each wrap a ``commit`` block (parsed by
        :meth:`CommitSummary.from_graphql_node`), ``labels.nodes`` carry
        ``name``, ``closingIssuesReferences.nodes`` carry ``number``,
        and ``comments.nodes`` follow :meth:`Comment.from_graphql_node`.

        Parameters
        ----------
        node : dict
            One pull-request node from the ``repository.pullRequests``
            connection.

        Returns
        -------
        PullRequest
            The parsed pull request.
        """
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

        return cls(
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
                CommitSummary.from_graphql_node(c["commit"])
                for c in commit_nodes
                if c.get("commit")
            ),
            closing_issue_numbers=tuple(int(n["number"]) for n in closing_nodes),
            comments=tuple(Comment.from_graphql_node(c) for c in comment_nodes),
        )
