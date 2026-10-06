"""Host-scoped git credentials and the child-process environment allowlist.

The portal clones and fetches GitHub repositories with a token it holds.
That token must never reach ``.git/config``, argv, or a log line, so it
travels only as an environment variable (:data:`TOKEN_ENV_VAR`) read by
one inline credential helper that answers for the configured GitHub host
(:data:`GITHUB_URL_ENV`, ``github.com`` by default) over its one protocol
and nothing else.

Public API
----------
* :func:`github_git_host` - the git host, from :data:`GITHUB_URL_ENV`.
* :func:`parse_github_url` - strict ``<GitHub URL>/<owner>/<repo>`` check.
* :func:`strip_userinfo` - drop ``user:password@`` from an http(s) URL.
* :func:`pass_through_env` - the child-env allowlist shared with the scan runner.
* :func:`git_env` - allowlisted env plus the token, no global / system git
  config and ``GIT_TERMINAL_PROMPT=0``.
* :func:`github_git_config` - the ``-c`` arguments every network git call carries.
* :func:`redact_tokens` - scrub anything shaped like a GitHub or connection token.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit, urlunsplit

from .exceptions import InvalidRepoUrlError

TOKEN_ENV_VAR = "WHYGRAPH_GIT_TOKEN"
"""Env var the inline credential helper reads the token from."""

TOKEN_FILE_ENV = "WHYGRAPH_GITHUB_TOKEN_FILE"
"""Env var naming the 0600 file a portal scan child reads its GitHub token from.

The portal rewrites the file before the token expires, so the child reads it
before each ``gh`` / ``git`` call (:func:`whygraph.services.github.token.github_token`)
instead of holding one value for its whole life.
"""

GITHUB_URL_ENV = "WHYGRAPH_GITHUB_URL"
"""GitHub's web root (GitHub Enterprise Server, or the test fake); git's host comes from it."""

DEFAULT_GITHUB_URL = "https://github.com"
"""The web root when :data:`GITHUB_URL_ENV` is unset."""

TOKEN_PATTERN = re.compile(r"(?:gh[opsur]_[A-Za-z0-9_.-]{8,}|wgc_[A-Za-z0-9_-]{8,})\**")
"""Anything shaped like a GitHub token, plus the ``*`` a tool masked its tail with.

Installation tokens are long (``ghs_<app id>_<JWT>``) and ``gh auth status``
prints most of one with only the tail masked, so scrubbing exact values
alone would miss it. WhyGraph's own connection tokens (``wgc_...``, M2e)
match too.
"""

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

_DEFAULT_PORTS = {"http": 80, "https": 443}
_HOST = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?|\[[0-9a-f:.]+\])(?::[0-9]{1,5})?"
)
_URL_PATH = re.compile(r"(?:/[A-Za-z0-9._~-]+)*")
_OWNER = r"(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})"
_REPO = r"(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?"


@dataclass(frozen=True)
class GitHost:
    """Where git reaches GitHub: the scheme and host of :data:`GITHUB_URL_ENV`.

    Attributes
    ----------
    scheme : str
        ``"https"``, or ``"http"`` for a loopback host only (the test fake).
    host : str
        The lowercased host name with ``:<port>`` when the port is not the
        scheme's default - exactly what git's credential protocol sends as
        ``host=``.
    path : str
        A path prefix (``""`` for a plain web root), without a trailing slash.
    """

    scheme: str
    host: str
    path: str = ""

    @property
    def url(self) -> str:
        """The normalized web root, ``<scheme>://<host><path>``."""
        return f"{self.scheme}://{self.host}{self.path}"


def _is_loopback(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def github_git_host(environ: Mapping[str, str] | None = None) -> GitHost:
    """The git host the portal clones and fetches from.

    Read from :data:`GITHUB_URL_ENV` (default :data:`DEFAULT_GITHUB_URL`),
    with the rules of the portal's own GitHub URL check: ``https``, or
    ``http`` for a loopback host only; no credentials, query or fragment.

    Parameters
    ----------
    environ : Mapping[str, str], optional
        Where the variable is read. Defaults to :data:`os.environ`.

    Returns
    -------
    GitHost
        The normalized scheme, host (with a non-default port) and path.

    Raises
    ------
    InvalidRepoUrlError
        If the configured URL breaks those rules.
    """
    source = os.environ if environ is None else environ
    raw = (source.get(GITHUB_URL_ENV) or "").strip() or DEFAULT_GITHUB_URL
    bad = InvalidRepoUrlError(
        f"{GITHUB_URL_ENV} must be a plain https URL, or http for a loopback "
        f"host (got {raw!r})"
    )
    try:
        parts = urlsplit(raw)
        hostname, port = parts.hostname, parts.port
    except ValueError as exc:
        raise bad from exc
    if (
        parts.scheme not in _DEFAULT_PORTS
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "?" in raw
        or "#" in raw
        or (parts.scheme == "http" and not _is_loopback(hostname))
    ):
        raise bad
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and port != _DEFAULT_PORTS[parts.scheme]:
        host = f"{host}:{port}"
    path = parts.path.rstrip("/")
    if not _HOST.fullmatch(host) or not _URL_PATH.fullmatch(path):
        raise bad
    return GitHost(parts.scheme, host, path)


def credential_helper(host: GitHost) -> str:
    """The inline git credential helper for ``host`` (a shell snippet, no packaged script).

    Git runs a ``!``-prefixed helper through ``sh`` with the operation name
    appended and the credential request on stdin. It prints a credential
    only for ``get``, ``host=<host>`` (with the port when it is not the
    default) and ``protocol=<scheme>``; for anything else (or an unset
    token) it prints nothing, so git falls through as anonymous rather than
    sending the token to another host. The token is expanded by the shell
    at run time, so it never appears in argv. ``host.host`` holds only
    host-name characters (:func:`github_git_host`), so quoting it is safe.

    Parameters
    ----------
    host : GitHost
        The one host and protocol the helper answers for.

    Returns
    -------
    str
        The ``credential.helper`` value.
    """
    return (
        '!f() { test "$1" = get || exit 0; h=; p=; '
        "while IFS='=' read -r k v; do "
        'case "$k" in host) h=$v;; protocol) p=$v;; esac; done; '
        f'test "$h" = \'{host.host}\' && test "$p" = {host.scheme} || exit 0; '
        'test -n "$WHYGRAPH_GIT_TOKEN" || exit 0; '
        'echo username=x-access-token; echo "password=$WHYGRAPH_GIT_TOKEN"; }; f'
    )


def github_git_config(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """``git -c ...`` arguments for every network call: one protocol, helpers reset.

    Every protocol is refused but ``https`` (``file://``, ``ext::``,
    ``ssh``), plus ``http`` only when :data:`GITHUB_URL_ENV` names a
    loopback ``http`` host; inherited credential helpers are reset, then
    :func:`credential_helper` answers for the configured host only.

    Parameters
    ----------
    environ : Mapping[str, str], optional
        Where :data:`GITHUB_URL_ENV` is read. Defaults to :data:`os.environ`.

    Returns
    -------
    tuple[str, ...]
        The ``-c`` pairs, to go between ``git`` and the subcommand.

    Raises
    ------
    InvalidRepoUrlError
        If :data:`GITHUB_URL_ENV` is invalid (:func:`github_git_host`).
    """
    host = github_git_host(environ)
    http = ("-c", "protocol.http.allow=always") if host.scheme == "http" else ()
    return (
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        *http,
        "-c",
        "credential.helper=",
        "-c",
        f"credential.helper={credential_helper(host)}",
    )


def parse_github_url(
    url: str, *, environ: Mapping[str, str] | None = None
) -> tuple[str, str]:
    """Validate a clone URL and split it into ``(owner, repo)``.

    Only ``<GitHub URL>/<owner>/<repo>`` with an optional ``.git`` suffix
    is accepted, where ``<GitHub URL>`` is :func:`github_git_host`'s
    normalized web root (``https://github.com`` by default). Userinfo,
    other ports, query strings, trailing slashes, other hosts and other
    schemes (``file://``, ``ssh://``, ``ext::``) are all rejected.

    Parameters
    ----------
    url : str
        The candidate URL.
    environ : Mapping[str, str], optional
        Where :data:`GITHUB_URL_ENV` is read. Defaults to :data:`os.environ`.

    Returns
    -------
    tuple[str, str]
        The owner and the repository name (without ``.git``).

    Raises
    ------
    InvalidRepoUrlError
        If ``url`` is not a plain repository URL on the configured host, or
        :data:`GITHUB_URL_ENV` is invalid.
    """
    base = github_git_host(environ).url
    match = re.fullmatch(f"{re.escape(base)}/{_OWNER}/{_REPO}", url)
    if match is None or match["repo"] in {".", ".."}:
        raise InvalidRepoUrlError(
            f"not a plain GitHub URL (expected {base}/<owner>/<repo>)"
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

    Git reads no global or system config (``GIT_CONFIG_GLOBAL=/dev/null``,
    ``GIT_CONFIG_NOSYSTEM=1``): a developer's ``url.*.insteadOf``,
    ``http.*`` or ``include`` must never rewrite or redirect the portal's
    clone, and ``HOME`` is passed through.

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
        The allowlist, ``GIT_TERMINAL_PROMPT=0``, the two config resets,
        the token when given, then ``extra``.
    """
    env = pass_through_env(environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if token:
        env[TOKEN_ENV_VAR] = token
    if extra:
        env.update(extra)
    return env


def redact_tokens(text: str) -> str:
    """Replace anything matching :data:`TOKEN_PATTERN` by its prefix and ``***``.

    Parameters
    ----------
    text : str
        Output that may hold a GitHub token, whole or partly masked.

    Returns
    -------
    str
        ``text`` with each match replaced by e.g. ``ghs_***`` or ``wgc_***``.
    """
    return TOKEN_PATTERN.sub(lambda m: m.group()[:4] + "***", text)
