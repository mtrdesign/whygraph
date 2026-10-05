"""A fake WhyGraph platform for the connected portal's client (M2e step 5).

:class:`FakePlatform` holds an in-memory store and its
:meth:`FakePlatform.handle` is a pure ``httpx.Request -> httpx.Response``
function, which pytest plugs into ``httpx.MockTransport`` (the shape of
``tests/github_fake.py``'s ``FakeGitHub.handle``). It serves what
:class:`whygraph.portal.platform_client.PlatformHttp` calls:

* base host: ``GET /api/v1/meta``, ``POST /api/connect/token`` (single-use
  codes, S256 PKCE, byte-equal ``redirect_uri``);
* org host (``<org>.<base host>``, bearer only): ``GET /api/v1/projects/{slug}``,
  ``POST .../evidence`` and ``.../rationale``, ``GET .../history``,
  ``.../commits/{sha}``, ``.../prs/{n}``, ``.../issues/{n}``, ``.../overview``
  and ``DELETE .../token``.

Replies are **table-driven**: :attr:`FakePlatform.responses` maps a route name
(``meta``, ``token``, ``status``, ``evidence``, ``rationale``, ``history``,
``commit``, ``pr``, ``issue``, ``overview``, ``revoke``) to a
``(status, body)`` pair that replaces the hand-written default, so step 4's
contract recorder can swap in recorded fixtures (:meth:`FakePlatform.from_fixtures`).
Failure modes are set on :attr:`FakePlatform.failure` (``unreachable``,
``oversized``, ``not_json``, ``redirect``, ``timeout``) or per token with
:meth:`FakePlatform.revoke_token`.

Never imported by ``src/``.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx

from whygraph.portal.platform_client import MAX_RESPONSE_BYTES

PLATFORM_ORIGIN = "https://wg.example.com"
ORG = "acme"
SLUG = "demo"
TOKEN = "wgc_" + "A1b2C3d4" * 5 + "xyz"
SHA = "a" * 40

FAILURES = ("unreachable", "oversized", "not_json", "redirect", "timeout")


def s256(verifier: str) -> str:
    """The S256 PKCE challenge of ``verifier``."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def status_body(slug: str = SLUG, **over: Any) -> dict:
    """A ``StatusOut``-shaped dict."""
    body = {
        "slug": slug,
        "name": "Demo",
        "github_full_name": "acme/demo",
        "clone_url": "https://github.com/acme/demo.git",
        "default_branch": "main",
        "last_scanned_head": SHA,
        "last_scan_at": "2026-10-05T10:00:00Z",
        "access_lost": False,
        "role": "member",
    }
    body.update(over)
    return body


def error_body(code: str, error: str = "refused", **extra: Any) -> dict:
    """An ``ErrorOut``-shaped dict."""
    return {"error": error, "code": code, **extra}


class FakePlatform:
    """An in-memory platform; see the module docstring.

    Parameters
    ----------
    platform_origin : str
        The base origin it answers for (its org host is derived).
    api_version, min_client : int, str
        What ``meta`` reports.

    Attributes
    ----------
    requests : list[httpx.Request]
        Every request seen, in order.
    responses : dict[str, tuple[int, Any]]
        Route name -> ``(status, JSON body)`` overrides.
    failure : str or None
        One of :data:`FAILURES`; applies to every request.
    projects : dict[str, dict]
        Slug -> ``StatusOut`` dict.
    tokens : dict[str, dict]
        Token -> ``{"org", "slug", "revoked": reason or None}``.
    codes : dict[str, dict]
        Pending authorization codes.
    """

    def __init__(
        self,
        platform_origin: str = PLATFORM_ORIGIN,
        *,
        api_version: int = 1,
        min_client: str = "0.0.0",
    ) -> None:
        self.platform_origin = platform_origin
        self.host = httpx.URL(platform_origin).netloc.decode()
        self.api_version = api_version
        self.min_client = min_client
        self.requests: list[httpx.Request] = []
        self.responses: dict[str, tuple[int, Any]] = {}
        self.failure: str | None = None
        self.projects: dict[str, dict] = {SLUG: status_body()}
        self.tokens: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}

    # -- configuration ------------------------------------------------------

    @classmethod
    def from_fixtures(cls, directory: Path) -> FakePlatform:
        """Replay recorded ``tests/fixtures/api_v1/*.json`` (step 4's recorder).

        Each file is ``{"status": int, "body": <JSON or null>}`` and its stem
        is the route name, so the recording becomes the reply of that route
        (:attr:`responses`). The recorded ``status`` answer also replaces
        :attr:`projects`'s entry for :data:`SLUG`, so the ``project`` a reply
        nests and the one ``status`` returns are the same row.

        Parameters
        ----------
        directory : Path
            The folder holding the recordings.

        Returns
        -------
        FakePlatform
            A fake answering every recorded route from its file.
        """
        fake = cls()
        for path in sorted(Path(directory).glob("*.json")):
            recorded = json.loads(path.read_text())
            fake.responses[path.stem] = (int(recorded["status"]), recorded.get("body"))
        status = fake.responses.get("status")
        if status is not None and isinstance(status[1], dict):
            fake.projects[SLUG] = status[1]
        return fake

    def add_code(
        self,
        code: str,
        *,
        verifier: str,
        redirect_uri: str,
        org: str = ORG,
        slug: str = SLUG,
        token: str = TOKEN,
    ) -> None:
        """Register a valid authorization code (single use)."""
        self.codes[code] = {
            "challenge": s256(verifier),
            "redirect_uri": redirect_uri,
            "org": org,
            "slug": slug,
            "token": token,
        }

    def add_token(self, token: str = TOKEN, *, org: str = ORG, slug: str = SLUG):
        """Register a live connection token."""
        self.tokens[token] = {"org": org, "slug": slug, "revoked": None}

    def revoke_token(self, token: str = TOKEN, reason: str = "user_revoked") -> None:
        """Mark a token revoked; the next v1 call answers ``401 token_revoked``."""
        self.tokens[token]["revoked"] = reason

    # -- the handler --------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one request (a pure function of the store and the request)."""
        self.requests.append(request)
        if self.failure == "timeout":
            raise httpx.ReadTimeout("fake timeout", request=request)
        if self.failure == "unreachable":
            return httpx.Response(503, json=error_body("busy"))
        if self.failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://evil.example/"})
        if self.failure == "oversized":
            return httpx.Response(
                200, content=b"[" + b"0," * MAX_RESPONSE_BYTES + b"0]"
            )
        if self.failure == "not_json":
            return httpx.Response(200, content=b"<html>not json</html>")
        host = request.url.host
        port = request.url.port
        netloc = request.headers.get("host") or (f"{host}:{port}" if port else host)
        path = request.url.path
        if netloc == self.host:
            return self._base(request, path)
        if netloc.endswith("." + self.host):
            return self._org(request, netloc[: -len(self.host) - 1], path)
        return httpx.Response(404, json=error_body("not_found"))

    def _out(self, route: str, default_status: int, default_body: Any):
        status, body = self.responses.get(route, (default_status, default_body))
        return httpx.Response(status, json=body)

    def _base(self, request: httpx.Request, path: str) -> httpx.Response:
        if request.method == "GET" and path == "/api/v1/meta":
            return self._out(
                "meta",
                200,
                {
                    "api_version": self.api_version,
                    "min_client": self.min_client,
                    "capabilities": ["evidence", "rationale", "history", "resources"],
                },
            )
        if request.method == "POST" and path == "/api/connect/token":
            return self._token(request)
        return httpx.Response(404, json=error_body("not_found"))

    def _token(self, request: httpx.Request) -> httpx.Response:
        if "token" in self.responses:
            return self._out("token", 200, {})
        body = json.loads(request.content or b"{}")
        pending = self.codes.pop(str(body.get("code")), None)  # popped first
        if pending is None:
            return httpx.Response(400, json=error_body("invalid_grant"))
        if (
            s256(str(body.get("code_verifier"))) != pending["challenge"]
            or body.get("redirect_uri") != pending["redirect_uri"]
        ):
            return httpx.Response(400, json=error_body("invalid_grant"))
        self.add_token(pending["token"], org=pending["org"], slug=pending["slug"])
        return self._out(
            "token",
            200,
            {
                "token": pending["token"],
                "org": pending["org"],
                "project": self.projects[pending["slug"]],
                "api_version": self.api_version,
            },
        )

    def _org(self, request: httpx.Request, org: str, path: str) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        entry = self.tokens.get(token)
        if entry is None or entry["org"] != org:
            return httpx.Response(401, json=error_body("invalid_token"))
        if entry["revoked"]:
            return httpx.Response(
                401, json=error_body("token_revoked", reason=entry["revoked"])
            )
        prefix = f"/api/v1/projects/{entry['slug']}"
        if path != prefix and not path.startswith(prefix + "/"):
            return httpx.Response(404, json=error_body("not_found"))
        tail = path[len(prefix) :].strip("/")
        project = self.projects[entry["slug"]]
        method = request.method
        if method == "GET" and tail == "":
            return self._out("status", 200, project)
        if method == "DELETE" and tail == "token":
            entry["revoked"] = "removed_locally"
            return self._out("revoke", 200, {"ok": True})
        if method == "POST" and tail == "evidence":
            return self._out(
                "evidence",
                200,
                {"evidence": [], "unknown_shas": [], "project": project},
            )
        if method == "POST" and tail == "rationale":
            return self._out("rationale", 200, _rationale_body(project))
        if method == "GET" and tail == "history":
            return self._out("history", 200, {"commits": [], "project": project})
        if method == "GET" and tail == "overview":
            return self._out("overview", 200, {"name": "Demo", "project": project})
        for route, noun in (("commit", "commits"), ("pr", "prs"), ("issue", "issues")):
            if method == "GET" and tail.startswith(noun + "/"):
                return self._out(
                    route, 200, {"id": tail.split("/", 1)[1], "project": project}
                )
        return httpx.Response(404, json=error_body("not_found"))


def _rationale_body(project: dict) -> dict:
    return {
        "target": {"path": "a.py", "line_start": 1, "line_end": 2},
        "purpose": "p",
        "why": "w",
        "constraints": [],
        "tradeoffs": [],
        "risks": [],
        "model": "m",
        "provider": "x",
        "cached_at": "2026-10-05T10:00:00Z",
        "evidence_count": {"commits": 0, "prs": 0, "issues": 0},
        "project": project,
    }
