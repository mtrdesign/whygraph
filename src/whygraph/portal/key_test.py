"""The free "Test key" probe: does a stored LLM key still work (M2f-3).

Plan section 4.13, decision Q6. A key test makes **one** read-only call that
costs no tokens - each provider's documented key-checking or model-listing
endpoint (:data:`PROBES`) - with the stored key, and reports a single word:

- ``ok`` - a ``2xx``;
- ``rejected`` - ``401`` / ``403``: the provider does not accept the key;
- ``rate_limited`` - ``429``;
- ``unreachable`` - a connection error, a timeout or a ``5xx``;
- ``unexpected`` - anything else (a redirect, which is never followed, a
  ``404`` from a mistyped endpoint, a URL httpx cannot use).

(The GitHub token test, :mod:`whygraph.portal.key_routes`, adds
``no_repo_access``.)

The provider's body is read (up to :data:`BODY_CAP` bytes) and discarded:
nothing it says reaches the caller, so the probe cannot be used to read the
content of whatever a custom ``base_url`` points at. No redirect is followed,
and the whole call is bounded by :data:`TIMEOUT_SEC`. The key goes only into
the provider's own auth header and is never logged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

_log = logging.getLogger(__name__)

TIMEOUT_SEC = 10.0
"""The whole probe's timeout (connect, read, write, pool)."""

BODY_CAP = 64 * 1024
"""Most bytes of a provider's answer read before the connection is closed."""

RESULTS: tuple[str, ...] = (
    "ok",
    "rejected",
    "rate_limited",
    "unreachable",
    "no_repo_access",
    "unexpected",
)
"""Every result word a key test can report."""


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _anthropic(key: str) -> dict[str, str]:
    return {"x-api-key": key, "anthropic-version": "2023-06-01"}


@dataclass(frozen=True)
class Probe:
    """How one provider's key is tested.

    Attributes
    ----------
    base_url : str
        The provider's default API root (what its adapter calls).
    path : str
        Appended to the base URL - the default one, or a project's or org's
        ``[llm.<provider>].base_url`` when one is set.
    headers : callable
        ``key -> headers`` carrying the key the way the provider expects.
    """

    base_url: str
    path: str
    headers: Callable[[str], dict[str, str]]


PROBES: dict[str, Probe] = {
    # GET /v1/models - "List Models", needs x-api-key + anthropic-version:
    # https://docs.anthropic.com/en/api/models-list
    "anthropic": Probe("https://api.anthropic.com", "/v1/models", _anthropic),
    # GET /v1/models - "List models", bearer auth:
    # https://platform.openai.com/docs/api-reference/models/list
    # A custom base_url is an OpenAI-compatible root ending in /v1, so the
    # probe is <base_url>/models (what Chat's model listing calls).
    "openai": Probe("https://api.openai.com/v1", "/models", _bearer),
    # GET /models - "Lists Models", bearer auth:
    # https://api-docs.deepseek.com/api/list-models
    "deepseek": Probe("https://api.deepseek.com", "/models", _bearer),
    # GET /api/v1/key - "Get current API key", bearer auth:
    # https://openrouter.ai/docs/api-reference/api-keys/get-current-key
    # (its /models answers without a key, so it would pass any key).
    "openrouter": Probe("https://openrouter.ai/api/v1", "/key", _bearer),
}
"""The per-provider probe table (plan section 0.3 #17). Ollama has no key to
test and is not here."""


@dataclass(frozen=True)
class KeyTestResult:
    """What a key test found.

    Attributes
    ----------
    ok : bool
        ``result == "ok"``.
    result : str
        One of :data:`RESULTS`.
    """

    ok: bool
    result: str

    @classmethod
    def of(cls, result: str) -> KeyTestResult:
        """Build the result for the word ``result``."""
        return cls(ok=result == "ok", result=result)


def result_for_status(status: int) -> str:
    """Map a provider's HTTP status to a result word.

    Parameters
    ----------
    status : int
        The answer's status code.

    Returns
    -------
    str
        ``ok`` (2xx), ``rejected`` (401, 403), ``rate_limited`` (429),
        ``unreachable`` (5xx) or ``unexpected`` (anything else, a redirect
        included).
    """
    if 200 <= status < 300:
        return "ok"
    if status in (401, 403):
        return "rejected"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "unreachable"
    return "unexpected"


def probe_url(provider: str, base_url: str | None = None) -> str:
    """The URL a key test of ``provider`` calls.

    Parameters
    ----------
    provider : str
        A key of :data:`PROBES`.
    base_url : str, optional
        The configured ``[llm.<provider>].base_url``; ``None`` for the
        provider's default root.

    Returns
    -------
    str
        The base URL (without a trailing slash) plus the probe's path.

    Raises
    ------
    KeyError
        For a provider with no probe.
    """
    probe = PROBES[provider]
    root = (base_url or probe.base_url).rstrip("/")
    return root + probe.path


def probe_llm_key(
    provider: str,
    api_key: str,
    base_url: str | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> KeyTestResult:
    """Test one stored LLM key with a free, read-only provider call.

    Parameters
    ----------
    provider : str
        ``anthropic``, ``openai``, ``deepseek`` or ``openrouter``.
    api_key : str
        The stored key (decrypted in memory; never logged or returned).
    base_url : str, optional
        A custom endpoint for the provider, probed instead of the default.
    transport : httpx.BaseTransport, optional
        Replaces the network (tests pass ``httpx.MockTransport``); ``None``
        in a real run.

    Returns
    -------
    KeyTestResult
        The result word; the provider's body is never part of it.

    Raises
    ------
    KeyError
        For a provider with no probe (the routes refuse those first).
    """
    probe = PROBES[provider]
    url = probe_url(provider, base_url)
    headers = {**probe.headers(api_key), "Accept": "application/json"}
    client_args: dict[str, Any] = {
        "follow_redirects": False,
        "timeout": httpx.Timeout(TIMEOUT_SEC),
    }
    if transport is not None:
        client_args["transport"] = transport
    try:
        with httpx.Client(**client_args) as client:
            with client.stream("GET", url, headers=headers) as response:
                read = 0
                for chunk in response.iter_bytes():
                    read += len(chunk)
                    if read >= BODY_CAP:
                        break
                return KeyTestResult.of(result_for_status(response.status_code))
    except httpx.TransportError as exc:  # connect error, timeout, bad scheme
        _log.info("key test of %s: %s", provider, type(exc).__name__)
        return KeyTestResult.of("unreachable")
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        _log.info("key test of %s: %s", provider, type(exc).__name__)
        return KeyTestResult.of("unexpected")


__all__ = [
    "BODY_CAP",
    "PROBES",
    "RESULTS",
    "TIMEOUT_SEC",
    "KeyTestResult",
    "Probe",
    "probe_llm_key",
    "probe_url",
    "result_for_status",
]
