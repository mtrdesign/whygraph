"""The platform side of a linked project's MCP calls (M2e step 8).

:class:`LinkedProject` is the one implementation of
:class:`whygraph.core.remote.RemoteProject`: it turns a tool body's
request into an ``/api/v1`` call through :class:`PlatformHttp`, hands back
JSON-ready data, and turns every failure into a
:class:`~whygraph.core.remote.RemoteError` whose message names the fix.

Two things live here rather than in :mod:`whygraph.mcp` on purpose:

* **The credential.** Only this side knows the connection token, the org
  and the project slug on the platform; the MCP tool bodies see seven
  method names and nothing else.
* **The link status.** Every answer and every refusal maps to the link's
  status through :func:`~whygraph.portal.platform_client.link_status_for`
  (the table of plan section 4.11). The mapping is recorded on the
  instance (:attr:`LinkedProject.status`, :attr:`LinkedProject.reason`),
  which is what a tool result's ``platform`` block reports; persisting it
  to the ``platform_links`` row, and refreshing it on a timer, is step 9's
  job and deliberately not done here.

Request bodies are built with :mod:`whygraph.api_v1`, so the privacy and
size rules the platform enforces are checked **before** anything leaves
the machine: a hunk whose path or SHA the shared models refuse is dropped
rather than sent.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Sequence, TypeVar

from pydantic import ValidationError

from whygraph.api_v1 import (
    MAX_HUNKS,
    MAX_LIMIT,
    MAX_ORIGINS,
    EvidenceIn,
    HunkIn,
    OriginIn,
    RationaleIn,
    TargetIn,
)
from whygraph.core.remote import RemoteError, RemoteNotFound
from whygraph.mcp.targets import Target
from whygraph.services.git import BlameHunk

from .platform_client import (
    PlatformError,
    PlatformHttp,
    PlatformRefused,
    link_status_for,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

NOT_FOUND_CODE = "not_found"
"""The platform's code for an object it does not hold."""


def _target_in(target: Target) -> TargetIn:
    """The wire form of an already-reduced target (see ``mcp.evidence``)."""
    return TargetIn(
        path=target.path,
        line_start=target.line_start,
        line_end=target.line_end,
        qualified_name=target.qualified_name,
    )


def _hunks_in(hunks: Sequence[BlameHunk]) -> list[HunkIn]:
    """The wire form of the pushed hunks, within the contract's size limits.

    A hunk the shared models refuse - an odd path, an out-of-range line -
    is skipped rather than sent, because the platform would refuse the
    whole request for it.
    """
    out: list[HunkIn] = []
    for hunk in hunks[:MAX_HUNKS]:
        try:
            out.append(
                HunkIn(
                    sha=hunk.sha,
                    origins=[
                        OriginIn(path=o.path, start=o.start, end=o.end)
                        for o in hunk.origins[:MAX_ORIGINS]
                    ],
                )
            )
        except ValidationError:
            logger.debug("skipping a hunk the /api/v1 contract refuses")
    return out


class LinkedProject:
    """A project on a WhyGraph platform, as its linked checkout sees it.

    Parameters
    ----------
    client : PlatformHttp
        Bound to the platform's org origin and carrying the connection
        token.
    slug : str
        The project's slug **on the platform**.
    org : str
        Its org slug there.
    platform_origin : str
        The platform's base origin, for the messages.
    status : str, optional
        The link's last known status (:data:`~whygraph.core.remote.LINK_STATUSES`).
    reason : str or None, optional
        Its last known reason.

    Attributes
    ----------
    url : str
        The org origin - where the project is managed.
    status : str
        The status implied by the most recent call.
    reason : str or None
        Its reason, when the platform gave one.
    """

    def __init__(
        self,
        client: PlatformHttp,
        *,
        slug: str,
        org: str,
        platform_origin: str,
        status: str = "ok",
        reason: str | None = None,
    ) -> None:
        self._client = client
        self.slug = slug
        self.org = org
        self.platform_origin = platform_origin
        self.status = status
        self.reason = reason

    @property
    def url(self) -> str:
        """Where the project is managed (the platform's org origin)."""
        return self._client.api_origin or self.platform_origin

    def __repr__(self) -> str:
        return (
            f"LinkedProject({self.org}/{self.slug} on {self.platform_origin}, "
            f"status={self.status})"
        )

    # -- plumbing -----------------------------------------------------------

    def _guard(self, call: Callable[[], _T]) -> _T:
        """Run one platform call, recording what it says about the link."""
        try:
            value = call()
        except PlatformError as exc:
            status, reason = link_status_for(exc)
            self.status, self.reason = status, reason
            raise self._error(exc, status, reason) from exc
        status, reason = link_status_for(self._client.last_project)
        self.status, self.reason = status, reason
        return value

    def _error(
        self, exc: PlatformError, status: str, reason: str | None
    ) -> RemoteError:
        """The tool-facing error for a failed call: what happened, and the fix."""
        if (
            isinstance(exc, PlatformRefused)
            and exc.code == NOT_FOUND_CODE
            and status == "ok"
        ):
            return RemoteNotFound(str(exc), status=status, reason=reason)
        where = f"{self.org}/{self.slug} on {self.platform_origin}"
        if status == "unreachable":
            message = (
                f"platform unreachable - {self.url} did not answer; until it "
                "does, only this checkout's git history is available"
            )
        elif status == "update_required":
            message = (
                f"update WhyGraph - {self.platform_origin} no longer speaks this "
                "version's API; run the installer again, then `whygraph up`"
            )
        elif status == "removed":
            message = (
                f"{where} was removed on the platform - remove it from this "
                "machine in the WhyGraph portal"
            )
        elif status == "revoked":
            named = f" ({reason})" if reason else ""
            message = (
                f"access to {where} was revoked{named} - reconnect the project "
                "from the WhyGraph portal, or remove it from this machine"
            )
        elif status == "access_lost":
            message = (
                f"the platform can no longer reach {where}'s repository - fix "
                "its access on the platform"
            )
        else:
            message = str(exc)
        return RemoteError(message, status=status, reason=reason)

    @staticmethod
    def _without_project(body: dict) -> dict:
        """The platform's reply without its ``project`` bookkeeping block."""
        return {key: value for key, value in body.items() if key != "project"}

    # -- the RemoteProject surface ------------------------------------------

    def evidence(
        self, target: Target, hunks: Sequence[BlameHunk], limit: int
    ) -> tuple[list[dict], list[str]]:
        """``POST /evidence``: the platform's evidence for the pushed hunks.

        Parameters
        ----------
        target : Target
            Already reduced to what may be sent
            (:func:`whygraph.mcp.evidence.pushed_target`).
        hunks : Sequence[BlameHunk]
            The pushed hunks only.
        limit : int
            Cap on the items, clamped to the contract's maximum.

        Returns
        -------
        tuple[list[dict], list[str]]
            The evidence items and the SHAs its database does not know.

        Raises
        ------
        RemoteError
            The platform could not be reached, or refused.
        """
        body = self._body(
            lambda: EvidenceIn(
                target=_target_in(target),
                hunks=_hunks_in(hunks),
                limit=max(1, min(int(limit), MAX_LIMIT)),
            )
        )
        reply = self._guard(lambda: self._client.evidence(self.slug, body))
        return (
            [item.model_dump(mode="json") for item in reply.evidence],
            list(reply.unknown_shas),
        )

    def rationale(self, target: Target, hunks: Sequence[BlameHunk]) -> dict:
        """``POST /rationale``: a card from the platform's shared cache.

        Raises
        ------
        RemoteError
            The platform could not be reached, or refused (a generation
            limit and a missing key arrive this way too, with the
            platform's own message).
        """
        body = self._body(
            lambda: RationaleIn(target=_target_in(target), hunks=_hunks_in(hunks))
        )
        card = self._guard(lambda: self._client.rationale(self.slug, body))
        return card.model_dump(mode="json")

    def history(self, path: str, limit: int, include_renames: bool) -> list[dict]:
        """``GET /history``: the area-history items for ``path``."""
        data = self._guard(
            lambda: self._client.history(
                self.slug,
                path,
                max(1, min(int(limit), MAX_LIMIT)),
                include_renames,
            )
        )
        items = data.get("evidence")
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def commit(self, sha: str) -> dict:
        """``GET /commits/{sha}``."""
        return self._without_project(
            self._guard(lambda: self._client.commit(self.slug, sha))
        )

    def pr(self, number: int) -> dict:
        """``GET /prs/{number}``."""
        return self._without_project(
            self._guard(lambda: self._client.pr(self.slug, number))
        )

    def issue(self, number: int) -> dict:
        """``GET /issues/{number}``."""
        return self._without_project(
            self._guard(lambda: self._client.issue(self.slug, number))
        )

    def overview(self) -> dict:
        """``GET /overview``."""
        return self._without_project(
            self._guard(lambda: self._client.overview(self.slug))
        )

    def revoke(self) -> None:
        """``DELETE /token``: give up this machine's access (step 9's removal)."""
        self._guard(lambda: self._client.revoke(self.slug))

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def _body(self, build: Callable[[], _T]) -> _T:
        """Build a request body, turning a contract violation into a refusal."""
        try:
            return build()
        except ValidationError as exc:
            raise RemoteError(
                "this target cannot be sent to the platform: "
                f"{exc.error_count()} field(s) the /api/v1 contract refuses",
                status="ok",
                reason="bad_request",
            ) from None


def status_of(outcome: Any) -> tuple[str, str | None]:
    """A platform answer's ``(status, reason)`` for a link (plan section 4.11).

    A thin alias of
    :func:`whygraph.portal.platform_client.link_status_for`, re-exported
    here so step 9's status refresh and this module read the mapping from
    one place.

    Parameters
    ----------
    outcome : Any
        A client exception, a ``StatusOut``, an HTTP status, or ``None``.

    Returns
    -------
    tuple[str, str or None]
        The link status and its reason.
    """
    return link_status_for(outcome)


__all__ = ["NOT_FOUND_CODE", "LinkedProject", "status_of"]
