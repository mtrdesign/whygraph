"""Normalize provider usage blocks to one token meaning.

Providers count prompt tokens differently: Anthropic's ``input_tokens``
excludes cache reads and cache writes, the OpenAI family's
``prompt_tokens`` includes them. Every adapter (completion and chat) runs
its provider's usage block through one of the functions here, so a
:class:`~whygraph.services.llm.types.CompletionResponse` or a
:class:`~whygraph.services.llm.chat.TurnDone` always means:

* ``input_tokens`` - every prompt token, cache reads and writes included;
* ``cache_read_tokens`` / ``cache_write_tokens`` - subsets of it;
* ``output_tokens`` - every completion token, reasoning included;
* ``reasoning_tokens`` - a subset of it;
* ``cost_usd`` - the provider's own figure, only where one is reported
  (OpenRouter).

Each function returns a dict whose keys are exactly those field names, so
an adapter can splat it into either value object.

Fields are read with :func:`_field`, which accepts SDK models (including
the pydantic ``model_extra`` that carries fields the SDK does not type,
such as OpenRouter's ``cost``), plain dicts (an extra nested object is a
dict) and duck-typed test doubles alike.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

UsageFields = dict[str, Any]
"""The normalized usage keys: ``input_tokens``, ``output_tokens``,
``cache_read_tokens``, ``cache_write_tokens``, ``reasoning_tokens``,
``cost_usd``."""

ANTHROPIC_USAGE_KEYS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
"""The raw Anthropic usage fields :func:`anthropic_usage_fields` reads."""


def _field(obj: Any, name: str) -> Any:
    """Read ``name`` from an SDK model, its ``model_extra``, or a dict."""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    value = getattr(obj, name, None)
    if value is None:
        extra = getattr(obj, "model_extra", None)
        if isinstance(extra, Mapping):
            value = extra.get(name)
    return value


def _int(value: Any) -> int | None:
    """``value`` when it is a non-negative int (never a bool), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _money(value: Any) -> float | None:
    """``value`` as a float when it is a finite, non-negative number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _sum(*values: int | None) -> int | None:
    """Sum the reported values; ``None`` only when none was reported."""
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def anthropic_raw_usage(usage: Any) -> dict[str, int | None]:
    """Extract the raw Anthropic usage fields from one ``usage`` object.

    Parameters
    ----------
    usage : Any
        A ``Usage`` / ``MessageDeltaUsage`` from the ``anthropic`` SDK (or
        a duck-typed stand-in). ``None`` yields all-``None`` fields.

    Returns
    -------
    dict[str, int or None]
        One entry per :data:`ANTHROPIC_USAGE_KEYS` name.
    """
    return {key: _int(_field(usage, key)) for key in ANTHROPIC_USAGE_KEYS}


def anthropic_usage_fields(raw: Mapping[str, int | None]) -> UsageFields:
    """Normalize raw Anthropic usage to the ledger meaning.

    Anthropic's ``input_tokens`` counts only the uncached part of the
    prompt, so the normalized ``input_tokens`` adds the cache reads and
    cache writes back.

    Parameters
    ----------
    raw : Mapping[str, int or None]
        The raw fields, as :func:`anthropic_raw_usage` returns them (the
        streaming adapter merges several of those first).

    Returns
    -------
    UsageFields
        The normalized usage; Anthropic reports no reasoning split and no
        cost, so those are ``None``.
    """
    cache_read = raw.get("cache_read_input_tokens")
    cache_write = raw.get("cache_creation_input_tokens")
    return {
        "input_tokens": _sum(raw.get("input_tokens"), cache_read, cache_write),
        "output_tokens": raw.get("output_tokens"),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": None,
        "cost_usd": None,
    }


def openai_usage_fields(usage: Any, *, provider: str) -> UsageFields:
    """Normalize an OpenAI-compatible ``usage`` block to the ledger meaning.

    ``prompt_tokens`` already includes cached tokens, so it is taken as is.
    Every ``*_details`` object may be missing on compatible endpoints.

    Parameters
    ----------
    usage : Any
        A ``CompletionUsage`` from the ``openai`` SDK (or a stand-in);
        ``None`` yields all-``None`` fields.
    provider : str
        The adapter's provider tag. ``"deepseek"`` falls back to its
        top-level ``prompt_cache_hit_tokens`` for cache reads;
        ``"openrouter"`` adds the cache-write count and the reported cost.

    Returns
    -------
    UsageFields
        The normalized usage.

    Notes
    -----
    OpenRouter returns usage on every response (the old ``usage:
    {include: true}`` request field is deprecated and ignored, so it is not
    sent). Its ``cost`` is what OpenRouter charged; on a BYOK request that
    is only OpenRouter's fee and the provider's own charge is in
    ``cost_details.upstream_inference_cost``, so the two are added. The
    upstream figure is skipped when the response says ``is_byok: false``,
    so a non-BYOK response that echoes it cannot be counted twice.
    """
    if usage is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "reasoning_tokens": None,
            "cost_usd": None,
        }
    prompt_details = _field(usage, "prompt_tokens_details")
    completion_details = _field(usage, "completion_tokens_details")

    cache_read = _int(_field(prompt_details, "cached_tokens"))
    if cache_read is None and provider == "deepseek":
        cache_read = _int(_field(usage, "prompt_cache_hit_tokens"))

    cache_write: int | None = None
    cost: float | None = None
    if provider == "openrouter":
        cache_write = _int(_field(prompt_details, "cache_write_tokens"))
        charged = _money(_field(usage, "cost"))
        upstream = None
        if _field(usage, "is_byok") is not False:
            upstream = _money(
                _field(_field(usage, "cost_details"), "upstream_inference_cost")
            )
        if charged is not None or upstream is not None:
            cost = (charged or 0.0) + (upstream or 0.0)

    return {
        "input_tokens": _int(_field(usage, "prompt_tokens")),
        "output_tokens": _int(_field(usage, "completion_tokens")),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": _int(_field(completion_details, "reasoning_tokens")),
        "cost_usd": cost,
    }


__all__ = [
    "ANTHROPIC_USAGE_KEYS",
    "UsageFields",
    "anthropic_raw_usage",
    "anthropic_usage_fields",
    "openai_usage_fields",
]
