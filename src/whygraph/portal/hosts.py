"""The portal's public address and the hosts it answers on.

A production portal lives at one *base URL* (``https://whygraph.example.com``)
and serves each organization on a subdomain of it
(``https://acme.whygraph.example.com``). This module parses and validates the
base URL, classifies a ``Host`` header against it, validates a post-sign-in
redirect target, and offers a never-fatal DNS self-check. Everything is pure
except :func:`self_check`.
"""

from __future__ import annotations

import ipaddress
import re
import secrets
import socket
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from urllib.parse import urlsplit

from .orgs import NEVER_ORG_HOSTS, is_valid_org_slug

BASE_URL_ENV = "WHYGRAPH_BASE_URL"
"""The production portal's public base URL (scheme, host, optional port)."""

TRUSTED_PROXIES_ENV = "WHYGRAPH_TRUSTED_PROXIES"
"""Comma-separated IPs / CIDRs whose ``X-Forwarded-For`` uvicorn trusts."""

_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class BaseUrl:
    """The portal's public base URL.

    Attributes
    ----------
    scheme : str
        ``"http"`` (only for ``*.localhost``) or ``"https"``.
    host : str
        Lower-case host name, two or more labels.
    port : int or None
        Explicit port; ``None`` when absent or the scheme's default.
    """

    scheme: str
    host: str
    port: int | None = None

    @classmethod
    def parse(cls, raw: str) -> BaseUrl:
        """Parse and validate a base URL.

        Parameters
        ----------
        raw : str
            For example ``https://whygraph.example.com`` or
            ``http://whygraph.localhost:8765``.

        Returns
        -------
        BaseUrl
            The parsed value; a default port is dropped.

        Raises
        ------
        ValueError
            Naming the broken rule: a scheme other than http / https, http
            outside ``*.localhost``, a path, query, fragment or userinfo, an
            IP literal, a single-label host, or a label over 63 characters.
        """
        raw = (raw or "").strip()
        try:
            parts = urlsplit(raw)
            port = parts.port
        except ValueError as exc:
            raise ValueError(f"base URL {raw!r} is not a valid URL: {exc}") from exc
        scheme = parts.scheme.lower()
        if scheme not in _DEFAULT_PORTS:
            raise ValueError("base URL scheme must be http or https")
        if "@" in parts.netloc:
            raise ValueError("base URL must not carry userinfo")
        if parts.path not in ("", "/"):
            raise ValueError("base URL must not have a path")
        if parts.query or parts.fragment or "?" in raw or "#" in raw:
            raise ValueError("base URL must not have a query or fragment")
        host = (parts.hostname or "").lower()
        if not host:
            raise ValueError("base URL needs a host")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("base URL must be a DNS name, not an IP address")
        labels = host.split(".")
        if len(labels) < 2:
            raise ValueError(
                "base URL host needs at least two labels (a cookie Domain on a "
                "single label is refused by browsers)"
            )
        for label in labels:
            if len(label) > 63:
                raise ValueError("base URL host has a label over 63 characters")
            if _LABEL_RE.fullmatch(label) is None:
                raise ValueError(f"base URL host has an invalid label {label!r}")
        if scheme == "http" and not host.endswith(".localhost"):
            raise ValueError("http base URLs are only allowed for *.localhost")
        if port == _DEFAULT_PORTS[scheme]:
            port = None
        return cls(scheme=scheme, host=host, port=port)

    @property
    def netloc(self) -> str:
        """``host[:port]``, the form a browser sends in ``Host``."""
        return self.host if self.port is None else f"{self.host}:{self.port}"

    @property
    def origin(self) -> str:
        """``scheme://host[:port]``, the form a browser sends in ``Origin``."""
        return f"{self.scheme}://{self.netloc}"

    def org_netloc(self, slug: str) -> str:
        """Return the ``Host`` value of an organization's subdomain."""
        return f"{slug}.{self.netloc}"

    def org_origin(self, slug: str) -> str:
        """Return the origin of an organization's subdomain."""
        return f"{self.scheme}://{self.org_netloc(slug)}"


@dataclass(frozen=True)
class HostKind:
    """What a ``Host`` header names.

    Attributes
    ----------
    slug : str or None
        The org slug, or ``None`` for the base host.
    """

    slug: str | None = None

    @property
    def is_base(self) -> bool:
        """Whether this is the base host (no org)."""
        return self.slug is None


BASE = HostKind()
"""The base host itself."""


def classify(host_header: str | None, base: BaseUrl) -> HostKind | None:
    """Say which host a ``Host`` header names.

    Parameters
    ----------
    host_header : str or None
        The raw header value (any case).
    base : BaseUrl
        The portal's base URL.

    Returns
    -------
    HostKind or None
        :data:`BASE`, ``HostKind(slug)`` for exactly one valid org label
        under the base, or ``None`` (the guard answers 421): another port, a
        missing port, ``www``-style never-org hosts, two labels deep and
        lookalike suffixes. A valid label that names no org is a 404 later.
    """
    host = (host_header or "").strip().lower()
    if not host:
        return None
    if host == base.netloc:
        return BASE
    suffix = "." + base.netloc
    if not host.endswith(suffix):
        return None
    label = host[: -len(suffix)]
    if "." in label or not is_valid_org_slug(label) or label in NEVER_ORG_HOSTS:
        return None
    return HostKind(label)


def safe_redirect(next_url: str | None, base: BaseUrl) -> str | None:
    """Return ``next_url`` if it is safe to redirect to after sign-in.

    Parameters
    ----------
    next_url : str or None
        The caller-supplied target.
    base : BaseUrl
        The portal's base URL.

    Returns
    -------
    str or None
        ``next_url`` unchanged when its scheme and port are the base URL's
        and its host is the base host or exactly one valid org label under
        it; else ``None`` (the caller falls back to ``<base>/orgs``).
    """
    if not next_url or any(c in next_url for c in "\\\r\n\t ") or len(next_url) > 2048:
        return None
    try:
        parts = urlsplit(next_url)
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError:
        return None
    if parts.scheme != base.scheme or "@" in parts.netloc:
        return None
    if parts.path and not parts.path.startswith("/"):
        return None
    if classify(parts.netloc, base) is None:
        return None
    return next_url


def parse_trusted_proxies(raw: str | None) -> str:
    """Validate ``WHYGRAPH_TRUSTED_PROXIES`` into uvicorn's ``forwarded_allow_ips``.

    Parameters
    ----------
    raw : str or None
        Comma-separated IP addresses and CIDR networks; empty or ``None``
        trusts no proxy.

    Returns
    -------
    str
        The entries normalized (``10.0.0.1/8`` becomes ``10.0.0.0/8``) and
        joined by commas; ``""`` for none. Normalizing matters: uvicorn
        silently treats an entry it cannot parse as a literal that never
        matches.

    Raises
    ------
    ValueError
        Naming the first entry that is neither an IP address nor a network
        (``*`` included: trusting every peer is never what a deployment means).
    """
    entries = []
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                entries.append(str(ipaddress.ip_network(entry, strict=False)))
            else:
                entries.append(str(ipaddress.ip_address(entry)))
        except ValueError:
            raise ValueError(
                f"{TRUSTED_PROXIES_ENV} entry {entry!r} is not an IP address or "
                "CIDR network"
            ) from None
    return ",".join(entries)


def _resolves(name: str) -> bool:
    try:
        socket.getaddrinfo(name, None)
    except OSError:
        return False
    return True


def self_check(base: BaseUrl) -> list[str]:
    """Check that DNS points the base host and its subdomains at this machine.

    Never raises and never blocks startup for more than about two seconds.
    Skipped (returns ``[]``) for ``*.localhost``, which browsers resolve to
    loopback by themselves.

    Parameters
    ----------
    base : BaseUrl
        The portal's base URL.

    Returns
    -------
    list of str
        One message per missing record; empty when healthy.
    """
    if base.host.endswith(".localhost"):
        return []
    probe = f"{secrets.token_hex(6)}.{base.host}"
    try:
        pool = ThreadPoolExecutor(max_workers=2)
        futures = {
            base.host: pool.submit(_resolves, base.host),
            probe: pool.submit(_resolves, probe),
        }
        pool.shutdown(wait=False)
        results: dict[str, bool] = {}
        for name, future in futures.items():
            try:
                results[name] = future.result(timeout=2)
            except FutureTimeout:
                results[name] = False
    except Exception:  # noqa: BLE001 - a self-check never fails startup
        return []
    problems = []
    if not results[base.host]:
        problems.append(f"{base.host} does not resolve: add a DNS record for it")
    if not results[probe]:
        problems.append(
            f"*.{base.host} does not resolve (tried {probe}): add a wildcard "
            f"DNS record, e.g. *.{base.host}"
        )
    return problems
