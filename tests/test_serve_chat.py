"""Integration tests for the ``/api/chat/*`` router (plan §7, §14).

Mirrors the ``test_serve_api.py`` fixture shape — including the
``_STATIC_DIR`` neutralization (so the tests don't depend on whether
``make playground`` has been run) and the ``_reset_engine`` bracketing.

The harness is monkeypatched **on the ``serve.chat`` module namespace**,
which is the house convention and the reason ``run_turn`` is imported at
module level there. That lets the streaming lifecycle — frame sequence,
which rows land, disconnect behaviour — be tested without a provider.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from whygraph import core
from whygraph.chat.harness import RoundLimit, ToolCallStarted, ToolResultReady
from whygraph.core.config import ChatConfig, Config, LlmConfig, OpenAIConfig
from whygraph.db import engine as db_engine
from whygraph.serve import chat as serve_chat
from whygraph.serve.app import create_app
from whygraph.services.llm.chat import ModelInfo, TextDelta, ToolCall, TurnDone
from whygraph.services.llm.exceptions import LlmError


@pytest.fixture
def chat_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A TestClient over ``create_app`` with an isolated, migrated DB."""
    wdb = tmp_path / "whygraph.db"
    monkeypatch.setattr(
        core,
        "_config",
        Config(whygraph_db=wdb, codegraph_db=tmp_path / "nope.db"),
    )
    monkeypatch.setattr("whygraph.serve.app._STATIC_DIR", tmp_path / "nostatic")
    db_engine._reset_engine()
    try:
        with TestClient(create_app(core._config)) as client:
            yield client
    finally:
        db_engine._reset_engine()
        core._reset_config()


def _new_session(client, **body) -> dict:
    response = client.post("/api/chat/sessions", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _frames(response) -> list[dict]:
    """Parse an SSE response body into decoded event payloads."""
    return [
        json.loads(block.removeprefix("data: "))
        for block in response.text.split("\n\n")
        if block.strip()
    ]


def _stub_harness(monkeypatch: pytest.MonkeyPatch, events) -> None:
    """Replace ``run_turn`` on the ``serve.chat`` namespace with a script."""

    def _fake_run_turn(*, client, history, registry=None, **kwargs):
        _fake_run_turn.history = history
        yield from events

    _fake_run_turn.history = ()
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
    assert [p["provider"] for p in payload] == [
        "anthropic",
        "openai",
        "deepseek",
        "openrouter",
    ]
    assert all(p["default_model"] for p in payload)
    assert {p["env_var"] for p in payload} == {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY",
    }


def test_provider_is_configured_from_config_or_env(
    chat_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        core,
        "_config",
        Config(llm=LlmConfig(openai=OpenAIConfig(api_key="sk-in-config"))),
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-in-env")

    flags = {
        p["provider"]: p["configured"]
        for p in chat_client.get("/api/chat/providers").json()
    }
    assert flags["openai"] is True  # from whygraph.toml
    assert flags["deepseek"] is True  # from the environment
    assert flags["anthropic"] is False
    assert flags["openrouter"] is False


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
    monkeypatch.setattr(
        core,
        "_config",
        Config(chat=ChatConfig(provider="deepseek", model="deepseek-reasoner")),
    )
    created = _new_session(chat_client)
    assert (created["provider"], created["model"]) == ("deepseek", "deepseek-reasoner")


def test_create_session_falls_back_to_the_provider_model(chat_client) -> None:
    """An empty ``[chat].model`` defers to ``[llm.<provider>].model``."""
    created = _new_session(chat_client, provider="openrouter")
    assert created["model"] == "openrouter/auto"


def test_create_session_rejects_a_non_chat_provider(chat_client) -> None:
    response = chat_client.post("/api/chat/sessions", json={"provider": "ollama"})
    assert response.status_code == 400
    assert "not a chat provider" in response.json()["detail"]


def test_unknown_session_is_404_everywhere(chat_client) -> None:
    assert chat_client.get("/api/chat/sessions/999").status_code == 404
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


def test_rename_rejects_an_empty_title(chat_client) -> None:
    session = _new_session(chat_client)
    response = chat_client.patch(
        f"/api/chat/sessions/{session['id']}", json={"title": "  "}
    )
    assert response.status_code == 400


def test_empty_message_is_rejected(chat_client) -> None:
    session = _new_session(chat_client)
    response = chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "   "}
    )
    assert response.status_code == 400


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
            ToolCallStarted(call=call),
            ToolResultReady(call=call, result=json.dumps({"count": 1})),
            TextDelta(text="Found it."),
            TurnDone("stop", 5, 6),
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
    assert messages[3]["output_tokens"] == 6


def test_tool_result_is_truncated_for_display_only(chat_client, monkeypatch) -> None:
    """The wire frame shows a preview; the stored row keeps the full result."""
    call = ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})
    big = json.dumps({"content": "y" * 5000})
    _stub_harness(
        monkeypatch,
        [
            ToolCallStarted(call=call),
            ToolResultReady(call=call, result=big),
            TextDelta(text="done"),
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
            ToolCallStarted(call=call),
            ToolResultReady(call=call, result="{}"),
            RoundLimit(rounds=3),
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
            ToolCallStarted(call=call),
            ToolResultReady(call=call, result="{}"),
            TextDelta(text="answer"),
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
        raise LlmError("provider not configured")

    monkeypatch.setattr(serve_chat, "make_chat_client", _boom)
    session = _new_session(chat_client, provider="openrouter")

    response = chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "hi"}
    )
    assert response.status_code == 200  # status is committed before streaming
    frames = _frames(response)
    assert [f["type"] for f in frames] == ["error"]
    assert "OPENROUTER_API_KEY" in frames[0]["message"]

    # The question is still in the transcript, so a retry needs no retyping.
    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    assert [m["role"] for m in messages] == ["user"]


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

    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "hi"),
        ("assistant", "I was saying"),
    ]


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


# ---------------------------------------------------------------------------
# Regression guard
# ---------------------------------------------------------------------------


def test_chat_router_does_not_shadow_explorer_routes(chat_client) -> None:
    """``/api/chat`` is mounted beside ``/api``, not over it."""
    # An Explorer route with no CodeGraph index still 503s (its own contract),
    # rather than 404-ing because the chat prefix swallowed it.
    assert chat_client.get("/api/tree").status_code == 503


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
    monkeypatch.setattr(
        core,
        "_config",
        Config(llm=LlmConfig(openai=OpenAIConfig(model="gpt-9-custom"))),
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
