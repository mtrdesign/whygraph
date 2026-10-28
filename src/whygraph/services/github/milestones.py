"""Reusable, lazy view of every milestone in a GitHub repository.

Milestones are few, so the crawl re-lists all of them (open and closed)
every time; there is no incremental form. Mirrors
:mod:`whygraph.services.github.issues`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from whygraph.core import Shell

from .commands import paginate_graphql

_LIST_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    milestones(
      first: 100
      after: $cursor
      states: [OPEN, CLOSED]
      orderBy: {field: NUMBER, direction: ASC}
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title description state
        dueOn closedAt updatedAt
        progressPercentage url
      }
    }
  }
}
"""


@dataclass(frozen=True, slots=True)
class Milestone:
    """A GitHub milestone.

    Attributes
    ----------
    number : int
        Milestone number within the repository.
    title : str
        Milestone title.
    description : str or None
        Description; ``None`` if empty.
    state : str
        Lowercased state: ``"open"`` or ``"closed"``.
    due_on, closed_at : str or None
        ISO 8601 timestamps; ``None`` when unset.
    updated_at : str
        ISO 8601 timestamp.
    progress_pct : float or None
        GitHub's ``progressPercentage`` (closed share of its issues and PRs).
    html_url : str
        Browser URL.
    """

    number: int
    title: str
    description: str | None
    state: str
    due_on: str | None
    closed_at: str | None
    updated_at: str
    progress_pct: float | None
    html_url: str

    @classmethod
    def from_graphql_node(cls, node: dict) -> "Milestone":
        """Parse one GraphQL milestone node into a :class:`Milestone`.

        Parameters
        ----------
        node : dict
            One node from the ``repository.milestones`` connection.

        Returns
        -------
        Milestone
            The parsed milestone.
        """
        progress = node.get("progressPercentage")
        return cls(
            number=int(node["number"]),
            title=node.get("title", ""),
            description=node.get("description") or None,
            state=str(node.get("state", "")).lower(),
            due_on=node.get("dueOn"),
            closed_at=node.get("closedAt"),
            updated_at=node["updatedAt"],
            progress_pct=None if progress is None else float(progress),
            html_url=node["url"],
        )


class Milestones:
    """All milestones in a repository; each iteration re-queries GitHub.

    Parameters
    ----------
    owner : str
        Repository owner (user or organization).
    name : str
        Repository name (without the ``.git`` suffix).
    shell : Shell, optional
        Override the subprocess wrapper used to run ``gh``.

    Raises
    ------
    GitHubError
        From iteration if ``gh`` fails, the response is malformed, or
        GitHub reports GraphQL errors.
    """

    def __init__(self, owner: str, name: str, *, shell: Shell | None = None) -> None:
        self.owner = owner
        self.name = name
        self._shell = shell or Shell()

    def __repr__(self) -> str:
        return f"Milestones(owner={self.owner!r}, name={self.name!r})"

    def __iter__(self) -> Iterator[Milestone]:
        for node in paginate_graphql(
            self._shell,
            query=_LIST_QUERY,
            path=("repository", "milestones"),
            variables={"owner": self.owner, "name": self.name},
        ):
            yield Milestone.from_graphql_node(node)
