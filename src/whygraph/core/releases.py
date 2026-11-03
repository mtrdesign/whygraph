"""WhyGraph release resolution, shared by the portal and the CLI (plan 43).

The portal's daily update check and the host's ``whygraph update`` (through
the hidden ``whygraph release-check`` command) both answer the same
question: given the running version and GitHub's release list, is there a
newer release, and does any release on the way need manual steps? This
module answers it once, in tested Python.

Two halves:

* :func:`fetch_releases` reads ``GET /repos/mtrdesign/whygraph/releases``
  and keeps, per release, only a validated ``X.Y.Z`` version, a
  timezone-aware publish time and one boolean (the manual-upgrade marker).
  No remote text, HTML or URL survives, so a spoofed response cannot
  inject anything; the release-notes link is built locally from
  :data:`NOTES_URL`. The response is streamed under
  :data:`MAX_RESPONSE_BYTES` and :data:`TOTAL_DEADLINE_SEC`, redirects are
  never followed, and a malformed item is skipped rather than fatal.
* :func:`resolve` is pure: it picks the target (after an optional grace
  period), compares it with the running version, and reports whether a
  release in ``(current, target]`` carries :data:`MANUAL_MARKER` and whether
  that in-between list is known to be complete.

Click-free and portal-free: :mod:`whygraph.core` never imports
:mod:`whygraph.portal`.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any, Sequence

import httpx

__all__ = [
    "FEED_URL",
    "MANUAL_MARKER",
    "MAX_RESPONSE_BYTES",
    "NOTES_URL",
    "PAGE_SIZE",
    "TOTAL_DEADLINE_SEC",
    "Release",
    "ReleaseCheckError",
    "Resolution",
    "build_client",
    "fetch_releases",
    "format_version",
    "parse_version",
    "resolve",
    "running_version",
]

FEED_URL = "https://api.github.com/repos/mtrdesign/whygraph/releases?per_page=30"
"""The release list (one page, newest first) of ``mtrdesign/whygraph``."""

PAGE_SIZE = 30
"""How many releases one :data:`FEED_URL` page holds (its ``per_page``)."""

NOTES_URL = "https://mtrdesign.github.io/whygraph/releases/{version}/"
"""The release-notes page, formatted with :func:`format_version` only."""

MANUAL_MARKER = "<!-- whygraph:manual-upgrade -->"
"""The marker in a release body that flags a release needing manual steps."""

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
"""The most a release list may carry, decompressed (2 MiB)."""

TOTAL_DEADLINE_SEC = 20.0
"""Whole-response bound, on top of the per-read ``Timeout(10, connect=5)``."""

_PART = r"(0|[1-9][0-9]{0,3})"
_SEMVER = re.compile(rf"{_PART}\.{_PART}\.{_PART}")

_REQUEST_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

Version = tuple[int, int, int]


def parse_version(text: str) -> Version | None:
    """Parse a strict ``X.Y.Z`` version.

    Parameters
    ----------
    text : str
        The candidate, without a leading ``v``.

    Returns
    -------
    tuple of int or None
        ``(major, minor, patch)``, or ``None`` when ``text`` is not exactly
        three dot-separated ASCII numbers of at most four digits without
        leading zeros (no surrounding whitespace, no pre-release suffix).
    """
    match = _SEMVER.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return (major, minor, patch)


def format_version(version: Version) -> str:
    """Rebuild ``X.Y.Z`` from the ints, so no URL is built from remote text.

    Parameters
    ----------
    version : tuple of int
        ``(major, minor, patch)``.

    Returns
    -------
    str
        The dotted version.
    """
    major, minor, patch = version
    return f"{int(major)}.{int(minor)}.{int(patch)}"


@dataclass(frozen=True)
class Release:
    """One published WhyGraph release, reduced to what is trusted.

    Attributes
    ----------
    version : tuple of int
        ``(major, minor, patch)`` from the ``vX.Y.Z`` tag.
    published_at : datetime
        When it was published (timezone-aware).
    manual : bool
        Whether its notes carry :data:`MANUAL_MARKER`.
    """

    version: Version
    published_at: datetime
    manual: bool


class ReleaseCheckError(Exception):
    """The release list could not be fetched or understood.

    Parameters
    ----------
    kind : str
        ``"network"``, ``"rate_limited"`` or ``"bad_response"``.

    Attributes
    ----------
    kind : str
        The failure kind, as passed.
    """

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class Resolution:
    """The answer of :func:`resolve`.

    Attributes
    ----------
    status : str
        ``"update"`` (target newer than current), ``"current"`` (equal),
        ``"downgrade"`` (an explicit target older than current),
        ``"ahead"`` (current newer than the newest release, a dev build) or
        ``"unknown"`` (no usable release, an unknown explicit target, or no
        current version to compare with).
    target : Release or None
        The resolved release; ``None`` when there is none.
    manual : bool
        Whether a release in ``(current, target]`` (every release up to the
        target when current is unknown) carries the manual marker.
    verified : bool
        Whether that in-between list is known to be complete: current is
        known and the oldest fetched release is not newer than current.
    """

    status: str
    target: Release | None
    manual: bool
    verified: bool


def build_client(
    user_agent: str, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """Build the HTTP client :func:`fetch_releases` expects.

    Parameters
    ----------
    user_agent : str
        The ``User-Agent`` to send: ``whygraph-portal`` or ``whygraph-cli``.
        Never a version.
    transport : httpx.BaseTransport, optional
        A transport to use instead of the network (tests).

    Returns
    -------
    httpx.Client
        A client with ``Timeout(10.0, connect=5.0)``, redirects off and the
        given ``User-Agent``. The caller closes it (``with`` block).
    """
    return httpx.Client(
        timeout=httpx.Timeout(10.0, connect=5.0),
        follow_redirects=False,
        headers={"User-Agent": user_agent},
        transport=transport,
    )


def fetch_releases(client: httpx.Client) -> list[Release]:
    """Fetch and validate the WhyGraph release list.

    Parameters
    ----------
    client : httpx.Client
        The client to send with, normally from :func:`build_client`. The
        ``User-Agent`` comes from the client's headers; this function adds
        only ``Accept`` and ``X-GitHub-Api-Version`` and never a version,
        cookie or token. Redirects are refused per request whatever the
        client's setting.

    Returns
    -------
    list of Release
        The valid, published (non-draft, non-prerelease) releases, newest
        version first. Items that fail validation are skipped.

    Raises
    ------
    ReleaseCheckError
        ``rate_limited`` on 403 / 429; ``bad_response`` on any other
        non-200 (a redirect included), a body over
        :data:`MAX_RESPONSE_BYTES`, an undecodable body, JSON that does not
        parse, or a root that is not a list; ``network`` on a transport
        error or when the whole response takes longer than
        :data:`TOTAL_DEADLINE_SEC`.
    """
    deadline = time.monotonic() + TOTAL_DEADLINE_SEC
    try:
        with client.stream(
            "GET", FEED_URL, headers=_REQUEST_HEADERS, follow_redirects=False
        ) as response:
            status = response.status_code
            if status in (403, 429):
                raise ReleaseCheckError("rate_limited")
            if status != 200:
                raise ReleaseCheckError("bad_response")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                raise ReleaseCheckError("bad_response")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise ReleaseCheckError("network")
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ReleaseCheckError("bad_response")
                chunks.append(chunk)
    except httpx.DecodingError:
        raise ReleaseCheckError("bad_response") from None
    except httpx.HTTPError:
        raise ReleaseCheckError("network") from None
    try:
        data = json.loads(b"".join(chunks))
    except (ValueError, RecursionError):
        raise ReleaseCheckError("bad_response") from None
    if not isinstance(data, list):
        raise ReleaseCheckError("bad_response")
    releases = [rel for rel in map(_parse_item, data) if rel is not None]
    releases.sort(key=lambda rel: rel.version, reverse=True)
    return releases


def _parse_item(item: Any) -> Release | None:
    """One list item as a :class:`Release`, or ``None`` when it fails validation."""
    if not isinstance(item, dict):
        return None
    if item.get("draft") is not False or item.get("prerelease") is not False:
        return None
    tag = item.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        return None
    version = parse_version(tag[1:])
    if version is None:
        return None
    published = item.get("published_at")
    if not isinstance(published, str):
        return None
    try:
        published_at = datetime.fromisoformat(published)
    except ValueError:
        return None
    if published_at.tzinfo is None or published_at.utcoffset() is None:
        return None
    body = item.get("body")
    manual = isinstance(body, str) and MANUAL_MARKER in body
    return Release(version=version, published_at=published_at, manual=manual)


def resolve(
    releases: Sequence[Release],
    current: Version | None,
    *,
    target: Version | None = None,
    now: datetime | None = None,
    grace_sec: float = 0,
) -> Resolution:
    """Resolve the target release against the running version.

    Parameters
    ----------
    releases : sequence of Release
        What :func:`fetch_releases` returned (any order).
    current : tuple of int or None
        The running version (:func:`running_version`); ``None`` for a
        non-semver build.
    target : tuple of int, optional
        An explicit target (``whygraph update --version``). It must be one
        of the candidates, else the status is ``"unknown"`` with no target.
    now : datetime, optional
        The current time (timezone-aware). With ``grace_sec``, releases
        published after ``now - grace_sec`` are not candidates yet. When
        ``None``, every release is a candidate.
    grace_sec : float, optional
        How long after publishing a release becomes a candidate (default 0).

    Returns
    -------
    Resolution
        See :class:`Resolution`. Without an explicit target the newest
        candidate is the target; with no current version the status is
        ``"unknown"`` but the target, ``manual`` (over every release up to
        the target) and ``verified`` (false) are still filled.
    """
    if now is None:
        candidates = list(releases)
    else:
        cutoff = now - timedelta(seconds=grace_sec)
        candidates = [rel for rel in releases if rel.published_at <= cutoff]
    if target is not None:
        chosen = next((rel for rel in candidates if rel.version == target), None)
        if chosen is None:
            return Resolution("unknown", None, False, False)
    elif candidates:
        chosen = max(candidates, key=lambda rel: rel.version)
    else:
        return Resolution("unknown", None, False, False)

    top = chosen.version
    manual = any(
        rel.manual and (current is None or rel.version > current) and rel.version <= top
        for rel in releases
    )
    # Only a list that reaches back to the running version proves no release
    # in between was missed; a short page is not proof, because skipped
    # (draft / prerelease / invalid) items also shorten it (plan 43, D10).
    verified = current is not None and min(rel.version for rel in releases) <= current
    if current is None:
        status = "unknown"
    elif top > current:
        status = "update"
    elif top == current:
        status = "current"
    elif target is not None:
        status = "downgrade"
    else:
        status = "ahead"
    return Resolution(status, chosen, manual, verified)


def running_version() -> Version | None:
    """The installed ``whygraph`` version, parsed.

    Repeats ``portal.routes._package_version``'s lookup, because that module
    is too heavy to import from :mod:`whygraph.core`.

    Returns
    -------
    tuple of int or None
        ``(major, minor, patch)``, or ``None`` when the package metadata is
        missing or the version is not a plain ``X.Y.Z`` (a dev build).
    """
    try:
        return parse_version(_pkg_version("whygraph"))
    except PackageNotFoundError:
        return None
