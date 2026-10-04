"""GitHub sign-in: configuration, pending logins and the OAuth protocol.

M2d-1's GitHub OAuth App sign-in. Part 1 is what the portal needs at
start-up: the validated configuration (:func:`load_github_config`), the
in-memory ``state`` map of sign-ins in flight (:class:`PendingLogins`) and
the one HTTP client with a host guard (:class:`GitHubOAuth`). Part 2 is the
protocol the routes in :mod:`whygraph.portal.auth_routes` drive:
:meth:`GitHubOAuth.begin` (``state`` + PKCE S256 and the authorize URL) and
:meth:`GitHubOAuth.identify` (the code exchange with ``code_verifier``, the
exact-``read:user`` scope check, ``GET /user`` and the best-effort token
revoke). Failures are :class:`GitHubAuthFailed` (the routes answer ``400
github_auth_failed``) or :class:`GitHubUnavailable` (``502
github_unavailable``).

No code, token, ``state`` or verifier is ever logged, put in an exception
message or returned beyond what the protocol needs (the ``state`` goes into
the authorize URL and the browser's cookie, nowhere else).

Notes
-----
This module must not import :mod:`whygraph.portal.app` (which defines
``PortalStartupError`` and imports the auth routes): it raises
``ValueError`` and ``app.py`` wraps it, the way it wraps ``BaseUrl.parse``.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

logger = logging.getLogger(__name__)

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

SCOPE = "read:user"
"""The one OAuth scope sign-in asks for, and the only one it accepts."""

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

    # -- the sign-in protocol (part 2) ---------------------------------------

    def begin(self, next: str | None, redirect_uri: str) -> tuple[str, str]:  # noqa: A002
        """Start a sign-in: a fresh ``state`` and PKCE verifier, kept server-side.

        Parameters
        ----------
        next : str or None
            The already validated post-sign-in destination; it travels in
            :attr:`pending`, never through GitHub.
        redirect_uri : str
            The one registered callback URL (``<base origin>/auth/github``).

        Returns
        -------
        tuple of (str, str)
            The ``state`` (for the browser's cookie) and the authorize URL to
            send the browser to.
        """
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        self.pending.put(state, verifier, next)
        return state, self.authorize_url(
            state=state, challenge=pkce_challenge(verifier), redirect_uri=redirect_uri
        )

    def authorize_url(self, *, state: str, challenge: str, redirect_uri: str) -> str:
        """Build GitHub's authorize URL for one sign-in.

        Parameters
        ----------
        state : str
            The OAuth ``state``.
        challenge : str
            The PKCE S256 challenge of the server-held verifier.
        redirect_uri : str
            The registered callback URL.

        Returns
        -------
        str
            ``<web_url>/login/oauth/authorize?client_id&redirect_uri&state&
            code_challenge&code_challenge_method=S256&scope=read:user``.
        """
        query = urlencode(
            {
                "client_id": self.config.client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": SCOPE,
            }
        )
        return f"{self.config.web_url}/login/oauth/authorize?{query}"

    def identify(
        self,
        code: str,
        verifier: str,
        redirect_uri: str,
        *,
        on_revoke_failure: Callable[[], None] | None = None,
    ) -> GitHubUser:
        """Exchange a code, read the user, revoke the token; keep nothing.

        Parameters
        ----------
        code : str
            The authorization code GitHub's redirect carried.
        verifier : str
            The PKCE verifier :meth:`begin` kept for this sign-in.
        redirect_uri : str
            The registered callback URL (sent again, as GitHub requires).
        on_revoke_failure : callable, optional
            Called (no arguments) when the best-effort revoke fails, so the
            caller can record it; a failed revoke is never an error.

        Returns
        -------
        GitHubUser
            The profile ``GET /user`` returned.

        Raises
        ------
        GitHubAuthFailed
            GitHub refused the exchange (a ``200`` with an ``error`` field,
            e.g. a wrong verifier or a used code), granted a scope other than
            exactly ``read:user``, or refused / mangled ``GET /user``.
        GitHubUnavailable
            A network error, a timeout or a ``5xx``.

        Notes
        -----
        Once a token exists it is revoked in a ``finally``, so every path
        that obtained one - success, a wrong scope, a failed ``/user``, and
        the caller's own later refusals (2FA, disabled) - leaves no live
        token behind.
        """
        token, scope = self._exchange(code, verifier, redirect_uri)
        try:
            if scope != SCOPE:
                raise GitHubAuthFailed("scope", "GitHub granted an unexpected scope")
            return self._user(token)
        finally:
            if not self.revoke(token) and on_revoke_failure is not None:
                on_revoke_failure()

    def _exchange(self, code: str, verifier: str, redirect_uri: str) -> tuple[str, str]:
        """``POST /login/oauth/access_token``; return the token and its scope."""
        response = self._send(
            "POST",
            f"{self.config.web_url}/login/oauth/access_token",
            "token exchange",
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
            headers={"Accept": "application/json"},
        )
        if response.status_code != 200:
            logger.warning("GitHub token exchange answered %s", response.status_code)
            raise GitHubAuthFailed("exchange_failed", "GitHub refused the sign-in code")
        body = _json(response)
        if body is None:
            raise GitHubUnavailable("GitHub answered the token exchange oddly")
        if body.get("error"):
            # GitHub's way to fail: a 200 with an error field. The code is
            # bad, used, expired or does not match the PKCE verifier.
            logger.info("GitHub token exchange refused: %s", str(body["error"])[:64])
            raise GitHubAuthFailed("exchange_failed", "GitHub refused the sign-in code")
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise GitHubAuthFailed("exchange_failed", "GitHub returned no access token")
        scope = body.get("scope")
        return token, scope if isinstance(scope, str) else ""

    def _user(self, token: str) -> GitHubUser:
        """``GET <api>/user`` with the token."""
        response = self._send(
            "GET",
            f"{self.config.api_url}/user",
            "user lookup",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
        )
        if response.status_code != 200:
            logger.warning("GitHub user lookup answered %s", response.status_code)
            raise GitHubAuthFailed("exchange_failed", "GitHub refused the user lookup")
        body = _json(response)
        ident = None if body is None else body.get("id")
        login = None if body is None else body.get("login")
        if (
            body is None
            or not isinstance(ident, int)
            or isinstance(ident, bool)
            or not isinstance(login, str)
            or not login
        ):
            raise GitHubUnavailable("GitHub answered the user lookup oddly")
        name = body.get("name")
        avatar = body.get("avatar_url")
        return GitHubUser(
            id=ident,
            login=login,
            name=name if isinstance(name, str) and name.strip() else None,
            avatar_url=avatar if isinstance(avatar, str) and avatar else None,
            # Only an explicit true counts: false and a missing field (a token
            # without read:user) are both "no 2FA".
            two_factor=body.get("two_factor_authentication") is True,
        )

    def revoke(self, token: str) -> bool:
        """Revoke an access token (best-effort); return whether GitHub did.

        ``DELETE <api>/applications/{client_id}/token`` with basic auth
        ``client_id:client_secret`` and the token in the JSON body.

        Parameters
        ----------
        token : str
            The access token to revoke.

        Returns
        -------
        bool
            ``True`` on GitHub's ``204``; ``False`` on any other answer or
            any error (never raised).
        """
        try:
            response = self._request(
                "DELETE",
                f"{self.config.api_url}/applications/{self.config.client_id}/token",
                auth=(self.config.client_id, self.config.client_secret),
                json={"access_token": token},
                headers={"Accept": "application/vnd.github+json"},
            )
        except Exception as exc:  # noqa: BLE001 -- best-effort by design
            logger.warning("GitHub token revoke failed: %s", type(exc).__name__)
            return False
        if response.status_code != 204:
            logger.warning("GitHub token revoke answered %s", response.status_code)
            return False
        return True

    def _send(self, method: str, url: str, what: str, **kwargs: Any) -> httpx.Response:
        """Send a request; a network error, timeout or ``5xx`` is unavailable."""
        try:
            response = self._request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            logger.warning("GitHub %s failed: %s", what, type(exc).__name__)
            raise GitHubUnavailable(f"GitHub could not be reached ({what})") from None
        if response.status_code >= 500:
            logger.warning("GitHub %s answered %s", what, response.status_code)
            raise GitHubUnavailable(f"GitHub is unavailable ({what})")
        return response

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()


@dataclass(frozen=True)
class GitHubUser:
    """What sign-in reads from ``GET /user``.

    Attributes
    ----------
    id : int
        The numeric user id - the identity (it never changes).
    login : str
        The current username.
    name : str or None
        The profile name, if set.
    avatar_url : str or None
        The avatar image URL.
    two_factor : bool
        ``True`` only when GitHub reported ``two_factor_authentication`` as
        exactly ``true``.
    """

    id: int
    login: str
    name: str | None
    avatar_url: str | None
    two_factor: bool


class GitHubAuthFailed(Exception):
    """GitHub refused the sign-in (the routes answer ``400 github_auth_failed``).

    Parameters
    ----------
    reason : str
        For the audit log: ``exchange_failed`` or ``scope``.
    message : str
        A message safe to show (never a code or token).
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class GitHubUnavailable(Exception):
    """GitHub could not answer (``502 github_unavailable``): network, timeout, 5xx."""


def pkce_challenge(verifier: str) -> str:
    """Return the PKCE S256 challenge of a verifier.

    Parameters
    ----------
    verifier : str
        The code verifier.

    Returns
    -------
    str
        ``base64url(sha256(verifier))`` without padding.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _json(response: httpx.Response) -> dict | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


__all__ = [
    "SCOPE",
    "AccessLogRedactor",
    "GitHubAuthConfig",
    "GitHubAuthFailed",
    "GitHubHostError",
    "GitHubOAuth",
    "GitHubUnavailable",
    "GitHubUser",
    "PendingLogin",
    "PendingLogins",
    "load_github_config",
    "parse_github_url",
    "pkce_challenge",
]
