"""Model suggestions for Settings: list a provider's model ids with a stored key.

The model fields in Settings stay free text; this module only supplies
suggestions. It is a sibling of :mod:`whygraph.portal.key_test` and makes the
same kind of call - one read-only ``GET`` with the **stored** key - but,
unlike a key test, it reads the answer back, so it narrows what an answer can
return. That narrowing is this module's invariant, because a provider's
``base_url`` may be a custom endpoint (any host an org owner or a project
admin chose) and a listing must not become a way to read what it serves:

- no redirect is followed, and the whole call is bounded by
  :data:`TIMEOUT_SEC`;
- at most :data:`BODY_CAP` bytes are read; a longer answer is ``unexpected``;
- only ``data[*].id`` strings that fully match :data:`_ID` (id-shaped: up to
  200 of ``[A-Za-z0-9._:/@+-]``) are kept, at most :data:`MAX_MODELS`;
- a ``display_name`` is kept only when it is a string of 1 to 100 printable
  characters, else the id stands in for it;
- nothing else of the body - no other field, no error text - reaches the
  caller.

The key goes only into the provider's own auth header and is never logged.
A listing is not ledgered as usage. When the live list is unavailable the
routes answer :func:`fallback_listing` instead.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from whygraph.services.llm.chat import ModelInfo, fallback_models

from .key_test import PROBES, result_for_status
from .prices import bundled_prices

_log = logging.getLogger(__name__)

TIMEOUT_SEC = 10.0
"""The whole listing's timeout (connect, read, write, pool)."""

BODY_CAP = 4 * 1024 * 1024
"""Most bytes of a listing read; a longer answer is ``unexpected``.

OpenRouter's listing measured about 0.8 MB (plan 36 section 2.2)."""

MAX_MODELS = 2_000
"""Most model ids a listing returns (the rest are dropped)."""

_ID = re.compile(r"[A-Za-z0-9._:/@+-]{1,200}")
"""An id-shaped string; used with ``fullmatch`` only."""

_DISPLAY_NAME_MAX = 100
"""Longest ``display_name`` kept; a longer one is replaced by the id."""

LIST_PATHS: dict[str, str] = {
    # GET /v1/models, one page of up to 1000 (plan 36 section 2.3):
    # https://docs.anthropic.com/en/api/models-list
    "anthropic": "/v1/models?limit=1000",
    "openai": "/models",
    "deepseek": "/models",
    # PROBES uses /key here (a key test); a listing needs /models.
    "openrouter": "/models",
}
"""Appended to ``PROBES[provider].base_url`` or the configured ``base_url``.

The providers that can be listed; Ollama is not one."""


@dataclass(frozen=True)
class ModelListing:
    """What a live listing found.

    Attributes
    ----------
    result : str
        ``ok``, ``rejected``, ``rate_limited``, ``unreachable`` or
        ``unexpected`` (the key-test words, :mod:`whygraph.portal.key_test`).
    models : tuple of ModelInfo
        The narrowed models, in the provider's order; empty unless
        ``result == "ok"``.
    """

    result: str
    models: tuple[ModelInfo, ...]


def list_url(provider: str, base_url: str | None = None) -> str:
    """The URL a listing of ``provider`` calls.

    Parameters
    ----------
    provider : str
        A key of :data:`LIST_PATHS`.
    base_url : str, optional
        The configured ``[llm.<provider>].base_url``; ``None`` for the
        provider's default root.

    Returns
    -------
    str
        The base URL (without a trailing slash) plus the listing path.

    Raises
    ------
    KeyError
        For a provider that cannot be listed.
    """
    path = LIST_PATHS[provider]
    root = (base_url or PROBES[provider].base_url).rstrip("/")
    return root + path


def _display_name(entry: dict, model_id: str) -> str:
    """The entry's ``display_name`` when it is short and printable, else the id."""
    name = entry.get("display_name")
    if (
        isinstance(name, str)
        and 0 < len(name) <= _DISPLAY_NAME_MAX
        and name.isprintable()
    ):
        return name
    return model_id


def _narrow(body: Any) -> tuple[ModelInfo, ...]:
    """The id-shaped models of a parsed listing (empty when there are none)."""
    if not isinstance(body, dict):
        return ()
    data = body.get("data")
    if not isinstance(data, list):
        return ()
    seen: set[str] = set()
    models: list[ModelInfo] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not _ID.fullmatch(model_id):
            continue
        if model_id in seen:
            continue
        seen.add(model_id)
        models.append(
            ModelInfo(id=model_id, display_name=_display_name(entry, model_id))
        )
        if len(models) >= MAX_MODELS:
            break
    return tuple(models)


def _failed(result: str) -> ModelListing:
    return ModelListing(result=result, models=())


def list_llm_models(
    provider: str,
    api_key: str,
    base_url: str | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ModelListing:
    """List ``provider``'s model ids with one stored key, narrowed.

    Parameters
    ----------
    provider : str
        ``anthropic``, ``openai``, ``deepseek`` or ``openrouter``.
    api_key : str
        The stored key (decrypted in memory; never logged or returned).
    base_url : str, optional
        A custom endpoint for the provider, listed instead of the default.
    transport : httpx.BaseTransport, optional
        Replaces the network (tests pass ``httpx.MockTransport``); ``None``
        in a real run.

    Returns
    -------
    ModelListing
        ``ok`` with at least one id-shaped model, or a failure word with no
        models: a non-``2xx`` maps as a key test does (a redirect, never
        followed, is ``unexpected``), and an answer over :data:`BODY_CAP`,
        one that is not JSON, one without a ``data`` list or one with no
        valid id is ``unexpected``. The body is asked for uncompressed and
        an encoded answer is ``unexpected``, so :data:`BODY_CAP` counts the
        bytes actually received (a compressed body could expand past it in
        one chunk).

    Raises
    ------
    KeyError
        For a provider that cannot be listed (the routes refuse those first).
    """
    url = list_url(provider, base_url)
    headers = {
        **PROBES[provider].headers(api_key),
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }
    client_args: dict[str, Any] = {
        "follow_redirects": False,
        "timeout": httpx.Timeout(TIMEOUT_SEC),
    }
    if transport is not None:
        client_args["transport"] = transport
    try:
        with httpx.Client(**client_args) as client:
            with client.stream("GET", url, headers=headers) as response:
                result = result_for_status(response.status_code)
                if result != "ok":
                    return _failed(result)
                encoding = response.headers.get("content-encoding", "identity")
                if encoding.strip().lower() not in ("", "identity"):
                    return _failed("unexpected")
                raw = bytearray()
                # No content coding (refused above), so these are the bytes received.
                for chunk in response.iter_bytes():
                    raw += chunk
                    if len(raw) > BODY_CAP:
                        return _failed("unexpected")
    except httpx.TransportError as exc:  # connect error, timeout, bad scheme
        _log.info("model listing of %s: %s", provider, type(exc).__name__)
        return _failed("unreachable")
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        _log.info("model listing of %s: %s", provider, type(exc).__name__)
        return _failed("unexpected")
    try:
        body = json.loads(bytes(raw))
    except (ValueError, RecursionError):  # not JSON, or nested too deep
        return _failed("unexpected")
    models = _narrow(body)
    if not models:
        return _failed("unexpected")
    return ModelListing(result="ok", models=models)


def fallback_listing(provider: str) -> tuple[ModelInfo, ...]:
    """The built-in suggestions for ``provider`` when no live list is available.

    Parameters
    ----------
    provider : str
        A provider tag.

    Returns
    -------
    tuple of ModelInfo
        Chat's :func:`~whygraph.services.llm.chat.fallback_models`, then the
        bundled price table's models for the provider (display name = id),
        deduplicated by id in that order; empty for an unknown provider.
    """
    models = list(fallback_models(provider))
    seen = {m.id for m in models}
    for row_provider, model_id in bundled_prices().rows:
        if row_provider == provider and model_id not in seen:
            seen.add(model_id)
            models.append(ModelInfo(id=model_id, display_name=model_id))
    return tuple(models)


__all__ = [
    "BODY_CAP",
    "LIST_PATHS",
    "MAX_MODELS",
    "TIMEOUT_SEC",
    "ModelListing",
    "fallback_listing",
    "list_llm_models",
    "list_url",
]
