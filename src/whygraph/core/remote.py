"""The seam between a linked project's tool bodies and its platform (M2e).

A *linked* project is a local checkout whose history lives on a WhyGraph
platform: there is no local WhyGraph database, so the MCP tool bodies
blame the working tree here and ask the platform about the **pushed**
SHAs. :class:`RemoteProject` is the narrow protocol they call to do that,
and :class:`whygraph.core.context.ProjectContext` carries one.

The protocol is what keeps :mod:`whygraph.core` free of
:mod:`whygraph.portal`: the only implementation,
:class:`whygraph.portal.linked.LinkedProject`, lives on the portal side
with the HTTP client, the connection token and the link row, while
:mod:`whygraph.mcp` sees nothing but these seven calls, two exception
types and the two display fields.

Every method returns JSON-ready data (plain ``dict`` / ``list``), already
stripped of platform bookkeeping, so a tool body can put it in a tool
result unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from whygraph.mcp.targets import Target
    from whygraph.services.git import BlameHunk

LINK_STATUSES = (
    "ok",
    "unreachable",
    "revoked",
    "removed",
    "access_lost",
    "update_required",
)
"""What :attr:`RemoteProject.status` may be (M2e plan section 4.11)."""


class RemoteError(Exception):
    """A :class:`RemoteProject` call did not produce an answer.

    The message is written for the agent that made the tool call and is
    free of credentials, so a tool body may surface it verbatim.

    Parameters
    ----------
    message : str
        What went wrong and what the user can do about it.
    status : str
        The link status this failure implies, one of
        :data:`LINK_STATUSES`. ``"ok"`` means the refusal was about this
        one request, not about the link.
    reason : str or None, optional
        The platform's machine-readable reason (a revocation reason, or an
        error code), when it gave one.

    Attributes
    ----------
    status : str
    reason : str or None
    """

    def __init__(self, message: str, *, status: str, reason: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


class RemoteNotFound(RemoteError):
    """The platform answered, and does not know the requested object.

    Raised for an unknown commit / PR / issue, so a tool body can fall
    back to local git (a commit) or report "not found" (a PR, an issue)
    instead of treating the link as broken.
    """


@runtime_checkable
class RemoteProject(Protocol):
    """What a linked project's platform can answer.

    Mirrors the platform's ``/api/v1/projects/{slug}`` routes one to one
    (see the M2e plan section 4.5). Each call either returns JSON-ready
    data or raises :class:`RemoteError`.

    Attributes
    ----------
    url : str
        Where the project is managed - the platform's org origin. Shown
        to the agent in a tool result's ``platform`` block.
    status : str
        The link status as of the latest call (:data:`LINK_STATUSES`).
    """

    url: str
    status: str

    def evidence(
        self, target: Target, hunks: Sequence[BlameHunk], limit: int
    ) -> tuple[list[dict], list[str]]:
        """Evidence for ``target``'s pushed ``hunks``.

        Returns
        -------
        tuple[list[dict], list[str]]
            The evidence items (the shape of
            :func:`whygraph.mcp.evidence._evidence_dict`) and the SHAs the
            platform's database does not know.
        """
        ...

    def rationale(self, target: Target, hunks: Sequence[BlameHunk]) -> dict:
        """The rationale card of ``target``, from the platform's shared cache."""
        ...

    def history(self, path: str, limit: int, include_renames: bool) -> list[dict]:
        """The area-history evidence items for ``path``."""
        ...

    def commit(self, sha: str) -> dict:
        """A scanned commit and the pull requests containing it."""
        ...

    def pr(self, number: int) -> dict:
        """A pull request and the issues it closes."""
        ...

    def issue(self, number: int) -> dict:
        """An issue and the pull requests that close it."""
        ...

    def overview(self) -> dict:
        """The repository-level summary."""
        ...


def platform_block(remote: RemoteProject) -> dict[str, Any]:
    """The ``platform`` block a linked project's tool result carries.

    Parameters
    ----------
    remote : RemoteProject
        The project's remote, after the call it just made.

    Returns
    -------
    dict
        ``{"status": <link status>, "url": <org origin>}``.
    """
    return {"status": remote.status, "url": remote.url}


__all__ = [
    "LINK_STATUSES",
    "RemoteError",
    "RemoteNotFound",
    "RemoteProject",
    "platform_block",
]
