"""Repository access probe for a candidate GitHub token.

:meth:`GitHubClient.check_auth` only says whether ``gh`` is authenticated.
The portal's add-project screen also needs to tell a bad token from a
token that lacks access to *this* repository from a repository that does
not exist, so :func:`check_repo_access` calls ``gh api repos/<owner>/<repo>``
with the candidate token and maps the HTTP status.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

from whygraph.core import Shell
from whygraph.services.git import pass_through_env, redact_tokens

from .exceptions import GitHubError, RepoAccessError

_HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")


@dataclass(frozen=True)
class RepoAccess:
    """What the probe learned about a repository the token can read.

    Attributes
    ----------
    full_name : str
        ``owner/name`` as GitHub spells it.
    private : bool
        Whether the repository is private.
    default_branch : str or None
        The repository's default branch, when reported.
    """

    full_name: str
    private: bool
    default_branch: str | None


def github_env(token: str | None) -> dict[str, str]:
    """Environment for a ``gh`` call made with an explicit token.

    The allowlisted pass-through environment plus ``GH_TOKEN`` (when a
    token is given). Nothing else is inherited, so the portal's own
    ``GH_TOKEN`` / ``GITHUB_TOKEN`` can never be used by mistake.

    Parameters
    ----------
    token : str or None
        The candidate token, or ``None`` for an unauthenticated call.

    Returns
    -------
    dict[str, str]
        A complete child environment.
    """
    env = pass_through_env()
    if token:
        env["GH_TOKEN"] = token
    return env


def check_repo_access(
    owner: str,
    name: str,
    token: str | None,
    *,
    shell: Shell | None = None,
) -> RepoAccess:
    """Probe ``GET /repos/<owner>/<name>`` with a candidate token.

    Parameters
    ----------
    owner : str
        Repository owner (already validated, e.g. by
        :func:`whygraph.services.git.parse_github_url`).
    name : str
        Repository name.
    token : str or None
        The candidate token; passed to ``gh`` only through ``GH_TOKEN``.
    shell : Shell, optional
        Shell used to run ``gh``. Defaults to a fresh one.

    Returns
    -------
    RepoAccess
        Summary of the repository, when the token can read it.

    Raises
    ------
    RepoAccessError
        ``code`` is ``"bad_token"`` (HTTP 401, or no credential at all),
        ``"no_access"`` (HTTP 403: SSO not authorised, missing scope or
        permission) or ``"not_found"`` (HTTP 404). GitHub also answers 404
        for a private repository the token cannot see, so ``not_found``
        means "does not exist or is not visible to this token".
    GitHubError
        For anything else: ``gh`` missing, a timeout, a rate limit, a
        network failure, or a malformed response.
    """
    runner = shell or Shell()
    try:
        result = runner.run(
            ["gh", "api", f"repos/{owner}/{name}"],
            check=False,
            env=github_env(token),
        )
    except FileNotFoundError as exc:
        raise GitHubError(
            "gh CLI is not installed. Install from https://cli.github.com/"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHubError("timed out probing repository access") from exc

    if result.returncode == 0:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubError(f"gh returned non-JSON output: {exc}") from exc
        return RepoAccess(
            full_name=str(payload.get("full_name") or f"{owner}/{name}"),
            private=bool(payload.get("private")),
            default_branch=payload.get("default_branch"),
        )

    message = (result.stderr or result.stdout).strip()
    if token:
        message = message.replace(token, "***")
    message = redact_tokens(message)
    match = _HTTP_STATUS.search(message)
    status = int(match.group(1)) if match else None

    if status == 401 or (status is None and "gh auth login" in message):
        raise RepoAccessError("bad_token", "GitHub rejected the token")
    if status == 403 and "rate limit" not in message.lower():
        raise RepoAccessError("no_access", f"the token cannot access {owner}/{name}")
    if status == 404:
        raise RepoAccessError(
            "not_found", f"{owner}/{name} does not exist or is not visible to the token"
        )
    raise GitHubError(f"repository probe failed: {message or 'gh exited non-zero'}")
