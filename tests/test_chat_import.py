"""Importing a transfer bundle's chats (``whygraph.serve.chat.import_sessions``), plan 42 step 4.

Covers every rule of plan 42 section 5.4: session and message columns, the
typed JSON columns (``tool_calls``, ``replay``, ``grounding``), size skips of
whole sessions, the message tree and leaf remapping, origin keys (re-import
and the A -> B -> A round trip), and that an imported session reads back,
replays to the harness and deletes like a native one. Everything runs against
a file-backed SQLite DB bound as the current project; no portal.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlmodel import col, select
from starlette.requests import Request

from whygraph.chat.grounding import (
    MAX_SUFFIX_CHARS,
    references_suffix,
    with_references,
)
from whygraph.core.config import Config
from whygraph.core.context import ProjectContext, use_project
from whygraph.core.import_checks import ImportFormatError, clean_text
from whygraph.db import ensure_initialized, get_session
from whygraph.db.models import ChatMessage as ChatMessageRow
from whygraph.db.models import ChatSession as ChatSessionRow
from whygraph.db.models import ImportedChat, ProjectImport
from whygraph.portal.transfer_bundle import ChatMessageRecord, ChatSessionRecord
from whygraph.serve import chat
from whygraph.services.llm.chat import ToolCall

T0 = "2026-09-01T10:00:00+00:00"
T1 = "2026-09-01T10:00:05+00:00"
OWNER = "importer-uid"


@pytest.fixture
def project(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """A migrated WhyGraph DB bound as the current project, with one import logged."""
    root = tmp_path / "repo"
    root.mkdir()
    db = tmp_path / "whygraph.db"
    ctx = ProjectContext(slug="demo", root=root, config=Config(whygraph_db=db))
    with use_project(ctx):
        ensure_initialized()
        yield SimpleNamespace(root=root, db=db, import_id=_log_import())


def _log_import() -> int:
    with get_session() as session:
        row = ProjectImport(
            bundle_sha256="0" * 64,
            imported_by=OWNER,
            imported_at=T0,
            source_mode="local",
            source_label="org/demo",
            whygraph_version="x",
            counts="{}",
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def _key(n: int) -> str:
    return hashlib.sha256(f"origin-{n}".encode()).hexdigest()


def _session(sid: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": sid,
        "title": f"Chat {sid}",
        "provider": "anthropic",
        "model": "claude-x",
        "created_at": T0,
        "updated_at": T1,
        "active_leaf_id": None,
        "effort": None,
        "mode": None,
        "origin_key": _key(sid),
    }
    return {**base, **kw}


def _message(mid: int, sid: int, parent: int | None, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": mid,
        "session_id": sid,
        "parent_id": parent,
        "role": "user",
        "content": f"message {mid}",
        "tool_calls": [],
        "tool_call_id": None,
        "is_error": None,
        "input_tokens": None,
        "output_tokens": None,
        "provider": None,
        "model": None,
        "error": None,
        "replay": None,
        "finish_reason": None,
        "effort": None,
        "mode": None,
        "grounding": None,
        "reasoning": None,
        "created_at": T0,
    }
    return {**base, **kw}


def _assistant(mid: int, sid: int, parent: int | None, **kw: Any) -> dict[str, Any]:
    values = {
        "role": "assistant",
        "provider": "anthropic",
        "model": "claude-x",
        "input_tokens": 10,
        "output_tokens": 5,
        "finish_reason": "end_turn",
    }
    return _message(mid, sid, parent, **{**values, **kw})


def _tool(mid: int, sid: int, parent: int, call_id: str, **kw: Any) -> dict[str, Any]:
    values = {"role": "tool", "tool_call_id": call_id, "is_error": False}
    return _message(mid, sid, parent, **{**values, **kw})


def _conversation(sid: int, first: int) -> tuple[dict, list[dict]]:
    """A session with a tool round: user, assistant (call), tool, assistant."""
    call = {"id": "toolu_1", "name": "get_symbol", "arguments": {"q": "a.b"}}
    messages = [
        _message(first, sid, None, content="Where is a.b?"),
        _assistant(first + 1, sid, first, content="", tool_calls=[call]),
        _tool(first + 2, sid, first + 1, "toolu_1", content='{"file": "a.py"}'),
        _assistant(first + 3, sid, first + 2, content="In a.py."),
    ]
    return _session(sid, active_leaf_id=first + 3), messages


def _import(
    project: SimpleNamespace,
    sessions: list,
    messages: list,
    *,
    owner_uid: str | None = OWNER,
) -> chat.ChatImportResult:
    with get_session() as session:
        return chat.import_sessions(
            session,
            sessions,
            messages,
            owner_uid=owner_uid,
            import_id=project.import_id,
        )


def _refused(
    project: SimpleNamespace, sessions: list, messages: list
) -> ImportFormatError:
    with pytest.raises(ImportFormatError) as info:
        _import(project, sessions, messages)
    with get_session() as session:
        assert session.exec(select(ChatSessionRow)).all() == []
        assert session.exec(select(ChatMessageRow)).all() == []
    return info.value


def _sessions() -> list[ChatSessionRow]:
    with get_session() as session:
        rows = session.exec(
            select(ChatSessionRow).order_by(col(ChatSessionRow.id))
        ).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def _messages(session_id: int) -> list[ChatMessageRow]:
    with get_session() as session:
        rows = session.exec(
            select(ChatMessageRow)
            .where(ChatMessageRow.session_id == session_id)
            .order_by(col(ChatMessageRow.id))
        ).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def _request() -> Request:
    """A local-mode request: no host kind, so no owner filter."""
    return Request({"type": "http", "state": {}})


# ---------------------------------------------------------------------------
# Sessions, the tree and the owner
# ---------------------------------------------------------------------------


def test_a_conversation_is_inserted_with_a_rebuilt_tree(project) -> None:
    # A native session first, so the new ids differ from the source's.
    with get_session() as session:
        session.add(
            ChatSessionRow(
                title="mine", provider="openai", model="m", created_at=T0, updated_at=T0
            )
        )
        session.commit()
    meta, messages = _conversation(40, 100)

    result = _import(project, [meta], messages)

    assert result.counts() == {
        "sessions": 1,
        "matched_existing": 0,
        "skipped_oversize": 0,
        "messages": 4,
        "providers": ["anthropic"],
    }
    new = _sessions()[-1]
    assert result.chat_ids == {40: new.id}
    assert new.id != 40
    assert new.owner_uid == OWNER
    assert (new.title, new.provider, new.model) == ("Chat 40", "anthropic", "claude-x")
    assert (new.created_at, new.updated_at) == (T0, T1)
    rows = _messages(new.id)
    assert [r.role for r in rows] == ["user", "assistant", "tool", "assistant"]
    assert rows[0].parent_id is None
    assert [r.parent_id for r in rows[1:]] == [r.id for r in rows[:-1]]
    assert new.active_leaf_id == rows[-1].id
    assert json.loads(rows[1].tool_calls) == [
        {"id": "toolu_1", "name": "get_symbol", "arguments": {"q": "a.b"}}
    ]
    assert rows[2].tool_call_id == "toolu_1"
    assert rows[2].is_error is False
    assert (rows[3].input_tokens, rows[3].output_tokens) == (10, 5)
    assert all(r.rating is None for r in rows)
    with get_session() as session:
        link = session.get(ImportedChat, _key(40))
        assert (link.session_id, link.import_id) == (new.id, project.import_id)


def test_messages_go_in_by_source_id_and_branches_survive(project) -> None:
    """Input order does not matter; an edit's sibling keeps its parent."""
    messages = [
        _assistant(4, 1, 3, content="second answer"),
        _message(1, 1, None, content="q"),
        _message(3, 1, None, content="q edited"),
        _assistant(2, 1, 1, content="first answer"),
    ]
    result = _import(project, [_session(1, active_leaf_id=4)], messages)

    target = result.chat_ids[1]
    rows = _messages(target)
    by_content = {r.content: r for r in rows}
    assert [r.content for r in rows] == [
        "q",
        "first answer",
        "q edited",
        "second answer",
    ]
    assert by_content["first answer"].parent_id == by_content["q"].id
    assert by_content["second answer"].parent_id == by_content["q edited"].id
    assert by_content["q edited"].parent_id is None
    assert _sessions()[0].active_leaf_id == by_content["second answer"].id


def test_wire_models_are_accepted(project) -> None:
    meta, messages = _conversation(1, 1)
    messages[0]["grounding"] = references_suffix(
        [{"text": "a.b", "kind": "function", "qualified_name": "a.b", "file": "a.py"}]
    )
    messages[1]["replay"] = {"reasoning_content": "thinking"}

    result = _import(
        project,
        [ChatSessionRecord.model_validate(meta)],
        [ChatMessageRecord.model_validate(m) for m in messages],
    )

    assert result.messages == 4
    rows = _messages(result.chat_ids[1])
    assert rows[0].grounding == messages[0]["grounding"]
    assert json.loads(rows[1].tool_calls)[0]["arguments"] == {"q": "a.b"}
    assert json.loads(rows[1].replay) == {"reasoning_content": "thinking"}


def test_an_empty_session_is_inserted(project) -> None:
    result = _import(project, [_session(1)], [])
    assert result.counts()["sessions"] == 1
    assert result.messages == 0
    assert _sessions()[0].active_leaf_id is None


def test_the_owner_may_be_unset(project) -> None:
    _import(project, [_session(1)], [], owner_uid=None)
    assert _sessions()[0].owner_uid is None


def test_nothing_is_committed(project) -> None:
    meta, messages = _conversation(1, 1)
    with get_session() as session:
        chat.import_sessions(
            session, [meta], messages, owner_uid=OWNER, import_id=project.import_id
        )
        session.rollback()
    assert _sessions() == []
    with get_session() as session:
        assert session.exec(select(ChatMessageRow)).all() == []
        assert session.exec(select(ImportedChat)).all() == []


@pytest.mark.parametrize(
    ("sessions", "messages", "field", "index"),
    [
        pytest.param(
            [_session(1)], [_message(1, 2, None)], "message.session_id", 0, id="unknown"
        ),
        pytest.param(
            [_session(1)],
            [_message(1, 1, None), _message(2, 1, 3), _message(3, 1, 1)],
            "message.parent_id",
            1,
            id="forward-parent",
        ),
        pytest.param(
            [_session(1), _session(2)],
            [_message(1, 1, None), _message(2, 2, 1)],
            "message.parent_id",
            1,
            id="cross-session-parent",
        ),
        pytest.param(
            [_session(1)], [_message(1, 1, 1)], "message.parent_id", 0, id="self-parent"
        ),
        pytest.param(
            [_session(1), _session(2, active_leaf_id=1)],
            [_message(1, 1, None)],
            "session.active_leaf_id",
            1,
            id="foreign-leaf",
        ),
        pytest.param(
            [_session(1), _session(1, origin_key=_key(9))],
            [],
            "session.id",
            1,
            id="repeated-session",
        ),
        pytest.param(
            [_session(1), _session(2, origin_key=_key(1))],
            [],
            "session.origin_key",
            1,
            id="repeated-origin",
        ),
        pytest.param(
            [_session(1)],
            [_message(1, 1, None), _message(1, 1, None)],
            "message.id",
            1,
            id="repeated-message",
        ),
    ],
)
def test_tree_and_identity_violations_refuse(
    project, sessions, messages, field, index
) -> None:
    error = _refused(project, sessions, messages)
    assert (error.field, error.index) == (field, index)


# ---------------------------------------------------------------------------
# Session columns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "stored"),
    [
        ("Fix the\nlogin bug", "Fix the login bug"),
        ("  padded\t", "padded"),
        ("x" * 200, "x" * chat.TITLE_MAX_CHARS),
        ("   ", chat.DEFAULT_TITLE),
        ("a\x00b\u202ec", "a\ufffdb\ufffdc"),
    ],
)
def test_titles_are_cleaned_to_one_line_and_truncated(project, title, stored) -> None:
    _import(project, [_session(1, title=title)], [])
    assert _sessions()[0].title == stored


def test_an_unknown_mode_is_stored_as_null(project) -> None:
    _import(
        project,
        [_session(1, mode="retired-mode"), _session(2, mode="lead", effort="high")],
        [_message(1, 1, None, mode="retired-mode", effort="turbo")],
    )
    first, second = _sessions()
    assert first.mode is None
    assert (second.mode, second.effort) == ("lead", "high")
    row = _messages(first.id)[0]
    assert (row.mode, row.effort) == (None, None)


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"provider": "ollama"}, "session.provider"),
        ({"provider": None}, "session.provider"),
        ({"effort": "turbo"}, "session.effort"),
        ({"model": " "}, "session.model"),
        ({"model": "a\nb"}, "session.model"),
        ({"title": None}, "session.title"),
        ({"origin_key": "X" * 64}, "session.origin_key"),
        ({"created_at": "yesterday"}, "session.created_at"),
        ({"id": -1}, "session.id"),
        ({"id": True}, "session.id"),
        ({"active_leaf_id": 2**31 + 1}, "session.active_leaf_id"),
    ],
)
def test_session_format_violations_refuse(project, overrides, field) -> None:
    error = _refused(project, [_session(1), _session(2, **overrides)], [])
    assert (error.field, error.index) == (field, 1)


def test_a_future_timestamp_refuses(project) -> None:
    later = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    error = _refused(project, [_session(1, updated_at=later)], [])
    assert error.field == "session.updated_at"
    error = _refused(project, [_session(1)], [_message(1, 1, None, created_at=later)])
    assert error.field == "message.created_at"


def test_an_over_long_model_skips_the_session(project) -> None:
    result = _import(project, [_session(1, model="m" * 201), _session(2)], [])
    assert (result.sessions, result.skipped_oversize) == (1, 1)
    assert list(result.chat_ids) == [2]


# ---------------------------------------------------------------------------
# Message columns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "field"),
    [
        (_message(1, 1, None, role="system"), "message.role"),
        (_message(1, 1, None, role=None), "message.role"),
        (_message(1, 1, None, content=None), "message.content"),
        (_message(1, 1, None, content=7), "message.content"),
        (_tool(1, 1, None, "c1", tool_call_id=None), "message.tool_call_id"),
        (_message(1, 1, None, tool_call_id="c1"), "message.tool_call_id"),
        (_assistant(1, 1, None, tool_call_id="c1\n"), "message.tool_call_id"),
        (
            _message(
                1, 1, None, tool_calls=[{"id": "c", "name": "n", "arguments": {}}]
            ),
            "message.tool_calls",
        ),
        (_assistant(1, 1, None, tool_calls={"id": "c"}), "message.tool_calls"),
        (
            _assistant(
                1, 1, None, tool_calls=[{"id": "c", "name": "n", "arguments": "{}"}]
            ),
            "message.tool_calls.arguments",
        ),
        (
            _assistant(
                1, 1, None, tool_calls=[{"id": "c", "name": "n", "arguments": []}]
            ),
            "message.tool_calls.arguments",
        ),
        (
            _assistant(
                1, 1, None, tool_calls=[{"id": "c", "name": None, "arguments": {}}]
            ),
            "message.tool_calls.name",
        ),
        (
            _assistant(
                1,
                1,
                None,
                tool_calls=[{"id": "c", "name": "n", "arguments": {"x": float("nan")}}],
            ),
            "message.tool_calls.arguments",
        ),
        (_message(1, 1, None, is_error="yes"), "message.is_error"),
        (_message(1, 1, None, is_error=True), "message.is_error"),
        (_message(1, 1, None, input_tokens=-1), "message.input_tokens"),
        (_message(1, 1, None, output_tokens=2**31 + 1), "message.output_tokens"),
        (_assistant(1, 1, None, model="a\x00b"), "message.model"),
        (_assistant(1, 1, None, finish_reason="stop\u202e"), "message.finish_reason"),
    ],
)
def test_message_format_violations_refuse(project, message, field) -> None:
    error = _refused(project, [_session(1)], [_message(9, 1, None), message])
    assert error.field == field
    assert error.index == 1


def test_arguments_nested_past_ten_levels_refuse(project) -> None:
    def nested(levels: int) -> dict:
        value: dict = {}
        for _ in range(levels - 1):
            value = {"k": value}
        return value

    ok = {"id": "c", "name": "n", "arguments": nested(10)}
    _import(project, [_session(1)], [_assistant(1, 1, None, tool_calls=[ok])])
    deep = {"id": "c", "name": "n", "arguments": nested(11)}
    with pytest.raises(ImportFormatError) as info:
        _import(
            project,
            [_session(2, origin_key=_key(2))],
            [_assistant(2, 2, None, tool_calls=[deep])],
        )
    assert info.value.field == "message.tool_calls.arguments"


def test_control_characters_in_chat_text_are_replaced(project) -> None:
    messages = [
        _message(1, 1, None, content="q\x00\x1b[2J\tkept\r\n"),
        _assistant(
            2, 1, 1, content="", tool_calls=[{"id": "c", "name": "n", "arguments": {}}]
        ),
        _tool(3, 1, 2, "c", content="file\x07 contents\x7f\x85"),
        _assistant(4, 1, 3, content="a", reasoning="r\x01", error="e\x02"),
    ]
    result = _import(project, [_session(1)], messages)
    rows = _messages(result.chat_ids[1])
    assert rows[0].content == "q\ufffd\ufffd[2J\tkept\r\n"
    assert rows[2].content == "file\ufffd contents\ufffd\ufffd"
    assert (rows[3].reasoning, rows[3].error) == ("r\ufffd", "e\ufffd")


@pytest.mark.parametrize(
    "oversize",
    [
        {"content": "x" * (chat.IMPORT_CONTENT_MAX_CHARS + 1)},
        {"reasoning": "x" * (chat.IMPORT_TEXT_MAX_CHARS + 1)},
        {"error": "x" * (chat.IMPORT_TEXT_MAX_CHARS + 1)},
        {"model": "m" * 201},
        {"finish_reason": "f" * 201},
        {"replay": {"reasoning_content": "x" * (chat.IMPORT_TEXT_MAX_CHARS + 1)}},
        {
            "tool_calls": [
                {
                    "id": "c",
                    "name": "n",
                    "arguments": {"blob": "x" * chat.IMPORT_ARGUMENTS_MAX_BYTES},
                }
            ]
        },
        {"tool_calls": [{"id": "c" * 201, "name": "n", "arguments": {}}]},
    ],
)
def test_an_oversize_message_skips_its_whole_session(project, oversize) -> None:
    keep, keep_messages = _conversation(1, 1)
    messages = [
        *keep_messages,
        _message(10, 2, None, content="question"),
        _assistant(11, 2, 10, **oversize),
        _message(12, 2, 11, content="follow-up"),
    ]

    result = _import(project, [keep, _session(2, active_leaf_id=12)], messages)

    assert result.counts() == {
        "sessions": 1,
        "matched_existing": 0,
        "skipped_oversize": 1,
        "messages": 4,
        "providers": ["anthropic"],
    }
    assert list(result.chat_ids) == [1]
    assert len(_sessions()) == 1
    with get_session() as session:
        assert session.get(ImportedChat, _key(2)) is None


def test_an_oversize_record_is_still_format_checked(project) -> None:
    bad = _assistant(
        1, 1, None, content="x" * (chat.IMPORT_CONTENT_MAX_CHARS + 1), model="a\nb"
    )
    assert _refused(project, [_session(1)], [bad]).field == "message.model"


# ---------------------------------------------------------------------------
# replay and grounding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "replay",
    [
        {"reasoning_content": "r", "content": [{"type": "text", "text": "x"}]},
        {"content": [{"type": "thinking"}]},
        {},
        {"reasoning_content": 3},
        "reasoning",
    ],
)
def test_a_replay_other_than_reasoning_content_refuses(project, replay) -> None:
    error = _refused(project, [_session(1)], [_assistant(1, 1, None, replay=replay)])
    assert error.field.startswith("message.replay")


def test_a_replay_on_a_user_row_refuses(project) -> None:
    message = _message(1, 1, None, replay={"reasoning_content": "r"})
    assert _refused(project, [_session(1)], [message]).field == "message.replay"


def _grounding_entries() -> list[dict]:
    return [
        {
            "text": "TurnJob",
            "kind": "class",
            "qualified_name": "whygraph.serve.chat.TurnJob",
            "file": "src/whygraph/serve/chat.py",
            "rationale_cached": True,
            "notes": 2,
        },
        {"text": "#12", "kind": "pull_request", "number": 12, "state": "merged"},
        {"text": "nope", "resolved": False},
    ]


def test_valid_grounding_is_rerendered_byte_identical(project) -> None:
    stored = references_suffix(_grounding_entries())
    result = _import(project, [_session(1)], [_message(1, 1, None, grounding=stored)])
    assert _messages(result.chat_ids[1])[0].grounding == stored


def test_grounding_strings_are_cleaned(project) -> None:
    entries = [{"text": "a\u0007b", "kind": "file", "file": "x.py"}]
    stored = references_suffix(entries)
    result = _import(project, [_session(1)], [_message(1, 1, None, grounding=stored)])
    assert _messages(result.chat_ids[1])[0].grounding == references_suffix(
        [{"text": "a\ufffdb", "kind": "file", "file": "x.py"}]
    )


@pytest.mark.parametrize(
    "grounding",
    [
        "not json",
        "{}",
        "[]",
        "[1]",
        "[{}]",
        '[{"text": ["nested"]}]',
        '[{"text": {"a": 1}}]',
        '[{"score": 1.5}]',
        '[{"score": NaN}]',
        '[{"number": -1}]',
        "[" * 5_000,
        7,
    ],
)
def test_malformed_grounding_refuses(project, grounding) -> None:
    message = _message(1, 1, None, grounding=grounding)
    assert _refused(project, [_session(1)], [message]).field == "message.grounding"


@pytest.mark.parametrize(
    "grounding",
    [
        '[{"kind":"\\ud800"}]',
        '[{"\\udfff":"file"}]',
        '[{"text":"a\\ud83db","kind":"file"}]',
    ],
)
def test_a_lone_surrogate_in_grounding_refuses(project, grounding) -> None:
    """A ``\\uXXXX`` escape of half a pair decodes, but no database can store it."""
    wire = json.dumps({**_message(1, 1, None), "grounding": grounding})
    message = ChatMessageRecord.model_validate_json(wire)
    error = _refused(project, [_session(1)], [message])
    assert error.field == "message.grounding"
    assert "\\ud8" not in str(error) and "\\udf" not in str(error)


def test_a_surrogate_pair_in_grounding_is_kept(project) -> None:
    entries = [{"text": "\U0001f600", "kind": "file", "file": "x.py"}]
    stored = json.dumps(entries)  # ASCII escapes: a valid pair
    result = _import(project, [_session(1)], [_message(1, 1, None, grounding=stored)])
    assert _messages(result.chat_ids[1])[0].grounding == references_suffix(entries)


def test_a_lone_surrogate_in_tool_call_arguments_refuses(project) -> None:
    call = {"id": "c", "name": "n", "arguments": {"path": "a\ud800"}}
    message = _assistant(1, 1, None, tool_calls=[call])
    error = _refused(project, [_session(1)], [message])
    assert error.field == "message.tool_calls.arguments"


@pytest.mark.parametrize(
    "ch", ["\ud800", "\u2028", "\u2029", "\u200e", "\u200f", "\u061c"]
)
def test_the_multi_line_fast_path_matches_clean_text(ch) -> None:
    for value in (f"a{ch}b", f"a{ch}b\nc"):
        fast = chat._Checker("m").text(value, "content", max_chars=100, multiline=True)
        assert fast == clean_text(value, multiline=True, max_chars=100)
    assert "\ud800" not in chat._Checker("m").text(
        "a\ud800", "content", max_chars=100, multiline=True
    )


@pytest.mark.parametrize(
    "ch", ["\ud800", "\u2028", "\u2029", "\u200e", "\u200f", "\u061c"]
)
def test_single_line_chat_fields_refuse_separators_marks_and_surrogates(ch) -> None:
    with pytest.raises(ImportFormatError) as info:
        chat._Checker("m").text(f"a{ch}b", "model", max_chars=100, multiline=False)
    assert info.value.field == "m.model"


@pytest.mark.parametrize(
    ("title", "stored"),
    [
        ("a\u2028b\u2029c", "a b c"),
        ("a\u200eb\u200fc\u061cd", "a\ufffdb\ufffdc\ufffdd"),
        ("a\ud800b", "a\ufffdb"),
    ],
)
def test_titles_drop_separators_marks_and_surrogates(project, title, stored) -> None:
    _import(project, [_session(1, title=title)], [])
    assert _sessions()[0].title == stored


def test_grounding_on_a_non_user_row_refuses(project) -> None:
    stored = references_suffix(_grounding_entries())
    message = _assistant(1, 1, None, grounding=stored)
    assert _refused(project, [_session(1)], [message]).field == "message.grounding"


def test_grounding_over_the_suffix_cap_skips_the_session(project) -> None:
    entries = [
        {"text": f"name{i}", "kind": "file", "file": "f" * 90} for i in range(30)
    ]
    rendered = (
        "[" + ",\n ".join(json.dumps(e, separators=(",", ":")) for e in entries) + "]"
    )
    assert len(with_references("", rendered)) > MAX_SUFFIX_CHARS
    result = _import(project, [_session(1)], [_message(1, 1, None, grounding=rendered)])
    assert (result.sessions, result.skipped_oversize) == (0, 1)


# ---------------------------------------------------------------------------
# Origin keys (plan 42 D6)
# ---------------------------------------------------------------------------


def test_native_origin_key_formula() -> None:
    row = ChatSessionRow(
        id=7, title="t", provider="openai", model="m", created_at=T0, updated_at=T0
    )
    expected = hashlib.sha256(f"chat||7|{T0}".encode()).hexdigest()
    assert chat.native_origin_key(row) == expected
    row.owner_uid = "u1"
    expected = hashlib.sha256(f"chat|u1|7|{T0}".encode()).hexdigest()
    assert chat.native_origin_key(row) == expected


def test_a_reimport_matches_and_adds_nothing(project) -> None:
    meta, messages = _conversation(1, 1)
    first = _import(project, [meta], messages)

    second = _import(project, [meta], messages)

    assert second.counts() == {
        "sessions": 0,
        "matched_existing": 1,
        "skipped_oversize": 0,
        "messages": 0,
        "providers": [],
    }
    assert second.chat_ids == first.chat_ids
    assert len(_sessions()) == 1
    assert len(_messages(first.chat_ids[1])) == 4


def test_a_round_trip_matches_the_native_session(project) -> None:
    """A -> B -> A: the session comes home under its native key and adds nothing."""
    with get_session() as session:
        native = ChatSessionRow(
            title="home",
            provider="openai",
            model="m",
            created_at=T0,
            updated_at=T0,
            owner_uid="u-a",
        )
        session.add(native)
        session.commit()
        session.refresh(native)
        native_id, key = native.id, chat.native_origin_key(native)

    result = _import(
        project,
        [_session(55, origin_key=key)],
        [_message(1, 55, None, content="copy")],
    )

    assert (result.sessions, result.matched_existing, result.messages) == (0, 1, 0)
    assert result.chat_ids == {55: native_id}
    assert _messages(native_id) == []


def test_a_matched_session_is_still_format_checked(project) -> None:
    meta, messages = _conversation(1, 1)
    _import(project, [meta], messages)
    messages[0]["role"] = "system"
    with pytest.raises(ImportFormatError):
        _import(project, [meta], messages)


def test_matching_wins_over_an_oversize_skip(project) -> None:
    _import(project, [_session(1)], [])
    result = _import(project, [_session(1, model="m" * 201)], [])
    assert (result.matched_existing, result.skipped_oversize) == (1, 0)
    assert result.chat_ids == {1: _sessions()[0].id}


# ---------------------------------------------------------------------------
# An imported session behaves like a native one
# ---------------------------------------------------------------------------


def test_an_imported_session_replays_to_the_harness(project) -> None:
    """The transcript history the next turn sends is well formed."""
    stored = references_suffix(_grounding_entries())
    meta, messages = _conversation(1, 1)
    messages[0]["grounding"] = stored
    messages[1]["replay"] = {"reasoning_content": "thought"}
    result = _import(project, [meta], messages)
    target = result.chat_ids[1]
    leaf = _sessions()[0].active_leaf_id

    history = chat._load_history(target, leaf)

    assert [m.role for m in history] == ["user", "assistant", "tool", "assistant"]
    assert history[0].content == with_references("Where is a.b?", stored)
    assert history[1].tool_calls == (
        ToolCall(id="toolu_1", name="get_symbol", arguments={"q": "a.b"}),
    )
    assert history[1].replay == {"reasoning_content": "thought"}
    assert history[2].tool_call_id == "toolu_1"
    assert history[3].content == "In a.py."


def test_an_imported_session_reads_back_and_forks(project, monkeypatch) -> None:
    meta, messages = _conversation(1, 1)
    target = _import(project, [meta], messages).chat_ids[1]

    transcript = chat.get_transcript(target, _request())
    assert [m["role"] for m in transcript["messages"]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert transcript["messages"][1]["tool_calls"][0]["arguments"] == {"q": "a.b"}

    monkeypatch.setattr(chat, "_caller_uid", lambda request: OWNER)
    fork = chat.fork_session(
        target,
        chat.ForkBody(message_id=transcript["messages"][2]["id"]),
        _request(),
    )
    assert fork["message_count"] == 3


def test_delete_of_an_imported_session_succeeds(project) -> None:
    meta, messages = _conversation(1, 1)
    target = _import(project, [meta], messages).chat_ids[1]

    response = chat.delete_session(target, _request())

    assert response.status_code == 204
    assert _sessions() == []
    with get_session() as session:
        assert session.get(ImportedChat, _key(1)) is None
        assert session.exec(select(ChatMessageRow)).all() == []
    # Gone means a re-import brings it back.
    assert _import(project, [meta], messages).sessions == 1


def test_new_ids_follow_rows_written_after_the_import(project) -> None:
    """Explicit ids leave the tables' rowid sequence consistent."""
    meta, messages = _conversation(1, 1)
    target = _import(project, [meta], messages).chat_ids[1]
    with get_session() as session:
        row = ChatSessionRow(
            title="later", provider="openai", model="m", created_at=T1, updated_at=T1
        )
        session.add(row)
        session.flush()
        message = ChatMessageRow(
            session_id=row.id, role="user", content="x", created_at=T1
        )
        session.add(message)
        session.commit()
        assert row.id == target + 1
        assert message.id == max(m.id for m in _messages(target)) + 1


def test_every_copied_column_has_an_import_rule() -> None:
    """A column added to the fork copy must get an import rule too."""
    _session_id, message, _oversize = chat._check_message(
        _message(1, 1, None), datetime.now(timezone.utc)
    )
    assert tuple(message.values) == chat._COPIED_COLUMNS
