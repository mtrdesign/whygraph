"""Named ``gh`` invocations as :class:`ShellCommand` argv+parser pairs.

Mirrors :mod:`whygraph.services.git.commands` for the GitHub side: the
single place that knows the shape of a ``gh api graphql`` call. Higher
layers (the per-resource collections, the client) compose against
:class:`GhApiGraphqlCmd` and :func:`paginate_graphql` rather than
hand-rolling argv strings.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from subprocess import CompletedProcess
from typing import Any

from whygraph.core import Shell, ShellCommand, ShellError

from .exceptions import GitHubError


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


def paginate_graphql(
    shell: Shell,
    *,
    query: str,
    path: tuple[str, ...],
    variables: Mapping[str, str],
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
    while True:
        request_vars = {**static}
        if cursor is not None:
            request_vars["cursor"] = cursor
        try:
            data = shell.run(GhApiGraphqlCmd(query, request_vars))
        except ShellError as exc:
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
