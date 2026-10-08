"""The price table: what a call costs, per million tokens, in USD.

Two sources, in order (plan section 4.4):

1. **Org overrides** - per-org rows on top of the bundled table (the
   ``price_overrides`` table), passed to :func:`price_for` as a
   :data:`PriceOverrides` mapping. Their version is ``"org:<updated_at>"``.
2. **The bundled table** - ``prices.json``, shipped in the wheel and
   re-checked every release (``cd-deploy-whygraph.yml`` refuses a release
   more than 120 days after its ``as_of``). Its version is
   ``"bundled:<as_of>"``.

A model in neither is unpriced (tokens only). A provider whose endpoint is
overridden in the effective config (``base_url`` / ``host``) is **never**
priced from the bundled table - the endpoint may be another service - so
only an org override prices it (:func:`has_custom_endpoint`).

Token meaning (the ledger's, plan section 4.2): ``input_tokens`` counts every
prompt token, cache reads and writes included; ``cache_read_tokens`` and
``cache_write_tokens`` are subsets of it. :func:`cost` prices the uncached
remainder at the input rate and each cache subset at its own rate.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from importlib import resources
from typing import Any

from whygraph.core.config import Config

from .config_layers import ENDPOINT_KEYS

PER_MILLION = Decimal(1_000_000)
"""Rates are per million tokens."""

_SIX_DP = Decimal("0.000001")


@dataclass(frozen=True)
class Price:
    """One model's rates, USD per million tokens.

    Attributes
    ----------
    input, output : Decimal
        Uncached prompt tokens and completion tokens (reasoning included).
    cache_read, cache_write : Decimal or None
        Cache-hit and cache-write rates; ``None`` means the input rate
        (a provider that does not charge cache writes separately, or an
        override that leaves them unset).
    """

    input: Decimal
    output: Decimal
    cache_read: Decimal | None = None
    cache_write: Decimal | None = None


PriceOverrides = Mapping[tuple[str, str], tuple[Price, str]]
"""An org's overrides: ``(provider, model) -> (Price, updated_at)``.

Keyed by the exact provider tag and model id the override was saved
under (no prefix stripping - an override names what the org runs).
``updated_at`` is the row's timestamp and becomes the price version
``"org:<updated_at>"``. Loaded from ``price_overrides`` by the caller.
"""

NO_OVERRIDES: PriceOverrides = {}
"""The empty override map (local callers that have no org overrides yet)."""


@dataclass(frozen=True)
class BundledPrices:
    """The parsed ``prices.json``.

    Attributes
    ----------
    as_of : str
        When the table was last checked against the providers' pages
        (``YYYY-MM-DD``).
    currency : str
        Always ``"USD"``.
    rows : Mapping[tuple[str, str], Price]
        ``(provider, model) -> Price``.
    """

    as_of: str
    currency: str
    rows: Mapping[tuple[str, str], Price]


def _rate(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


@cache
def bundled_prices() -> BundledPrices:
    """Load the bundled price table (read once, then cached).

    Returns
    -------
    BundledPrices
        The table shipped in the wheel.
    """
    raw = json.loads(
        resources.files("whygraph.portal")
        .joinpath("prices.json")
        .read_text(encoding="utf-8")
    )
    rows = {
        (row["provider"], row["model"]): Price(
            input=Decimal(str(row["input"])),
            output=Decimal(str(row["output"])),
            cache_read=_rate(row.get("cache_read")),
            cache_write=_rate(row.get("cache_write")),
        )
        for row in raw["rows"]
    }
    return BundledPrices(as_of=raw["as_of"], currency=raw["currency"], rows=rows)


BUNDLED_AS_OF: str = bundled_prices().as_of
"""The bundled table's ``as_of`` date."""


def _bundled_candidates(provider: str, model: str) -> list[tuple[str, str]]:
    """The bundled keys a ``(provider, model)`` pair may be priced under."""
    if provider == "openrouter":
        # An OpenRouter id is ``vendor/model``: price the underlying model.
        # Its Anthropic ids spell versions with dots (claude-sonnet-4.6).
        vendor, sep, name = model.partition("/")
        if not sep:
            return []
        keys = [(vendor, name)]
        if vendor == "anthropic" and "." in name:
            keys.append((vendor, name.replace(".", "-")))
        return keys
    # A provider-prefixed name (``anthropic/claude-...``) is looked up
    # without its prefix, as the first-scan estimate always did.
    return [(provider, model), (provider, model.rsplit("/", 1)[-1])]


def price_for(
    provider: str | None,
    model: str | None,
    overrides: PriceOverrides,
    *,
    custom_endpoint: bool,
) -> tuple[Price, str] | None:
    """Resolve the price of one model, with the version it came from.

    Parameters
    ----------
    provider, model : str or None
        The provider tag and the model id as configured (or served).
    overrides : PriceOverrides
        The org's overrides (:data:`NO_OVERRIDES` when there are none).
    custom_endpoint : bool
        Whether the provider's endpoint is overridden in the effective
        config (:func:`has_custom_endpoint`); if so the bundled table is
        skipped and only an override applies.

    Returns
    -------
    tuple[Price, str] or None
        The price and its version (``"org:<updated_at>"`` or
        ``"bundled:<as_of>"``), or ``None`` when the model is unpriced.
    """
    if not provider or not model:
        return None
    hit = overrides.get((provider, model))
    if hit is not None:
        price, updated_at = hit
        return price, f"org:{updated_at}"
    if custom_endpoint:
        return None
    table = bundled_prices()
    for key in _bundled_candidates(provider, model):
        price = table.rows.get(key)
        if price is not None:
            return price, f"bundled:{table.as_of}"
    return None


def has_custom_endpoint(config: Config, provider: str | None) -> bool:
    """Whether ``config`` points ``provider`` at a non-default endpoint.

    The typed counterpart of ``config_layers.endpoint_of`` for an
    effective :class:`~whygraph.core.config.Config`.

    Parameters
    ----------
    config : Config
        The effective (merged) config.
    provider : str or None
        A provider tag.

    Returns
    -------
    bool
        ``True`` when the provider's section sets ``base_url`` or ``host``.
    """
    section = config.llm.section(provider) if provider else None
    if section is None:
        return False
    return any(getattr(section, key, None) is not None for key in ENDPOINT_KEYS)


def _count(rec: object, name: str) -> int:
    return int(getattr(rec, name, None) or 0)


def cost(price: Price, rec: object) -> Decimal:
    """What a call cost at ``price``, in USD, rounded to 6 decimal places.

    ``(input - cache_read - cache_write) * input rate + cache_read *
    cache-read rate + cache_write * cache-write rate + output * output
    rate``, per million tokens. A ``None`` cache rate uses the input rate.

    Parameters
    ----------
    price : Price
        The rates to apply.
    rec : object
        Anything with ``input_tokens``, ``output_tokens``,
        ``cache_read_tokens`` and ``cache_write_tokens`` attributes (a
        usage record, a token estimate); a missing or ``None`` count is 0.

    Returns
    -------
    Decimal
        The cost in USD. The uncached remainder never goes below zero,
        even if a provider reports more cache tokens than input tokens.
    """
    inp = _count(rec, "input_tokens")
    out = _count(rec, "output_tokens")
    read = _count(rec, "cache_read_tokens")
    write = _count(rec, "cache_write_tokens")
    uncached = max(inp - read - write, 0)
    read_rate = price.input if price.cache_read is None else price.cache_read
    write_rate = price.input if price.cache_write is None else price.cache_write
    total = (
        uncached * price.input
        + read * read_rate
        + write * write_rate
        + out * price.output
    ) / PER_MILLION
    return total.quantize(_SIX_DP, rounding=ROUND_HALF_UP)


__all__ = [
    "BUNDLED_AS_OF",
    "NO_OVERRIDES",
    "PER_MILLION",
    "BundledPrices",
    "Price",
    "PriceOverrides",
    "bundled_prices",
    "cost",
    "has_custom_endpoint",
    "price_for",
]
