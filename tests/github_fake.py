"""A fake GitHub for the portal's OAuth App sign-in (M2d-1 plan section 4.9).

One module serves every non-production use - pytest, e2e, smoke and the dev
loop - so they cannot drift apart. :class:`FakeGitHub` holds an in-memory
store and its :meth:`FakeGitHub.handle` is a pure
``httpx.Request -> httpx.Response`` function, which pytest plugs into
``httpx.MockTransport``. Run as a script it wraps the same handler in a
stdlib ``http.server``::

    uv run --no-sync python tests/github_fake.py --host 127.0.0.1 --port 18767 \\
        --client-id ID --client-secret-file SECRET \\
        --redirect-uri http://whygraph.localhost:5173/auth/github

Behaves like GitHub where the portal depends on it: client id / secret and
the ``redirect_uri`` are checked, PKCE (S256) is verified, codes are single
use and expire, failures are **200 with an ``error`` field**,
``GET /user`` includes ``two_factor_authentication`` only for a token granted
``read:user`` and a revoked token answers 401. Routes:

* web: ``GET /login/oauth/authorize``, ``POST /login/oauth/access_token``
* API (the GHES shape, under ``/api/v3``): ``GET /user``,
  ``DELETE /applications/{client_id}/token``

M2d-2 (plan section 4.11) adds the **GitHub App**, enabled by the ``app_*``
constructor arguments: user authorization on the same authorize / token
endpoints keyed by the app's client id (``ghu_`` tokens that expire; PKCE is
optional for a code from the install redirect), the install page
``GET /apps/{slug}/installations/new`` (redirects to the app's callback with
``code``, ``installation_id``, ``setup_action`` and ``state``), app routes
authenticated by an RS256 JWT verified against the registered public key
(``POST /app/installations/{id}/access_tokens``,
``GET /repos/{o}/{r}/installation``), the user routes
(``GET /user/installations``, ``GET /user/installations/{id}/repositories``,
``GET /repositories/{id}``) over an installations / repositories data model
(:meth:`FakeGitHub.add_installation`, :meth:`FakeGitHub.add_repo`), and
**git over dumb HTTP**: a repository with a bare ``path`` is served by
``GET /{owner}/{repo}.git/...`` only to an unexpired installation token
scoped to it (anything else is ``401`` with ``WWW-Authenticate: Basic``).
:meth:`FakeGitHub.webhook_delivery` / :meth:`FakeGitHub.send_webhook` build
and send a signed delivery. :func:`create_bare_repo` and :func:`push_commit`
keep fixture repositories.

Never imported by ``src/`` and never copied into the image.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

API_PREFIX = "/api/v3"
CODE_TTL_SEC = 600.0
USER_TOKEN_TTL_SEC = 8 * 60 * 60
"""Lifetime of an app user token (``ghu_``), as on GitHub."""
INSTALLATION_TOKEN_TTL_SEC = 60 * 60
"""Default lifetime of an installation token (``ghs_``); see ``installation_token_ttl``."""
APP_PERMISSIONS: dict[str, str] = {
    "contents": "read",
    "metadata": "read",
    "pull_requests": "read",
    "issues": "read",
}
"""What the fake app is granted; a token may ask for a subset."""


@dataclass
class FakeUser:
    """One GitHub account in the store."""

    id: int
    login: str
    name: str | None
    avatar_url: str
    two_factor: bool = True


@dataclass
class _Code:
    user_id: int
    challenge: str | None
    method: str | None
    scope: str
    redirect_uri: str
    expires: float
    used: bool = False
    client_id: str = ""


@dataclass
class _Token:
    user_id: int
    scope: str
    revoked: bool = False
    app: bool = False
    expires: float | None = None


@dataclass
class FakeInstallation:
    """One installation of the fake GitHub App."""

    id: int
    account: str
    account_type: str = "User"
    suspended: bool = False
    repository_selection: str = "selected"


@dataclass
class FakeRepo:
    """One repository: who reads it, which installation covers it, its git dir."""

    id: int
    full_name: str
    installation: int | None
    default_branch: str = "main"
    readers: dict[str, dict[str, bool]] = field(default_factory=dict)
    public: bool = False
    path: Path | None = None


@dataclass
class _InstallationToken:
    installation_id: int
    repo_ids: tuple[int, ...]
    permissions: dict[str, str]
    expires: float
    revoked: bool = False


@dataclass
class _Forced:
    status: int | None
    exc: BaseException | None
    times: int | None


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_GIT_PATH = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/(?P<rest>.+)$")


class FakeGitHub:
    """In-memory GitHub: users, codes and tokens, plus :meth:`handle`.

    Parameters
    ----------
    client_id, client_secret : str
        The OAuth App credentials the fake accepts.
    redirect_uri : str
        The one registered callback URL; anything else is refused.
    now : callable, optional
        Returns epoch seconds; tests inject a controllable clock.
    app_client_id, app_client_secret : str, optional
        The GitHub App's credentials; the app routes answer only when set.
    app_callback : str, optional
        The app's first callback URL (where authorize and install redirect).
    app_public_key : RSAPublicKey or PEM bytes / str, optional
        Verifies the app JWT.
    app_slug : str
        The app's URL name (``/apps/<slug>/installations/new``).
    webhook_secret : str, optional
        Signs :meth:`webhook_delivery`.
    installation_token_ttl : float
        Lifetime of a minted installation token, in seconds.

    Attributes
    ----------
    users : dict[str, FakeUser]
        Keyed by login (as stored, case preserved).
    codes, tokens : dict
        Authorization codes and access tokens issued so far.
    requests : list[httpx.Request]
        Every request handled, in order (assert on revoke calls here).
    granted_scope : str or None
        When set, the scope every token exchange grants, whatever the
        authorize request asked for.
    installations : dict[int, FakeInstallation]
    repos : dict[int, FakeRepo]
    installation_tokens : dict[str, _InstallationToken]
        Minted ``ghs_`` tokens (set ``expires`` / ``revoked`` to age one).
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        *,
        now: Callable[[], float] = time.time,
        app_client_id: str | None = None,
        app_client_secret: str | None = None,
        app_callback: str | None = None,
        app_public_key: rsa.RSAPublicKey | bytes | str | None = None,
        app_slug: str = "whygraph-test",
        webhook_secret: str | None = None,
        installation_token_ttl: float = INSTALLATION_TOKEN_TTL_SEC,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.now = now
        self.app_client_id = app_client_id
        self.app_client_secret = app_client_secret
        self.app_callback = app_callback
        if isinstance(app_public_key, (bytes, str)):
            key = serialization.load_pem_public_key(
                app_public_key.encode()
                if isinstance(app_public_key, str)
                else app_public_key
            )
            assert isinstance(key, rsa.RSAPublicKey)
            app_public_key = key
        self.app_public_key = app_public_key
        self.app_slug = app_slug
        self.webhook_secret = webhook_secret
        self.installation_token_ttl = installation_token_ttl
        self.installations: dict[int, FakeInstallation] = {}
        self.repos: dict[int, FakeRepo] = {}
        self.installation_tokens: dict[str, _InstallationToken] = {}
        self.users: dict[str, FakeUser] = {}
        self.codes: dict[str, _Code] = {}
        self.tokens: dict[str, _Token] = {}
        self.requests: list[httpx.Request] = []
        self.granted_scope: str | None = None
        self._forced: dict[str, list[_Forced]] = {}
        self._lock = threading.RLock()
        for ident, (login, tfa) in enumerate(
            [("ben", True), ("cy", True), ("dee", True), ("nofa", False)], start=1001
        ):
            self.add_user(login, id=ident, two_factor=tfa)

    # -- test controls ------------------------------------------------------

    def add_user(
        self,
        login: str,
        *,
        id: int | None = None,  # noqa: A002
        two_factor: bool = True,
        name: str | None = None,
    ) -> FakeUser:
        """Add an account; ``id`` defaults to the next free integer."""
        with self._lock:
            ident = (
                id
                if id is not None
                else max((u.id for u in self.users.values()), default=1000) + 1
            )
            user = FakeUser(
                id=ident,
                login=login,
                name=name if name is not None else login.capitalize(),
                avatar_url=f"https://avatars.example.test/u/{ident}",
                two_factor=two_factor,
            )
            self.users[login] = user
            return user

    def rename_user(self, old: str, new: str) -> FakeUser:
        """Rename an account; its id stays (GitHub's rule)."""
        with self._lock:
            user = self.users.pop(old)
            user.login = new
            self.users[new] = user
            return user

    def remove_user(self, login: str) -> None:
        """Delete an account (its login becomes free for a new id)."""
        with self._lock:
            self.users.pop(login, None)

    def force(
        self,
        route: str,
        *,
        status: int | None = None,
        exc: BaseException | None = None,
        times: int | None = 1,
    ) -> None:
        """Make a route fail: a status (e.g. 500) or a raised exception.

        Parameters
        ----------
        route : str
            ``"authorize"``, ``"access_token"``, ``"user"`` or ``"revoke"``;
            the GitHub App's ``"install"``, ``"app_token"``,
            ``"repo_installation"``, ``"user_installations"``,
            ``"installation_repos"``, ``"repository"`` and ``"git"``.
        status : int, optional
            Answer with this HTTP status and a plain-text body.
        exc : BaseException, optional
            Raise it instead (``httpx.ReadTimeout`` simulates a timeout).
        times : int or None
            How many requests to fail; ``None`` is every request.
        """
        with self._lock:
            self._forced.setdefault(route, []).append(_Forced(status, exc, times))

    def clear_forced(self) -> None:
        """Drop every forced failure."""
        with self._lock:
            self._forced.clear()

    def tokens_for(self, login: str) -> list[str]:
        """Access tokens issued to ``login`` (revoked ones included)."""
        with self._lock:
            uid = self.users[login].id
            return [t for t, v in self.tokens.items() if v.user_id == uid]

    def is_revoked(self, token: str) -> bool:
        """Whether ``token`` was revoked."""
        with self._lock:
            return self.tokens[token].revoked

    def calls(self, method: str, path_prefix: str) -> list[httpx.Request]:
        """Handled requests with this method whose path starts with a prefix."""
        return [
            r
            for r in self.requests
            if r.method == method and r.url.path.startswith(path_prefix)
        ]

    def issue_code(
        self,
        login: str,
        *,
        challenge: str | None = None,
        scope: str = "read:user",
        app: bool = False,
    ) -> str:
        """Mint an authorization code directly (skips the authorize step).

        With ``app`` the code belongs to the GitHub App (an install-redirect
        code when ``challenge`` is ``None``).
        """
        with self._lock:
            return self._new_code(
                self.users[login],
                challenge,
                "S256" if challenge else None,
                "" if app else scope,
                (self.app_callback or "") if app else self.redirect_uri,
                client_id=self.app_client_id if app else None,
            )

    # -- test controls: the GitHub App ----------------------------------------

    def add_installation(
        self,
        id: int,  # noqa: A002
        account: str,
        *,
        account_type: str = "User",
        repository_selection: str = "selected",
    ) -> FakeInstallation:
        """Install the app on an account (a user or an organization login)."""
        with self._lock:
            inst = FakeInstallation(
                id, account, account_type, repository_selection=repository_selection
            )
            self.installations[id] = inst
            return inst

    def uninstall(self, installation: int) -> None:
        """Remove an installation (its tokens stop working, minting is ``404``)."""
        with self._lock:
            self.installations.pop(installation, None)
            for repo in self.repos.values():
                if repo.installation == installation:
                    repo.installation = None

    def add_repo(
        self,
        id: int,  # noqa: A002
        full_name: str,
        installation: int | None,
        default_branch: str = "main",
        readers: dict[str, dict[str, bool]] | None = None,
        public: bool = False,
        path: Path | None = None,
    ) -> FakeRepo:
        """Add a repository.

        Parameters
        ----------
        id : int
            GitHub's numeric repository id.
        full_name : str
            ``owner/name``.
        installation : int or None
            The installation that covers it (``None``: the app cannot see it).
        default_branch : str
            The default branch.
        readers : dict, optional
            ``{login: permissions}`` - users with explicit access and their
            permissions (missing keys are ``False``, e.g.
            ``{"ben": {"pull": True}}``). The owner of a ``owner/name`` repo
            always has every permission.
        public : bool
            A public repo is readable by any user token (spike #4).
        path : Path, optional
            A bare git repository served over dumb HTTP
            (:func:`create_bare_repo`).
        """
        with self._lock:
            repo = FakeRepo(
                id,
                full_name,
                installation,
                default_branch,
                dict(readers or {}),
                public,
                path,
            )
            self.repos[id] = repo
            return repo

    def issue_installation_token(
        self,
        installation: int,
        repo_ids: list[int] | None = None,
        *,
        ttl: float | None = None,
    ) -> str:
        """Mint an installation token directly (skips the JWT); all repos if ``None``."""
        with self._lock:
            inst = self.installations[installation]
            ids = (
                tuple(repo_ids)
                if repo_ids is not None
                else tuple(self._covered(inst.id))
            )
            token = "ghs_" + secrets.token_hex(20)
            self.installation_tokens[token] = _InstallationToken(
                inst.id,
                ids,
                dict(APP_PERMISSIONS),
                self.now() + (self.installation_token_ttl if ttl is None else ttl),
            )
            return token

    def webhook_delivery(
        self,
        event: str,
        payload: dict,
        *,
        secret: str | None = None,
        delivery: str | None = None,
    ) -> tuple[dict[str, str], bytes]:
        """Build a signed webhook delivery as GitHub sends it.

        Parameters
        ----------
        event : str
            ``X-GitHub-Event`` (``push``, ``installation``, ...).
        payload : dict
            The JSON body.
        secret : str, optional
            Overrides ``webhook_secret`` (to send a wrongly signed one).
        delivery : str, optional
            ``X-GitHub-Delivery``; a fresh UUID by default.

        Returns
        -------
        tuple of (dict, bytes)
            The headers (``X-Hub-Signature-256`` and the legacy SHA-1
            ``X-Hub-Signature`` included) and the raw body.
        """
        key = (secret if secret is not None else self.webhook_secret or "").encode()
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "GitHub-Hookshot/fake",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery or str(uuid.uuid4()),
            "X-GitHub-Hook-Installation-Target-Type": "integration",
            "X-GitHub-Hook-Installation-Target-Id": "1",
            "X-Hub-Signature-256": "sha256="
            + hmac.new(key, body, hashlib.sha256).hexdigest(),
            "X-Hub-Signature": "sha1=" + hmac.new(key, body, hashlib.sha1).hexdigest(),
        }
        return headers, body

    def send_webhook(
        self,
        url: str,
        event: str,
        payload: dict,
        *,
        host: str | None = None,
        client: httpx.Client | None = None,
    ) -> httpx.Response:
        """Sign and POST a delivery (``Host: <host>`` when given).

        ``client`` may be any object with httpx's ``post`` (a
        ``TestClient`` too); by default a short-lived ``httpx.Client``.
        """
        headers, body = self.webhook_delivery(event, payload)
        if host is not None:
            headers["Host"] = host
        if client is not None:
            return client.post(url, content=body, headers=headers)
        with httpx.Client(timeout=10.0) as own:
            return own.post(url, content=body, headers=headers)

    # -- the handler --------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one request (a pure function of the store and the request)."""
        with self._lock:
            self.requests.append(request)
            path = request.url.path
            if request.method == "GET" and path == "/login/oauth/authorize":
                route, fn = "authorize", self._authorize
            elif request.method == "POST" and path == "/login/oauth/access_token":
                route, fn = "access_token", self._access_token
            elif request.method == "GET" and path == f"{API_PREFIX}/user":
                route, fn = "user", self._user
            elif request.method == "DELETE" and path.startswith(
                f"{API_PREFIX}/applications/"
            ):
                route, fn = "revoke", self._revoke
            elif (found := self._app_route(request)) is not None:
                route, fn = found
            else:
                return httpx.Response(404, json={"message": "Not Found"})
            forced = self._take_forced(route)
            if forced is not None:
                if forced.exc is not None:
                    raise forced.exc
                return httpx.Response(forced.status or 500, text="forced failure")
            return fn(request)

    def _take_forced(self, route: str) -> _Forced | None:
        queue = self._forced.get(route)
        if not queue:
            return None
        item = queue[0]
        if item.times is not None:
            item.times -= 1
            if item.times <= 0:
                queue.pop(0)
        return item

    def _app_route(
        self, request: httpx.Request
    ) -> tuple[str, Callable[[httpx.Request], httpx.Response]] | None:
        """The GitHub App's routes (``None`` when the app is off or no match)."""
        if self.app_client_id is None:
            return None
        method, path = request.method, request.url.path
        api = API_PREFIX
        if method == "GET" and path == f"/apps/{self.app_slug}/installations/new":
            return "install", self._install_page
        if method == "POST" and re.fullmatch(
            rf"{api}/app/installations/\d+/access_tokens", path
        ):
            return "app_token", self._mint
        if method == "GET" and re.fullmatch(
            rf"{api}/repos/[^/]+/[^/]+/installation", path
        ):
            return "repo_installation", self._repo_installation
        if method == "GET" and path == f"{api}/user/installations":
            return "user_installations", self._user_installations
        if method == "GET" and re.fullmatch(
            rf"{api}/user/installations/\d+/repositories", path
        ):
            return "installation_repos", self._installation_repos
        if method == "GET" and re.fullmatch(rf"{api}/repositories/\d+", path):
            return "repository", self._repository
        if method == "GET" and not path.startswith(api) and _GIT_PATH.match(path):
            return "git", self._git
        return None

    def _new_code(
        self,
        user: FakeUser,
        challenge: str | None,
        method: str | None,
        scope: str,
        redirect_uri: str,
        *,
        client_id: str | None = None,
    ) -> str:
        code = secrets.token_hex(10)
        self.codes[code] = _Code(
            user.id,
            challenge,
            method,
            scope,
            redirect_uri,
            self.now() + CODE_TTL_SEC,
            client_id=client_id if client_id is not None else self.client_id,
        )
        return code

    def _user_by_id(self, user_id: int) -> FakeUser | None:
        return next((u for u in self.users.values() if u.id == user_id), None)

    # -- web: authorize -----------------------------------------------------

    def _authorize(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params.items())
        app = (
            self.app_client_id is not None
            and params.get("client_id") == self.app_client_id
        )
        if not app and params.get("client_id") != self.client_id:
            return httpx.Response(404, text="Unknown OAuth application")
        callback = (self.app_callback or "") if app else self.redirect_uri
        redirect_uri = params.get("redirect_uri", callback)
        if redirect_uri != callback:
            return httpx.Response(
                422, text="The redirect_uri is not associated with this application"
            )
        login = params.get("login")
        if login is None:
            return self._authorize_page(params)
        user = self.users.get(login)
        if user is None:
            return httpx.Response(404, text=f"No such GitHub user: {login}")
        scope = params.get("scope", "")
        code = self._new_code(
            user,
            params.get("code_challenge"),
            params.get("code_challenge_method"),
            "" if app else scope,
            redirect_uri,
            client_id=self.app_client_id if app else None,
        )
        query = {"code": code}
        if "state" in params:
            query["state"] = params["state"]
        if app:
            # The authorize callback carries GitHub's issuer (spike #1).
            query["iss"] = f"{self._origin(request)}/login/oauth"
        return httpx.Response(
            302, headers={"location": f"{redirect_uri}?{urlencode(query)}"}
        )

    def _authorize_page(self, params: dict[str, str]) -> httpx.Response:
        rows = []
        for login in self.users:
            href = "/login/oauth/authorize?" + urlencode({**params, "login": login})
            rows.append(
                f'<li><a class="continue" href="{html.escape(href)}">'
                f"Continue as {html.escape(login)}</a></li>"
            )
        body = (
            "<!doctype html><html><head><title>Fake GitHub</title></head><body>"
            "<h1>Authorize WhyGraph (fake GitHub)</h1><ul>"
            + "".join(rows)
            + "</ul></body></html>"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    # -- web: token exchange ------------------------------------------------

    @staticmethod
    def _body_params(request: httpx.Request) -> dict[str, str]:
        raw = request.content.decode("utf-8", "replace")
        ctype = request.headers.get("content-type", "")
        if "json" in ctype:
            try:
                data = json.loads(raw or "{}")
            except json.JSONDecodeError:
                return {}
            return (
                {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}
            )
        return {k: v[0] for k, v in parse_qs(raw).items()}

    @staticmethod
    def _error(error: str, description: str) -> httpx.Response:
        return httpx.Response(
            200, json={"error": error, "error_description": description}
        )

    def _access_token(self, request: httpx.Request) -> httpx.Response:
        p = self._body_params(request)
        app = (
            self.app_client_id is not None
            and p.get("client_id") == self.app_client_id
            and p.get("client_secret") == self.app_client_secret
        )
        if not app and (
            p.get("client_id") != self.client_id
            or p.get("client_secret") != self.client_secret
        ):
            return self._error(
                "incorrect_client_credentials",
                "The client_id and/or client_secret passed are incorrect.",
            )
        entry = self.codes.get(p.get("code", ""))
        if (
            entry is None
            or entry.used
            or entry.expires <= self.now()
            or entry.client_id != p.get("client_id")
        ):
            if entry is not None:
                entry.used = True
            return self._error(
                "bad_verification_code", "The code passed is incorrect or expired."
            )
        entry.used = True  # single use, whatever happens next
        if app:
            # A GitHub App needs no redirect_uri (spike #1); one sent must match.
            if p.get("redirect_uri") not in (None, self.app_callback):
                return self._error(
                    "redirect_uri_mismatch",
                    "The redirect_uri MUST match the registered callback URL.",
                )
        elif p.get("redirect_uri") != self.redirect_uri:
            return self._error(
                "redirect_uri_mismatch",
                "The redirect_uri MUST match the registered callback URL.",
            )
        if entry.challenge is not None:
            verifier = p.get("code_verifier")
            if not verifier or _s256(verifier) != entry.challenge:
                return self._error(
                    "bad_verification_code", "The code_verifier does not match."
                )
        if app:
            token = "ghu_" + secrets.token_hex(20)
            self.tokens[token] = _Token(
                entry.user_id, "", app=True, expires=self.now() + USER_TOKEN_TTL_SEC
            )
            return httpx.Response(
                200,
                json={
                    "access_token": token,
                    "expires_in": USER_TOKEN_TTL_SEC,
                    "refresh_token": "ghr_" + secrets.token_hex(20),
                    "refresh_token_expires_in": 15_811_200,
                    "token_type": "bearer",
                    "scope": "",
                },
            )
        token = "gho_" + secrets.token_hex(20)
        scope = self.granted_scope if self.granted_scope is not None else entry.scope
        self.tokens[token] = _Token(entry.user_id, scope)
        return httpx.Response(
            200,
            json={"access_token": token, "token_type": "bearer", "scope": scope},
        )

    # -- API ----------------------------------------------------------------

    def _bearer(self, request: httpx.Request) -> str | None:
        auth = request.headers.get("authorization", "")
        scheme, _, value = auth.partition(" ")
        return value.strip() if scheme.lower() in ("bearer", "token") else None

    def _token_user(self, request: httpx.Request) -> tuple[_Token, FakeUser] | None:
        """The live user token of a request and its user, or ``None``."""
        entry = self.tokens.get(self._bearer(request) or "")
        if entry is None or entry.revoked:
            return None
        if entry.expires is not None and entry.expires <= self.now():
            return None
        user = self._user_by_id(entry.user_id)
        return None if user is None else (entry, user)

    def _user(self, request: httpx.Request) -> httpx.Response:
        found = self._token_user(request)
        if found is None:
            return httpx.Response(401, json={"message": "Bad credentials"})
        entry, user = found
        body: dict[str, object] = {
            "id": user.id,
            "login": user.login,
            "name": user.name,
            "avatar_url": user.avatar_url,
            "type": "User",
        }
        if "read:user" in {s.strip() for s in entry.scope.split(",")}:
            body["two_factor_authentication"] = user.two_factor
        return httpx.Response(200, json=body)

    def _revoke(self, request: httpx.Request) -> httpx.Response:
        client_id = request.url.path[len(f"{API_PREFIX}/applications/") :].split("/")[0]
        app = self.app_client_id is not None and client_id == self.app_client_id
        if app:
            ok = self._basic_ok(request, self.app_client_id, self.app_client_secret)
        else:
            ok = client_id == self.client_id and self._basic_ok(request)
        if not ok:
            return httpx.Response(401, json={"message": "Bad credentials"})
        try:
            data = json.loads(request.content or b"{}")
        except json.JSONDecodeError:
            data = {}
        entry = self.tokens.get(str(data.get("access_token", "")))
        if entry is None or entry.app != app:
            return httpx.Response(404, json={"message": "Not Found"})
        entry.revoked = True
        return httpx.Response(204)

    def _basic_ok(
        self,
        request: httpx.Request,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> bool:
        raw = self._basic(request)
        if raw is None:
            return False
        if client_id is None:
            client_id, client_secret = self.client_id, self.client_secret
        return raw == (client_id, client_secret)

    @staticmethod
    def _basic(request: httpx.Request) -> tuple[str, str] | None:
        """The ``(user, password)`` of a basic ``Authorization`` header."""
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "basic":
            return None
        try:
            raw = base64.b64decode(value).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        user, sep, password = raw.partition(":")
        return (user, password) if sep else None

    @staticmethod
    def _origin(request: httpx.Request) -> str:
        return f"{request.url.scheme}://{request.url.netloc.decode('ascii')}"

    # -- the GitHub App: install page --------------------------------------

    def _install_page(self, request: httpx.Request) -> httpx.Response:
        """``/apps/<slug>/installations/new``: pick an account, then redirect.

        ``login`` (who installs) is required to redirect, else a page of
        "Install as ..." links. ``installation_id`` configures an existing
        installation (``setup_action=update``); otherwise a new one is
        created on ``account`` (default: ``login``). ``request=1`` simulates
        an org member asking the owners (``setup_action=request``).
        """
        params = dict(request.url.params.items())
        login = params.get("login")
        if login is None:
            rows = []
            for name in self.users:
                href = request.url.path + "?" + urlencode({**params, "login": name})
                rows.append(
                    f'<li><a class="install" href="{html.escape(href)}">'
                    f"Install as {html.escape(name)}</a></li>"
                )
            body = (
                "<!doctype html><html><head><title>Fake GitHub</title></head><body>"
                f"<h1>Install {html.escape(self.app_slug)} (fake GitHub)</h1><ul>"
                + "".join(rows)
                + "</ul></body></html>"
            )
            return httpx.Response(200, text=body, headers={"content-type": "text/html"})
        user = self.users.get(login)
        if user is None:
            return httpx.Response(404, text=f"No such GitHub user: {login}")
        query: dict[str, str] = {
            "code": self._new_code(
                user,
                None,
                None,
                "",
                self.app_callback or "",
                client_id=self.app_client_id,
            )
        }
        if params.get("request") == "1":
            query["setup_action"] = "request"
        else:
            existing = params.get("installation_id")
            if (
                existing is not None
                and existing.isdigit()
                and (int(existing) in self.installations)
            ):
                inst = self.installations[int(existing)]
                query["setup_action"] = "update"
            else:
                inst = self.add_installation(
                    max(self.installations, default=100) + 1,
                    params.get("account", login),
                )
                query["setup_action"] = "install"
            query["installation_id"] = str(inst.id)
        if "state" in params:
            query["state"] = params["state"]
        return httpx.Response(
            302, headers={"location": f"{self.app_callback}?{urlencode(query)}"}
        )

    # -- the GitHub App: as the app (JWT) ----------------------------------

    def _jwt_ok(self, request: httpx.Request) -> bool:
        """Whether the bearer is a valid, current RS256 app JWT."""
        token = self._bearer(request)
        if not token or self.app_public_key is None:
            return False
        try:
            head, body, sig = token.split(".")
            header = json.loads(_unb64url(head))
            claims = json.loads(_unb64url(body))
            self.app_public_key.verify(
                _unb64url(sig),
                f"{head}.{body}".encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except Exception:  # noqa: BLE001 -- anything malformed is "not a JWT"
            return False
        iat, exp = claims.get("iat"), claims.get("exp")
        if header.get("alg") != "RS256" or claims.get("iss") != self.app_client_id:
            return False
        if not isinstance(iat, int) or not isinstance(exp, int):
            return False
        now = self.now()
        return iat <= now + 60 and exp > now and exp - iat <= 600

    @staticmethod
    def _jwt_refused() -> httpx.Response:
        return httpx.Response(
            401, json={"message": "A JSON web token could not be decoded"}
        )

    def _covered(self, installation: int) -> list[int]:
        return sorted(
            r.id for r in self.repos.values() if r.installation == installation
        )

    def _mint(self, request: httpx.Request) -> httpx.Response:
        if not self._jwt_ok(request):
            return self._jwt_refused()
        ident = int(request.url.path.split("/")[-2])
        inst = self.installations.get(ident)
        if inst is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if inst.suspended:
            return httpx.Response(
                403, json={"message": "This installation has been suspended"}
            )
        try:
            data = json.loads(request.content or b"{}")
        except json.JSONDecodeError:
            return httpx.Response(400, json={"message": "Problems parsing JSON"})
        covered = self._covered(inst.id)
        wanted = data.get("repository_ids")
        names = data.get("repositories")
        if wanted is None and names is None:
            ids = covered
        else:
            ids = [int(i) for i in wanted or []]
            by_name = {r.full_name.split("/")[1]: r.id for r in self.repos.values()}
            ids += [by_name.get(n, -1) for n in names or []]
            if not ids or any(i not in covered for i in ids):
                return httpx.Response(
                    422,
                    json={
                        "message": "There is at least one repository that does not "
                        "exist or is not accessible to the parent installation."
                    },
                )
        permissions = data.get("permissions") or dict(APP_PERMISSIONS)
        if not isinstance(permissions, dict) or any(
            APP_PERMISSIONS.get(k) != v for k, v in permissions.items()
        ):
            return httpx.Response(
                422,
                json={
                    "message": "The permissions requested are not granted to this installation."
                },
            )
        token = "ghs_" + secrets.token_hex(20)
        expires = self.now() + self.installation_token_ttl
        self.installation_tokens[token] = _InstallationToken(
            inst.id, tuple(ids), dict(permissions), expires
        )
        return httpx.Response(
            201,
            json={
                "token": token,
                "expires_at": _iso(expires),
                "permissions": permissions,
                "repository_selection": (
                    inst.repository_selection
                    if wanted is None and names is None
                    else "selected"
                ),
                "repositories": [
                    {"id": i, "full_name": self.repos[i].full_name} for i in ids
                ],
            },
        )

    def _repo_by_name(self, owner: str, name: str) -> FakeRepo | None:
        wanted = f"{unquote(owner)}/{unquote(name)}".lower()
        return next(
            (r for r in self.repos.values() if r.full_name.lower() == wanted), None
        )

    def _installation_json(self, inst: FakeInstallation) -> dict:
        return {
            "id": inst.id,
            "account": {
                "login": inst.account,
                "type": inst.account_type,
                "avatar_url": f"https://avatars.example.test/a/{inst.account}",
            },
            "repository_selection": inst.repository_selection,
            "app_slug": self.app_slug,
            "target_type": inst.account_type,
        }

    def _repo_installation(self, request: httpx.Request) -> httpx.Response:
        if not self._jwt_ok(request):
            return self._jwt_refused()
        parts = request.url.path.split("/")
        repo = self._repo_by_name(parts[-3], parts[-2])
        inst = (
            None
            if repo is None or repo.installation is None
            else self.installations.get(repo.installation)
        )
        if inst is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json=self._installation_json(inst))

    # -- the GitHub App: as the user (ghu_) --------------------------------

    def _permissions(self, user: FakeUser, repo: FakeRepo) -> dict[str, bool] | None:
        """``user``'s explicit permissions on ``repo``, or ``None``."""
        if repo.full_name.split("/")[0].lower() == user.login.lower():
            return dict.fromkeys(("admin", "maintain", "push", "triage", "pull"), True)
        if user.login in repo.readers:
            base = dict.fromkeys(("admin", "maintain", "push", "triage", "pull"), False)
            return {**base, **repo.readers[user.login]}
        return None

    def _sees(self, user: FakeUser, inst: FakeInstallation) -> bool:
        if inst.account.lower() == user.login.lower():
            return True
        return any(
            self._permissions(user, r) is not None
            for r in self.repos.values()
            if r.installation == inst.id
        )

    def _app_user(self, request: httpx.Request) -> FakeUser | httpx.Response:
        found = self._token_user(request)
        if found is None:
            return httpx.Response(401, json={"message": "Bad credentials"})
        if not found[0].app:
            return httpx.Response(
                403,
                json={
                    "message": "You must authenticate with an access token "
                    "authorized to a GitHub App in order to list installations"
                },
            )
        return found[1]

    def _repo_json(self, repo: FakeRepo, perms: dict[str, bool] | None) -> dict:
        body: dict[str, object] = {
            "id": repo.id,
            "name": repo.full_name.split("/")[1],
            "full_name": repo.full_name,
            "private": not repo.public,
            "default_branch": repo.default_branch,
            "owner": {"login": repo.full_name.split("/")[0]},
        }
        if perms is not None:
            body["permissions"] = perms
        return body

    def _user_installations(self, request: httpx.Request) -> httpx.Response:
        user = self._app_user(request)
        if isinstance(user, httpx.Response):
            return user
        visible = [
            self._installation_json(i)
            for _, i in sorted(self.installations.items())
            if self._sees(user, i)
        ]
        return httpx.Response(
            200, json={"total_count": len(visible), "installations": visible}
        )

    def _installation_repos(self, request: httpx.Request) -> httpx.Response:
        user = self._app_user(request)
        if isinstance(user, httpx.Response):
            return user
        inst = self.installations.get(int(request.url.path.split("/")[-2]))
        if inst is None or not self._sees(user, inst):
            return httpx.Response(404, json={"message": "Not Found"})
        rows = [
            self._repo_json(r, perms)
            for _, r in sorted(self.repos.items())
            if r.installation == inst.id
            and (perms := self._permissions(user, r)) is not None
        ]
        params = request.url.params
        per_page = max(1, min(int(params.get("per_page", "30")), 100))
        page = max(1, int(params.get("page", "1")))
        chunk = rows[(page - 1) * per_page : page * per_page]
        return httpx.Response(
            200, json={"total_count": len(rows), "repositories": chunk}
        )

    def _repository(self, request: httpx.Request) -> httpx.Response:
        repo = self.repos.get(int(request.url.path.split("/")[-1]))
        bearer = self._bearer(request) or ""
        inst_token = self.installation_tokens.get(bearer)
        if inst_token is not None:
            if not self._installation_token_live(inst_token):
                return httpx.Response(401, json={"message": "Bad credentials"})
            if repo is None or repo.id not in inst_token.repo_ids:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=self._repo_json(repo, None))
        found = self._token_user(request)
        if found is None:
            return httpx.Response(401, json={"message": "Bad credentials"})
        perms = None if repo is None else self._permissions(found[1], repo)
        if repo is not None and perms is None and repo.public:
            perms = {"admin": False, "push": False, "pull": True}
        if repo is None or perms is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json=self._repo_json(repo, perms))

    # -- the GitHub App: git over dumb HTTP --------------------------------

    def _installation_token_live(self, token: _InstallationToken) -> bool:
        return (
            not token.revoked
            and token.expires > self.now()
            and token.installation_id in self.installations
        )

    def _git(self, request: httpx.Request) -> httpx.Response:
        """Serve a bare repo's files to an installation token scoped to it."""
        match = _GIT_PATH.match(request.url.path)
        assert match is not None
        repo = self._repo_by_name(match["owner"], match["repo"])
        basic = self._basic(request)
        token = None if basic is None else self.installation_tokens.get(basic[1])
        if (
            repo is None
            or repo.path is None
            or token is None
            or not self._installation_token_live(token)
            or repo.id not in token.repo_ids
        ):
            return httpx.Response(
                401,
                text="Invalid username or token.",
                headers={"WWW-Authenticate": 'Basic realm="fake"'},
            )
        rest = unquote(match["rest"])
        root = repo.path.resolve()
        target = (root / rest).resolve()
        if ".." in Path(rest).parts or not target.is_relative_to(root):
            return httpx.Response(404, text="Not Found")
        if not target.is_file():
            return httpx.Response(404, text="Not Found")
        # ``info/refs`` answered as a plain file (whatever ``?service=``
        # asked) makes git fall back to the dumb protocol.
        return httpx.Response(
            200,
            content=target.read_bytes(),
            headers={
                "content-type": "text/plain"
                if rest == "info/refs"
                else "application/octet-stream"
            },
        )


# -- fixture repositories ---------------------------------------------------

GIT_ENV: dict[str, str] = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.test",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.test",
}
"""What every fixture ``git`` runs with: no user or system config, no prompt."""


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **GIT_ENV},
        capture_output=True,
        text=True,
        check=check,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def create_bare_repo(
    path: Path,
    *,
    default_branch: str = "main",
    files: dict[str, str] | None = None,
    message: str = "Initial commit",
) -> str:
    """Create a bare repository with one commit, ready for dumb HTTP.

    Returns
    -------
    str
        The commit's SHA.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _git("init", "--bare", "-q", f"--initial-branch={default_branch}", str(path))
    return push_commit(
        path,
        branch=default_branch,
        files=files or {"README.md": "# fixture\n"},
        message=message,
    )


def push_commit(
    path: Path,
    *,
    branch: str = "main",
    files: dict[str, str] | None = None,
    message: str = "Update",
) -> str:
    """Commit ``files`` (default: a line appended to ``CHANGES.txt``) onto a branch.

    The commit is made in a throw-away work tree and pushed into the bare
    repository at ``path``, then ``git update-server-info`` refreshes what
    dumb HTTP serves.

    Returns
    -------
    str
        The new commit's SHA.
    """
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "work"
        _git("init", "-q", f"--initial-branch={branch}", str(work))
        if _git(
            "--git-dir",
            str(path),
            "rev-parse",
            "-q",
            "--verify",
            f"refs/heads/{branch}",
            check=False,
        ):
            _git("fetch", "-q", str(path), branch, cwd=work)
            _git("reset", "-q", "--hard", "FETCH_HEAD", cwd=work)
        if files is None:
            changes = work / "CHANGES.txt"
            old = changes.read_text() if changes.exists() else ""
            files = {"CHANGES.txt": old + message + "\n"}
        for name, content in files.items():
            target = work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        _git("add", "-A", cwd=work)
        _git("commit", "-q", "-m", message, cwd=work)
        _git("push", "-q", str(path), f"HEAD:refs/heads/{branch}", cwd=work)
        sha = _git("rev-parse", "HEAD", cwd=work)
    _git("--git-dir", str(path), "update-server-info")
    return sha


# -- process mode -----------------------------------------------------------


def _make_handler(fake: FakeGitHub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            host = self.headers.get("host", "127.0.0.1")
            request = httpx.Request(
                self.command,
                f"http://{host}{self.path}",
                headers=dict(self.headers.items()),
                content=body,
            )
            try:
                response = fake.handle(request)
            except Exception as exc:  # noqa: BLE001 -- a forced failure
                self.send_error(502, str(exc))
                return
            self.send_response(response.status_code)
            for key, value in response.headers.items():
                if key.lower() not in ("content-length", "transfer-encoding"):
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        do_GET = do_POST = do_DELETE = _serve  # noqa: N815

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    return Handler


def main(argv: list[str] | None = None) -> None:
    """Serve the fake over HTTP (e2e, smoke and the dev loop)."""
    parser = argparse.ArgumentParser(description="A fake GitHub for WhyGraph sign-in")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18767)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret-file", required=True)
    parser.add_argument("--redirect-uri", required=True)
    args = parser.parse_args(argv)
    with open(args.client_secret_file, encoding="utf-8") as handle:
        secret = handle.read().strip()
    fake = FakeGitHub(args.client_id, secret, args.redirect_uri)
    server = ThreadingHTTPServer((args.host, args.port), _make_handler(fake))
    print(f"fake GitHub on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


__all__ = [
    "API_PREFIX",
    "APP_PERMISSIONS",
    "FakeGitHub",
    "FakeInstallation",
    "FakeRepo",
    "FakeUser",
    "create_bare_repo",
    "main",
    "push_commit",
]

if __name__ == "__main__":
    main()
