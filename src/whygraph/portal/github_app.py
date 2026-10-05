"""The WhyGraph GitHub App: configuration, installation tokens, user authorization.

M2d-2's production projects come from GitHub through one GitHub App that
the self-hoster creates. This module is what the portal needs to talk to
it:

* :func:`load_github_app_config` - the five ``WHYGRAPH_GITHUB_APP_*``
  variables, validated (the private key must be RSA, the webhook secret at
  least :data:`WEBHOOK_SECRET_MIN_LENGTH` characters);
* :class:`GitHubApp` - one origin-guarded HTTP client (the host guard and
  error mapping of :class:`whygraph.portal.github_auth.GitHubHttp`):
  the app's RS256 JWT (:meth:`GitHubApp.app_jwt`), repo-scoped read-only
  installation tokens cached until shortly before they expire
  (:meth:`GitHubApp.installation_token`), and the user-authorization calls
  the import page makes with an 8-hour user token;
* :class:`UserTokens` - those user tokens, in memory, keyed by the WhyGraph
  session that obtained them.

The private key never leaves the portal process. No token, JWT, code or
secret is ever logged, put in an exception message or returned beyond what
the protocol needs; the dataclasses that carry one keep it out of their
``repr``.

Notes
-----
Like :mod:`whygraph.portal.github_auth`, this module must not import
:mod:`whygraph.portal.app`: configuration errors are ``ValueError`` and
``app.py`` wraps them in ``PortalStartupError``.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .github_auth import (
    DEFAULT_API_URL,
    DEFAULT_WEB_URL,
    GITHUB_API_URL_ENV,
    GITHUB_URL_ENV,
    PENDING_MAX,
    PENDING_TTL_SEC,
    GitHubAuthFailed,
    GitHubHttp,
    GitHubUnavailable,
    GitHubUser,
    _json,
    parse_github_url,
)

logger = logging.getLogger(__name__)

APP_SLUG_ENV = "WHYGRAPH_GITHUB_APP_SLUG"
APP_CLIENT_ID_ENV = "WHYGRAPH_GITHUB_APP_CLIENT_ID"
APP_CLIENT_SECRET_FILE_ENV = "WHYGRAPH_GITHUB_APP_CLIENT_SECRET_FILE"
APP_PRIVATE_KEY_FILE_ENV = "WHYGRAPH_GITHUB_APP_PRIVATE_KEY_FILE"
APP_WEBHOOK_SECRET_FILE_ENV = "WHYGRAPH_GITHUB_APP_WEBHOOK_SECRET_FILE"
APP_ENV_VARS: tuple[str, ...] = (
    APP_SLUG_ENV,
    APP_CLIENT_ID_ENV,
    APP_CLIENT_SECRET_FILE_ENV,
    APP_PRIVATE_KEY_FILE_ENV,
    APP_WEBHOOK_SECRET_FILE_ENV,
)
"""The GitHub App's variables: production mode needs all of them."""

WEBHOOK_SECRET_MIN_LENGTH = 32
"""The shortest webhook secret the portal accepts."""

JWT_BACKDATE_SEC = 60
"""How far the app JWT's ``iat`` lies in the past (clock drift, GitHub's advice)."""
JWT_LIFETIME_SEC = 540
"""How far the app JWT's ``exp`` lies in the future (GitHub allows at most 10 min)."""

TOKEN_REFRESH_MARGIN_SEC = 5 * 60
"""A cached installation token is reused until this long before it expires."""

USER_TOKEN_MARGIN_SEC = 60
"""A user token is not handed out within this long of its expiry."""
USER_TOKEN_DEFAULT_TTL_SEC = 8 * 60 * 60
"""The lifetime assumed when GitHub's exchange names none (expiring tokens are 8 h)."""

REPOS_PER_PAGE = 100
"""Repositories per page of :meth:`GitHubApp.installation_repos`."""

INSTALLATION_PERMISSIONS: Mapping[str, str] = {
    "contents": "read",
    "metadata": "read",
    "pull_requests": "read",
    "issues": "read",
}
"""What every installation token is scoped to: the app's four read permissions."""

_ACCESS_LOST = frozenset({403, 404, 422})
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_API_VERSION = "2022-11-28"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitHubAppConfig:
    """The validated GitHub App settings.

    Attributes
    ----------
    slug : str
        The app's URL name (``https://github.com/apps/<slug>``).
    client_id : str
        The app's client id; also the JWT's ``iss`` (no app id is needed).
    client_secret : str
        The client secret, read from a file.
    private_key : rsa.RSAPrivateKey
        The app's private key, read from a PEM file.
    webhook_secret : str
        The webhook secret, read from a file.
    web_url : str
        GitHub's web root, no trailing slash (shared with sign-in).
    api_url : str
        GitHub's API root, no trailing slash (shared with sign-in).
    """

    slug: str
    client_id: str
    client_secret: str = field(repr=False)
    private_key: rsa.RSAPrivateKey = field(repr=False)
    webhook_secret: str = field(repr=False)
    web_url: str
    api_url: str


def _read_secret_file(env: str, path: str) -> str:
    """Read a secret file named by ``env``; refuse an unreadable or empty one."""
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read {env} ({path}): {exc}") from exc
    if not value:
        raise ValueError(f"{env} ({path}) is empty")
    return value


def load_github_app_config(environ: Mapping[str, str]) -> GitHubAppConfig:
    """Read and validate the GitHub App settings from the environment.

    Production mode requires the app (plan section 0.2 #9): a portal without
    it could hold no projects, so it refuses to start instead of failing at
    the first import.

    Parameters
    ----------
    environ : Mapping[str, str]
        Usually ``os.environ``.

    Returns
    -------
    GitHubAppConfig
        The validated settings.

    Raises
    ------
    ValueError
        One or more of :data:`APP_ENV_VARS` is unset or empty (the message
        names each missing one), a file is unreadable or empty, the slug is
        malformed, the key is not an RSA private key, the webhook secret is
        shorter than :data:`WEBHOOK_SECRET_MIN_LENGTH` characters, or a
        GitHub URL is invalid; the message names the variable and never
        shows a secret.
    """
    values = {name: (environ.get(name) or "").strip() for name in APP_ENV_VARS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ValueError(
            "production mode needs the GitHub App: set all of "
            f"{', '.join(APP_ENV_VARS)}; missing: {', '.join(missing)}"
        )
    slug = values[APP_SLUG_ENV]
    if not _SLUG_RE.match(slug):
        raise ValueError(
            f"{APP_SLUG_ENV} must be the app's URL name (lowercase letters, digits "
            f"and hyphens), got {slug!r}"
        )
    secret = _read_secret_file(
        APP_CLIENT_SECRET_FILE_ENV, values[APP_CLIENT_SECRET_FILE_ENV]
    )
    pem = _read_secret_file(APP_PRIVATE_KEY_FILE_ENV, values[APP_PRIVATE_KEY_FILE_ENV])
    try:
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise ValueError(
            f"{APP_PRIVATE_KEY_FILE_ENV} ({values[APP_PRIVATE_KEY_FILE_ENV]}) is not "
            "an unencrypted PEM private key"
        ) from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError(
            f"{APP_PRIVATE_KEY_FILE_ENV} ({values[APP_PRIVATE_KEY_FILE_ENV]}) must "
            "be an RSA private key (GitHub signs app JWTs with RS256)"
        )
    webhook_secret = _read_secret_file(
        APP_WEBHOOK_SECRET_FILE_ENV, values[APP_WEBHOOK_SECRET_FILE_ENV]
    )
    if len(webhook_secret) < WEBHOOK_SECRET_MIN_LENGTH:
        raise ValueError(
            f"{APP_WEBHOOK_SECRET_FILE_ENV} ({values[APP_WEBHOOK_SECRET_FILE_ENV]}) "
            f"must hold at least {WEBHOOK_SECRET_MIN_LENGTH} characters"
        )
    web = (environ.get(GITHUB_URL_ENV) or "").strip() or DEFAULT_WEB_URL
    api = (environ.get(GITHUB_API_URL_ENV) or "").strip() or DEFAULT_API_URL
    return GitHubAppConfig(
        slug=slug,
        client_id=values[APP_CLIENT_ID_ENV],
        client_secret=secret,
        private_key=key,
        webhook_secret=webhook_secret,
        web_url=parse_github_url(web, name=GITHUB_URL_ENV),
        api_url=parse_github_url(api, name=GITHUB_API_URL_ENV),
    )


# ---------------------------------------------------------------------------
# What the client returns
# ---------------------------------------------------------------------------


class GitHubAccessLost(Exception):
    """The app cannot reach a repository any more (uninstalled, removed, deleted).

    Raised when minting an installation token (or reading a repository with
    one) answers ``403``, ``404`` or ``422``. The caller marks the project
    access-lost; it is never removed automatically.

    Parameters
    ----------
    status : int
        GitHub's HTTP status.
    message : str
        A message safe to show (never a token).
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class GitHubNotFound(Exception):
    """A user token cannot see an installation or a repository (``403`` / ``404``)."""


class GitHubTokenRejected(Exception):
    """GitHub answered ``401`` to a user token: expired, revoked or not the app's."""


@dataclass(frozen=True)
class InstallationToken:
    """A repo-scoped, read-only installation token.

    Attributes
    ----------
    token : str
        The ``ghs_`` token (kept out of ``repr``).
    expires_at : float
        Epoch seconds when GitHub expires it.
    installation_id, repo_id : int
        What it was minted for.
    """

    token: str = field(repr=False)
    expires_at: float
    installation_id: int
    repo_id: int


@dataclass(frozen=True)
class UserAuthorization:
    """The result of a user-authorization code exchange.

    Attributes
    ----------
    token : str
        The ``ghu_`` user token (kept out of ``repr``).
    expires_at : float
        Epoch seconds when it expires. The refresh token is never kept.
    """

    token: str = field(repr=False)
    expires_at: float


@dataclass(frozen=True)
class Installation:
    """An installation of the app that a user can see.

    Attributes
    ----------
    id : int
        The installation id.
    account_login : str
        The user or organization the app is installed on.
    account_type : str
        ``"User"`` or ``"Organization"``.
    avatar_url : str or None
        The account's avatar.
    repository_selection : str
        ``"all"`` or ``"selected"``.
    """

    id: int
    account_login: str
    account_type: str
    avatar_url: str | None
    repository_selection: str


@dataclass(frozen=True)
class GitHubRepo:
    """A repository as GitHub describes it.

    Attributes
    ----------
    id : int
        The numeric repository id (the identity).
    full_name : str
        ``owner/name`` today (it can change).
    private : bool
        Whether the repository is private.
    default_branch : str
        The default branch.
    permissions : Mapping[str, bool]
        The token holder's permissions (``pull``, ``push``, ...); empty when
        GitHub sent none (an installation token).
    """

    id: int
    full_name: str
    private: bool
    default_branch: str
    permissions: Mapping[str, bool] = field(default_factory=dict)

    @property
    def can_pull(self) -> bool:
        """Whether the permissions grant read access (``pull`` exactly ``true``)."""
        return self.permissions.get("pull") is True


@dataclass(frozen=True)
class RepoPage:
    """One page of an installation's repositories.

    Attributes
    ----------
    repos : list of GitHubRepo
        The repositories on this page.
    total_count : int
        How many the user can see through the installation in all.
    """

    repos: list[GitHubRepo]
    total_count: int


def _parse_repo(body: Mapping[str, Any] | None) -> GitHubRepo:
    """Build a :class:`GitHubRepo` from a GitHub repository object."""
    if body is None:
        raise GitHubUnavailable("GitHub answered a repository lookup oddly")
    ident = body.get("id")
    full_name = body.get("full_name")
    branch = body.get("default_branch")
    if (
        not isinstance(ident, int)
        or isinstance(ident, bool)
        or not isinstance(full_name, str)
        or not _FULL_NAME_RE.match(full_name)
        or not isinstance(branch, str)
        or not branch
    ):
        raise GitHubUnavailable("GitHub answered a repository lookup oddly")
    perms = body.get("permissions")
    return GitHubRepo(
        id=ident,
        full_name=full_name,
        private=body.get("private") is True,
        default_branch=branch,
        permissions=(
            {k: v for k, v in perms.items() if isinstance(v, bool)}
            if isinstance(perms, dict)
            else {}
        ),
    )


def _parse_installation(body: Any) -> Installation | None:
    """Build an :class:`Installation`, or ``None`` for a malformed entry."""
    if not isinstance(body, dict):
        return None
    ident = body.get("id")
    account = body.get("account")
    if not isinstance(ident, int) or isinstance(ident, bool):
        return None
    if not isinstance(account, dict) or not isinstance(account.get("login"), str):
        return None
    avatar = account.get("avatar_url")
    return Installation(
        id=ident,
        account_login=account["login"],
        account_type=str(account.get("type") or "User"),
        avatar_url=avatar if isinstance(avatar, str) and avatar else None,
        repository_selection=str(body.get("repository_selection") or "selected"),
    )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _epoch(value: Any) -> float | None:
    """GitHub's ``2026-10-04T12:00:00Z`` as epoch seconds, or ``None``."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo is not None else None


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class GitHubApp(GitHubHttp):
    """The portal's GitHub App client.

    Parameters
    ----------
    config : GitHubAppConfig
        The validated settings.
    transport : httpx.BaseTransport, optional
        Replaces the network (tests pass ``httpx.MockTransport``).
    clock : callable, optional
        Returns epoch seconds (default ``time.time``); injectable for tests.

    Attributes
    ----------
    config : GitHubAppConfig
    pending : PendingAuthorizations
        The user authorizations and installs started from the portal.
    """

    def __init__(
        self,
        config: GitHubAppConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(config, transport=transport)
        self._clock = clock
        self.pending = PendingAuthorizations()
        self._tokens: dict[tuple[int, int], InstallationToken] = {}
        self._tokens_lock = threading.Lock()

    # -- as the app ----------------------------------------------------------

    def app_jwt(self) -> str:
        """Sign a fresh app JWT (RS256).

        Returns
        -------
        str
            ``header.payload.signature`` with ``iss`` = the client id,
            ``iat`` = now - :data:`JWT_BACKDATE_SEC` and ``exp`` = now +
            :data:`JWT_LIFETIME_SEC`.
        """
        now = int(self._clock())
        header = {"alg": "RS256", "typ": "JWT"}
        payload = {
            "iat": now - JWT_BACKDATE_SEC,
            "exp": now + JWT_LIFETIME_SEC,
            "iss": self.config.client_id,
        }
        signing_input = ".".join(
            _b64url(json.dumps(part, separators=(",", ":")).encode("utf-8"))
            for part in (header, payload)
        )
        signature = self.config.private_key.sign(
            signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
        )
        return f"{signing_input}.{_b64url(signature)}"

    def _app_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
        }

    def installation_token(
        self, installation_id: int, repo_id: int, *, force: bool = False
    ) -> InstallationToken:
        """Return a read-only token scoped to one repository, minting when needed.

        ``POST /app/installations/{id}/access_tokens`` with
        ``repository_ids=[repo_id]`` and :data:`INSTALLATION_PERMISSIONS`.
        A token is cached per ``(installation_id, repo_id)`` and reused
        until :data:`TOKEN_REFRESH_MARGIN_SEC` before it expires.

        Parameters
        ----------
        installation_id : int
            The installation that covers the repository.
        repo_id : int
            The repository's numeric id.
        force : bool, optional
            Mint a new token even when a cached one is still usable (the
            scan runner's token-file refresh); it replaces the cached one.

        Returns
        -------
        InstallationToken
            The (possibly cached) token.

        Raises
        ------
        GitHubAccessLost
            GitHub answered ``403``, ``404`` or ``422`` (the installation is
            gone or suspended, or it does not cover the repository).
        GitHubUnavailable
            A network error, a timeout, a ``5xx`` or any other answer
            (e.g. a ``401``: the app's key or clock is wrong).
        """
        key = (installation_id, repo_id)
        with self._tokens_lock:
            cached = self._tokens.get(key)
            if (
                not force
                and cached is not None
                and cached.expires_at - TOKEN_REFRESH_MARGIN_SEC > self._clock()
            ):
                return cached
            self._tokens.pop(key, None)
        response = self._send(
            "POST",
            f"{self.config.api_url}/app/installations/{int(installation_id)}"
            "/access_tokens",
            "installation token",
            headers=self._app_headers(),
            json={
                "repository_ids": [int(repo_id)],
                "permissions": dict(INSTALLATION_PERMISSIONS),
            },
        )
        if response.status_code in _ACCESS_LOST:
            logger.info(
                "GitHub refused an installation token (installation %s, repo %s): %s",
                installation_id,
                repo_id,
                response.status_code,
            )
            raise GitHubAccessLost(
                response.status_code,
                "the GitHub App cannot reach this repository any more",
            )
        if response.status_code != 201:
            logger.warning(
                "GitHub installation token answered %s", response.status_code
            )
            raise GitHubUnavailable(
                f"GitHub refused an installation token ({response.status_code})"
            )
        body = _json(response)
        token = None if body is None else body.get("token")
        expires_at = None if body is None else _epoch(body.get("expires_at"))
        if not isinstance(token, str) or not token or expires_at is None:
            raise GitHubUnavailable("GitHub answered the installation token oddly")
        minted = InstallationToken(token, expires_at, installation_id, repo_id)
        with self._tokens_lock:
            self._tokens[key] = minted
        return minted

    def repository_installation(self, full_name: str) -> int | None:
        """Find the installation that covers a repository, by name (JWT).

        ``GET /repos/{owner}/{repo}/installation`` - used after a transfer.

        Parameters
        ----------
        full_name : str
            ``owner/name``.

        Returns
        -------
        int or None
            The installation id, or ``None`` when the app is not installed
            on the repository (``404``).

        Raises
        ------
        ValueError
            ``full_name`` is not ``owner/name``.
        GitHubUnavailable
            A network error, a timeout or an unexpected answer.
        """
        if not _FULL_NAME_RE.match(full_name):
            raise ValueError("a repository name must be owner/name")
        owner, name = full_name.split("/")
        response = self._send(
            "GET",
            f"{self.config.api_url}/repos/{quote(owner)}/{quote(name)}/installation",
            "repository installation",
            headers=self._app_headers(),
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            logger.warning(
                "GitHub repository installation answered %s", response.status_code
            )
            raise GitHubUnavailable(
                f"GitHub refused the installation lookup ({response.status_code})"
            )
        body = _json(response)
        ident = None if body is None else body.get("id")
        if not isinstance(ident, int) or isinstance(ident, bool):
            raise GitHubUnavailable("GitHub answered the installation lookup oddly")
        return ident

    def installation_repository(self, token: str, repo_id: int) -> GitHubRepo:
        """Read a repository with an installation token (``GET /repositories/{id}``).

        Parameters
        ----------
        token : str
            An installation token scoped to the repository.
        repo_id : int
            The repository's numeric id.

        Returns
        -------
        GitHubRepo
            Its current name and default branch.

        Raises
        ------
        GitHubAccessLost
            ``403`` / ``404`` (deleted, or no longer covered).
        GitHubUnavailable
            A network error, a timeout or an unexpected answer.
        """
        response = self._send(
            "GET",
            f"{self.config.api_url}/repositories/{int(repo_id)}",
            "repository lookup",
            headers=self._bearer(token),
        )
        if response.status_code in (403, 404):
            raise GitHubAccessLost(
                response.status_code,
                "the GitHub App cannot reach this repository any more",
            )
        if response.status_code != 200:
            logger.warning("GitHub repository lookup answered %s", response.status_code)
            raise GitHubUnavailable(
                f"GitHub refused the repository lookup ({response.status_code})"
            )
        return _parse_repo(_json(response))

    # -- user authorization --------------------------------------------------

    def authorize_url(self, state: str, challenge: str) -> str:
        """Build the app's user-authorization URL.

        No ``redirect_uri``: GitHub then uses the app's first callback URL
        (``<base>/auth/github-app``), the one the install redirect uses too.

        Parameters
        ----------
        state : str
            The OAuth ``state``.
        challenge : str
            The PKCE S256 challenge of the server-held verifier.

        Returns
        -------
        str
            ``<web_url>/login/oauth/authorize?client_id&state&code_challenge&
            code_challenge_method=S256``.
        """
        query = urlencode(
            {
                "client_id": self.config.client_id,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.config.web_url}/login/oauth/authorize?{query}"

    def install_url(self, state: str) -> str:
        """Build the app's install / configure page URL.

        Parameters
        ----------
        state : str
            The OAuth ``state``; GitHub hands it back on the redirect after
            the install (with ``code``, ``installation_id`` and
            ``setup_action``).

        Returns
        -------
        str
            ``<web_url>/apps/<slug>/installations/new?state=...``.
        """
        return (
            f"{self.config.web_url}/apps/{self.config.slug}/installations/new?"
            + urlencode({"state": state})
        )

    def exchange(self, code: str, verifier: str | None) -> UserAuthorization:
        """Exchange an authorization code for a user token.

        Parameters
        ----------
        code : str
            The code GitHub's redirect carried.
        verifier : str or None
            The PKCE verifier for an *authorize* arrival; ``None`` for an
            *install* arrival (GitHub saw no challenge on the install link).

        Returns
        -------
        UserAuthorization
            The user token and its expiry (the refresh token is dropped).

        Raises
        ------
        GitHubAuthFailed
            GitHub refused the code (a ``200`` with an ``error`` field, or
            another status), or returned no token.
        GitHubUnavailable
            A network error, a timeout or a ``5xx``.
        """
        data = {
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "code": code,
        }
        if verifier is not None:
            data["code_verifier"] = verifier
        response = self._send(
            "POST",
            f"{self.config.web_url}/login/oauth/access_token",
            "token exchange",
            data=data,
            headers={"Accept": "application/json"},
        )
        if response.status_code != 200:
            logger.warning("GitHub token exchange answered %s", response.status_code)
            raise GitHubAuthFailed("exchange_failed", "GitHub refused the code")
        body = _json(response)
        if body is None:
            raise GitHubUnavailable("GitHub answered the token exchange oddly")
        if body.get("error"):
            logger.info("GitHub token exchange refused: %s", str(body["error"])[:64])
            raise GitHubAuthFailed("exchange_failed", "GitHub refused the code")
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise GitHubAuthFailed("exchange_failed", "GitHub returned no access token")
        ttl = body.get("expires_in")
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
            ttl = USER_TOKEN_DEFAULT_TTL_SEC
        return UserAuthorization(token, self._clock() + ttl)

    def user(self, token: str) -> GitHubUser:
        """Read the user a user token belongs to (``GET /user``).

        Parameters
        ----------
        token : str
            A user token.

        Returns
        -------
        GitHubUser
            The profile; ``two_factor`` is always ``False`` (an app token
            carries no such field).

        Raises
        ------
        GitHubAuthFailed
            GitHub refused the token.
        GitHubUnavailable
            A network error, a timeout, a ``5xx`` or a mangled answer.
        """
        return self._user(token)

    @staticmethod
    def _bearer(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
        }

    def _user_get(
        self, url: str, what: str, token: str, params: Mapping[str, Any] | None = None
    ) -> dict:
        """GET with a user token: ``401`` rejected, ``403`` / ``404`` not found."""
        response = self._send(
            "GET", url, what, headers=self._bearer(token), params=params
        )
        if response.status_code == 401:
            raise GitHubTokenRejected(f"GitHub rejected the user token ({what})")
        if response.status_code in (403, 404):
            raise GitHubNotFound(f"not visible on GitHub ({what})")
        if response.status_code != 200:
            logger.warning("GitHub %s answered %s", what, response.status_code)
            raise GitHubUnavailable(
                f"GitHub refused the {what} ({response.status_code})"
            )
        body = _json(response)
        if body is None:
            raise GitHubUnavailable(f"GitHub answered the {what} oddly")
        return body

    def installations(self, token: str) -> list[Installation]:
        """List the app's installations the user can see (``GET /user/installations``).

        Parameters
        ----------
        token : str
            A user token.

        Returns
        -------
        list of Installation
            Up to 100 installations (one page; nobody sees more in practice).

        Raises
        ------
        GitHubTokenRejected
            ``401``: the token is expired or revoked.
        GitHubNotFound
            ``403`` / ``404``.
        GitHubUnavailable
            A network error, a timeout or an unexpected answer.
        """
        body = self._user_get(
            f"{self.config.api_url}/user/installations",
            "installation list",
            token,
            {"per_page": 100},
        )
        items = body.get("installations")
        if not isinstance(items, list):
            raise GitHubUnavailable("GitHub answered the installation list oddly")
        return [i for i in map(_parse_installation, items) if i is not None]

    def installation_repos(
        self, token: str, installation_id: int, page: int
    ) -> RepoPage:
        """List one page of an installation's repositories the user can see.

        ``GET /user/installations/{id}/repositories``, :data:`REPOS_PER_PAGE`
        per page.

        Parameters
        ----------
        token : str
            A user token.
        installation_id : int
            The installation.
        page : int
            The page, from 1.

        Returns
        -------
        RepoPage
            The repositories and the total the user can see.

        Raises
        ------
        GitHubTokenRejected
            ``401``.
        GitHubNotFound
            The user cannot see the installation (``403`` / ``404``).
        GitHubUnavailable
            A network error, a timeout or an unexpected answer.
        """
        body = self._user_get(
            f"{self.config.api_url}/user/installations/{int(installation_id)}"
            "/repositories",
            "repository list",
            token,
            {"per_page": REPOS_PER_PAGE, "page": max(1, int(page))},
        )
        items = body.get("repositories")
        total = body.get("total_count")
        if not isinstance(items, list):
            raise GitHubUnavailable("GitHub answered the repository list oddly")
        repos = [
            _parse_repo(item if isinstance(item, dict) else None) for item in items
        ]
        return RepoPage(
            repos=repos,
            total_count=total
            if isinstance(total, int) and not isinstance(total, bool)
            else len(repos),
        )

    def repository(self, token: str, repo_id: int) -> GitHubRepo:
        """Read a repository with a user token (``GET /repositories/{id}``).

        Parameters
        ----------
        token : str
            A user token.
        repo_id : int
            The repository's numeric id.

        Returns
        -------
        GitHubRepo
            With the user's ``permissions``.

        Raises
        ------
        GitHubTokenRejected
            ``401``.
        GitHubNotFound
            The user cannot read it (``403`` / ``404``).
        GitHubUnavailable
            A network error, a timeout or an unexpected answer.
        """
        return _parse_repo(
            self._user_get(
                f"{self.config.api_url}/repositories/{int(repo_id)}",
                "repository lookup",
                token,
            )
        )


# ---------------------------------------------------------------------------
# Authorizations in flight
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingAuthorization:
    """A user authorization (or install) started from an org's import page.

    Attributes
    ----------
    verifier : str
        The PKCE verifier (unused by an install arrival; never leaves the server).
    session_id : int
        The WhyGraph session that started it.
    user_id : int
        The user who started it.
    org_slug : str
        The org whose import page started it (where the callback returns).
    expires_at : float
        Clock reading after which the entry is dead.
    """

    verifier: str = field(repr=False)
    session_id: int
    user_id: int
    org_slug: str
    expires_at: float


class PendingAuthorizations:
    """In-memory map of GitHub App authorizations in flight, keyed by ``state``.

    Thread-safe, as M2d-1's :class:`~whygraph.portal.github_auth.PendingLogins`:
    entries live :data:`~whygraph.portal.github_auth.PENDING_TTL_SEC`, are
    single use, and past
    :data:`~whygraph.portal.github_auth.PENDING_MAX` the oldest is evicted.

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
        self._entries: OrderedDict[str, PendingAuthorization] = OrderedDict()
        self._lock = threading.Lock()

    def put(
        self,
        state: str,
        *,
        verifier: str,
        session_id: int,
        user_id: int,
        org_slug: str,
    ) -> None:
        """Remember a started authorization.

        Parameters
        ----------
        state : str
            The OAuth ``state``.
        verifier : str
            The PKCE verifier.
        session_id, user_id : int
            Who started it, in which session.
        org_slug : str
            The org it was started for.
        """
        entry = PendingAuthorization(
            verifier, session_id, user_id, org_slug, self._clock() + self._ttl
        )
        with self._lock:
            self._entries.pop(state, None)
            self._entries[state] = entry
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def pop(self, state: str) -> PendingAuthorization | None:
        """Take an authorization out of the map (single use).

        Parameters
        ----------
        state : str
            The OAuth ``state``.

        Returns
        -------
        PendingAuthorization or None
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


# ---------------------------------------------------------------------------
# User tokens, in memory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserToken:
    """A user token held for one WhyGraph session.

    Attributes
    ----------
    token : str
        The ``ghu_`` token (kept out of ``repr``).
    user_id : int
        The ``users.id`` it was obtained for.
    github_id : int
        The GitHub user id it belongs to.
    expires : float
        Epoch seconds when it expires.
    """

    token: str = field(repr=False)
    user_id: int
    github_id: int
    expires: float


class UserTokens:
    """The import page's user tokens, in memory, keyed by session id.

    Thread-safe. Never persisted, never returned in a response.
    **Correctness comes from the read**: :meth:`get` hands an entry out only
    when it belongs to the asking user and has not expired, so an entry
    left behind by a session that ended some other way is unreachable.
    :meth:`drop` and :meth:`sweep` only free memory.

    Parameters
    ----------
    clock : callable, optional
        Returns epoch seconds (default ``time.time``); injectable for tests.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._entries: dict[int, UserToken] = {}
        self._lock = threading.Lock()

    def put(
        self,
        session_id: int,
        *,
        token: str,
        user_id: int,
        github_id: int,
        expires: float,
    ) -> None:
        """Hold a token for a session, replacing any earlier one.

        Parameters
        ----------
        session_id : int
            The WhyGraph session (``sessions.id``).
        token : str
            The user token.
        user_id : int
            The signed-in user's ``users.id``.
        github_id : int
            The token's GitHub user id.
        expires : float
            Epoch seconds when the token expires.
        """
        entry = UserToken(token, user_id, github_id, expires)
        with self._lock:
            self._entries[session_id] = entry

    def get(self, session_id: int, user_id: int) -> UserToken | None:
        """Return a session's token if it belongs to ``user_id`` and is still live.

        Parameters
        ----------
        session_id : int
            The request's session.
        user_id : int
            The request's user.

        Returns
        -------
        UserToken or None
            ``None`` when there is none, it belongs to another user, or it
            expires within :data:`USER_TOKEN_MARGIN_SEC`.
        """
        with self._lock:
            entry = self._entries.get(session_id)
        if entry is None or entry.user_id != user_id:
            return None
        if entry.expires - USER_TOKEN_MARGIN_SEC <= self._clock():
            return None
        return entry

    def drop(self, session_id: int) -> UserToken | None:
        """Forget a session's token.

        Parameters
        ----------
        session_id : int
            The session that ended.

        Returns
        -------
        UserToken or None
            The dropped entry (so the caller can revoke it), if any.
        """
        with self._lock:
            return self._entries.pop(session_id, None)

    def drop_user(self, user_id: int) -> list[UserToken]:
        """Forget every token held for a user's sessions (all of them ended).

        Parameters
        ----------
        user_id : int
            The user whose sessions ended (password change, reset, disabling).

        Returns
        -------
        list of UserToken
            The dropped entries (so the caller can revoke them).
        """
        with self._lock:
            sids = [sid for sid, e in self._entries.items() if e.user_id == user_id]
            return [self._entries.pop(sid) for sid in sids]

    def sweep(self) -> int:
        """Forget every expired token.

        Returns
        -------
        int
            How many entries were removed.
        """
        now = self._clock()
        with self._lock:
            dead = [sid for sid, e in self._entries.items() if e.expires <= now]
            for sid in dead:
                del self._entries[sid]
        return len(dead)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


__all__ = [
    "APP_ENV_VARS",
    "INSTALLATION_PERMISSIONS",
    "GitHubAccessLost",
    "GitHubApp",
    "GitHubAppConfig",
    "GitHubNotFound",
    "GitHubRepo",
    "GitHubTokenRejected",
    "Installation",
    "InstallationToken",
    "PendingAuthorization",
    "PendingAuthorizations",
    "RepoPage",
    "UserAuthorization",
    "UserToken",
    "UserTokens",
    "load_github_app_config",
]
