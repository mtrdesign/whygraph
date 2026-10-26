"""Model suggestions' listing (plan 36 sections 4.1.1 and 9.1 #1-#6).

:func:`whygraph.portal.model_list.list_llm_models` through an
``httpx.MockTransport``: each provider's URL and auth headers, the status
words, no redirect followed, the body cap, malformed bodies, the id-shaped
narrowing; and :func:`~whygraph.portal.model_list.fallback_listing`.
Nothing here reaches the network.
"""

from __future__ import annotations

import gzip
import json
from typing import Any

import httpx
import pytest

from whygraph.portal.model_list import (
    BODY_CAP,
    MAX_MODELS,
    ModelListing,
    fallback_listing,
    list_llm_models,
)
from whygraph.portal.prices import bundled_prices
from whygraph.services.llm.chat import FALLBACK_MODELS, ModelInfo, fallback_models


def _transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _answer(body: Any, status: int = 200) -> Any:
    return lambda request: httpx.Response(status, json=body)


# ---------------------------------------------------------------------------
# #1 / #2: URLs, headers, names
# ---------------------------------------------------------------------------


def test_anthropic_lists_one_page_with_its_display_names() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5"},
                    {"id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5"},
                ],
                "has_more": False,
            },
        )

    found = list_llm_models("anthropic", "sk-ant-1", transport=_transport(handler))
    assert found == ModelListing(
        result="ok",
        models=(
            ModelInfo("claude-opus-5-5", "Claude Opus 5.5"),
            ModelInfo("claude-haiku-4-5", "Claude Haiku 4.5"),
        ),
    )
    (request,) = seen
    assert (request.method, str(request.url)) == (
        "GET",
        "https://api.anthropic.com/v1/models?limit=1000",
    )
    assert request.headers["x-api-key"] == "sk-ant-1"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["accept"] == "application/json"
    assert "authorization" not in request.headers


@pytest.mark.parametrize(
    ("provider", "base_url", "url"),
    [
        ("openai", None, "https://api.openai.com/v1/models"),
        ("deepseek", None, "https://api.deepseek.com/models"),
        ("openrouter", None, "https://openrouter.ai/api/v1/models"),
        (
            "openai",
            "http://gateway.internal:8080/v1/",
            "http://gateway.internal:8080/v1/models",
        ),
    ],
)
def test_openai_compatible_listings_use_the_ids_as_names(
    provider: str, base_url: str | None, url: str
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"object": "list", "data": [{"id": "gpt-5"}, {"id": "o4"}]}
        )

    found = list_llm_models(provider, "k-1", base_url, transport=_transport(handler))
    assert found.result == "ok"
    assert found.models == (ModelInfo("gpt-5", "gpt-5"), ModelInfo("o4", "o4"))
    (request,) = seen
    assert str(request.url) == url
    assert request.headers["authorization"] == "Bearer k-1"


# ---------------------------------------------------------------------------
# #3: statuses, redirects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "result"),
    [(401, "rejected"), (403, "rejected"), (429, "rate_limited"), (503, "unreachable")],
)
def test_a_refusal_is_a_word_with_no_models(status: int, result: str) -> None:
    found = list_llm_models(
        "anthropic",
        "k",
        transport=_transport(_answer({"data": [{"id": "leaked"}]}, status)),
    )
    assert found == ModelListing(result=result, models=())


def test_a_redirect_is_not_followed() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    found = list_llm_models("openai", "k", transport=_transport(handler))
    assert found == ModelListing(result="unexpected", models=())
    assert seen == ["https://api.openai.com/v1/models"]


@pytest.mark.parametrize(
    ("exc", "result"),
    [
        (httpx.ConnectError("refused"), "unreachable"),
        (httpx.ReadTimeout("slow"), "unreachable"),
    ],
)
def test_network_failures_are_unreachable(exc: Exception, result: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    assert list_llm_models("openai", "k", transport=_transport(handler)).result == (
        result
    )


def test_an_unusable_base_url_is_not_a_crash() -> None:
    found = list_llm_models("openai", "k", "ftp://gateway.example/v1")
    assert found.result in ("unreachable", "unexpected")
    assert found.models == ()


# ---------------------------------------------------------------------------
# #4: body cap and malformed bodies
# ---------------------------------------------------------------------------


def test_a_body_over_the_cap_is_unexpected() -> None:
    entry = json.dumps({"id": "m"}) + ","
    padding = entry * (BODY_CAP // len(entry) + 10)
    body = ('{"data": [' + padding + '{"id": "last"}]}').encode()
    assert len(body) > BODY_CAP

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    assert list_llm_models("openrouter", "k", transport=_transport(handler)) == (
        ModelListing(result="unexpected", models=())
    )


def test_a_compressed_body_is_unexpected() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("accept-encoding", ""))
        body = gzip.compress(b'{"data": [{"id": "gpt-x"}]}')
        return httpx.Response(200, content=body, headers={"Content-Encoding": "gzip"})

    assert list_llm_models("openai", "k", transport=_transport(handler)) == (
        ModelListing(result="unexpected", models=())
    )
    assert seen == ["identity"]


@pytest.mark.parametrize(
    "content",
    [
        b"<html>internal secret page</html>",
        b'{"data": {"id": "gpt-5"}}',
        b'["gpt-5"]',
        b'{"models": [{"id": "gpt-5"}]}',
        b'{"data": []}',
        b"[" * 100_000,
    ],
)
def test_a_malformed_body_is_unexpected(content: bytes) -> None:
    found = list_llm_models(
        "openai",
        "k",
        transport=_transport(lambda r: httpx.Response(200, content=content)),
    )
    assert found == ModelListing(result="unexpected", models=())


# ---------------------------------------------------------------------------
# #5: the id-shaped narrowing
# ---------------------------------------------------------------------------


def test_only_id_shaped_ids_are_kept() -> None:
    body = {
        "data": [
            {"id": "<script>alert(1)</script>"},
            {"id": "a" * 201},
            {"id": 42},
            {"display_name": "no id"},
            "not-a-dict",
            {"id": "gpt-5\n"},
            {"id": "a" * 200},
            {"id": "anthropic/claude-opus-5-5:beta@v1+x"},
            {"id": "gpt-5", "display_name": "GPT 5"},
            {"id": "gpt-5", "display_name": "duplicate"},
            {"id": "long-name", "display_name": "x" * 101},
            {"id": "ctrl-name", "display_name": "bad\x00name"},
            {"id": "blank-name", "display_name": ""},
            {"id": "num-name", "display_name": 7},
        ]
    }
    found = list_llm_models("openrouter", "k", transport=_transport(_answer(body)))
    assert found.result == "ok"
    assert found.models == (
        ModelInfo("a" * 200, "a" * 200),
        ModelInfo(
            "anthropic/claude-opus-5-5:beta@v1+x",
            "anthropic/claude-opus-5-5:beta@v1+x",
        ),
        ModelInfo("gpt-5", "GPT 5"),
        ModelInfo("long-name", "long-name"),
        ModelInfo("ctrl-name", "ctrl-name"),
        ModelInfo("blank-name", "blank-name"),
        ModelInfo("num-name", "num-name"),
    )


def test_no_valid_id_is_unexpected() -> None:
    body = {"data": [{"id": "<script>"}, {"id": None}]}
    found = list_llm_models("openai", "k", transport=_transport(_answer(body)))
    assert found == ModelListing(result="unexpected", models=())


def test_a_long_listing_is_truncated() -> None:
    body = {"data": [{"id": f"m-{i}"} for i in range(MAX_MODELS + 500)]}
    found = list_llm_models("openrouter", "k", transport=_transport(_answer(body)))
    assert found.result == "ok"
    assert len(found.models) == MAX_MODELS
    assert found.models[0].id == "m-0"
    assert found.models[-1].id == f"m-{MAX_MODELS - 1}"


def test_the_key_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    caplog.set_level("DEBUG")
    list_llm_models("openai", "sk-very-secret-1234", transport=_transport(handler))
    assert "sk-very-secret-1234" not in caplog.text


# ---------------------------------------------------------------------------
# #6: the fallback list
# ---------------------------------------------------------------------------


def test_the_anthropic_fallback_adds_the_priced_models() -> None:
    listing = fallback_listing("anthropic")
    ids = [m.id for m in listing]
    assert len(ids) == len(set(ids))
    chat_ids = [model_id for model_id, _ in FALLBACK_MODELS["anthropic"]]
    priced = [m for p, m in bundled_prices().rows if p == "anthropic"]
    assert ids[: len(chat_ids)] == chat_ids  # Chat's list first, labels kept
    assert listing[: len(chat_ids)] == fallback_models("anthropic")
    assert set(ids) == set(chat_ids) | set(priced)
    assert "claude-opus-5-5" in ids


def test_the_openrouter_fallback_is_chats() -> None:
    assert fallback_listing("openrouter") == fallback_models("openrouter")


def test_an_unknown_provider_has_no_fallback() -> None:
    assert fallback_listing("nosuch") == ()
