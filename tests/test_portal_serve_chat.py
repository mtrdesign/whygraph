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
from types import SimpleNamespace
from typing import Iterator

import pytest
from sqlmodel import select

from test_portal_app import env  # noqa: F401 -- `env` is a fixture
from test_portal_serve_api import ScopedClient, portal_with_project, use_project_config
from whygraph.chat.harness import (
    RoundLimit,
    RoundUsage,
    ToolCallStarted,
    ToolResultReady,
)
from whygraph.core.config import ChatConfig, Config, LlmConfig, OpenAIConfig
from whygraph.core.context import use_project
from whygraph.db import get_session as project_session
from whygraph.db.models import ChatSession as ChatSessionRow
from whygraph.portal import db as portal_db
from whygraph.portal.models import User
from whygraph.serve import chat as serve_chat
from whygraph.services.llm.chat import (
    ModelInfo,
    TextDelta,
    ToolCall,
    ToolCallMade,
    TurnDone,
)
from whygraph.services.llm.exceptions import LlmError


@pytest.fixture
def chat_client(env: SimpleNamespace) -> Iterator[ScopedClient]:  # noqa: F811
    """The portal, scoped to ``demo``, with an isolated, migrated DB."""
    with portal_with_project(env, codegraph=False) as client:
        yield client


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
    use_project_config(
        chat_client,
        monkeypatch,
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
    use_project_config(
        chat_client,
        monkeypatch,
        Config(chat=ChatConfig(provider="deepseek", model="deepseek-reasoner")),
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
        chat_client, monkeypatch, Config(llm=LlmConfig(model="openai/gpt-4o-mini"))
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
    assert "not a chat provider" in response.json()["detail"]


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


def test_local_mode_writes_the_owner_but_filters_nothing(chat_client) -> None:
    """M2d-1 plan section 4.7: one user, so the list and the routes see every row."""
    created = _new_session(chat_client, title="mine")
    assert set(created) == {
        "id",
        "title",
        "provider",
        "model",
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

    # The question is still in the transcript, so a retry needs no retyping —
    # and the failure is a row, not just a frame, so a refresh replays it.
    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[-1]["content"] == ""
    assert "OPENROUTER_API_KEY" in messages[-1]["error"]


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


def test_client_disconnect_persists_a_stopped_marker(chat_client, monkeypatch) -> None:
    """Abandoning the stream mid-turn records ``"Stopped."`` beside the partial.

    Closing the frame iterator early is what the Stop button does to the
    generator, so the ``GeneratorExit`` handler is exercised directly rather
    than through TestClient (which always drains the body).
    """
    _stub_harness(
        monkeypatch,
        [TextDelta(text="partial"), TextDelta(text=" more"), TurnDone("stop")],
    )
    session = _new_session(chat_client)
    chat_client.post(
        f"/api/chat/sessions/{session['id']}/messages", json={"content": "hi"}
    )
    # Drop the assistant row the completed turn wrote; this test is about the
    # abort path, which starts from a fresh turn.
    session_id = session["id"]

    # Outside a request nothing binds the project; bind it like the route does.
    with use_project(chat_client.context()):
        frames = serve_chat._turn_frames(session_id, "openai", "gpt-4o")
        next(frames)  # consume the first text_delta, then walk away
        frames.close()

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

    messages = chat_client.get(f"/api/chat/sessions/{session['id']}").json()["messages"]
    assert messages[-1]["error"] == "RuntimeError: bug"


# ---------------------------------------------------------------------------
# Regression guard
# ---------------------------------------------------------------------------


def test_chat_router_does_not_shadow_explorer_routes(chat_client) -> None:
    """``.../chat`` is mounted beside the project's data routes, not over it."""
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
    use_project_config(
        chat_client,
        monkeypatch,
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
