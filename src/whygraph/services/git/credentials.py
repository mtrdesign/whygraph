"""Host-scoped git credentials and the child-process environment allowlist.

The portal clones and fetches GitHub repositories with a token the user
typed in. That token must never reach ``.git/config``, argv, or a log
line, so it travels only as an environment variable
(:data:`TOKEN_ENV_VAR`) read by one inline credential helper that answers
for ``github.com`` over ``https`` and nothing else.

Public API
----------
* :func:`parse_github_url` - strict ``https://github.com/<owner>/<repo>`` check.
* :func:`strip_userinfo` - drop ``user:password@`` from an http(s) URL.
* :func:`pass_through_env` - the child-env allowlist shared with the scan runner.
* :func:`git_env` - allowlisted env plus the token and ``GIT_TERMINAL_PROMPT=0``.
* :data:`GITHUB_GIT_CONFIG` - the ``-c`` arguments every network git call carries.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

from .exceptions import InvalidRepoUrlError

TOKEN_ENV_VAR = "WHYGRAPH_GIT_TOKEN"
"""Env var the inline credential helper reads the token from."""

_PASS_THROUGH_EXACT = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
"""Env names copied verbatim from the portal's own environment (plus ``LC_*``)."""

_GITHUB_URL = re.compile(
    r"https://github\.com/"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?"
)

GITHUB_CREDENTIAL_HELPER = (
    '!f() { test "$1" = get || exit 0; h=; p=; '
    "while IFS='=' read -r k v; do "
    'case "$k" in host) h=$v;; protocol) p=$v;; esac; done; '
    'test "$h" = github.com && test "$p" = https || exit 0; '
    'test -n "$WHYGRAPH_GIT_TOKEN" || exit 0; '
    'echo username=x-access-token; echo "password=$WHYGRAPH_GIT_TOKEN"; }; f'
)
"""Inline git credential helper (shell snippet, no packaged script).

Git runs a ``!``-prefixed helper through ``sh`` with the operation name
appended and the credential request on stdin. It prints a credential only
for ``get``, ``host=github.com`` and ``protocol=https``; for anything else
(or an unset token) it prints nothing, so git falls through as anonymous
rather than sending the token to another host. The token is expanded by
the shell at run time, so it never appears in argv.
"""

GITHUB_GIT_CONFIG: tuple[str, ...] = (
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.https.allow=always",
    "-c",
    "credential.helper=",
    "-c",
    f"credential.helper={GITHUB_CREDENTIAL_HELPER}",
)
"""``git -c ...`` arguments for every network call: https only, helpers reset."""


def parse_github_url(url: str) -> tuple[str, str]:
    """Validate a clone URL and split it into ``(owner, repo)``.

    Only ``https://github.com/<owner>/<repo>`` with an optional ``.git``
    suffix is accepted. Userinfo, ports, query strings, trailing slashes,
    other hosts and other schemes (``file://``, ``ssh://``, ``ext::``) are
    all rejected.

    Parameters
    ----------
    url : str
        The candidate URL.

    Returns
    -------
    tuple[str, str]
        The owner and the repository name (without ``.git``).

    Raises
    ------
    InvalidRepoUrlError
        If ``url`` is not a plain GitHub https repository URL.
    """
    match = _GITHUB_URL.fullmatch(url)
    if match is None or match["repo"] in {".", ".."}:
        raise InvalidRepoUrlError(
            "not a plain GitHub URL (expected https://github.com/<owner>/<repo>)"
        )
    return match["owner"], match["repo"]


def strip_userinfo(url: str) -> str:
    """Remove ``user[:password]@`` from an ``http(s)`` URL.

    Anything that is not an http(s) URL with userinfo is returned
    unchanged, so scp-style (``git@github.com:o/r``) and ``ssh://git@``
    remotes keep their ``git`` user.

    Parameters
    ----------
    url : str
        A remote URL as read from ``.git/config``.

    Returns
    -------
    str
        The URL safe to store or display.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


def pass_through_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The allowlisted subset of the portal's environment for child processes.

    ``PATH``, ``HOME``, ``LANG``, ``LC_*``, ``TZ``, the TLS bundle
    variables and the proxy variables (both cases). Never an API key,
    ``GH_TOKEN`` or ``GITHUB_TOKEN``: credentials are injected explicitly
    per call, never inherited.

    Parameters
    ----------
    environ : Mapping[str, str], optional
        The environment to filter. Defaults to :data:`os.environ`.

    Returns
    -------
    dict[str, str]
        A new dict holding only the allowlisted names.
    """
    source = os.environ if environ is None else environ
    return {
        k: v
        for k, v in source.items()
        if k in _PASS_THROUGH_EXACT or k.startswith("LC_")
    }


def git_env(
    token: str | None = None,
    *,
    extra: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Environment for a network git call, with the token as an env var only.

    Parameters
    ----------
    token : str, optional
        GitHub token, exposed to the credential helper as
        :data:`TOKEN_ENV_VAR`. ``None`` or empty means anonymous access.
    extra : Mapping[str, str], optional
        Additional variables merged last.
    environ : Mapping[str, str], optional
        Source environment for the allowlist. Defaults to :data:`os.environ`.

    Returns
    -------
    dict[str, str]
        The allowlist, ``GIT_TERMINAL_PROMPT=0``, the token when given,
        then ``extra``.
    """
    env = pass_through_env(environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        env[TOKEN_ENV_VAR] = token
    if extra:
        env.update(extra)
    return env
