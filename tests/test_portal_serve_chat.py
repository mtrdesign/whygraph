"""Integration tests for the project-scoped ``/api/projects/<slug>/chat/*`` router.

Ported from the 1.x ``test_serve_chat.py`` (``whygraph serve`` is gone): the
same router runs under the portal (plan section 4.5.1), so each test drives
:func:`whygraph.portal.app.create_portal_app` with one initialized project
through :class:`~test_portal_serve_api.ScopedClient`, which rewrites
``/api/chat/...`` to ``/api/projects/demo/chat/...``. The project has a
migrated WhyGraph DB and deliberately no CodeGraph index.

Config overrides go through the project's bound context
(:func:`~test_portal_serve_api.use_project_config`), which keeps the
project's own DB paths, so every test is hermetic.

The harness is monkeypatched **on the ``serve.chat`` module namespace**,
which is the house convention and the reason ``run_turn`` is imported at
module level there. That lets the streaming lifecycle - frame sequence,
which rows land, disconnect behaviour - be tested without a provider.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import Iterator

import pytest
from sqlmodel import select

from test_portal_app import env  # noqa: F401 -- `env` is a fixture
from test_portal_serve_api import ScopedClient, portal_with_project, use_project_config
from whygraph.chat.harness import (
    BudgetStop,
    ContextTrimmed,
    Continuation,
    EmptyReply,
    RoundLimit,
    RoundUsage,
    ToolCallStarted,
    ToolResultReady,
    TurnCancelled,
)
from whygraph.core.config import (
    AnthropicConfig,
    ChatConfig,
    Config,
    DeepSeekConfig,
    LlmConfig,
    OpenAIConfig,
    OpenRouterConfig,
)
from whygraph.core.context import use_project
from whygraph.db import get_session as project_session
from whygraph.db.models import ChatSession as ChatSessionRow
from whygraph.portal import db as portal_db
from whygraph.portal.models import Project, User
from whygraph.portal.secrets import LLM_API_KEY, put_secret
from whygraph.serve import chat as serve_chat
from whygraph.services.llm.chat import (
    ModelInfo,
    TextDelta,
    ToolCall,
    ToolCallMade,
    TurnDone,
)
from whygraph.services.llm.exceptions import LlmError, LlmKeyMissing


CHAT_PROVIDER_TAGS = ("anthropic", "openai", "deepseek", "openrouter")


def _store_org_keys(client: ScopedClient, providers=CHAT_PROVIDER_TAGS) -> None:
    """Store an org-level LLM key per provider, then drop the context cache."""
    with portal_db.get_session() as session:
        org_id = session.exec(
            select(Project.org_id).where(Project.slug == "demo")
        ).one()
        for provider in providers:
            put_secret(
                session,
                kind=LLM_API_KEY,
                value=f"sk-{provider}",
                provider=provider,
                org_id=org_id,
            )
    client.app.state.portal.contexts.invalidate()


def _llm(*providers: str, **kwargs) -> LlmConfig:
    """An ``LlmConfig`` with a stored key for each of ``providers``."""
    sections = {
        "anthropic": AnthropicConfig,
        "openai": OpenAIConfig,
        "deepseek": DeepSeekConfig,
        "openrouter": OpenRouterConfig,
    }
    for provider in providers:
        kwargs[provider] = sections[provider](api_key=f"sk-{provider}")
    return LlmConfig(**kwargs)


@pytest.fixture
def chat_client(env: SimpleNamespace) -> Iterator[ScopedClient]:  # noqa: F811
    """The portal, scoped to ``demo``, with a stored org key for every chat provider."""
    with portal_with_project(env, codegraph=False) as client:
        _store_org_keys(client)
        yield client


@pytest.fixture
def keyless_chat_client(env: SimpleNamespace) -> Iterator[ScopedClient]:  # noqa: F811
    """The portal, scoped to ``demo``, with no LLM key stored anywhere."""
    with portal_with_project(env, codegraph=False) as client:
        yield client


def _new_session(client, **body) -> dict:
    response = client.post("/api/chat/sessions", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _frames(response) -> list[dict]:
    """Parse an SSE response body into decoded event payloads.

    Comment blocks (``: heartbeat``) and empty chunks carry no event.
    """
    return [
        json.loads(block.removeprefix("data: "))
        for block in response.text.split("\n\n")
        if block.strip() and not block.startswith(":")
    ]


def _wait_released(key, timeout: float = 5.0) -> None:
    """Wait until the worker gave the session's lease back."""
    deadline = time.monotonic() + timeout
    while key in serve_chat._LEASES:
        assert time.monotonic() < deadline, "the worker never released the lease"
        time.sleep(0.01)


def _stub_harness(monkeypatch: pytest.MonkeyPatch, events) -> None:
    """Replace ``run_turn`` on the ``serve.chat`` namespace with a script."""

    def _fake_run_turn(*, client, history, registry=None, **kwargs):
        _fake_run_turn.history = history
        _fake_run_turn.kwargs = kwargs
        yield from events

    _fake_run_turn.history = ()
    _fake_run_turn.kwargs = {}
    monkeypatch.setattr(serve_chat, "run_turn", _fake_run_turn)
    # The provider client is never used by the stub, but make_chat_client is
    # still called — keep it from needing a real key.
    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: object())
    return _fake_run_turn


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


def test_providers_lists_the_four_chat_providers(chat_client) -> None:
    payload = chat_client.get("/api/chat/providers").json()
    assert [p["provider"] for p in payload] == list(CHAT_PROVIDER_TAGS)
    assert all(p["default_model"] for p in payload)
    assert all("env_var" not in p for p in payload)
    assert all(p["configured"] for p in payload)
    assert {p["provider"] for p in payload if p["default"]} == {"anthropic"}


def test_provider_is_configured_from_stored_keys_only(
    chat_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_project_config(chat_client, monkeypatch, Config(llm=_llm("openai")))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-in-env")

    flags = {
        p["provider"]: p["configured"]
        for p in chat_client.get("/api/chat/providers").json()
    }
    assert flags["openai"] is True  # a stored key
    assert flags["deepseek"] is False  # the environment never counts
    assert flags["anthropic"] is False
    assert flags["openrouter"] is False


def _defaults(client) -> dict[str, bool]:
    return {
        p["provider"]: p["default"] for p in client.get("/api/chat/providers").json()
    }


def test_keyless_providers_have_no_default(
    keyless_chat_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(2):
        payload = keyless_chat_client.get("/api/chat/providers").json()
        assert not any(p["configured"] or p["default"] for p in payload)
        assert all("env_var" not in p for p in payload)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-in-env")


def test_the_only_keyed_provider_is_the_default(
    chat_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_project_config(chat_client, monkeypatch, Config(llm=_llm("openrouter")))
    assert _defaults(chat_client) == {
        "anthropic": False,
        "openai": False,
        "deepseek": False,
        "openrouter": True,
    }


def test_the_configured_chat_provider_is_the_default_only_with_a_key(
    chat_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_project_config(
        chat_client,
        monkeypatch,
        Config(
            llm=_llm("anthropic", "openrouter"),
            chat=ChatConfig(provider="openrouter"),
        ),
    )
    assert _defaults(chat_client)["openrouter"] is True

    use_project_config(
        chat_client,
        monkeypatch,
        Config(
            llm=_llm("anthropic", "openrouter"),
            chat=ChatConfig(provider="deepseek"),
        ),
    )
    defaults = _defaults(chat_client)
    assert defaults["anthropic"] is True and defaults["deepseek"] is False


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------


def test_session_crud_round_trip(chat_client) -> None:
    assert chat_client.get("/api/chat/sessions").json() == []

    created = _new_session(chat_client, provider="openai", model="gpt-4o")
    assert created["title"] == serve_chat.DEFAULT_TITLE
    assert (created["provider"], created["model"]) == ("openai", "gpt-4o")
    assert created["message_count"] == 0

    listed = chat_client.get("/api/chat/sessions").json()
    assert [s["id"] for s in listed] == [created["id"]]

    renamed = chat_client.patch(
        f"/api/chat/sessions/{created['id']}", json={"title": "Auth investigation"}
    ).json()
    assert renamed["title"] == "Auth investigation"

    transcript = chat_client.get(f"/api/chat/sessions/{created['id']}").json()
    assert transcript["messages"] == []
    assert transcript["title"] == "Auth investigation"

    assert chat_client.delete(f"/api/chat/sessions/{created['id']}").status_code == 204
    assert chat_client.get("/api/chat/sessions").json() == []


def test_create_session_defaults_from_chat_config(
    chat_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_project_config(
        chat_client,
        monkeypatch,
        Config(
            llm=_llm("deepseek"),
            chat=ChatConfig(provider="deepseek", model="deepseek-reasoner"),
        ),
    )
    created = _new_session(chat_client)
    assert (created["provider"], created["model"]) == ("deepseek", "deepseek-reasoner")


def test_create_session_falls_back_to_the_provider_model(chat_client) -> None:
    """An empty ``[chat].model`` defers to ``[llm.<provider>].model``."""
    created = _new_session(chat_client, provider="openrouter")
    assert created["model"] == "openrouter/auto"


def test_create_session_defaults_from_llm_model(
    chat_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config v2: with no ``[chat]``, the session follows ``[llm].model``."""
    use_project_config(
        chat_client,
        monkeypatch,
        Config(llm=_llm("openai", "anthropic", model="openai/gpt-4o-mini")),
    )
    created = _new_session(chat_client)
    assert (created["provider"], created["model"]) == ("openai", "gpt-4o-mini")

    by_provider = {
        p["provider"]: p["default_model"]
        for p in chat_client.get("/api/chat/providers").json()
    }
    assert by_provider["openai"] == "gpt-4o-mini"
    assert by_provider["anthropic"] == "claude-opus-4-7"


def test_create_session_rejects_a_configured_non_chat_provider(
    chat_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_project_config(
        chat_client, monkeypatch, Config(chat=ChatConfig(provider="ollama"))
    )
    response = chat_client.post("/api/chat/sessions", json={})
    assert response.status_code == 400
    assert response.json()["code"] == "bad_config"
    assert "not a chat provider" in response.json()["error"]


def test_create_session_without_a_key_is_refused_and_writes_nothing(
    keyless_chat_client,
) -> None:
    response = keyless_chat_client.post("/api/chat/sessions", json={})
    assert response.status_code == 409
    assert response.json()["code"] == "no_llm_key"
    assert keyless_chat_client.get("/api/chat/sessions").json() == []


def test_create_session_for_a_keyless_provider_is_refused(
    chat_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_project_config(chat_client, monkeypatch, Config(llm=_llm("anthropic")))
    response = chat_client.post("/api/chat/sessions", json={"provider": "deepseek"})
    assert response.status_code == 409
    assert response.json()["code"] == "no_llm_key"
    assert "(deepseek)" in response.json()["error"]
    assert chat_client.get("/api/chat/sessions").json() == []


def test_patch_to_a_keyless_provider_is_refused_but_a_model_change_is_not(
    chat_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _new_session(chat_client, provider="anthropic")
    use_project_config(chat_client, monkeypatch, Config(llm=_llm("anthropic")))
    url = f"/api/chat/sessions/{session['id']}"

    refused = chat_client.patch(url, json={"provider": "deepseek"})
    assert refused.status_code == 409
    assert refused.json()["code"] == "no_llm_key"
    assert chat_client.get(url).json()["provider"] == "anthropic"

    ok = chat_client.patch(url, json={"model": "claude-x"})
    assert ok.status_code == 200 and ok.json()["model"] == "claude-x"


def test_send_to_a_keyless_session_is_refused_and_persists_nothing(
    chat_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _new_session(chat_client, provider="deepseek")
    use_project_config(chat_client, monkeypatch, Config(llm=_llm("anthropic")))

    response = chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "hi"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "no_llm_key"
    assert (
        chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"] == []
    )


def test_create_session_rejects_a_non_chat_provider(chat_client) -> None:
    response = chat_client.post("/api/chat/sessions", json={"provider": "ollama"})
    assert response.status_code == 400
    assert response.json()["code"] == "bad_provider"
    assert "not a chat provider" in response.json()["error"]


def test_unknown_session_is_404_everywhere(chat_client) -> None:
    missing = chat_client.get("/api/chat/sessions/999")
    assert missing.status_code == 404
    assert missing.json() == {"error": "session 999 not found", "code": "not_found"}
    assert (
        chat_client.patch("/api/chat/sessions/999", json={"title": "x"}).status_code
        == 404
    )
    assert chat_client.delete("/api/chat/sessions/999").status_code == 404
    assert (
        chat_client.post(
            "/api/chat/sessions/999/messages", json={"content": "hi"}
        ).status_code
        == 404
    )


def test_local_mode_writes_the_owner_but_filters_nothing(chat_client) -> None:
    """M2d-1 plan section 4.7: one user, so the list and the routes see every row."""
    created = _new_session(chat_client, title="mine")
    assert set(created) == {
        "id",
        "title",
        "provider",
        "model",
        "effort",
        "streaming",
        "created_at",
        "updated_at",
        "message_count",
    }
    with portal_db.get_session() as session:
        (local_uid,) = session.exec(select(User.uid)).all()
    with use_project(chat_client.context()), project_session() as session:
        assert session.get(ChatSessionRow, created["id"]).owner_uid == local_uid
        # A pre-M2d-1 row and one recorded for someone else stay visible.
        for title, owner, when in (
            ("ownerless", None, "2000-01-02T00:00:00+00:00"),
            ("foreign", "someone-else", "2000-01-01T00:00:00+00:00"),
        ):
            session.add(
                ChatSessionRow(
                    title=title,
                    provider="openai",
                    model="gpt-4o",
                    created_at=when,
                    updated_at=when,
                    owner_uid=owner,
                )
            )
        session.commit()

    listed = chat_client.get("/api/chat/sessions").json()
    assert [s["title"] for s in listed] == ["mine", "ownerless", "foreign"]
    assert all(set(s) == set(created) for s in listed)
    for row in listed:
        url = f"/api/chat/sessions/{row['id']}"
        assert chat_client.get(url).status_code == 200
        renamed = chat_client.patch(url, json={"title": f"{row['title']} 2"})
        assert renamed.status_code == 200
    assert (
        chat_client.delete(f"/api/chat/sessions/{listed[1]['id']}").status_code == 204
    )


def test_rename_rejects_an_empty_title(chat_client) -> None:
    session = _new_session(chat_client)
    response = chat_client.patch(
        f"/api/chat/sessions/{session['id']}", json={"title": "  "}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "bad_title"


def test_empty_message_is_rejected(chat_client) -> None:
    session = _new_session(chat_client)
    response = chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "   "}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "bad_content"


def test_delete_removes_the_messages_too(chat_client, monkeypatch) -> None:
    _stub_harness(monkeypatch, [TextDelta(text="hi"), TurnDone("stop")])
    session = _new_session(chat_client)
    chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "q"}
    )

    assert chat_client.delete(f"/api/chat/sessions/{session['id']}").status_code == 204
    # A leftover message row would have blocked the parent delete (FKs are on),
    # so a clean 204 plus an empty list is the proof.
    assert chat_client.get("/api/chat/sessions").json() == []


# ---------------------------------------------------------------------------
# Titling
# ---------------------------------------------------------------------------


def test_first_message_titles_the_session(chat_client, monkeypatch) -> None:
    _stub_harness(monkeypatch, [TextDelta(text="ok"), TurnDone("stop")])
    session = _new_session(chat_client)

    chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages",
        json={"content": "why is the harness sync?"},
    )
    assert (
        chat_client.get(f"/api/chat/sessions/{session['id']}").json()["title"]
        == "why is the harness sync?"
    )


def test_first_message_title_is_truncated(chat_client, monkeypatch) -> None:
    _stub_harness(monkeypatch, [TurnDone("stop")])
    session = _new_session(chat_client)
    chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "z" * 200}
    )
    title = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["title"]
    assert len(title) == serve_chat.TITLE_MAX_CHARS


def test_an_explicit_rename_wins_over_first_message_titling(
    chat_client, monkeypatch
) -> None:
    _stub_harness(monkeypatch, [TurnDone("stop")])
    session = _new_session(chat_client)
    chat_client.patch(f"/api/chat/sessions/{session['id']}", json={"title": "Mine"})

    chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages",
        json={"content": "first question"},
    )
    assert (
        chat_client.get(f"/api/chat/sessions/{session['id']}").json()["title"] == "Mine"
    )


def test_a_title_given_at_creation_is_kept(chat_client, monkeypatch) -> None:
    _stub_harness(monkeypatch, [TurnDone("stop")])
    session = _new_session(chat_client, title="Preset")
    chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "q"}
    )
    assert (
        chat_client.get(f"/api/chat/sessions/{session['id']}").json()["title"]
        == "Preset"
    )


# ---------------------------------------------------------------------------
# The streaming turn
# ---------------------------------------------------------------------------


def test_text_only_turn_streams_and_persists(chat_client, monkeypatch) -> None:
    _stub_harness(
        monkeypatch,
        [
            TextDelta(text="Because "),
            TextDelta(text="history."),
            RoundUsage(input_tokens=11, output_tokens=22),
            TurnDone("stop", 11, 22),
        ],
    )
    session = _new_session(chat_client)

    response = chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "why?"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    frames = _frames(response)
    assert [f["type"] for f in frames] == ["text_delta", "text_delta", "done"]
    assert frames[-1]["input_tokens"] == 11
    assert frames[-1]["output_tokens"] == 22

    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "why?"),
        ("assistant", "Because history."),
    ]
    assert messages[1]["id"] == frames[-1]["message_id"]
    assert all(m["session_id"] if "session_id" in m else True for m in messages)


def test_tool_round_frames_and_rows(chat_client, monkeypatch) -> None:
    """A tool round produces the full frame sequence and three new rows."""
    call = ToolCall(id="c1", name="search_symbols", arguments={"query": "x"})
    _stub_harness(
        monkeypatch,
        [
            TextDelta(text="Looking… "),
            RoundUsage(input_tokens=3, output_tokens=4),
            ToolCallStarted(call=call),
            ToolResultReady(call=call, result=json.dumps({"count": 1})),
            TextDelta(text="Found it."),
            RoundUsage(input_tokens=5, output_tokens=6),
            TurnDone("stop", 8, 10),
        ],
    )
    session = _new_session(chat_client)
    response = chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "find x"}
    )

    frames = _frames(response)
    assert [f["type"] for f in frames] == [
        "text_delta",
        "tool_call",
        "tool_result",
        "text_delta",
        "done",
    ]
    assert frames[1]["name"] == "search_symbols"
    assert frames[1]["arguments"] == {"query": "x"}
    assert json.loads(frames[2]["result"]) == {"count": 1}

    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    # The tool-calling assistant row carries the serialized call…
    assert messages[1]["tool_calls"] == [
        {"id": "c1", "name": "search_symbols", "arguments": {"query": "x"}}
    ]
    # …the tool row answers it by id…
    assert messages[2]["tool_call_id"] == "c1"
    assert json.loads(messages[2]["content"]) == {"count": 1}
    # …and the final prose is its own row, with the usage attached.
    assert messages[3]["content"] == "Found it."
    assert messages[3]["tool_calls"] == []
    # Each round's row carries that round's tokens; `done` the turn total.
    assert (messages[1]["input_tokens"], messages[1]["output_tokens"]) == (3, 4)
    assert (messages[3]["input_tokens"], messages[3]["output_tokens"]) == (5, 6)
    assert (frames[-1]["input_tokens"], frames[-1]["output_tokens"]) == (8, 10)


def test_tool_result_is_truncated_for_display_only(chat_client, monkeypatch) -> None:
    """The wire frame shows a preview; the stored row keeps the full result."""
    call = ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})
    big = json.dumps({"content": "y" * 5000})
    _stub_harness(
        monkeypatch,
        [
            RoundUsage(),
            ToolCallStarted(call=call),
            ToolResultReady(call=call, result=big),
            TextDelta(text="done"),
            RoundUsage(),
            TurnDone("stop"),
        ],
    )
    session = _new_session(chat_client)
    response = chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "read it"}
    )

    frame = next(f for f in _frames(response) if f["type"] == "tool_result")
    assert len(frame["result"]) == serve_chat.DISPLAY_RESULT_CHARS

    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    tool_row = next(m for m in messages if m["role"] == "tool")
    assert tool_row["content"] == big  # full fidelity on disk


def test_round_limit_emits_its_own_frame(chat_client, monkeypatch) -> None:
    call = ToolCall(id="c1", name="search_symbols", arguments={"query": "x"})
    _stub_harness(
        monkeypatch,
        [
            RoundUsage(),
            ToolCallStarted(call=call),
            ToolResultReady(call=call, result="{}"),
            RoundLimit(rounds=3),
            RoundUsage(),
            TurnDone("tool_calls"),
        ],
    )
    session = _new_session(chat_client)
    response = chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "loop"}
    )
    types = [f["type"] for f in _frames(response)]
    assert "round_limit" in types
    assert types[-1] == "done"  # never a hung stream


def test_tool_only_rounds_are_separate_rows_with_their_own_usage(
    chat_client, monkeypatch
) -> None:
    """One assistant row per provider round; tool-only rounds no longer merge."""
    a = ToolCall(id="a", name="search_symbols", arguments={"query": "a"})
    b = ToolCall(id="b", name="get_symbol", arguments={"qualified_name": "b"})
    _stub_harness(
        monkeypatch,
        [
            RoundUsage(input_tokens=100, output_tokens=10, model="served-1"),
            ToolCallStarted(call=a),
            ToolResultReady(call=a, result='{"a": 1}'),
            RoundUsage(input_tokens=200, output_tokens=20),
            ToolCallStarted(call=b),
            ToolResultReady(call=b, result='{"b": 2}'),
            TextDelta(text="Answer."),
            RoundUsage(input_tokens=300, output_tokens=30, model="served-3"),
            TurnDone("stop", 600, 60, model="served-3"),
        ],
    )
    session = _new_session(chat_client, provider="openrouter", model="openrouter/auto")
    frames = _frames(
        chat_client.post(
            f"/api/chat/sessions/{session['id']}/messages", json={"content": "q"}
        )
    )
    # RoundUsage is not a frame of its own.
    assert [f["type"] for f in frames] == [
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "text_delta",
        "done",
    ]

    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    assert [m["role"] for m in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    rounds = [m for m in messages if m["role"] == "assistant"]
    assert [[c["id"] for c in m["tool_calls"]] for m in rounds] == [["a"], ["b"], []]
    assert [(m["input_tokens"], m["output_tokens"]) for m in rounds] == [
        (100, 10),
        (200, 20),
        (300, 30),
    ]
    # The served model wins where the round reported one; the session's
    # requested model otherwise. The provider is always the session's.
    assert [m["model"] for m in rounds] == ["served-1", "openrouter/auto", "served-3"]
    assert all(m["provider"] == "openrouter" for m in rounds)
    # Each tool row follows the round that called it.
    assert messages[2]["tool_call_id"] == "a"
    assert messages[4]["tool_call_id"] == "b"

    done = frames[-1]
    assert (done["input_tokens"], done["output_tokens"]) == (600, 60)
    assert done["message_id"] == rounds[-1]["id"]


class _TwoRoundClient:
    """A port-shaped client: a tool-only round, then an answer round."""

    provider = "openai"
    model = "gpt-4o"

    def __init__(self) -> None:
        self.calls = 0

    def stream_turn(self, request):
        self.calls += 1
        if self.calls == 1:
            yield ToolCallMade(
                call=ToolCall(id="u1", name="no_such_tool", arguments={})
            )
            yield TurnDone("tool_calls", 40, 4, model="gpt-4o-2026-01-01")
        else:
            yield TextDelta(text="Done.")
            yield TurnDone("stop", 60, 6, model="gpt-4o-2026-01-01")


def test_real_harness_rows_match_its_rounds(chat_client, monkeypatch) -> None:
    """The real ``run_turn`` and the serve layer agree on round boundaries."""
    monkeypatch.setattr(
        serve_chat, "make_chat_client", lambda *a, **k: _TwoRoundClient()
    )
    session = _new_session(chat_client, provider="openai", model="gpt-4o")
    frames = _frames(
        chat_client.post(
            f"/api/chat/sessions/{session['id']}/messages", json={"content": "q"}
        )
    )
    assert frames[-1]["type"] == "done"
    assert (frames[-1]["input_tokens"], frames[-1]["output_tokens"]) == (100, 10)

    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    rounds = [m for m in messages if m["role"] == "assistant"]
    assert [(m["input_tokens"], m["output_tokens"]) for m in rounds] == [
        (40, 4),
        (60, 6),
    ]
    assert {m["model"] for m in rounds} == {"gpt-4o-2026-01-01"}


def test_round_limit_flushes_the_last_tool_round(chat_client, monkeypatch) -> None:
    call = ToolCall(id="c1", name="search_symbols", arguments={"query": "x"})
    _stub_harness(
        monkeypatch,
        [
            TextDelta(text="Searching."),
            RoundUsage(input_tokens=10, output_tokens=1),
            ToolCallStarted(call=call),
            ToolResultReady(call=call, result="{}"),
            RoundLimit(rounds=1),
            TextDelta(text="Here is what I have."),
            RoundUsage(input_tokens=20, output_tokens=2),
            TurnDone("stop", 30, 3),
        ],
    )
    session = _new_session(chat_client)
    chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "q"}
    )

    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "q"),
        ("assistant", "Searching."),
        ("tool", "{}"),
        ("assistant", "Here is what I have."),
    ]
    assert [m["output_tokens"] for m in messages if m["role"] == "assistant"] == [1, 2]


def test_a_budget_stop_writes_the_budget_row_as_the_turns_last(
    chat_client, monkeypatch
) -> None:
    """The buffered round is flushed, then one budget row; a refresh replays it."""
    call = ToolCall(id="c1", name="search_symbols", arguments={"query": "x"})
    _stub_harness(
        monkeypatch,
        [
            TextDelta(text="Searching."),
            RoundUsage(input_tokens=10, output_tokens=1),
            ToolCallStarted(call=call),
            ToolResultReady(call=call, result="{}"),
            BudgetStop(scope="project"),
            TurnDone("tool_calls", 10, 1),
        ],
    )
    session = _new_session(chat_client)
    frames = _frames(
        chat_client.post(
            f"/api/chat/sessions/{session['id']}/messages", json={"content": "q"}
        )
    )
    assert [f["type"] for f in frames] == [
        "text_delta",
        "tool_call",
        "tool_result",
        "budget_exceeded",
        "done",
    ]
    assert frames[3]["scope"] == "project"
    assert frames[3]["message"] == serve_chat.BUDGET_STOP_MESSAGE
    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    assert [(m["role"], m["content"], m["error"]) for m in messages] == [
        ("user", "q", None),
        ("assistant", "Searching.", None),
        ("tool", "{}", None),
        ("assistant", serve_chat.BUDGET_STOP_MESSAGE, "budget_exceeded"),
    ]
    assert messages[1]["input_tokens"] == 10
    assert messages[3]["input_tokens"] is None
    assert frames[3]["message_id"] == frames[-1]["message_id"] == messages[3]["id"]
    assert (frames[-1]["input_tokens"], frames[-1]["output_tokens"]) == (10, 1)


def test_a_round_that_reported_tokens_gets_a_row_even_without_text(
    chat_client, monkeypatch
) -> None:
    """The rows of a turn add up to its total, even for an empty answer."""
    call = ToolCall(id="c1", name="search_symbols", arguments={"query": "x"})
    _stub_harness(
        monkeypatch,
        [
            RoundUsage(input_tokens=10, output_tokens=1),
            ToolCallStarted(call=call),
            ToolResultReady(call=call, result="{}"),
            RoundUsage(input_tokens=12, output_tokens=0),
            TurnDone("stop", 22, 1),
        ],
    )
    session = _new_session(chat_client)
    frames = _frames(
        chat_client.post(
            f"/api/chat/sessions/{session['id']}/messages", json={"content": "q"}
        )
    )
    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    rounds = [m for m in messages if m["role"] == "assistant"]
    assert [(m["content"], m["input_tokens"]) for m in rounds] == [("", 10), ("", 12)]
    assert sum(m["input_tokens"] for m in rounds) == frames[-1]["input_tokens"]


def test_an_error_after_a_finished_round_keeps_that_rounds_usage(
    chat_client, monkeypatch
) -> None:
    call = ToolCall(id="c1", name="search_symbols", arguments={"query": "x"})

    def _fails_in_round_two(*, client, history, registry=None, **kwargs):
        yield RoundUsage(input_tokens=10, output_tokens=1, model="served")
        yield ToolCallStarted(call=call)
        yield ToolResultReady(call=call, result="{}")
        yield TextDelta(text="Partial")
        raise LlmError("connection reset")

    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: object())
    monkeypatch.setattr(serve_chat, "run_turn", _fails_in_round_two)
    session = _new_session(chat_client, provider="openai", model="gpt-4o")
    chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "q"}
    )

    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    rounds = [m for m in messages if m["role"] == "assistant"]
    # Round 1 was flushed whole when round 2's text began; round 2 never
    # finished, so its row has the error and no tokens.
    assert [(m["input_tokens"], m["model"], m["error"]) for m in rounds] == [
        (10, "served", None),
        (None, "gpt-4o", "connection reset"),
    ]


def test_history_is_passed_to_the_harness_and_accumulates(
    chat_client, monkeypatch
) -> None:
    """Turn 2 sees turn 1's rows — the transcript is the model's memory."""
    spy = _stub_harness(monkeypatch, [TextDelta(text="a1"), TurnDone("stop")])
    session = _new_session(chat_client)

    chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "q1"}
    )
    assert [(m.role, m.content) for m in spy.history] == [("user", "q1")]

    chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "q2"}
    )
    assert [(m.role, m.content) for m in spy.history] == [
        ("user", "q1"),
        ("assistant", "a1"),
        ("user", "q2"),
    ]


def test_history_round_trips_tool_calls(chat_client, monkeypatch) -> None:
    """A persisted tool round decodes back into port ToolCalls."""
    call = ToolCall(id="c1", name="get_symbol", arguments={"qualified_name": "pkg.a"})
    _stub_harness(
        monkeypatch,
        [
            RoundUsage(),
            ToolCallStarted(call=call),
            ToolResultReady(call=call, result="{}"),
            TextDelta(text="answer"),
            RoundUsage(),
            TurnDone("stop"),
        ],
    )
    session = _new_session(chat_client)
    chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "q1"}
    )

    spy = _stub_harness(monkeypatch, [TextDelta(text="a2"), TurnDone("stop")])
    chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "q2"}
    )

    assistant = next(m for m in spy.history if m.tool_calls)
    assert assistant.tool_calls == (call,)
    tool_message = next(m for m in spy.history if m.role == "tool")
    assert tool_message.tool_call_id == "c1"


# ---------------------------------------------------------------------------
# Failure paths (§7.2 — in-band, never a hung spinner)
# ---------------------------------------------------------------------------


def test_unconfigured_provider_yields_a_single_error_frame(
    chat_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a, **k):
        raise LlmKeyMissing("openrouter is not configured - set OPENROUTER_API_KEY")

    monkeypatch.setattr(serve_chat, "make_chat_client", _boom)
    # A stored key passes the up-front gate; the in-band frame is the backstop
    # for a client that still cannot be built.
    session = _new_session(chat_client, provider="openrouter")

    response = chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "hi"}
    )
    assert response.status_code == 200  # status is committed before streaming
    frames = _frames(response)
    assert [f["type"] for f in frames] == ["error"]
    # The SPA words a missing key from the code and the provider (MODE-4):
    # no environment variable or `whygraph.toml` key in the text.
    assert frames[0]["code"] == "no_llm_key"
    assert frames[0]["provider"] == "openrouter"
    assert "OPENROUTER_API_KEY" not in frames[0]["message"]

    # The question is still in the transcript, so a retry needs no retyping —
    # and the failure is a row, not just a frame, so a refresh replays it.
    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[-1]["content"] == ""
    assert messages[-1]["error"] == frames[0]["message"]


def test_mid_stream_provider_error_is_in_band_and_keeps_partial_text(
    chat_client, monkeypatch
) -> None:
    def _partial(*, client, history, registry=None, **kwargs):
        yield TextDelta(text="I was saying")
        raise LlmError("connection reset")

    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: object())
    monkeypatch.setattr(serve_chat, "run_turn", _partial)
    session = _new_session(chat_client)

    response = chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "hi"}
    )
    frames = _frames(response)
    assert [f["type"] for f in frames] == ["text_delta", "error"]
    assert "connection reset" in frames[-1]["message"]
    assert frames[-1]["code"] == "provider_error"

    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "hi"),
        ("assistant", "I was saying"),
    ]
    # The partial text AND the reason it stopped, on the same row.
    assert messages[-1]["error"] == "connection reset"


def test_provider_error_before_any_token_still_persists_a_reply_row(
    chat_client, monkeypatch
) -> None:
    """A 401 before the first delta must not leave the user unanswered.

    Nothing is buffered at that point, so the empty-buffer guard in
    ``_flush_round`` has to yield to the error and write a row anyway.
    """

    def _immediate(*, client, history, registry=None, **kwargs):
        raise LlmError("401 invalid x-api-key")
        yield  # pragma: no cover -- makes this a generator

    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: object())
    monkeypatch.setattr(serve_chat, "run_turn", _immediate)
    session = _new_session(chat_client)

    frames = _frames(
        chat_client.post(
            f"/api/chat/sessions/{session['id']}/messages", json={"content": "hi"}
        )
    )
    assert [f["type"] for f in frames] == ["error"]

    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "hi"),
        ("assistant", ""),
    ]
    assert messages[-1]["error"] == "401 invalid x-api-key"


def test_an_unreachable_provider_is_coded_llm_unavailable(
    chat_client, monkeypatch
) -> None:
    """A connection failure under the ``LlmError`` reads as ``llm_unavailable``."""

    class APIConnectionError(Exception):
        pass

    def _unreachable(*, client, history, registry=None, **kwargs):
        raise LlmError("openai API error") from APIConnectionError("refused")
        yield  # pragma: no cover -- makes this a generator

    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: object())
    monkeypatch.setattr(serve_chat, "run_turn", _unreachable)
    session = _new_session(chat_client)

    frames = _frames(
        chat_client.post(
            f"/api/chat/sessions/{session['id']}/messages", json={"content": "hi"}
        )
    )
    assert frames == [
        {"type": "error", "code": "llm_unavailable", "message": "openai API error"}
    ]


def test_client_disconnect_persists_a_stopped_marker(chat_client, monkeypatch) -> None:
    """Abandoning the stream mid-turn stops the worker, which records ``"Stopped."``.

    The test client drains the body before it reports a disconnect, so the
    job's frame generator is driven directly: closing it is what the server
    does when the client goes away (plan 37 §9.31).
    """
    gate = threading.Event()

    def _slow(*, client, history, registry=None, cancel=None, **kwargs):
        yield TextDelta(text="partial")
        while not cancel.is_set():
            gate.wait(0.01)
        raise TurnCancelled

    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: object())
    monkeypatch.setattr(serve_chat, "run_turn", _slow)
    session = _new_session(chat_client)
    session_id = session["id"]

    with use_project(chat_client.context()):
        user_id = serve_chat._persist_user_message(session_id, "hi")
        key = serve_chat._lease_key(session_id)
        job = serve_chat.TurnJob(key, session_id, "openai", "gpt-4o", start_id=user_id)
        serve_chat._acquire(key, job)
        job.start()
        frames = job.frames()
        first = next(f for f in frames if f)  # the text_delta, past empty chunks
        assert json.loads(first.removeprefix("data: "))["text"] == "partial"
        frames.close()  # the client went away
        _wait_released(key)

    assert job.cancel_event.is_set()
    messages = chat_client.get(f"/api/chat/sessions/{session_id}").json()["messages"]
    stopped = messages[-1]
    assert stopped["role"] == "assistant"
    assert stopped["content"] == "partial"
    assert stopped["error"] == "Stopped."


def test_happy_path_assistant_row_has_no_error(chat_client, monkeypatch) -> None:
    """Regression guard: a successful turn leaves ``error`` NULL."""
    _stub_harness(monkeypatch, [TextDelta(text="all good"), TurnDone("stop")])
    session = _new_session(chat_client)
    chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "hi"}
    )

    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    assert [m["error"] for m in messages] == [None, None]


def test_unexpected_crash_is_also_an_in_band_error(chat_client, monkeypatch) -> None:
    def _crash(*, client, history, registry=None, **kwargs):
        yield TextDelta(text="uh")
        raise RuntimeError("bug")

    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: object())
    monkeypatch.setattr(serve_chat, "run_turn", _crash)
    session = _new_session(chat_client)

    frames = _frames(
        chat_client.post(
            f"/api/chat/sessions/{session['id']}/messages", json={"content": "hi"}
        )
    )
    assert frames[-1]["type"] == "error"
    assert "RuntimeError: bug" in frames[-1]["message"]
    assert frames[-1]["code"] == "provider_error"

    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    assert messages[-1]["error"] == "RuntimeError: bug"


# ---------------------------------------------------------------------------
# Regression guard
# ---------------------------------------------------------------------------


def test_chat_router_does_not_shadow_explorer_routes(chat_client) -> None:
    """``.../chat`` is mounted beside the project's data routes, not over it."""
    # An Explorer route with no CodeGraph index still 503s (its own contract),
    # rather than 404-ing because the chat prefix swallowed it.
    response = chat_client.get("/api/tree")
    assert response.status_code == 503
    assert response.json()["code"] == "not_indexed"


# ---------------------------------------------------------------------------
# Model listing (live + fallback)
# ---------------------------------------------------------------------------


def _stub_list_models(monkeypatch, models=None, error: str | None = None):
    """Replace make_chat_client with one whose list_models is scripted."""

    class _Client:
        def list_models(self):
            if error is not None:
                raise LlmError(error)
            return tuple(models or ())

    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: _Client())


def test_models_live_listing(chat_client, monkeypatch) -> None:
    _stub_list_models(
        monkeypatch,
        [
            ModelInfo(id="claude-opus-5", display_name="Claude Opus 5"),
            ModelInfo(id="claude-haiku-4-5", display_name="Claude Haiku 4.5"),
        ],
    )
    payload = chat_client.get(
        "/api/chat/models", params={"provider": "anthropic"}
    ).json()

    assert payload["source"] == "live"
    assert [m["id"] for m in payload["models"]] == ["claude-opus-5", "claude-haiku-4-5"]
    assert payload["models"][0]["display_name"] == "Claude Opus 5"
    assert "error" not in payload


def test_models_falls_back_when_listing_fails(chat_client, monkeypatch) -> None:
    """A scoped key can chat but not enumerate — the dropdown must still fill."""
    _stub_list_models(monkeypatch, error="401 API key is invalid")
    payload = chat_client.get(
        "/api/chat/models", params={"provider": "anthropic"}
    ).json()

    assert payload["source"] == "fallback"
    assert "401" in payload["error"]
    ids = [m["id"] for m in payload["models"]]
    assert ids, "fallback list must never be empty"
    # The configured default is always offered, and is not duplicated.
    assert payload["default_model"] in ids
    assert len(ids) == len(set(ids))


def test_models_fallback_includes_a_configured_model_absent_from_the_static_list(
    chat_client, monkeypatch
) -> None:
    use_project_config(
        chat_client,
        monkeypatch,
        Config(
            llm=_llm(openai=OpenAIConfig(api_key="sk-openai", model="gpt-9-custom"))
        ),
    )
    _stub_list_models(monkeypatch, error="network down")
    payload = chat_client.get("/api/chat/models", params={"provider": "openai"}).json()
    assert payload["models"][0]["id"] == "gpt-9-custom"


def test_models_empty_live_list_is_treated_as_a_failure(
    chat_client, monkeypatch
) -> None:
    """An empty list would render an empty dropdown — fall back instead."""
    _stub_list_models(monkeypatch, [])
    payload = chat_client.get(
        "/api/chat/models", params={"provider": "deepseek"}
    ).json()
    assert payload["source"] == "fallback"
    assert payload["models"]


def test_models_rejects_a_non_chat_provider(chat_client) -> None:
    response = chat_client.get("/api/chat/models", params={"provider": "ollama"})
    assert response.status_code == 400
    assert response.json()["code"] == "bad_provider"


# ---------------------------------------------------------------------------
# Switching provider / model mid-session
# ---------------------------------------------------------------------------


def test_patch_switches_model(chat_client) -> None:
    session = _new_session(chat_client, provider="anthropic", model="claude-opus-5")
    updated = chat_client.patch(
        f"/api/chat/sessions/{session['id']}", json={"model": "claude-haiku-4-5"}
    ).json()
    assert (updated["provider"], updated["model"]) == ("anthropic", "claude-haiku-4-5")


def test_patch_switching_provider_resolves_a_new_default_model(chat_client) -> None:
    """The old model id is meaningless on the new provider."""
    session = _new_session(chat_client, provider="anthropic", model="claude-opus-5")
    updated = chat_client.patch(
        f"/api/chat/sessions/{session['id']}", json={"provider": "openrouter"}
    ).json()
    assert updated["provider"] == "openrouter"
    assert updated["model"] == "openrouter/auto"


def test_patch_provider_and_model_together_keeps_the_given_model(chat_client) -> None:
    session = _new_session(chat_client, provider="anthropic")
    updated = chat_client.patch(
        f"/api/chat/sessions/{session['id']}",
        json={"provider": "openrouter", "model": "anthropic/claude-opus-5"},
    ).json()
    assert (updated["provider"], updated["model"]) == (
        "openrouter",
        "anthropic/claude-opus-5",
    )


def test_patch_rejects_a_non_chat_provider_and_blank_values(chat_client) -> None:
    session = _new_session(chat_client)
    sid = session["id"]
    assert (
        chat_client.patch(
            f"/api/chat/sessions/{sid}", json={"provider": "ollama"}
        ).status_code
        == 400
    )
    assert (
        chat_client.patch(f"/api/chat/sessions/{sid}", json={"model": "  "}).status_code
        == 400
    )
    assert (
        chat_client.patch(f"/api/chat/sessions/{sid}", json={"title": "  "}).status_code
        == 400
    )


def test_patch_title_only_leaves_provider_and_model_alone(chat_client) -> None:
    session = _new_session(chat_client, provider="deepseek", model="deepseek-reasoner")
    updated = chat_client.patch(
        f"/api/chat/sessions/{session['id']}", json={"title": "Renamed"}
    ).json()
    assert updated["title"] == "Renamed"
    assert (updated["provider"], updated["model"]) == ("deepseek", "deepseek-reasoner")


def test_assistant_rows_record_the_model_that_produced_them(
    chat_client, monkeypatch
) -> None:
    """Switching mid-session must not rewrite earlier turns' attribution."""
    _stub_harness(monkeypatch, [TextDelta(text="from opus"), TurnDone("stop")])
    session = _new_session(chat_client, provider="anthropic", model="claude-opus-5")
    sid = session["id"]
    chat_client.post(f"/api/chat/sessions/{sid}/messages", json={"content": "q1"})

    chat_client.patch(f"/api/chat/sessions/{sid}", json={"model": "claude-haiku-4-5"})
    _stub_harness(monkeypatch, [TextDelta(text="from haiku"), TurnDone("stop")])
    chat_client.post(f"/api/chat/sessions/{sid}/messages", json={"content": "q2"})

    messages = chat_client.get(f"/api/chat/sessions/{sid}").json()["messages"]
    assistants = [m for m in messages if m["role"] == "assistant"]
    assert [(a["content"], a["model"]) for a in assistants] == [
        ("from opus", "claude-opus-5"),
        ("from haiku", "claude-haiku-4-5"),
    ]
    assert all(a["provider"] == "anthropic" for a in assistants)
    # User rows carry no attribution.
    assert all(m["model"] is None for m in messages if m["role"] == "user")


# ---------------------------------------------------------------------------
# Turn-ending fixes (plan 37, phase 0): the history sanitizer, replay
# persistence, request ids and transport errors
# ---------------------------------------------------------------------------


def _seed_rows(client, session_id: int, rows: list[dict]) -> list[int]:
    """Write raw transcript rows into the session as one linear path.

    Each row hangs under the previous one and the last becomes the leaf;
    returns the new ids in order.
    """
    from whygraph.db.models import ChatMessage as ChatMessageRow

    ids: list[int] = []
    with use_project(client.context()), project_session() as session:
        parent = session.get(ChatSessionRow, session_id).active_leaf_id
        for i, row in enumerate(rows):
            message = ChatMessageRow(
                session_id=session_id,
                parent_id=parent,
                created_at=f"2026-10-27T00:00:{i:02d}+00:00",
                **row,
            )
            session.add(message)
            session.flush()
            parent = message.id
            ids.append(message.id)
        chat = session.get(ChatSessionRow, session_id)
        chat.active_leaf_id = parent
        session.add(chat)
        session.commit()
    return ids


def test_history_sanitizer_drops_rows_a_provider_must_not_see(
    chat_client, monkeypatch
) -> None:
    """Empty, errored, budget-stop and orphaned rows never reach the model."""
    from whygraph.core.usage import BUDGET_EXCEEDED

    session = _new_session(chat_client)
    dangling = json.dumps([{"id": "c9", "name": "get_symbol", "arguments": {}}])
    answered = json.dumps([{"id": "c1", "name": "get_symbol", "arguments": {}}])
    _seed_rows(
        chat_client,
        session["id"],
        [
            {"role": "user", "content": "q1"},
            # A round that only reported tokens.
            {"role": "assistant", "content": "", "input_tokens": 5},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "", "error": "401 invalid x-api-key"},
            {"role": "user", "content": "q3"},
            {"role": "assistant", "content": "", "error": "Stopped."},
            {"role": "user", "content": "q4"},
            {
                "role": "assistant",
                "content": "monthly budget",
                "error": BUDGET_EXCEEDED,
            },
            {"role": "user", "content": "q5"},
            # A tool call whose result never landed.
            {"role": "assistant", "content": "", "tool_calls": dangling},
            {"role": "user", "content": "q6"},
            # A tool row no assistant row announced.
            {"role": "tool", "content": "{}", "tool_call_id": "c7"},
            # Partial text before a failure keeps its text; the error does not.
            {
                "role": "assistant",
                "content": "I was saying",
                "error": "connection reset",
            },
            {"role": "user", "content": "q7"},
            {"role": "assistant", "content": "", "tool_calls": answered},
            {"role": "tool", "content": "{}", "tool_call_id": "c1"},
            {"role": "tool", "content": "{}", "tool_call_id": "c-unknown"},
            {"role": "assistant", "content": "a7"},
        ],
    )

    spy = _stub_harness(monkeypatch, [TextDelta(text="ok"), TurnDone("stop")])
    chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "q8"}
    )

    assert [(m.role, m.content, m.tool_call_id) for m in spy.history] == [
        ("user", "q1", None),
        ("user", "q2", None),
        ("user", "q3", None),
        ("user", "q4", None),
        ("user", "q5", None),
        ("user", "q6", None),
        ("assistant", "I was saying", None),
        ("user", "q7", None),
        ("assistant", "", None),
        ("tool", "{}", "c1"),
        ("assistant", "a7", None),
        ("user", "q8", None),
    ]
    assert [c.id for c in spy.history[8].tool_calls] == ["c1"]

    # The transcript still shows the user every row.
    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    assert len(messages) == 18 + 2


def test_reasoning_content_is_persisted_and_replayed_on_the_next_turn(
    chat_client, monkeypatch
) -> None:
    replay = {"reasoning_content": "thought", "reasoning_details": [{"text": "t"}]}
    _stub_harness(
        monkeypatch,
        [
            TextDelta(text="a1"),
            RoundUsage(finish_reason="stop", replay=replay),
            TurnDone("stop"),
        ],
    )
    session = _new_session(chat_client)
    chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "q1"}
    )

    from whygraph.db.models import ChatMessage as ChatMessageRow

    with use_project(chat_client.context()), project_session() as db:
        row = db.exec(
            select(ChatMessageRow).where(ChatMessageRow.role == "assistant")
        ).one()
        # Only the persisted keys are written: OpenRouter's details are in-turn only.
        assert json.loads(row.replay) == {"reasoning_content": "thought"}

    spy = _stub_harness(monkeypatch, [TextDelta(text="a2"), TurnDone("stop")])
    chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "q2"}
    )
    assistant = next(m for m in spy.history if m.role == "assistant")
    assert assistant.replay == {"reasoning_content": "thought"}


def test_a_round_without_replay_stores_none(chat_client, monkeypatch) -> None:
    _stub_harness(
        monkeypatch,
        [TextDelta(text="a1"), RoundUsage(finish_reason="stop"), TurnDone("stop")],
    )
    session = _new_session(chat_client)
    chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "q1"}
    )

    from whygraph.db.models import ChatMessage as ChatMessageRow

    with use_project(chat_client.context()), project_session() as db:
        row = db.exec(
            select(ChatMessageRow).where(ChatMessageRow.role == "assistant")
        ).one()
        assert row.replay is None


def test_a_provider_request_id_is_shown_with_the_error(
    chat_client, monkeypatch
) -> None:
    def _rejected(*, client, history, registry=None, **kwargs):
        raise LlmError("anthropic API error: 400", request_id="req_011")
        yield  # pragma: no cover -- makes this a generator

    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: object())
    monkeypatch.setattr(serve_chat, "run_turn", _rejected)
    session = _new_session(chat_client)

    frames = _frames(
        chat_client.post(
            f"/api/chat/sessions/{session['id']}/messages", json={"content": "hi"}
        )
    )
    assert frames[-1]["message"] == "anthropic API error: 400 (request id: req_011)"
    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    assert messages[-1]["error"] == frames[-1]["message"]


def test_a_cut_stream_is_coded_llm_unavailable(chat_client, monkeypatch) -> None:
    """A read timeout under the ``LlmError`` reads as ``llm_unavailable``."""
    import httpx

    def _timed_out(*, client, history, registry=None, **kwargs):
        yield TextDelta(text="partial")
        raise LlmError(
            "anthropic stream interrupted: ReadTimeout"
        ) from httpx.ReadTimeout("timed out")

    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: object())
    monkeypatch.setattr(serve_chat, "run_turn", _timed_out)
    session = _new_session(chat_client)

    frames = _frames(
        chat_client.post(
            f"/api/chat/sessions/{session['id']}/messages", json={"content": "hi"}
        )
    )
    assert frames[-1]["code"] == "llm_unavailable"
    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    assert messages[-1]["content"] == "partial"
    assert messages[-1]["error"] == "anthropic stream interrupted: ReadTimeout"


# ---------------------------------------------------------------------------
# Branching, the lease, Stop, heartbeats and effort (plan 37 §4.5)
# ---------------------------------------------------------------------------


def _url(session_id: int, suffix: str = "") -> str:
    return f"/api/chat/sessions/{session_id}{suffix}"


def _transcript(client, session_id: int) -> dict:
    response = client.get(_url(session_id))
    assert response.status_code == 200, response.text
    return response.json()


def _path(client, session_id: int) -> list[tuple[str, str]]:
    return [
        (m["role"], m["content"]) for m in _transcript(client, session_id)["messages"]
    ]


def _reply(text: str) -> list:
    return [
        TextDelta(text=text),
        RoundUsage(finish_reason="end_turn"),
        TurnDone("end_turn"),
    ]


def test_edit_creates_a_sibling_version_with_arrows(chat_client, monkeypatch) -> None:
    _stub_harness(monkeypatch, _reply("a1"))
    session = _new_session(chat_client)
    sid = session["id"]
    chat_client.post(_url(sid, "/messages"), json={"content": "q1"})
    q1 = _transcript(chat_client, sid)["messages"][0]["id"]

    spy = _stub_harness(monkeypatch, _reply("a1b"))
    response = chat_client.post(
        _url(sid, f"/messages/{q1}/edit"), json={"content": "q1b"}
    )
    assert response.status_code == 200
    assert [f["type"] for f in _frames(response)] == ["text_delta", "done"]
    assert [(m.role, m.content) for m in spy.history] == [("user", "q1b")]

    transcript = _transcript(chat_client, sid)
    assert _path(chat_client, sid) == [("user", "q1b"), ("assistant", "a1b")]
    edited = transcript["messages"][0]
    # Root-level siblings: both versions of the first question have no parent.
    assert edited["parent_id"] is None
    assert transcript["versions"][str(edited["id"])] == {
        "index": 2,
        "count": 2,
        "siblings": [q1, edited["id"]],
    }
    assert transcript["active_leaf_id"] == transcript["messages"][-1]["id"]
    assert transcript["message_count"] == 4  # every row, on any branch
    assert transcript["title"] == "q1"  # an edit never retitles

    back = chat_client.post(_url(sid, "/branch"), json={"message_id": q1})
    assert back.status_code == 200
    assert [(m["role"], m["content"]) for m in back.json()["messages"]] == [
        ("user", "q1"),
        ("assistant", "a1"),
    ]
    assert back.json()["versions"][str(q1)]["index"] == 1
    assert _path(chat_client, sid) == [("user", "q1"), ("assistant", "a1")]


def test_editing_a_later_question_keeps_the_earlier_turns(
    chat_client, monkeypatch
) -> None:
    _stub_harness(monkeypatch, _reply("a1"))
    sid = _new_session(chat_client)["id"]
    chat_client.post(_url(sid, "/messages"), json={"content": "q1"})
    _stub_harness(monkeypatch, _reply("a2"))
    chat_client.post(_url(sid, "/messages"), json={"content": "q2"})
    q2 = _transcript(chat_client, sid)["messages"][2]["id"]

    spy = _stub_harness(monkeypatch, _reply("a2b"))
    chat_client.post(_url(sid, f"/messages/{q2}/edit"), json={"content": "q2b"})
    assert [(m.role, m.content) for m in spy.history] == [
        ("user", "q1"),
        ("assistant", "a1"),
        ("user", "q2b"),
    ]
    assert _path(chat_client, sid) == [
        ("user", "q1"),
        ("assistant", "a1"),
        ("user", "q2b"),
        ("assistant", "a2b"),
    ]


def test_regenerate_answers_the_question_again(chat_client, monkeypatch) -> None:
    _stub_harness(monkeypatch, _reply("a1"))
    sid = _new_session(chat_client)["id"]
    chat_client.post(_url(sid, "/messages"), json={"content": "q1"})
    q1 = _transcript(chat_client, sid)["messages"][0]["id"]

    spy = _stub_harness(monkeypatch, _reply("a2"))
    response = chat_client.post(_url(sid, f"/messages/{q1}/regenerate"))
    assert response.status_code == 200
    # The history ends with the question, never with the old reply.
    assert [(m.role, m.content) for m in spy.history] == [("user", "q1")]

    transcript = _transcript(chat_client, sid)
    assert _path(chat_client, sid) == [("user", "q1"), ("assistant", "a2")]
    reply = transcript["messages"][1]
    assert reply["parent_id"] == q1
    assert transcript["versions"][str(reply["id"])]["index"] == 2
    assert transcript["versions"][str(reply["id"])]["count"] == 2


def test_a_regenerate_that_writes_nothing_keeps_the_old_reply(
    chat_client, monkeypatch
) -> None:
    _stub_harness(monkeypatch, _reply("a1"))
    sid = _new_session(chat_client)["id"]
    chat_client.post(_url(sid, "/messages"), json={"content": "q1"})
    q1 = _transcript(chat_client, sid)["messages"][0]["id"]

    _stub_harness(monkeypatch, [TurnDone("end_turn")])
    chat_client.post(_url(sid, f"/messages/{q1}/regenerate"))
    transcript = _transcript(chat_client, sid)
    assert _path(chat_client, sid) == [("user", "q1"), ("assistant", "a1")]
    assert transcript["versions"] == {}


def test_a_tool_row_can_end_a_turn_and_versions_group_under_the_question(
    chat_client, monkeypatch
) -> None:
    call = ToolCall(id="c1", name="get_symbol", arguments={})
    _stub_harness(
        monkeypatch,
        [
            RoundUsage(finish_reason="tool_use"),
            ToolCallStarted(call=call),
            ToolResultReady(call=call, result="{}"),
            TurnDone("tool_use"),
        ],
    )
    sid = _new_session(chat_client)["id"]
    chat_client.post(_url(sid, "/messages"), json={"content": "q1"})
    transcript = _transcript(chat_client, sid)
    assert [m["role"] for m in transcript["messages"]] == ["user", "assistant", "tool"]
    assert transcript["active_leaf_id"] == transcript["messages"][-1]["id"]
    q1 = transcript["messages"][0]["id"]

    _stub_harness(monkeypatch, _reply("prose"))
    chat_client.post(_url(sid, f"/messages/{q1}/regenerate"))
    transcript = _transcript(chat_client, sid)
    assert [(m["role"], m["content"]) for m in transcript["messages"]] == [
        ("user", "q1"),
        ("assistant", "prose"),
    ]
    first_reply = q1 + 1
    assert (
        transcript["versions"][str(transcript["messages"][1]["id"])]["siblings"][0]
        == first_reply
    )

    # Branching back to the tool reply follows it down to its tool row.
    back = chat_client.post(
        _url(sid, "/branch"), json={"message_id": first_reply}
    ).json()
    assert [m["role"] for m in back["messages"]] == ["user", "assistant", "tool"]


def test_rows_record_finish_reason_effort_and_parent(chat_client, monkeypatch) -> None:
    _stub_harness(monkeypatch, _reply("a1"))
    sid = _new_session(chat_client, effort="high")["id"]
    chat_client.post(_url(sid, "/messages"), json={"content": "q1"})
    user_row, reply = _transcript(chat_client, sid)["messages"]
    assert reply["finish_reason"] == "end_turn"
    assert reply["effort"] == "high"
    assert reply["parent_id"] == user_row["id"]
    assert user_row["finish_reason"] is None


def test_new_routes_check_the_message_belongs_and_has_the_right_role(
    chat_client, monkeypatch
) -> None:
    _stub_harness(monkeypatch, _reply("a1"))
    sid = _new_session(chat_client)["id"]
    other = _new_session(chat_client)["id"]
    chat_client.post(_url(sid, "/messages"), json={"content": "q1"})
    chat_client.post(_url(other, "/messages"), json={"content": "elsewhere"})
    q1, a1 = (m["id"] for m in _transcript(chat_client, sid)["messages"])
    foreign = _transcript(chat_client, other)["messages"][0]["id"]

    for path, body in (
        (f"/messages/{a1}/edit", {"content": "x"}),  # an assistant row
        (f"/messages/{a1}/regenerate", None),
        (f"/messages/{foreign}/edit", {"content": "x"}),  # another session's row
        (f"/messages/{foreign}/regenerate", None),
        ("/messages/99999/regenerate", None),
    ):
        response = chat_client.post(_url(sid, path), json=body)
        assert response.status_code == 404, path
        assert response.json()["code"] == "not_found"
    branch = chat_client.post(_url(sid, "/branch"), json={"message_id": foreign})
    assert branch.status_code == 404
    assert (
        chat_client.post(_url(99999, f"/messages/{q1}/regenerate")).status_code == 404
    )
    # Nothing was written by any refusal.
    assert _transcript(chat_client, sid)["message_count"] == 2
    empty = chat_client.post(_url(sid, f"/messages/{q1}/edit"), json={"content": "  "})
    assert empty.status_code == 400


def test_a_budget_block_or_a_missing_key_refuses_edit_and_regenerate(
    chat_client, monkeypatch
) -> None:
    from dataclasses import replace as dc_replace

    from whygraph.core.usage import BUDGET_EXCEEDED

    _stub_harness(monkeypatch, _reply("a1"))
    sid = _new_session(chat_client)["id"]
    chat_client.post(_url(sid, "/messages"), json={"content": "q1"})
    q1 = _transcript(chat_client, sid)["messages"][0]["id"]

    ctx = use_project_config(
        chat_client, monkeypatch, Config(llm=_llm(*CHAT_PROVIDER_TAGS))
    )
    blocked = dc_replace(ctx, llm_block=BUDGET_EXCEEDED, llm_block_scope="project")
    contexts = chat_client.app.state.portal.contexts

    async def _aget(project_id: int):
        return blocked

    monkeypatch.setattr(contexts, "get", lambda project_id: blocked)
    monkeypatch.setattr(contexts, "aget", _aget)
    for path, body in (
        (f"/messages/{q1}/edit", {"content": "x"}),
        (f"/messages/{q1}/regenerate", None),
    ):
        response = chat_client.post(_url(sid, path), json=body)
        assert response.status_code == 403, path
        assert response.json()["code"] == "budget_exceeded"

    provider = _transcript(chat_client, sid)["provider"]
    others = [p for p in CHAT_PROVIDER_TAGS if p != provider]
    use_project_config(chat_client, monkeypatch, Config(llm=_llm(*others)))
    for path, body in (
        (f"/messages/{q1}/edit", {"content": "x"}),
        (f"/messages/{q1}/regenerate", None),
    ):
        response = chat_client.post(_url(sid, path), json=body)
        assert response.status_code == 409, path
        assert response.json()["code"] == "no_llm_key"
    assert _transcript(chat_client, sid)["message_count"] == 2


def _blocking_harness(monkeypatch, release: threading.Event, started: threading.Event):
    def _blocked(*, client, history, registry=None, cancel=None, **kwargs):
        started.set()
        while not release.is_set():
            if cancel is not None and cancel.is_set():
                raise TurnCancelled
            release.wait(0.01)
        yield TextDelta(text="late")
        yield RoundUsage(finish_reason="end_turn")
        yield TurnDone("end_turn")

    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: object())
    monkeypatch.setattr(serve_chat, "run_turn", _blocked)


def _truncated_reply(text: str, finish_reason: str = "max_tokens") -> list:
    return [
        TextDelta(text=text),
        RoundUsage(finish_reason=finish_reason),
        TurnDone(finish_reason),
    ]


def _truncated_session(chat_client, monkeypatch, finish_reason: str = "max_tokens"):
    """A session whose only turn ended at the output limit; returns ids."""
    _stub_harness(monkeypatch, _truncated_reply("cut off", finish_reason))
    sid = _new_session(chat_client)["id"]
    chat_client.post(_url(sid, "/messages"), json={"content": "q1"})
    q1, a1 = (m["id"] for m in _transcript(chat_client, sid)["messages"])
    return sid, q1, a1


@pytest.mark.parametrize("finish_reason", ["max_tokens", "length"])
def test_continue_extends_a_truncated_reply(
    chat_client, monkeypatch, finish_reason
) -> None:
    sid, q1, a1 = _truncated_session(chat_client, monkeypatch, finish_reason)

    spy = _stub_harness(monkeypatch, _reply(" and the rest"))
    response = chat_client.post(_url(sid, f"/messages/{a1}/continue"))
    assert response.status_code == 200
    assert [f["type"] for f in _frames(response)] == ["text_delta", "done"]
    # The history ends with the truncated reply and the harness is told to
    # carry on from it; no user row was written.
    assert [(m.role, m.content) for m in spy.history] == [
        ("user", "q1"),
        ("assistant", "cut off"),
    ]
    assert spy.kwargs["continue_from_assistant"] is True

    transcript = _transcript(chat_client, sid)
    assert [m["role"] for m in transcript["messages"]] == [
        "user",
        "assistant",
        "assistant",
    ]
    tail = transcript["messages"][2]
    assert tail["parent_id"] == a1
    assert transcript["versions"] == {}
    assert transcript["active_leaf_id"] == tail["id"]

    # The next turn sees one merged reply.
    spy = _stub_harness(monkeypatch, _reply("a2"))
    chat_client.post(_url(sid, "/messages"), json={"content": "q2"})
    assert [(m.role, m.content) for m in spy.history] == [
        ("user", "q1"),
        ("assistant", "cut off and the rest"),
        ("user", "q2"),
    ]
    assert spy.kwargs["continue_from_assistant"] is False


def test_continue_only_answers_for_a_truncated_active_leaf(
    chat_client, monkeypatch
) -> None:
    sid, q1, a1 = _truncated_session(chat_client, monkeypatch)
    # A finished reply (end_turn) on a second question is not continuable,
    # and neither is the earlier truncated one that is no longer the leaf.
    _stub_harness(monkeypatch, _reply("done"))
    chat_client.post(_url(sid, "/messages"), json={"content": "q2"})
    finished = _transcript(chat_client, sid)["messages"][-1]["id"]
    other = _new_session(chat_client)["id"]
    chat_client.post(_url(other, "/messages"), json={"content": "elsewhere"})
    foreign = _transcript(chat_client, other)["messages"][0]["id"]

    for target in (a1, q1, finished, foreign, 99999):
        response = chat_client.post(_url(sid, f"/messages/{target}/continue"))
        assert response.status_code == 404, target
        assert response.json()["code"] == "not_found"
    assert chat_client.post(_url(99999, f"/messages/{a1}/continue")).status_code == 404
    # Nothing was written by any refusal.
    assert _transcript(chat_client, sid)["message_count"] == 4


def test_continue_is_refused_while_a_turn_runs(chat_client, monkeypatch) -> None:
    sid, q1, a1 = _truncated_session(chat_client, monkeypatch)
    holder = object()
    with use_project(chat_client.context()):
        lease = serve_chat._lease_key(sid)
        serve_chat._acquire(lease, holder)
    try:
        response = chat_client.post(_url(sid, f"/messages/{a1}/continue"))
        assert response.status_code == 409
        assert response.json()["code"] == "turn_in_progress"
    finally:
        serve_chat._release(lease, holder)
    assert _transcript(chat_client, sid)["message_count"] == 2


def test_one_turn_per_session_and_delete_or_branch_wait(
    chat_client, monkeypatch
) -> None:
    _stub_harness(monkeypatch, _reply("a1"))
    sid = _new_session(chat_client)["id"]
    chat_client.post(_url(sid, "/messages"), json={"content": "q1"})
    q1 = _transcript(chat_client, sid)["messages"][0]["id"]

    release, started = threading.Event(), threading.Event()
    _blocking_harness(monkeypatch, release, started)
    first: dict = {}
    thread = threading.Thread(
        target=lambda: first.setdefault(
            "response", chat_client.post(_url(sid, "/messages"), json={"content": "q2"})
        ),
        daemon=True,  # never hold the test process if an assertion below fails
    )
    thread.start()
    try:
        assert started.wait(5)
        assert _transcript(chat_client, sid)["streaming"] is True
        listed = {s["id"]: s for s in chat_client.get("/api/chat/sessions").json()}
        assert listed[sid]["streaming"] is True
        for method, path, body in (
            ("post", "/messages", {"content": "q3"}),
            ("post", f"/messages/{q1}/edit", {"content": "x"}),
            ("post", f"/messages/{q1}/regenerate", None),
            ("post", "/branch", {"message_id": q1}),
            ("delete", "", None),
        ):
            kwargs = {"json": body} if body is not None else {}
            response = getattr(chat_client, method)(_url(sid, path), **kwargs)
            assert response.status_code == 409, path
            assert response.json()["code"] == "turn_in_progress"
    finally:
        release.set()
        thread.join(10)
    assert [f["type"] for f in _frames(first["response"])] == ["text_delta", "done"]
    assert _transcript(chat_client, sid)["streaming"] is False
    assert chat_client.delete(_url(sid)).status_code == 204


def test_two_orgs_style_keys_do_not_collide() -> None:
    """The lease key is the resolved DB path, so slugs and session ids may repeat."""
    a = ("/data/projects/org-a/demo/whygraph.db", 1)
    b = ("/data/projects/org-b/demo/whygraph.db", 1)
    holder_a, holder_b = object(), object()
    serve_chat._acquire(a, holder_a)
    try:
        serve_chat._acquire(b, holder_b)  # no 409
        serve_chat._release(b, holder_b)
    finally:
        serve_chat._release(a, holder_a)
    assert a not in serve_chat._LEASES and b not in serve_chat._LEASES


def _job(client, sid: int, content: str = "q"):
    with use_project(client.context()):
        user_id = serve_chat._persist_user_message(sid, content)
        key = serve_chat._lease_key(sid)
        job = serve_chat.TurnJob(key, sid, "openai", "gpt-4o", start_id=user_id)
        serve_chat._acquire(key, job)
    return job, key


def test_a_never_read_response_still_finishes_and_releases(
    chat_client, monkeypatch
) -> None:
    _stub_harness(monkeypatch, _reply("written anyway"))
    sid = _new_session(chat_client)["id"]
    job, key = _job(chat_client, sid)
    with use_project(chat_client.context()):
        job.start()  # nobody iterates `job.frames()`
        _wait_released(key)
    assert _path(chat_client, sid)[-1] == ("assistant", "written anyway")


def test_a_worker_whose_error_flush_fails_still_releases(
    chat_client, monkeypatch
) -> None:
    def _fails(*, client, history, registry=None, **kwargs):
        raise LlmError("boom")
        yield  # pragma: no cover -- makes this a generator

    def _gone(*args, **kwargs):
        raise RuntimeError("project removed")

    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: object())
    monkeypatch.setattr(serve_chat, "run_turn", _fails)
    sid = _new_session(chat_client)["id"]
    job, key = _job(chat_client, sid)
    monkeypatch.setattr(serve_chat, "_persist_assistant_turn", _gone)
    with use_project(chat_client.context()):
        job.start()
        _wait_released(key)
    assert job not in serve_chat._LIVE_TURNS


def test_stop_aborts_the_stream_records_stopped_and_releases(
    chat_client, monkeypatch
) -> None:
    release, started = threading.Event(), threading.Event()
    _blocking_harness(monkeypatch, release, started)
    aborted = threading.Event()

    class _Client:
        def abort(self):
            aborted.set()
            raise OSError("socket already gone")  # swallowed by stop()

    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: _Client())
    sid = _new_session(chat_client)["id"]
    job, key = _job(chat_client, sid)
    with use_project(chat_client.context()):
        job.start()
        assert started.wait(5)
        job.stop()
        _wait_released(key)
    assert aborted.is_set()
    messages = _transcript(chat_client, sid)["messages"]
    assert (messages[-1]["role"], messages[-1]["error"]) == ("assistant", "Stopped.")
    # A send right after Stop goes through.
    _stub_harness(monkeypatch, _reply("next"))
    assert (
        chat_client.post(_url(sid, "/messages"), json={"content": "again"}).status_code
        == 200
    )


def test_stop_all_turns_cancels_live_turns(chat_client, monkeypatch) -> None:
    release, started = threading.Event(), threading.Event()
    _blocking_harness(monkeypatch, release, started)
    sid = _new_session(chat_client)["id"]
    job, key = _job(chat_client, sid)
    with use_project(chat_client.context()):
        job.start()
        assert started.wait(5)
        assert job in serve_chat._LIVE_TURNS
        serve_chat.stop_all_turns()
        _wait_released(key)
    assert job.cancel_event.is_set()
    assert _transcript(chat_client, sid)["messages"][-1]["error"] == "Stopped."


def test_a_silent_turn_carries_heartbeats(chat_client, monkeypatch) -> None:
    monkeypatch.setattr(serve_chat, "HEARTBEAT_SEC", 0.2)
    monkeypatch.setattr(serve_chat, "POLL_SEC", 0.05)

    def _slow(*, client, history, registry=None, **kwargs):
        time.sleep(1.0)
        yield from _reply("finally")

    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: object())
    monkeypatch.setattr(serve_chat, "run_turn", _slow)
    sid = _new_session(chat_client)["id"]
    response = chat_client.post(_url(sid, "/messages"), json={"content": "q"})
    assert ": heartbeat\n\n" in response.text
    assert [f["type"] for f in _frames(response)] == ["text_delta", "done"]


def test_effort_resolution_and_session_fields(chat_client, monkeypatch) -> None:
    use_project_config(
        chat_client,
        monkeypatch,
        Config(llm=_llm(*CHAT_PROVIDER_TAGS), chat=ChatConfig(effort="low")),
    )
    providers = chat_client.get("/api/chat/providers").json()
    assert {p["default_effort"] for p in providers} == {"low"}

    seen: list = []

    def _record(*, client, history, registry=None, effort=None, **kwargs):
        seen.append(effort)
        yield from _reply("a")

    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: object())
    monkeypatch.setattr(serve_chat, "run_turn", _record)

    session = _new_session(chat_client)
    sid = session["id"]
    assert session["effort"] is None and session["streaming"] is False
    chat_client.post(_url(sid, "/messages"), json={"content": "q1"})
    assert seen[-1] == "low"  # the [chat].effort default
    assert _transcript(chat_client, sid)["messages"][-1]["effort"] == "low"

    patched = chat_client.patch(_url(sid), json={"effort": "max"})
    assert patched.status_code == 200 and patched.json()["effort"] == "max"
    chat_client.post(_url(sid, "/messages"), json={"content": "q2"})
    assert seen[-1] == "max"  # the session's level wins

    # A PATCH without `effort` leaves it; an explicit null resets it.
    assert chat_client.patch(_url(sid), json={"title": "t"}).json()["effort"] == "max"
    assert chat_client.patch(_url(sid), json={"effort": None}).json()["effort"] is None
    chat_client.post(_url(sid, "/messages"), json={"content": "q3"})
    assert seen[-1] == "low"
    assert _transcript(chat_client, sid)["effort"] is None

    for bad in ("turbo", ""):
        response = chat_client.patch(_url(sid), json={"effort": bad})
        assert response.status_code == 400 and response.json()["code"] == "bad_effort"
    response = chat_client.post("/api/chat/sessions", json={"effort": "turbo"})
    assert response.status_code == 400 and response.json()["code"] == "bad_effort"
    assert _new_session(chat_client, effort="xhigh")["effort"] == "xhigh"


def test_a_continued_reply_is_one_message_next_turn(chat_client, monkeypatch) -> None:
    _stub_harness(
        monkeypatch,
        [
            TextDelta(text="Part"),
            RoundUsage(
                output_tokens=5,
                finish_reason="max_tokens",
                replay={"reasoning_content": "r1"},
            ),
            Continuation(attempt=1, finish_reason="max_tokens"),
            TextDelta(text=" two."),
            RoundUsage(
                output_tokens=3,
                finish_reason="end_turn",
                replay={"reasoning_content": "r2"},
            ),
            TurnDone("end_turn"),
        ],
    )
    sid = _new_session(chat_client)["id"]
    frames = _frames(chat_client.post(_url(sid, "/messages"), json={"content": "q1"}))
    assert {"type": "continuation", "attempt": 1} in frames
    rows = _transcript(chat_client, sid)["messages"]
    assert [(m["content"], m["finish_reason"]) for m in rows[1:]] == [
        ("Part", "max_tokens"),
        (" two.", "end_turn"),
    ]

    spy = _stub_harness(monkeypatch, _reply("ok"))
    chat_client.post(_url(sid, "/messages"), json={"content": "q2"})
    assistant = [m for m in spy.history if m.role == "assistant"]
    assert [m.content for m in assistant] == ["Part two."]
    assert assistant[0].replay == {"reasoning_content": "r1r2"}


def test_an_errored_continuation_is_not_merged(chat_client, monkeypatch) -> None:
    sid = _new_session(chat_client)["id"]
    _seed_rows(
        chat_client,
        sid,
        [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "Part", "finish_reason": "max_tokens"},
            {"role": "assistant", "content": " cut", "error": "Stopped."},
        ],
    )
    spy = _stub_harness(monkeypatch, _reply("ok"))
    chat_client.post(_url(sid, "/messages"), json={"content": "q2"})
    assert [m.content for m in spy.history if m.role == "assistant"] == ["Part", " cut"]


def test_new_frames_for_the_turn_endings(chat_client, monkeypatch) -> None:
    _stub_harness(
        monkeypatch,
        [
            RoundUsage(input_tokens=1, finish_reason="end_turn"),
            EmptyReply(),
            ContextTrimmed(rounds_elided=2),
            TextDelta(text="answer"),
            RoundUsage(finish_reason="end_turn"),
            TurnDone("end_turn"),
        ],
    )
    sid = _new_session(chat_client)["id"]
    frames = _frames(chat_client.post(_url(sid, "/messages"), json={"content": "q"}))
    assert {"type": "empty_reply"} in frames
    assert {"type": "context_trimmed", "rounds_elided": 2} in frames
    # The silent round (it reported tokens) is its own row, before the answer.
    rows = _transcript(chat_client, sid)["messages"]
    assert [m["content"] for m in rows] == ["q", "", "answer"]


def test_effort_unsupported_is_coded(chat_client, monkeypatch) -> None:
    def _rejected(*, client, history, registry=None, **kwargs):
        raise LlmError("output_config.effort: not supported", code="effort_unsupported")
        yield  # pragma: no cover -- makes this a generator

    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: object())
    monkeypatch.setattr(serve_chat, "run_turn", _rejected)
    sid = _new_session(chat_client)["id"]
    frames = _frames(chat_client.post(_url(sid, "/messages"), json={"content": "q"}))
    assert frames[-1]["code"] == "effort_unsupported"


def test_reasoning_is_framed_and_kept_per_round(chat_client, monkeypatch) -> None:
    from whygraph.services.llm.chat import ReasoningDelta

    call = ToolCall(id="c1", name="get_symbol", arguments={})
    _stub_harness(
        monkeypatch,
        [
            ReasoningDelta(text="look "),
            ReasoningDelta(text="it up"),
            RoundUsage(finish_reason="tool_use"),
            ToolCallStarted(call=call),
            ToolResultReady(call=call, result="{}"),
            ReasoningDelta(text="now answer"),
            TextDelta(text="Done."),
            RoundUsage(finish_reason="end_turn"),
            TurnDone("end_turn"),
        ],
    )
    sid = _new_session(chat_client)["id"]
    frames = _frames(chat_client.post(_url(sid, "/messages"), json={"content": "q"}))
    assert [f["text"] for f in frames if f["type"] == "reasoning_delta"] == [
        "look ",
        "it up",
        "now answer",
    ]
    rows = _transcript(chat_client, sid)["messages"]
    assert [(m["role"], m["reasoning"]) for m in rows] == [
        ("user", None),
        ("assistant", "look it up"),
        ("tool", None),
        ("assistant", "now answer"),
    ]


# ---------------------------------------------------------------------------
# Branch in new chat (plan 37 section 7.1.4)
# ---------------------------------------------------------------------------


def _no_provider(monkeypatch) -> None:
    """Make any attempt to build a chat client (a provider call) fail the test."""

    def _boom(*args, **kwargs):
        raise AssertionError("a fork must not build a chat client")

    monkeypatch.setattr(serve_chat, "make_chat_client", _boom)


def _rows(client, session_id: int) -> list[dict]:
    """Every stored row of a session, ascending, as plain dicts."""
    from whygraph.db.models import ChatMessage as ChatMessageRow

    with use_project(client.context()), project_session() as session:
        return [
            row.model_dump()
            for row in session.exec(
                select(ChatMessageRow)
                .where(ChatMessageRow.session_id == session_id)
                .order_by(ChatMessageRow.id)
            ).all()
        ]


def test_fork_copies_the_active_path_up_to_the_message(
    chat_client, monkeypatch
) -> None:
    from whygraph.services.llm.chat import ReasoningDelta

    call = ToolCall(id="c1", name="get_symbol", arguments={})
    _stub_harness(
        monkeypatch,
        [
            ReasoningDelta(text="think"),
            RoundUsage(finish_reason="tool_use", input_tokens=7, output_tokens=3),
            ToolCallStarted(call=call),
            ToolResultReady(call=call, result="{}"),
            TextDelta(text="a1"),
            RoundUsage(finish_reason="end_turn"),
            TurnDone("end_turn"),
        ],
    )
    source = _new_session(chat_client, effort="high")
    sid = source["id"]
    chat_client.post(_url(sid, "/messages"), json={"content": "q1"})
    _stub_harness(monkeypatch, _reply("a2"))
    chat_client.post(_url(sid, "/messages"), json={"content": "q2"})
    before = _transcript(chat_client, sid)
    ids = [m["id"] for m in before["messages"]]
    assert [m["role"] for m in before["messages"]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
    ]
    cut = ids[3]  # the last row of the first reply

    _no_provider(monkeypatch)
    response = chat_client.post(_url(sid, "/fork"), json={"message_id": cut})
    assert response.status_code == 201, response.text
    forked = response.json()
    assert forked["id"] != sid
    assert forked["title"] == f"{before['title']} (branch)"
    assert (forked["provider"], forked["model"], forked["effort"]) == (
        source["provider"],
        source["model"],
        "high",
    )
    assert forked["message_count"] == 4
    assert forked["streaming"] is False

    # The caller owns it, and the rows are fresh with a rebuilt parent chain.
    with use_project(chat_client.context()), project_session() as session:
        mine = session.get(ChatSessionRow, forked["id"])
        assert mine.owner_uid == session.get(ChatSessionRow, sid).owner_uid
        assert mine.owner_uid is not None
    copies = _rows(chat_client, forked["id"])
    originals = {r["id"]: r for r in _rows(chat_client, sid)}
    assert len(copies) == 4
    assert not {c["id"] for c in copies} & set(originals)
    assert copies[0]["parent_id"] is None
    assert [c["parent_id"] for c in copies[1:]] == [c["id"] for c in copies[:-1]]
    for copy, old_id in zip(copies, ids[:4]):
        old = originals[old_id]
        for column in serve_chat._COPIED_COLUMNS:
            assert copy[column] == old[column], column
    # The interesting columns really were there to copy.
    assert (copies[1]["reasoning"], copies[1]["input_tokens"]) == ("think", 7)
    assert copies[1]["tool_calls"] != "[]" and copies[2]["role"] == "tool"

    transcript = _transcript(chat_client, forked["id"])
    assert transcript["active_leaf_id"] == copies[-1]["id"]
    assert [(m["role"], m["content"]) for m in transcript["messages"]] == [
        (m["role"], m["content"]) for m in before["messages"][:4]
    ]

    # The source did not change, and the new chat is independent of it.
    assert _transcript(chat_client, sid) == before
    listed = {s["id"]: s for s in chat_client.get("/api/chat/sessions").json()}
    assert listed[forked["id"]]["message_count"] == 4


def test_fork_at_a_user_row_ends_with_the_question(chat_client, monkeypatch) -> None:
    _stub_harness(monkeypatch, _reply("a1"))
    sid = _new_session(chat_client)["id"]
    chat_client.post(_url(sid, "/messages"), json={"content": "q1"})
    _stub_harness(monkeypatch, _reply("a2"))
    chat_client.post(_url(sid, "/messages"), json={"content": "q2"})
    q2 = _transcript(chat_client, sid)["messages"][2]["id"]

    _no_provider(monkeypatch)
    forked = chat_client.post(_url(sid, "/fork"), json={"message_id": q2}).json()
    assert forked["message_count"] == 3
    assert _path(chat_client, forked["id"]) == [
        ("user", "q1"),
        ("assistant", "a1"),
        ("user", "q2"),
    ]


def test_fork_copies_only_the_active_version(chat_client, monkeypatch) -> None:
    _stub_harness(monkeypatch, _reply("a1"))
    sid = _new_session(chat_client)["id"]
    chat_client.post(_url(sid, "/messages"), json={"content": "q1"})
    q1 = _transcript(chat_client, sid)["messages"][0]["id"]
    # A second version of the reply, then the first one is no longer active.
    _stub_harness(monkeypatch, _reply("a1b"))
    chat_client.post(_url(sid, f"/messages/{q1}/regenerate"))
    transcript = _transcript(chat_client, sid)
    new_reply = transcript["messages"][1]["id"]
    old_reply = transcript["versions"][str(new_reply)]["siblings"][0]
    assert old_reply != new_reply

    _no_provider(monkeypatch)
    forked = chat_client.post(_url(sid, "/fork"), json={"message_id": new_reply}).json()
    assert _path(chat_client, forked["id"]) == [("user", "q1"), ("assistant", "a1b")]
    assert _transcript(chat_client, forked["id"])["versions"] == {}
    assert forked["message_count"] == 2

    # The superseded version is off the active path now.
    refused = chat_client.post(_url(sid, "/fork"), json={"message_id": old_reply})
    assert refused.status_code == 404
    assert refused.json()["code"] == "not_found"


def test_fork_refuses_a_message_off_the_path_or_not_the_sessions(
    chat_client, monkeypatch
) -> None:
    _stub_harness(monkeypatch, _reply("a1"))
    sid = _new_session(chat_client)["id"]
    chat_client.post(_url(sid, "/messages"), json={"content": "q1"})
    q1 = _transcript(chat_client, sid)["messages"][0]["id"]
    _stub_harness(monkeypatch, _reply("q1 edited"))
    chat_client.post(_url(sid, f"/messages/{q1}/edit"), json={"content": "q1b"})
    other = _new_session(chat_client)["id"]
    chat_client.post(_url(other, "/messages"), json={"content": "elsewhere"})
    foreign = _transcript(chat_client, other)["messages"][0]["id"]
    sessions_before = len(chat_client.get("/api/chat/sessions").json())

    _no_provider(monkeypatch)
    # The original question lost the path to the edit; the rest never belonged.
    for target in (q1, foreign, 99999):
        response = chat_client.post(_url(sid, "/fork"), json={"message_id": target})
        assert response.status_code == 404, target
        assert response.json()["code"] == "not_found"
    unknown = chat_client.post(_url(99999, "/fork"), json={"message_id": q1})
    assert unknown.status_code == 404
    # Nothing was created by any refusal.
    assert len(chat_client.get("/api/chat/sessions").json()) == sessions_before


def test_fork_is_refused_while_a_turn_holds_the_lease(chat_client, monkeypatch) -> None:
    _stub_harness(monkeypatch, _reply("a1"))
    sid = _new_session(chat_client)["id"]
    chat_client.post(_url(sid, "/messages"), json={"content": "q1"})
    q1 = _transcript(chat_client, sid)["messages"][0]["id"]
    holder = object()
    with use_project(chat_client.context()):
        lease = serve_chat._lease_key(sid)
        serve_chat._acquire(lease, holder)
    try:
        response = chat_client.post(_url(sid, "/fork"), json={"message_id": q1})
        assert response.status_code == 409
        assert response.json()["code"] == "turn_in_progress"
    finally:
        serve_chat._release(lease, holder)
    assert len(chat_client.get("/api/chat/sessions").json()) == 1
    # The lease is free again once the fork is done.
    assert (
        chat_client.post(_url(sid, "/fork"), json={"message_id": q1}).status_code == 201
    )
    assert _transcript(chat_client, sid)["streaming"] is False
