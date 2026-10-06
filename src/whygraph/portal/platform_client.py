"""The connected local portal's client for a WhyGraph platform (M2e).

:class:`PlatformHttp` is the one HTTP client a linked project uses to talk to
a hosted platform: the ``meta`` probe and the code exchange on the platform
(base) origin, and the ``/api/v1`` data routes on the org (``api``) origin
with the connection token as a bearer. It is modelled on
:class:`whygraph.portal.github_auth.GitHubHttp`: no redirects, short
timeouts, an injectable transport, and an origin allowlist of exactly the
two origins.

Every reply is read as a stream and refused past :data:`MAX_RESPONSE_BYTES`,
parsed as JSON and validated with the :mod:`whygraph.api_v1` models. Strings
are capped *before* validation by :func:`cap_strings` (titles 1 KiB, bodies
64 KiB): truncating the parsed JSON keeps the shared response models free of
client-only policy.

The token never appears in an exception message, a log line or a return
value other than the exchange reply: every message is passed through
:func:`~whygraph.services.git.credentials.redact_tokens` and a local
``wgc_`` redaction.

Notes
-----
``*.localhost`` is not resolved to loopback by every platform's resolver and
httpx has no resolver hook, so :class:`LocalhostTransport` rewrites the URL
host to ``127.0.0.1`` and keeps the original ``Host`` header (the dev loop
and e2e). ``http`` platforms are accepted only with
``WHYGRAPH_DEV_PLATFORM_HTTP=1`` and only for loopback hosts.
"""

from __future__ import annotations

import json
import logging
import os
import re
from importlib import metadata
from ipaddress import ip_address
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from whygraph.api_v1 import (
    API_VERSION,
    ErrorOut,
    EvidenceIn,
    EvidenceReplyOut,
    MetaOut,
    RationaleIn,
    RationaleOut,
    StatusOut,
    TokenReply,
)
from whygraph.services.git.credentials import redact_tokens

from .orgs import is_valid_org_slug

logger = logging.getLogger(__name__)

DEV_PLATFORM_HTTP_ENV = "WHYGRAPH_DEV_PLATFORM_HTTP"
"""Set to ``1`` to allow an ``http`` platform on a loopback host (dev, e2e)."""

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
"""The most a platform reply may carry (2 MiB)."""

MAX_TITLE_CHARS = 1024
"""Cap of a title-like string in a reply (1 KiB)."""

MAX_BODY_CHARS = 64 * 1024
"""Cap of any other string in a reply (64 KiB)."""

_TITLE_KEYS = frozenset({"title", "subject", "name", "commit_titles"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_WGC = re.compile(r"wgc_[A-Za-z0-9_-]{8,}")

REMOVED_REASONS = frozenset({"project_deleted", "org_deleted"})
"""Revocation reasons that mean the platform project is gone."""


class PlatformError(Exception):
    """Base of every platform client failure; the message is token-free."""


class PlatformUnreachable(PlatformError):
    """No usable answer: connection error, timeout, ``5xx``, redirect, or a
    reply that is oversized, not JSON or not the expected shape."""


class PlatformRefused(PlatformError):
    """The platform answered ``4xx`` with its error envelope.

    Attributes
    ----------
    status : int
        The HTTP status.
    code : str
        The envelope's machine-readable code.
    reason : str or None
        The revocation reason, when a token was refused.
    retry_after : int or None
        Seconds to wait, on a ``429`` / ``503``.
    """

    def __init__(
        self,
        status: int,
        code: str,
        reason: str | None = None,
        retry_after: int | None = None,
        message: str | None = None,
    ) -> None:
        super().__init__(_redact(message or f"the platform refused ({status} {code})"))
        self.status = status
        self.code = code
        self.reason = reason
        self.retry_after = retry_after


class UpdateRequired(PlatformError):
    """The platform's ``api_version`` is not ours, or we are below ``min_client``."""


def _redact(text: str) -> str:
    """Strip GitHub tokens and ``wgc_`` connection tokens from ``text``."""
    return _WGC.sub("wgc_***", redact_tokens(text))


def _is_loopback(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def dev_platform_http_enabled() -> bool:
    """Return whether ``WHYGRAPH_DEV_PLATFORM_HTTP`` is ``1``."""
    return os.environ.get(DEV_PLATFORM_HTTP_ENV) == "1"


def parse_platform_url(raw: str, *, allow_http: bool) -> str:
    """Validate a platform URL and return its normalized origin.

    Parameters
    ----------
    raw : str
        The user-supplied platform URL.
    allow_http : bool
        Whether ``http`` is acceptable (the dev switch); even then only for
        a loopback IP, ``localhost`` or ``*.localhost``.

    Returns
    -------
    str
        ``scheme://host[:port]`` - lower-cased, default port dropped, no
        path, query, fragment or credentials.

    Raises
    ------
    ValueError
        Not ``https`` (or a refused ``http``), no host, or credentials, a
        path other than ``/``, a query or a fragment present.
    """
    value = (raw or "").strip()
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"not a valid platform URL: {exc}") from None
    if parts.scheme not in ("http", "https"):
        raise ValueError("the platform URL must start with https://")
    if not host:
        raise ValueError("the platform URL has no host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("the platform URL must not contain credentials")
    if parts.path not in ("", "/"):
        raise ValueError("the platform URL must not contain a path")
    if parts.query or "?" in value:
        raise ValueError("the platform URL must not contain a query")
    if parts.fragment or "#" in value:
        raise ValueError("the platform URL must not contain a fragment")
    if parts.scheme == "http" and not (allow_http and _is_loopback(host)):
        raise ValueError(
            "the platform URL must use https (http is only allowed for a "
            "loopback host in the development setup)"
        )
    shown = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS[parts.scheme]:
        shown = f"{shown}:{port}"
    return f"{parts.scheme}://{shown}"


def api_origin_for(platform_origin: str, org_slug: str) -> str:
    """The org host of a platform: ``<scheme>://<org>.<platform host>[:port]``.

    Parameters
    ----------
    platform_origin : str
        A normalized origin from :func:`parse_platform_url`.
    org_slug : str
        The org the token belongs to.

    Returns
    -------
    str
        The origin of the platform's ``/api/v1``.

    Raises
    ------
    ValueError
        ``org_slug`` is not a valid org slug.
    """
    if not isinstance(org_slug, str) or not is_valid_org_slug(org_slug):
        raise ValueError("the platform named an invalid org")
    parts = urlsplit(platform_origin)
    return f"{parts.scheme}://{org_slug}.{parts.netloc}"


class LocalhostTransport(httpx.BaseTransport):
    """Maps ``localhost`` / ``*.localhost`` to ``127.0.0.1`` for the inner transport.

    The ``Host`` header keeps the original ``host[:port]`` so a platform
    that routes by host still sees the org subdomain. Other hosts pass
    through unchanged.

    Parameters
    ----------
    inner : httpx.BaseTransport, optional
        The wrapped transport (default: a plain ``httpx.HTTPTransport``).
    """

    def __init__(self, inner: httpx.BaseTransport | None = None) -> None:
        self._inner = inner if inner is not None else httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Rewrite a localhost request's URL host, then send it."""
        host = (request.url.host or "").lower().rstrip(".")
        if host == "localhost" or host.endswith(".localhost"):
            port = request.url.port
            request.headers["Host"] = f"{host}:{port}" if port else host
            request.url = request.url.copy_with(host="127.0.0.1")
        return self._inner.handle_request(request)

    def close(self) -> None:
        """Close the inner transport."""
        self._inner.close()


def cap_strings(value: Any, key: str | None = None) -> Any:
    """Truncate every string in parsed JSON to its cap.

    Parameters
    ----------
    value : Any
        Parsed JSON.
    key : str, optional
        The enclosing dict key; title-like keys (``title``, ``subject``,
        ``name``, ``commit_titles``) get :data:`MAX_TITLE_CHARS`, everything
        else :data:`MAX_BODY_CHARS`.

    Returns
    -------
    Any
        The same structure with long strings cut.
    """
    if isinstance(value, str):
        limit = MAX_TITLE_CHARS if key in _TITLE_KEYS else MAX_BODY_CHARS
        return value[:limit]
    if isinstance(value, list):
        return [cap_strings(v, key) for v in value]
    if isinstance(value, dict):
        return {k: cap_strings(v, k) for k, v in value.items()}
    return value


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return (
        scheme,
        (parts.hostname or "").lower(),
        parts.port or _DEFAULT_PORTS.get(scheme),
    )


def _semver(text: str) -> tuple[int, ...]:
    nums = re.match(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", text.strip().lstrip("v"))
    if not nums:
        return (0,)
    return tuple(int(n) for n in nums.groups(default="0"))


def _our_version() -> str:
    try:
        return metadata.version("whygraph")
    except metadata.PackageNotFoundError:
        return "0"


class PlatformHttp:
    """An origin-guarded HTTP client for one platform.

    Parameters
    ----------
    platform_origin : str
        The platform (base) origin, as :func:`parse_platform_url` returns.
    api_origin : str, optional
        The org origin; usually set later by :meth:`bind` once the exchange
        names the org.
    token : str, optional
        The connection token; sent only to ``api_origin``.
    transport : httpx.BaseTransport, optional
        Replaces the network (tests pass ``httpx.MockTransport``); always
        wrapped in a :class:`LocalhostTransport`.
    timeout, connect_timeout : float
        Seconds (defaults 10 and 5).

    Attributes
    ----------
    platform_origin, api_origin : str or None
    last_project : StatusOut or None
        The ``project`` of the latest reply that carried one, so the caller
        can refresh the link's status without a second request.
    """

    def __init__(
        self,
        *,
        platform_origin: str,
        api_origin: str | None = None,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
        connect_timeout: float = 5.0,
    ) -> None:
        allow_http = dev_platform_http_enabled()
        self.platform_origin = parse_platform_url(
            platform_origin, allow_http=allow_http
        )
        self.api_origin: str | None = None
        self._token = token
        self.last_project: StatusOut | None = None
        if api_origin is not None:
            self.api_origin = parse_platform_url(api_origin, allow_http=allow_http)
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            transport=LocalhostTransport(transport),
            follow_redirects=False,
            headers={"X-WhyGraph-Client": "1"},
        )

    def bind(self, org: str, token: str) -> None:
        """Aim the client at an org's origin with its token.

        Parameters
        ----------
        org : str
            The org slug from the exchange reply.
        token : str
            The connection token.

        Raises
        ------
        ValueError
            ``org`` is not a valid org slug.
        """
        self.api_origin = api_origin_for(self.platform_origin, org)
        self._token = token

    # -- plumbing -----------------------------------------------------------

    def _call(
        self,
        method: str,
        base: str | None,
        path: str,
        *,
        bearer: bool,
        body: BaseModel | dict | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Send one request and return the parsed, capped JSON body."""
        if base is None or _origin(base) not in {
            _origin(self.platform_origin),
            *([_origin(self.api_origin)] if self.api_origin else []),
        }:
            raise PlatformUnreachable("refusing a request to an unexpected origin")
        headers: dict[str, str] = {}
        if bearer and self._token and _origin(base) == _origin(self.api_origin or ""):
            headers["Authorization"] = f"Bearer {self._token}"
        kwargs: dict[str, Any] = {"headers": headers, "params": params}
        if body is not None:
            kwargs["json"] = (
                body.model_dump(mode="json") if isinstance(body, BaseModel) else body
            )
        try:
            with self._client.stream(method, base + path, **kwargs) as response:
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                    raise PlatformUnreachable("the platform's reply is too large")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise PlatformUnreachable("the platform's reply is too large")
                    chunks.append(chunk)
                status = response.status_code
        except httpx.HTTPError as exc:
            logger.warning("platform request failed: %s", type(exc).__name__)
            raise PlatformUnreachable("the platform could not be reached") from None
        raw = b"".join(chunks)
        if status >= 500 or 300 <= status < 400:
            raise PlatformUnreachable(f"the platform is unavailable ({status})")
        try:
            data = cap_strings(json.loads(raw)) if raw else None
        except ValueError:
            raise PlatformUnreachable("the platform's reply is not JSON") from None
        if status >= 400:
            try:
                err = ErrorOut.model_validate(data)
            except ValidationError:
                raise PlatformUnreachable(
                    f"the platform answered {status} without an error envelope"
                ) from None
            raise PlatformRefused(
                status, err.code, err.reason, err.retry_after, err.error
            )
        return data

    def _v1(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._call(method, self.api_origin, path, bearer=True, **kwargs)

    @staticmethod
    def _model(model: type[BaseModel], data: Any) -> Any:
        try:
            return model.model_validate(data)
        except ValidationError:
            raise PlatformUnreachable(
                "the platform's reply is not the expected shape"
            ) from None

    def _note_project(self, data: Any) -> None:
        if isinstance(data, dict) and isinstance(data.get("project"), dict):
            try:
                self.last_project = StatusOut.model_validate(data["project"])
            except ValidationError:
                pass

    @staticmethod
    def _slug(slug: str) -> str:
        return f"/api/v1/projects/{quote(slug, safe='')}"

    # -- platform origin ----------------------------------------------------

    def meta(self) -> MetaOut:
        """``GET /api/v1/meta`` (no credentials).

        Returns
        -------
        MetaOut
            What the platform speaks.

        Raises
        ------
        UpdateRequired
            Its ``api_version`` is not :data:`~whygraph.api_v1.API_VERSION`,
            or this WhyGraph is older than its ``min_client``.
        PlatformUnreachable, PlatformRefused
            As for every call.
        """
        out: MetaOut = self._model(
            MetaOut,
            self._call("GET", self.platform_origin, "/api/v1/meta", bearer=False),
        )
        if out.api_version != API_VERSION:
            raise UpdateRequired(
                f"the platform speaks API version {out.api_version}, "
                f"this WhyGraph speaks {API_VERSION}"
            )
        if _semver(_our_version()) < _semver(out.min_client):
            raise UpdateRequired(
                f"the platform needs WhyGraph {out.min_client} or newer"
            )
        return out

    def exchange(self, code: str, code_verifier: str, redirect_uri: str) -> TokenReply:
        """``POST /api/connect/token``: trade the code for a token (no credentials).

        Parameters
        ----------
        code, code_verifier, redirect_uri : str
            The authorization code, the PKCE verifier and the exact
            ``redirect_uri`` the consent used.

        Returns
        -------
        TokenReply
            The token, the org and the project. Its ``org`` is validated as
            an org slug.

        Raises
        ------
        PlatformRefused, PlatformUnreachable
            As for every call; also for a reply naming an invalid org.
        """
        data = self._call(
            "POST",
            self.platform_origin,
            "/api/connect/token",
            bearer=False,
            body={
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
            },
        )
        reply: TokenReply = self._model(TokenReply, data)
        if not is_valid_org_slug(reply.org):
            raise PlatformUnreachable("the platform named an invalid org")
        self.last_project = reply.project
        return reply

    # -- org origin, bearer -------------------------------------------------

    def status(self, slug: str) -> StatusOut:
        """``GET /api/v1/projects/{slug}``."""
        data = self._v1("GET", self._slug(slug))
        out: StatusOut = self._model(StatusOut, data)
        self.last_project = out
        return out

    def evidence(self, slug: str, body: EvidenceIn) -> EvidenceReplyOut:
        """``POST .../evidence``: evidence for the pushed hunks of a target."""
        data = self._v1("POST", f"{self._slug(slug)}/evidence", body=body)
        self._note_project(data)
        return self._model(EvidenceReplyOut, data)

    def rationale(self, slug: str, body: RationaleIn) -> RationaleOut:
        """``POST .../rationale``: a rationale card."""
        data = self._v1("POST", f"{self._slug(slug)}/rationale", body=body)
        self._note_project(data)
        return self._model(RationaleOut, data)

    def history(
        self,
        slug: str,
        path: str,
        limit: int = 20,
        include_renames: bool = True,
    ) -> dict[str, Any]:
        """``GET .../history``: the area history of a path (parsed JSON)."""
        data = self._v1(
            "GET",
            f"{self._slug(slug)}/history",
            params={
                "path": path,
                "limit": limit,
                "include_renames": "true" if include_renames else "false",
            },
        )
        return self._object(data)

    def commit(self, slug: str, sha: str) -> dict[str, Any]:
        """``GET .../commits/{sha}`` (parsed JSON)."""
        return self._resource(slug, f"commits/{quote(sha, safe='')}")

    def pr(self, slug: str, number: int) -> dict[str, Any]:
        """``GET .../prs/{number}`` (parsed JSON)."""
        return self._resource(slug, f"prs/{int(number)}")

    def issue(self, slug: str, number: int) -> dict[str, Any]:
        """``GET .../issues/{number}`` (parsed JSON)."""
        return self._resource(slug, f"issues/{int(number)}")

    def overview(self, slug: str) -> dict[str, Any]:
        """``GET .../overview`` (parsed JSON)."""
        return self._resource(slug, "overview")

    def revoke(self, slug: str) -> None:
        """``DELETE .../token``: revoke this connection (the caller catches errors)."""
        self._v1("DELETE", f"{self._slug(slug)}/token")

    def _resource(self, slug: str, tail: str) -> dict[str, Any]:
        return self._object(self._v1("GET", f"{self._slug(slug)}/{tail}"))

    def _object(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise PlatformUnreachable("the platform's reply is not the expected shape")
        self._note_project(data)
        return data

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()


def link_status_for(
    outcome: BaseException | StatusOut | int | None,
) -> tuple[str, str | None]:
    """Map a platform answer to a link's ``(status, reason)``.

    Implements the table of plan section 4.11.

    Parameters
    ----------
    outcome : BaseException or StatusOut or int or None
        A client exception, a successful ``StatusOut`` (``access_lost``
        decides ``ok`` vs ``access_lost``), an HTTP status, or ``None`` for
        a plain success.

    Returns
    -------
    tuple[str, str or None]
        ``status`` is ``ok``, ``access_lost``, ``removed``, ``revoked``,
        ``unreachable`` or ``update_required``; ``reason`` is the
        revocation reason or error code when there is one.

    Notes
    -----
    Beyond the table: any other refusal (``404 not_found`` for an unknown
    commit, ``429``, ``409 no_llm_key``, a bad request) is an answer about
    one request, not about the link, so the link stays ``ok`` with the
    code as its reason. A deleted project always arrives as
    ``token_revoked`` (the token is bound to the project id).
    """
    if outcome is None:
        return "ok", None
    if isinstance(outcome, StatusOut):
        return ("access_lost" if outcome.access_lost else "ok"), None
    if isinstance(outcome, int):
        if 200 <= outcome < 300:
            return "ok", None
        if outcome == 401:
            return "revoked", None
        return "unreachable", None
    if isinstance(outcome, UpdateRequired):
        return "update_required", None
    if isinstance(outcome, PlatformRefused):
        if outcome.code == "token_revoked":
            if outcome.reason in REMOVED_REASONS:
                return "removed", outcome.reason
            return "revoked", outcome.reason
        if outcome.code == "invalid_token":
            return "revoked", "invalid_token"
        return "ok", outcome.code
    return "unreachable", None


__all__ = [
    "DEV_PLATFORM_HTTP_ENV",
    "MAX_BODY_CHARS",
    "MAX_RESPONSE_BYTES",
    "MAX_TITLE_CHARS",
    "REMOVED_REASONS",
    "LocalhostTransport",
    "PlatformError",
    "PlatformHttp",
    "PlatformRefused",
    "PlatformUnreachable",
    "UpdateRequired",
    "api_origin_for",
    "cap_strings",
    "dev_platform_http_enabled",
    "link_status_for",
    "parse_platform_url",
]
