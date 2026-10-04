"""GitHub sign-in plumbing: configuration, pending logins and the HTTP client.

Part 1 of M2d-1's GitHub OAuth App sign-in. This module holds what the
portal needs at start-up and nothing that talks to GitHub yet: the
validated configuration (:func:`load_github_config`), the in-memory
``state`` map of sign-ins in flight (:class:`PendingLogins`) and the one
HTTP client with a host guard (:class:`GitHubOAuth`).

Notes
-----
This module must not import :mod:`whygraph.portal.app` (which defines
``PortalStartupError`` and imports the auth routes): it raises
``ValueError`` and ``app.py`` wraps it, the way it wraps ``BaseUrl.parse``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

CLIENT_ID_ENV = "WHYGRAPH_GITHUB_OAUTH_CLIENT_ID"
CLIENT_SECRET_FILE_ENV = "WHYGRAPH_GITHUB_OAUTH_CLIENT_SECRET_FILE"
GITHUB_URL_ENV = "WHYGRAPH_GITHUB_URL"
GITHUB_API_URL_ENV = "WHYGRAPH_GITHUB_API_URL"

DEFAULT_WEB_URL = "https://github.com"
DEFAULT_API_URL = "https://api.github.com"

PENDING_TTL_SEC = 10 * 60
"""How long a started sign-in stays valid."""
PENDING_MAX = 10_000
"""The most sign-ins in flight; the oldest is evicted past it."""

_DEFAULT_PORTS = {"http": 80, "https": 443}


def parse_github_url(raw: str, *, name: str = "GitHub URL") -> str:
    """Validate a GitHub endpoint URL and return it without a trailing slash.

    Parameters
    ----------
    raw : str
        The configured value, e.g. ``https://github.com`` or a GitHub
        Enterprise Server API root ``https://ghe.example.com/api/v3``.
    name : str
        What to call the value in error messages (the variable name).

    Returns
    -------
    str
        The URL with any trailing slash dropped.

    Raises
    ------
    ValueError
        Not ``https`` (``http`` is allowed only for a loopback IP,
        ``localhost`` or ``*.localhost``), no host, or a query, fragment
        or credentials present.
    """
    value = (raw or "").strip()
    try:
        parts = urlsplit(value)
        host = parts.hostname
        parts.port  # noqa: B018 -- raises ValueError on a bad port
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid URL: {exc}") from exc
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"{name} must start with https:// (got {value!r})")
    if not host:
        raise ValueError(f"{name} has no host (got {value!r})")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{name} must not contain credentials")
    if parts.query or "?" in value:
        raise ValueError(f"{name} must not contain a query")
    if parts.fragment or "#" in value:
        raise ValueError(f"{name} must not contain a fragment")
    if parts.scheme == "http" and not _is_loopback(host):
        raise ValueError(
            f"{name} must use https (http is only allowed for localhost or a "
            f"loopback address, got {value!r})"
        )
    return value.rstrip("/")


def _is_loopback(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class GitHubAuthConfig:
    """The validated GitHub OAuth App settings.

    Attributes
    ----------
    client_id : str
        The OAuth App's client id.
    client_secret : str
        The client secret, read from a file.
    web_url : str
        GitHub's web root (``https://github.com``), no trailing slash.
    api_url : str
        GitHub's API root (``https://api.github.com``, or a GHES
        ``<url>/api/v3``), no trailing slash.
    """

    client_id: str
    client_secret: str
    web_url: str
    api_url: str


def load_github_config(environ: Mapping[str, str]) -> GitHubAuthConfig:
    """Read and validate the GitHub OAuth App settings from the environment.

    Parameters
    ----------
    environ : Mapping[str, str]
        Usually ``os.environ``.

    Returns
    -------
    GitHubAuthConfig
        The validated settings.

    Raises
    ------
    ValueError
        A variable is missing, empty or invalid, or the secret file is
        unreadable or empty; the message names the variable.
    """
    client_id = (environ.get(CLIENT_ID_ENV) or "").strip()
    if not client_id:
        raise ValueError(
            f"production mode needs {CLIENT_ID_ENV} (the GitHub OAuth App's client id)"
        )
    secret_file = (environ.get(CLIENT_SECRET_FILE_ENV) or "").strip()
    if not secret_file:
        raise ValueError(
            f"production mode needs {CLIENT_SECRET_FILE_ENV} (a file holding the "
            "GitHub OAuth App's client secret)"
        )
    try:
        secret = Path(secret_file).read_text(encoding="utf-8").rstrip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(
            f"cannot read {CLIENT_SECRET_FILE_ENV} ({secret_file}): {exc}"
        ) from exc
    if not secret:
        raise ValueError(f"{CLIENT_SECRET_FILE_ENV} ({secret_file}) is empty")
    web = (environ.get(GITHUB_URL_ENV) or "").strip() or DEFAULT_WEB_URL
    api = (environ.get(GITHUB_API_URL_ENV) or "").strip() or DEFAULT_API_URL
    return GitHubAuthConfig(
        client_id=client_id,
        client_secret=secret,
        web_url=parse_github_url(web, name=GITHUB_URL_ENV),
        api_url=parse_github_url(api, name=GITHUB_API_URL_ENV),
    )


@dataclass(frozen=True)
class PendingLogin:
    """A started sign-in, kept until its callback.

    Attributes
    ----------
    verifier : str
        The PKCE code verifier (never leaves the server).
    next : str or None
        Where to land after sign-in, already validated.
    expires_at : float
        Clock reading after which the entry is dead.
    """

    verifier: str
    next: str | None
    expires_at: float


class PendingLogins:
    """In-memory map of sign-ins in flight, keyed by OAuth ``state``.

    Thread-safe. Entries live :data:`PENDING_TTL_SEC` and are single use;
    past :data:`PENDING_MAX` entries the oldest is evicted.

    Parameters
    ----------
    clock : callable, optional
        Returns seconds; injectable for tests (default ``time.monotonic``).
    ttl : float
        Entry lifetime in seconds.
    max_entries : int
        Capacity before the oldest entry is evicted.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = PENDING_TTL_SEC,
        max_entries: int = PENDING_MAX,
    ) -> None:
        self._clock = clock
        self._ttl = ttl
        self._max = max_entries
        self._entries: OrderedDict[str, PendingLogin] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, state: str, verifier: str, next: str | None) -> None:  # noqa: A002
        """Remember a started sign-in.

        Parameters
        ----------
        state : str
            The OAuth ``state`` value.
        verifier : str
            The PKCE verifier.
        next : str or None
            The validated post-sign-in destination.
        """
        entry = PendingLogin(verifier, next, self._clock() + self._ttl)
        with self._lock:
            self._entries.pop(state, None)
            self._entries[state] = entry
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def pop(self, state: str) -> PendingLogin | None:
        """Take a sign-in out of the map (single use).

        Parameters
        ----------
        state : str
            The OAuth ``state`` value.

        Returns
        -------
        PendingLogin or None
            The entry, or ``None`` when it is unknown, used or expired.
        """
        with self._lock:
            entry = self._entries.pop(state, None)
        if entry is None or entry.expires_at <= self._clock():
            return None
        return entry

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class AccessLogRedactor(logging.Filter):
    """Blank the query of ``/auth/github`` in uvicorn's access log.

    GitHub's redirect carries the single-use ``code`` and the ``state`` in
    that query. uvicorn's access records carry the request line as
    ``args[2]``; this filter rewrites ``/auth/github?...`` to
    ``/auth/github?<redacted>`` and always lets the record through.
    """

    PATH = "/auth/github"

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact the callback query in place; never drops a record."""
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path = args[2]
            if path == self.PATH or path.startswith((self.PATH + "?", self.PATH + "/")):
                head, sep, _ = path.partition("?")
                if sep:
                    record.args = (*args[:2], head + "?<redacted>", *args[3:])
        return True


class GitHubHostError(RuntimeError):
    """A request was aimed at a host that is not the configured GitHub."""


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return (
        scheme,
        (parts.hostname or "").lower(),
        parts.port or _DEFAULT_PORTS.get(scheme),
    )


class GitHubOAuth:
    """The portal's GitHub client: config, one HTTP client, pending sign-ins.

    Parameters
    ----------
    config : GitHubAuthConfig
        The validated settings.
    transport : httpx.BaseTransport, optional
        Replaces the network (tests pass ``httpx.MockTransport``).

    Attributes
    ----------
    config : GitHubAuthConfig
    pending : PendingLogins
    """

    def __init__(
        self,
        config: GitHubAuthConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self.pending = PendingLogins()
        self._allowed = {_origin(config.web_url), _origin(config.api_url)}
        self._client = httpx.Client(
            timeout=httpx.Timeout(10.0, connect=5.0),
            transport=transport,
            follow_redirects=False,
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Send a request, refusing any host but the configured GitHub's.

        Raises
        ------
        GitHubHostError
            ``url``'s scheme, host and port match neither ``web_url`` nor
            ``api_url``.
        """
        if _origin(url) not in self._allowed:
            raise GitHubHostError(
                f"refusing a request to {urlsplit(url).netloc or url!r}: it is "
                "not the configured GitHub"
            )
        return self._client.request(method, url, **kwargs)

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()


__all__ = [
    "AccessLogRedactor",
    "GitHubAuthConfig",
    "GitHubHostError",
    "GitHubOAuth",
    "PendingLogin",
    "PendingLogins",
    "load_github_config",
    "parse_github_url",
]
