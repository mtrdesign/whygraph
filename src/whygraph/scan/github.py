"""GitHub PR and Issue listing via the GraphQL API (`gh api graphql`)."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


class GitHubError(RuntimeError):
    pass


@dataclass(frozen=True)
class PullRequest:
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
    labels: list[str]
    # JSON-encoded into pull_requests.commit_titles. Each entry is a dict
    # with: oid (full SHA), headline, author_login (GitHub username or None),
    # author_name, author_email. Field name kept for column-name parity.
    commit_titles: list[dict] = field(default_factory=list)
    closing_issue_numbers: list[int] = field(default_factory=list)
    comments: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str | None
    state: str
    created_at: str
    updated_at: str
    closed_at: str | None
    author: str | None
    html_url: str
    labels: list[str]


_GITHUB_URL_PATTERNS = [
    re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$"),
    re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$"),
    re.compile(r"^ssh://git@github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$"),
]


def detect_repo(repo_root: Path) -> tuple[str, str] | None:
    """Return ``(owner, name)`` if ``origin`` points at github.com, else None."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    for pat in _GITHUB_URL_PATTERNS:
        m = pat.match(url)
        if m:
            return m.group(1), m.group(2)
    return None


def check_auth() -> None:
    """Raise GitHubError if ``gh`` is missing or unauthenticated."""
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitHubError(
            "gh CLI is not installed. Install from https://cli.github.com/"
        ) from exc
    if result.returncode != 0:
        raise GitHubError("gh CLI is not authenticated. Run `gh auth login` and retry.")


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


def list_pull_requests(
    owner: str,
    name: str,
    on_page: Callable[[int], None] | None = None,
) -> list[PullRequest]:
    """Return all PRs with embedded commit titles and closing-issue refs."""
    all_prs: list[PullRequest] = []
    cursor: str | None = None
    while True:
        data = _graphql(_PRS_QUERY, owner=owner, name=name, cursor=cursor)
        conn = (data.get("repository") or {}).get("pullRequests")
        if conn is None:
            raise GitHubError("GraphQL response missing repository.pullRequests")
        for node in conn.get("nodes") or []:
            all_prs.append(_parse_pr_node(node))
        if on_page is not None:
            on_page(len(all_prs))
        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
    return all_prs


def list_issues(
    owner: str,
    name: str,
    on_page: Callable[[int], None] | None = None,
) -> list[Issue]:
    """Return all issues (excludes PRs — GitHub's REST issues endpoint mixes them, GraphQL splits them)."""
    all_issues: list[Issue] = []
    cursor: str | None = None
    while True:
        data = _graphql(_ISSUES_QUERY, owner=owner, name=name, cursor=cursor)
        conn = (data.get("repository") or {}).get("issues")
        if conn is None:
            raise GitHubError("GraphQL response missing repository.issues")
        for node in conn.get("nodes") or []:
            all_issues.append(_parse_issue_node(node))
        if on_page is not None:
            on_page(len(all_issues))
        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
    return all_issues


def _parse_pr_node(node: dict) -> PullRequest:
    merge_commit = node.get("mergeCommit") or {}
    author = node.get("author") or {}
    label_nodes = ((node.get("labels") or {}).get("nodes")) or []
    commit_nodes = ((node.get("commits") or {}).get("nodes")) or []
    closing_nodes = ((node.get("closingIssuesReferences") or {}).get("nodes")) or []
    comment_nodes = ((node.get("comments") or {}).get("nodes")) or []

    state_raw = str(node.get("state", "")).lower()
    # GraphQL distinguishes MERGED; map to closed to match REST conventions
    # in the rest of the schema. Merged-ness is preserved via merged_at.
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
        labels=[lbl["name"] for lbl in label_nodes if lbl.get("name")],
        commit_titles=[
            _build_commit_entry(c["commit"]) for c in commit_nodes if c.get("commit")
        ],
        closing_issue_numbers=[int(n["number"]) for n in closing_nodes],
        comments=[
            {
                "author": (c.get("author") or {}).get("login"),
                "body": c.get("body") or "",
                "created_at": c.get("createdAt"),
            }
            for c in comment_nodes
        ],
    )


def _build_commit_entry(commit: dict) -> dict:
    """Shape a single PR commit node into the dict stored in commit_titles."""
    author = commit.get("author") or {}
    user = author.get("user") or {}
    return {
        "oid": commit.get("oid") or "",
        "headline": commit.get("messageHeadline", ""),
        "author_login": user.get("login"),
        "author_name": author.get("name"),
        "author_email": author.get("email"),
    }


def _parse_issue_node(node: dict) -> Issue:
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
        labels=[lbl["name"] for lbl in label_nodes if lbl.get("name")],
    )


def _graphql(query: str, **variables: str | None) -> dict:
    """Run a GraphQL query via ``gh api graphql`` and return the ``data`` payload."""
    args = ["api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if v is None:
            continue  # nullable variables are simply omitted
        args.extend(["-f", f"{k}={v}"])
    out = _run_gh(args)
    payload = json.loads(out)
    if "errors" in payload:
        msgs = "; ".join(e.get("message", "") for e in payload["errors"])
        raise GitHubError(f"GraphQL errors: {msgs}")
    return payload.get("data") or {}


def _run_gh(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitHubError(
            "gh CLI is not installed. Install from https://cli.github.com/"
        ) from exc
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise GitHubError(f"gh {' '.join(args[:2])} failed: {stderr}")
    return result.stdout
