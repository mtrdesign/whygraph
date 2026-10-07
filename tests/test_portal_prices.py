"""The price table (``portal/prices.py``) and the estimate's use of it."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from whygraph.core.config import Config
from whygraph.portal.estimate import ORG_PRICES_LABEL, TokenEstimate, render_estimate
from whygraph.portal.prices import (
    BUNDLED_AS_OF,
    NO_OVERRIDES,
    Price,
    bundled_prices,
    cost,
    has_custom_endpoint,
    price_for,
)

BUNDLED = f"bundled:{BUNDLED_AS_OF}"


def _usage(**counts: int | None) -> SimpleNamespace:
    base = {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
    }
    return SimpleNamespace(**{**base, **counts})


def test_bundled_table_shape() -> None:
    table = bundled_prices()
    assert table.currency == "USD"
    assert table.as_of == BUNDLED_AS_OF
    assert len(BUNDLED_AS_OF) == 10 and BUNDLED_AS_OF[4] == "-"
    assert table.rows
    for (provider, model), price in table.rows.items():
        assert provider and model
        assert price.input > 0 and price.output > 0, (provider, model)
        for rate in (price.cache_read, price.cache_write):
            assert rate is None or rate >= 0
        assert isinstance(price.input, Decimal)


def test_bundled_table_covers_the_defaults() -> None:
    rows = bundled_prices().rows
    # Every model the first-scan estimate priced before the table moved.
    for model in (
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-5-5",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ):
        assert ("anthropic", model) in rows
    # The OpenAI defaults (config + the chat picker's fallback list).
    assert ("openai", "gpt-4o") in rows
    assert ("openai", "gpt-4o-mini") in rows


def test_bundled_price_and_version() -> None:
    price, version = price_for(
        "anthropic", "claude-opus-4-7", NO_OVERRIDES, custom_endpoint=False
    )
    assert version == BUNDLED
    assert (price.input, price.output) == (Decimal(5), Decimal(25))


def test_unknown_or_missing_is_unpriced() -> None:
    assert price_for("ollama", "llama3", NO_OVERRIDES, custom_endpoint=False) is None
    assert price_for("anthropic", "nope", NO_OVERRIDES, custom_endpoint=False) is None
    assert price_for(None, "gpt-4o", NO_OVERRIDES, custom_endpoint=False) is None
    assert price_for("openai", None, NO_OVERRIDES, custom_endpoint=False) is None
    assert price_for("openai", "", NO_OVERRIDES, custom_endpoint=False) is None


def test_override_beats_bundled() -> None:
    mine = Price(input=Decimal("1.5"), output=Decimal("7"))
    overrides = {("anthropic", "claude-opus-4-7"): (mine, "2026-10-01T12:00:00+00:00")}
    price, version = price_for(
        "anthropic", "claude-opus-4-7", overrides, custom_endpoint=False
    )
    assert price is mine
    assert version == "org:2026-10-01T12:00:00+00:00"
    # Another model still comes from the bundled table.
    other = price_for("anthropic", "claude-haiku-4-5", overrides, custom_endpoint=False)
    assert other is not None and other[1] == BUNDLED


def test_override_prices_a_model_the_table_does_not() -> None:
    mine = Price(input=Decimal("0.1"), output=Decimal("0.2"))
    overrides = {("ollama", "llama3"): (mine, "t")}
    assert price_for("ollama", "llama3", overrides, custom_endpoint=False) == (
        mine,
        "org:t",
    )


def test_provider_prefix_is_stripped() -> None:
    price, version = price_for(
        "anthropic", "anthropic/claude-sonnet-4-6", NO_OVERRIDES, custom_endpoint=False
    )
    assert version == BUNDLED and price.input == Decimal(3)
    # The provider tag still matters: an Anthropic model under openai is unknown.
    assert (
        price_for("openai", "claude-sonnet-4-6", NO_OVERRIDES, custom_endpoint=False)
        is None
    )


def test_openrouter_vendor_ids_price_the_underlying_model() -> None:
    def priced(model: str) -> Price | None:
        hit = price_for("openrouter", model, NO_OVERRIDES, custom_endpoint=False)
        return hit[0] if hit else None

    rows = bundled_prices().rows
    assert priced("anthropic/claude-opus-4-7") == rows[("anthropic", "claude-opus-4-7")]
    # OpenRouter spells Anthropic versions with dots.
    assert (
        priced("anthropic/claude-sonnet-4.6")
        == rows[("anthropic", "claude-sonnet-4-6")]
    )
    assert priced("openai/gpt-4o-mini") == rows[("openai", "gpt-4o-mini")]
    assert priced("openrouter/auto") is None
    assert priced("claude-opus-4-7") is None  # no vendor: nothing to price by
    assert priced("anthropic/claude-opus-4-7:free") is None


def test_custom_endpoint_ignores_bundled_table() -> None:
    assert price_for("openai", "gpt-4o", NO_OVERRIDES, custom_endpoint=True) is None
    mine = Price(input=Decimal("0.5"), output=Decimal("1"))
    overrides = {("openai", "gpt-4o"): (mine, "t")}
    assert price_for("openai", "gpt-4o", overrides, custom_endpoint=True) == (
        mine,
        "org:t",
    )


def test_has_custom_endpoint() -> None:
    config = Config()
    for provider in ("anthropic", "openai", "deepseek", "openrouter", "ollama"):
        assert has_custom_endpoint(config, provider) is False
    assert has_custom_endpoint(config, None) is False
    assert has_custom_endpoint(config, "mystery") is False

    llm = config.llm
    custom = replace(
        config,
        llm=replace(
            llm,
            openai=replace(llm.openai, base_url="http://127.0.0.1:9999/v1"),
            ollama=replace(llm.ollama, host="http://gpu-box:11434"),
        ),
    )
    assert has_custom_endpoint(custom, "openai") is True
    assert has_custom_endpoint(custom, "ollama") is True
    assert has_custom_endpoint(custom, "anthropic") is False


def test_cost_formula_with_cache_subsets() -> None:
    price = Price(
        input=Decimal(5),
        output=Decimal(25),
        cache_read=Decimal("0.5"),
        cache_write=Decimal("6.25"),
    )
    rec = _usage(
        input_tokens=10_000,
        cache_read_tokens=6_000,
        cache_write_tokens=1_000,
        output_tokens=2_000,
    )
    # 3,000 uncached * 5 + 6,000 * 0.5 + 1,000 * 6.25 + 2,000 * 25, per million.
    want = (
        Decimal(3_000 * 5) + Decimal(3_000) + Decimal(6_250) + Decimal(50_000)
    ) / Decimal(1_000_000)
    assert cost(price, rec) == want == Decimal("0.074250")


def test_cost_null_cache_rate_uses_input_rate() -> None:
    price = Price(input=Decimal("2.5"), output=Decimal(10), cache_read=None)
    rec = _usage(input_tokens=1_000, cache_read_tokens=400, output_tokens=100)
    # All 1,000 input tokens at 2.5 (the cache read falls back to it).
    assert cost(price, rec) == Decimal("0.003500")
    # OpenAI-style: cache read discounted, no cache-write rate.
    gpt = Price(input=Decimal("2.5"), output=Decimal(10), cache_read=Decimal("1.25"))
    assert cost(gpt, rec) == Decimal("0.003000")


def test_cost_missing_counts_are_zero_and_rounding() -> None:
    price = Price(input=Decimal(3), output=Decimal(15))
    assert cost(price, _usage()) == Decimal("0.000000")
    assert cost(price, SimpleNamespace(output_tokens=1)) == Decimal("0.000015")
    # Half a millionth of a dollar rounds up to 6 dp.
    half = Price(input=Decimal("0.5"), output=Decimal(0))
    assert cost(half, _usage(input_tokens=1)) == Decimal("0.000001")
    # More cache than input never goes negative.
    assert cost(price, _usage(input_tokens=10, cache_read_tokens=20)) >= 0


@pytest.mark.parametrize(
    ("provider", "model", "priced"),
    [
        ("anthropic", "claude-opus-4-7", True),
        ("openai", "gpt-4o", True),
        ("openrouter", "anthropic/claude-haiku-4-5", True),
        ("ollama", "llama3", False),
    ],
)
def test_estimate_prices_every_priced_provider(
    provider: str, model: str, priced: bool
) -> None:
    tokens = TokenEstimate(
        commits=1,
        large_commits=0,
        synthesis_calls=0,
        input_tokens=1_000_000,
        output_tokens=100_000,
    )
    body = render_estimate(tokens, provider=provider, model=model)
    if not priced:
        assert body["cost"] is None
        return
    assert body["cost"]["prices_as_of"] == BUNDLED_AS_OF
    assert body["cost"]["currency"] == "USD"
    assert body["cost"]["usd"] > 0


def test_estimate_custom_endpoint_and_override() -> None:
    tokens = TokenEstimate(1, 0, 0, input_tokens=1_000_000, output_tokens=0)
    assert (
        render_estimate(
            tokens, provider="openai", model="gpt-4o", custom_endpoint=True
        )["cost"]
        is None
    )
    overrides = {("openai", "gpt-4o"): (Price(Decimal(1), Decimal(2)), "t")}
    body = render_estimate(
        tokens,
        provider="openai",
        model="gpt-4o",
        overrides=overrides,
        custom_endpoint=True,
    )
    assert body["cost"]["usd"] == 1.0
    assert body["cost"]["prices_as_of"] == ORG_PRICES_LABEL
