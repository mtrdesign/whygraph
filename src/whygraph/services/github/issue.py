"""In-memory value object for a parsed GitHub issue.

Exposes :class:`Issue` plus the per-node parser that produces it from
a GitHub GraphQL response. GitHub's GraphQL schema separates issues
from pull requests (unlike the REST ``/issues`` endpoint, which mixes
them), so this module deals only with true issues.
"""

from __future__ import annotations

from dataclasses import dataclass


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

    @classmethod
    def from_graphql_node(cls, node: dict) -> "Issue":
        """Parse one GraphQL issue node into an :class:`Issue`.

        Expects the shape produced by the query in
        :mod:`whygraph.services.github.issues`: ``number``, ``title``,
        ``body``, ``state``, ``createdAt``, ``updatedAt``, ``closedAt``,
        ``author { login }``, ``url``, and ``labels.nodes`` each
        carrying ``name``.

        Parameters
        ----------
        node : dict
            One issue node from the ``repository.issues`` connection.

        Returns
        -------
        Issue
            The parsed issue.
        """
        author = node.get("author") or {}
        label_nodes = ((node.get("labels") or {}).get("nodes")) or []
        return cls(
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
