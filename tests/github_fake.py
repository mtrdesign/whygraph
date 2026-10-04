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

Never imported by ``src/`` and never copied into the image.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode

import httpx

API_PREFIX = "/api/v3"
CODE_TTL_SEC = 600.0


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


@dataclass
class _Token:
    user_id: int
    scope: str
    revoked: bool = False


@dataclass
class _Forced:
    status: int | None
    exc: BaseException | None
    times: int | None


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


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
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.now = now
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
            ``"authorize"``, ``"access_token"``, ``"user"`` or ``"revoke"``.
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
    ) -> str:
        """Mint an authorization code directly (skips the authorize step)."""
        with self._lock:
            return self._new_code(
                self.users[login],
                challenge,
                "S256" if challenge else None,
                scope,
                self.redirect_uri,
            )

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

    def _new_code(
        self,
        user: FakeUser,
        challenge: str | None,
        method: str | None,
        scope: str,
        redirect_uri: str,
    ) -> str:
        code = secrets.token_hex(10)
        self.codes[code] = _Code(
            user.id, challenge, method, scope, redirect_uri, self.now() + CODE_TTL_SEC
        )
        return code

    def _user_by_id(self, user_id: int) -> FakeUser | None:
        return next((u for u in self.users.values() if u.id == user_id), None)

    # -- web: authorize -----------------------------------------------------

    def _authorize(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params.items())
        if params.get("client_id") != self.client_id:
            return httpx.Response(404, text="Unknown OAuth application")
        redirect_uri = params.get("redirect_uri", self.redirect_uri)
        if redirect_uri != self.redirect_uri:
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
            scope,
            redirect_uri,
        )
        query = {"code": code}
        if "state" in params:
            query["state"] = params["state"]
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
        if (
            p.get("client_id") != self.client_id
            or p.get("client_secret") != self.client_secret
        ):
            return self._error(
                "incorrect_client_credentials",
                "The client_id and/or client_secret passed are incorrect.",
            )
        entry = self.codes.get(p.get("code", ""))
        if entry is None or entry.used or entry.expires <= self.now():
            if entry is not None:
                entry.used = True
            return self._error(
                "bad_verification_code", "The code passed is incorrect or expired."
            )
        entry.used = True  # single use, whatever happens next
        if p.get("redirect_uri") != self.redirect_uri:
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

    def _user(self, request: httpx.Request) -> httpx.Response:
        token = self._bearer(request)
        entry = self.tokens.get(token or "")
        user = (
            None if entry is None or entry.revoked else self._user_by_id(entry.user_id)
        )
        if entry is None or user is None:
            return httpx.Response(401, json={"message": "Bad credentials"})
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
        if client_id != self.client_id or not self._basic_ok(request):
            return httpx.Response(401, json={"message": "Bad credentials"})
        try:
            data = json.loads(request.content or b"{}")
        except json.JSONDecodeError:
            data = {}
        entry = self.tokens.get(str(data.get("access_token", "")))
        if entry is None:
            return httpx.Response(404, json={"message": "Not Found"})
        entry.revoked = True
        return httpx.Response(204)

    def _basic_ok(self, request: httpx.Request) -> bool:
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            raw = base64.b64decode(value).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        return raw == f"{self.client_id}:{self.client_secret}"


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


__all__ = ["FakeGitHub", "FakeUser", "API_PREFIX", "main"]

if __name__ == "__main__":
    main()
