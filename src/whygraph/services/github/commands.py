"""Named ``gh`` invocations as :class:`ShellCommand` argv+parser pairs.

Mirrors :mod:`whygraph.services.git.commands` for the GitHub side: the
single place that knows the shape of a ``gh api graphql`` call. Higher
layers (the per-resource collections, the client) compose against
:class:`GhApiGraphqlCmd` and :func:`paginate_graphql` rather than
hand-rolling argv strings.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta, timezone
from subprocess import CompletedProcess
from typing import Any

from whygraph.core import Shell, ShellCommand, ShellError

from .exceptions import GitHubError
from .token import GH_TOKEN_ENV, token_env


class GhApiGraphqlCmd(ShellCommand[dict]):
    """``gh api graphql -f query=… [-f var=val …]`` — one GraphQL request.

    Returns the ``data`` payload of the GraphQL response, with the
    top-level ``errors`` array (if any) turned into a
    :class:`GitHubError`. Authentication, retries, and rate-limit
    handling are delegated to the ``gh`` CLI — this command is only
    responsible for shaping argv and decoding stdout.

    Parameters
    ----------
    query : str
        The GraphQL query string.
    variables : Mapping[str, str], optional
        GraphQL variables, passed to ``gh`` as ``-f key=value`` pairs.
        ``gh`` only accepts string-valued ``-f`` flags, so callers must
        pre-stringify any cursors/IDs. Defaults to empty.
    """

    def __init__(
        self,
        query: str,
        variables: Mapping[str, str] | None = None,
    ) -> None:
        self._query = query
        self._variables = dict(variables) if variables else {}

    def argv(self) -> list[str]:
        args = ["gh", "api", "graphql", "-f", f"query={self._query}"]
        for key, value in self._variables.items():
            args.extend(["-f", f"{key}={value}"])
        return args

    def parse(self, result: CompletedProcess[str]) -> dict:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubError(f"gh returned non-JSON output: {exc}") from exc
        if "errors" in payload:
            msgs = "; ".join(e.get("message", "") for e in payload["errors"])
            raise GitHubError(f"GraphQL errors: {msgs}")
        return payload.get("data") or {}


_log = logging.getLogger(__name__)

Tolerate = Callable[[dict], bool]
"""Decides, from a failed call's decoded JSON answer, that it is not a failure."""


def forbidden_under(field: str) -> Tolerate:
    """Build a ``tolerate`` predicate for a scoped ``FORBIDDEN`` answer.

    Parameters
    ----------
    field : str
        A GraphQL field name, e.g. ``"reviewRequests"``.

    Returns
    -------
    Callable[[dict], bool]
        ``True`` when the answer carries ``data`` and **every** entry of
        its ``errors`` has ``type: FORBIDDEN`` and a ``path`` through
        ``field``. Any other error, or a mix, is not tolerated.
    """

    def tolerate(payload: dict) -> bool:
        errors = payload.get("errors")
        return (
            isinstance(payload.get("data"), dict)
            and isinstance(errors, list)
            and bool(errors)
            and all(
                isinstance(e, dict)
                and e.get("type") == "FORBIDDEN"
                and field in (e.get("path") or [])
                for e in errors
            )
        )

    return tolerate


def tolerated_data(exc: ShellError, tolerate: Tolerate | None) -> dict | None:
    """Return the ``data`` of a failed ``gh api graphql`` call that is tolerable.

    ``gh`` exits non-zero when the answer carries ``errors``, but its
    stdout still holds the JSON, partial ``data`` included.

    Parameters
    ----------
    exc : ShellError
        The failed invocation.
    tolerate : Callable[[dict], bool] or None
        The predicate; ``None`` tolerates nothing.

    Returns
    -------
    dict or None
        The answer's ``data`` when tolerated (one warning is logged with
        the error count), else ``None``.
    """
    if tolerate is None:
        return None
    try:
        payload = json.loads(exc.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or not tolerate(payload):
        return None
    _log.warning(
        "GitHub refused %d nested field(s) (FORBIDDEN); dropped them, kept the rest",
        len(payload["errors"]),
    )
    return payload["data"]


LOOKBACK = timedelta(minutes=10)
"""How far below a stored watermark an incremental crawl starts (late-visible updates)."""


def parse_github_ts(value: str) -> datetime:
    """Parse a GitHub ``DateTime`` into an aware UTC datetime.

    Parameters
    ----------
    value : str
        A timestamp such as ``2026-10-26T12:00:00Z``.

    Returns
    -------
    datetime
        The same instant, timezone-aware.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_github_ts(value: datetime) -> str:
    """Format an aware datetime as GitHub's ``YYYY-MM-DDTHH:MM:SSZ``.

    Parameters
    ----------
    value : datetime
        A timezone-aware datetime.

    Returns
    -------
    str
        The instant in UTC, to whole seconds.
    """
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def floor_for(watermark: str) -> datetime:
    """Return ``watermark`` minus :data:`LOOKBACK`.

    Parameters
    ----------
    watermark : str
        A stored GitHub timestamp.

    Returns
    -------
    datetime
        The oldest ``updatedAt`` an incremental crawl reads.
    """
    return parse_github_ts(watermark) - LOOKBACK


GATEWAY_TIMEOUTS = ("HTTP 502", "HTTP 504")
"""``gh`` stderr markers of a query GitHub gave up on (too heavy for its limit)."""

PAGE_SIZES = (50, 25, 10)
"""Page sizes tried in turn when a page times out upstream.

A 100-PR page of the full PR fields took 8-12 s on ``biomejs/biome`` (often a
502); 50 took about 7 s.
"""


def is_gateway_timeout(exc: ShellError) -> bool:
    """Return whether ``exc`` is a GitHub 502 / 504 (an over-heavy query).

    Parameters
    ----------
    exc : ShellError
        The failed ``gh`` invocation.

    Returns
    -------
    bool
        ``True`` when stderr names ``HTTP 502`` or ``HTTP 504``.
    """
    return any(marker in exc.stderr for marker in GATEWAY_TIMEOUTS)


def paginate_graphql(
    shell: Shell,
    *,
    query: str,
    path: tuple[str, ...],
    variables: Mapping[str, str],
    query_for_size: Callable[[int], str] | None = None,
    tolerate: Tolerate | None = None,
) -> Iterator[dict]:
    """Yield ``nodes`` from a paginated GraphQL connection.

    Threads cursor-based pagination through repeated
    :class:`GhApiGraphqlCmd` invocations. The query must accept a
    ``$cursor: String`` variable and select ``pageInfo { hasNextPage
    endCursor }`` alongside ``nodes`` at the connection located by
    ``path``.

    Parameters
    ----------
    shell : Shell
        Subprocess wrapper used to run each ``gh`` invocation.
    query : str
        The GraphQL query string. Must reference ``$cursor`` so this
        helper can drive pagination.
    path : tuple[str, ...]
        Key path through the ``data`` payload to the connection object
        (e.g. ``("repository", "pullRequests")``).
    variables : Mapping[str, str]
        Static variables passed to every invocation (typically
        ``{"owner": owner, "name": name}``). The ``cursor`` variable is
        injected by this function and overrides any caller value.
    query_for_size : Callable[[int], str], optional
        Builds the query for a page size. When given, ``query`` is
        ignored; a ``HTTP 502`` / ``504`` retries the same cursor at the
        next size of :data:`PAGE_SIZES` (50, 25, 10) and the reduced
        size stays for the rest of the crawl. Without it a failure
        raises at once.
    tolerate : Callable[[dict], bool], optional
        Checked on the decoded stdout of a failed call before anything
        else (:func:`tolerated_data`); when it accepts the answer, the
        page's ``data`` is used instead of raising.

    Notes
    -----
    Each page is its own ``gh`` process, so a token file the portal
    refreshes is re-read for every page (:func:`~.token.token_env`).

    Yields
    ------
    dict
        One ``nodes`` entry per yielded value.

    Raises
    ------
    GitHubError
        On ``gh`` subprocess failure, malformed response, or when the
        ``path`` does not resolve in the response.
    """
    static = dict(variables)
    cursor: str | None = None
    size_index = 0
    while True:
        request_vars = {**static}
        if cursor is not None:
            request_vars["cursor"] = cursor
        text = query_for_size(PAGE_SIZES[size_index]) if query_for_size else query
        try:
            data = shell.run(
                GhApiGraphqlCmd(text, request_vars), env=token_env(GH_TOKEN_ENV)
            )
        except ShellError as exc:
            data = tolerated_data(exc, tolerate)
            if data is not None:
                pass
            elif (
                query_for_size is not None
                and is_gateway_timeout(exc)
                and size_index + 1 < len(PAGE_SIZES)
            ):
                size_index += 1
                continue
            else:
                detail = exc.stderr.strip() or exc.stdout.strip()
                raise GitHubError(f"gh api graphql failed: {detail}") from exc
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
