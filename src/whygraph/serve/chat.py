"""The ``/api/chat/*`` routes — session CRUD plus the streaming turn.

Every handler is a **sync** ``def``, same contract as
:mod:`whygraph.serve.routes`: FastAPI runs it in the threadpool, so each
request gets its own thread and its own ``get_session()``. The streaming
endpoint is a sync *generator* wrapped in a ``StreamingResponse`` — FastAPI
iterates sync generators in the threadpool too, so SSE arrives without
introducing async anywhere, without ``sse-starlette``, and without a
WebSocket.

Two consequences of streaming shape the error contract:

* **HTTP status is committed before the first token.** A provider failure
  mid-stream therefore cannot be a 4xx/5xx; it is an in-band ``error``
  frame, and the frontend treats that as terminal.
* **The client can vanish mid-turn.** Rows are persisted *as each turn
  completes* rather than all at once at the end, so a disconnect loses at
  most the in-flight assistant turn and never leaves a user message with no
  visible reply.

This module is where the harness meets persistence: the harness yields
events and knows nothing about rows; this router owns every read and write.

Sessions are **per user** on a production host: each row records its
creator's ``users.uid`` (``owner_uid``), and the list and every
``/sessions/{id}`` route see only the caller's own rows - another user's
session (or an ownerless one) is a plain ``404``. Local mode has one user
and filters nothing. The caller comes from the scope the portal's guard
filled, not from a ``portal`` import, so ``serve/`` stays below the portal.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import col, func, select

from whygraph.chat.harness import (
    BudgetStop,
    RoundLimit,
    RoundUsage,
    ToolCallStarted,
    ToolResultReady,
    run_turn,
)
from whygraph.chat.tools import ToolRegistry
from whygraph.core import ConfigError, get_config
from whygraph.core.context import current_project
from whygraph.core.usage import BUDGET_EXCEEDED, UsageBlocked, set_scope_field
from whygraph.db import get_session
from whygraph.db.models import ChatMessage as ChatMessageRow
from whygraph.db.models import ChatSession as ChatSessionRow
from whygraph.services.llm import LlmError, LlmKeyMissing
from whygraph.services.llm.chat import (
    CHAT_PROVIDERS,
    ChatMessage,
    ModelInfo,
    TextDelta,
    ToolCall,
    TurnDone,
    chat_provider_env_var,
    fallback_models,
    make_chat_client,
)

from .errors import ServeError

_log = logging.getLogger(__name__)

router = APIRouter()

DEFAULT_TITLE = "New chat"
"""Title a session is created with, and the sentinel that lets the first
user message replace it. An explicit rename always wins thereafter."""

TITLE_MAX_CHARS = 80

DISPLAY_RESULT_CHARS = 2000
"""Tool results are truncated again for the SSE frame: the model sees up to
30 KB, but a tool card only ever shows a preview, and shipping 30 KB per
call down the wire would stall the stream for no benefit."""


def _now_iso() -> str:
    """UTC ISO-8601 at second resolution — the schema-wide timestamp shape."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class CreateSessionBody(BaseModel):
    """Body for ``POST /api/chat/sessions``. Every field optional."""

    provider: str | None = None
    model: str | None = None
    title: str | None = None


class UpdateSessionBody(BaseModel):
    """Body for ``PATCH /api/chat/sessions/{id}``. Every field optional.

    Provider and model are mutable so the header dropdowns can switch
    models mid-conversation; the change applies from the next turn.
    """

    title: str | None = None
    provider: str | None = None
    model: str | None = None


class SendMessageBody(BaseModel):
    """Body for ``POST /api/chat/sessions/{id}/messages``."""

    content: str


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _session_dict(row: ChatSessionRow, *, message_count: int | None = None) -> dict:
    """Serialize a session row for the sidebar / detail responses."""
    payload = {
        "id": row.id,
        "title": row.title,
        "provider": row.provider,
        "model": row.model,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if message_count is not None:
        payload["message_count"] = message_count
    return payload


def _message_dict(row: ChatMessageRow) -> dict:
    """Serialize a message row, decoding its ``tool_calls`` JSON.

    Tool rows are included on purpose: replaying a session has to re-render
    the tool cards the user saw live, not just the prose.
    """
    try:
        tool_calls = json.loads(row.tool_calls)
    except json.JSONDecodeError:  # pragma: no cover -- we always write valid JSON
        tool_calls = []
    return {
        "id": row.id,
        "role": row.role,
        "content": row.content,
        "tool_calls": tool_calls,
        "tool_call_id": row.tool_call_id,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "provider": row.provider,
        "model": row.model,
        "error": row.error,
        "created_at": row.created_at,
    }


def _frame(payload: dict) -> str:
    """Encode one SSE frame: ``data: <json>\\n\\n``."""
    return f"data: {json.dumps(payload)}\n\n"


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


def _is_configured(provider: str) -> bool:
    """Whether ``provider`` has a key, from config or the environment."""
    llm = get_config().llm
    section = getattr(llm, provider, None)
    if section is not None and getattr(section, "api_key", None):
        return True
    env_var = chat_provider_env_var(provider)
    return bool(env_var and os.environ.get(env_var))


@router.get("/providers")
def providers() -> list[dict]:
    """The four chat providers, each flagged configured or not.

    The picker shows unconfigured providers *disabled with their env-var
    name* rather than hiding them — "openrouter needs OPENROUTER_API_KEY"
    is actionable, a missing entry is a mystery.
    """
    config = get_config()
    return [
        {
            "provider": provider,
            "configured": _is_configured(provider),
            "default_model": config.model_for("chat", provider=provider).model,
            "env_var": chat_provider_env_var(provider),
        }
        for provider in CHAT_PROVIDERS
    ]


@router.get("/models")
def models(provider: str = Query(...)) -> dict:
    """List a provider's models for the model dropdown.

    Asks the provider rather than shipping a hardcoded catalogue, which
    would rot on every model release and could never cover OpenRouter.

    Listing is **allowed to fail** and still returns 200: a scoped
    Anthropic key can be valid for ``/messages`` yet 401 here, so a
    provider that chats perfectly well may refuse to enumerate. In that
    case the response carries the short built-in list with
    ``source: "fallback"`` and the provider's error, so the dropdown is
    never empty and the UI can say why it looks short.
    """
    if provider not in CHAT_PROVIDERS:
        raise ServeError(
            400,
            f"{provider!r} is not a chat provider; available: {CHAT_PROVIDERS}",
            code="bad_provider",
        )

    configured_model = get_config().model_for("chat", provider=provider).model
    try:
        client = make_chat_client(provider)
        listed = client.list_models()
        if not listed:
            raise LlmError("provider returned an empty model list")
        return {
            "provider": provider,
            "source": "live",
            "default_model": configured_model,
            "models": [{"id": m.id, "display_name": m.display_name} for m in listed],
        }
    except LlmError as exc:
        _log.info("live model listing failed for %s: %s", provider, exc)
        # Include the configured model even if it isn't in the static list —
        # it is by definition one the user intends to use.
        entries = list(fallback_models(provider))
        if configured_model and all(m.id != configured_model for m in entries):
            entries.insert(0, ModelInfo(configured_model, configured_model))
        return {
            "provider": provider,
            "source": "fallback",
            "default_model": configured_model,
            "error": str(exc),
            "models": [{"id": m.id, "display_name": m.display_name} for m in entries],
        }


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------


def _owner_filter(request: Request) -> str | None:
    """The caller's uid on a production host; ``None`` in local mode (never filtered)."""
    state = request.scope.get("state", {})
    if state.get("host_kind") in (None, "local"):
        return None
    return state["principal"].uid


def _caller_uid(request: Request) -> str | None:
    """The uid of whoever the guard resolved, in either mode (``None`` if nobody)."""
    principal = request.scope.get("state", {}).get("principal")
    return None if principal is None else principal.uid


@router.get("/sessions")
def list_sessions(request: Request) -> list[dict]:
    """The caller's sessions, newest activity first, with their message counts."""
    query = select(ChatSessionRow)
    owner = _owner_filter(request)
    if owner is not None:
        query = query.where(ChatSessionRow.owner_uid == owner)
    with get_session() as session:
        rows = session.exec(query.order_by(col(ChatSessionRow.updated_at).desc())).all()
        counts = dict(
            session.exec(
                select(ChatMessageRow.session_id, func.count()).group_by(
                    col(ChatMessageRow.session_id)
                )
            ).all()
        )
        return [_session_dict(row, message_count=counts.get(row.id, 0)) for row in rows]


@router.post("/sessions", status_code=201)
def create_session(request: Request, body: CreateSessionBody | None = None) -> dict:
    """Create a session, defaulting provider / model from ``model_for("chat")``.

    The resolved model is stored on the row rather than looked up per turn,
    so the transcript records what actually answered even if config changes
    later. The caller's uid is stored as ``owner_uid`` in both modes.
    """
    body = body or CreateSessionBody()
    if body.provider and body.provider not in CHAT_PROVIDERS:
        raise ServeError(
            400,
            f"{body.provider!r} is not a chat provider; available: {CHAT_PROVIDERS}",
            code="bad_provider",
        )
    try:
        provider, default_model = get_config().model_for(
            "chat", provider=body.provider or None
        )
    except ConfigError as exc:
        raise ServeError(400, str(exc), code="bad_config") from exc
    model = body.model or default_model

    now = _now_iso()
    row = ChatSessionRow(
        title=(body.title or DEFAULT_TITLE)[:TITLE_MAX_CHARS],
        provider=provider,
        model=model,
        created_at=now,
        updated_at=now,
        owner_uid=_caller_uid(request),
    )
    with get_session() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return _session_dict(row, message_count=0)


def _require_session(
    session, session_id: int, owner: str | None = None
) -> ChatSessionRow:
    """Fetch a session row or raise 404 - also when ``owner`` is set and differs."""
    row = session.get(ChatSessionRow, session_id)
    if row is None or (owner is not None and row.owner_uid != owner):
        raise ServeError(404, f"session {session_id} not found", code="not_found")
    return row


@router.get("/sessions/{session_id}")
def get_transcript(session_id: int, request: Request) -> dict:
    """One session plus its full transcript, for replay after a restart."""
    with get_session() as session:
        row = _require_session(session, session_id, _owner_filter(request))
        messages = session.exec(
            select(ChatMessageRow)
            .where(ChatMessageRow.session_id == session_id)
            .order_by(col(ChatMessageRow.id))
        ).all()
        return {
            **_session_dict(row, message_count=len(messages)),
            "messages": [_message_dict(m) for m in messages],
        }


@router.patch("/sessions/{session_id}")
def update_session(session_id: int, body: UpdateSessionBody, request: Request) -> dict:
    """Update a session's title, provider, and/or model.

    A title set here always wins over first-message titling. A provider or
    model change takes effect on the **next** turn — turns already in the
    transcript keep their own recorded attribution, so switching never
    rewrites history.
    """
    if body.provider is not None and body.provider not in CHAT_PROVIDERS:
        raise ServeError(
            400,
            f"{body.provider!r} is not a chat provider; available: {CHAT_PROVIDERS}",
            code="bad_provider",
        )

    with get_session() as session:
        row = _require_session(session, session_id, _owner_filter(request))

        if body.title is not None:
            title = body.title.strip()
            if not title:
                raise ServeError(400, "title must not be empty", code="bad_title")
            row.title = title[:TITLE_MAX_CHARS]

        if body.provider is not None and body.provider != row.provider:
            row.provider = body.provider
            # Switching provider invalidates the old model id, so resolve a
            # default for the new one unless this same request names a model.
            if body.model is None:
                row.model = get_config().model_for("chat", provider=body.provider).model

        if body.model is not None:
            model = body.model.strip()
            if not model:
                raise ServeError(400, "model must not be empty", code="bad_model")
            row.model = model

        row.updated_at = _now_iso()
        session.add(row)
        session.commit()
        session.refresh(row)
        return _session_dict(row)


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: int, request: Request) -> Response:
    """Delete a session and its messages.

    Messages are removed explicitly rather than by cascade: the FK has no
    ``ON DELETE`` clause, and ``foreign_keys=ON`` would otherwise reject the
    parent delete.
    """
    with get_session() as session:
        row = _require_session(session, session_id, _owner_filter(request))
        for message in session.exec(
            select(ChatMessageRow).where(ChatMessageRow.session_id == session_id)
        ).all():
            session.delete(message)
        session.delete(row)
        session.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# The chat turn
# ---------------------------------------------------------------------------


def _load_history(session_id: int) -> list[ChatMessage]:
    """Read a session's persisted rows as port :class:`ChatMessage`s.

    The system prompt is not among them — it is re-rendered per turn (see
    :func:`whygraph.chat.harness.build_system_prompt`).

    Rows are converted to port dataclasses **inside** the session: ``get_session``
    commits on exit, which expires every loaded attribute, so touching a row
    afterwards would raise ``DetachedInstanceError``.
    """
    history: list[ChatMessage] = []
    with get_session() as session:
        rows = session.exec(
            select(ChatMessageRow)
            .where(ChatMessageRow.session_id == session_id)
            .order_by(col(ChatMessageRow.id))
        ).all()
        for row in rows:
            calls = tuple(
                ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"])
                for c in json.loads(row.tool_calls)
            )
            history.append(
                ChatMessage(
                    role=row.role,  # type: ignore[arg-type] -- constrained on write
                    content=row.content,
                    tool_calls=calls,
                    tool_call_id=row.tool_call_id,
                )
            )
    return history


def _persist_user_message(session_id: int, content: str) -> int:
    """Write the user row and apply first-message titling.

    Persisted **before** the model is called so a provider failure still
    leaves the question in the transcript — the user can retry without
    retyping.
    """
    now = _now_iso()
    with get_session() as session:
        row = _require_session(session, session_id)
        is_first = (
            session.exec(
                select(func.count())
                .select_from(ChatMessageRow)
                .where(ChatMessageRow.session_id == session_id)
            ).one()
            == 0
        )
        if is_first and row.title == DEFAULT_TITLE:
            row.title = content.strip()[:TITLE_MAX_CHARS] or DEFAULT_TITLE
        row.updated_at = now
        session.add(row)
        message = ChatMessageRow(
            session_id=session_id,
            role="user",
            content=content,
            created_at=now,
        )
        session.add(message)
        session.commit()
        session.refresh(message)
        return message.id


def _persist_assistant_turn(
    session_id: int,
    *,
    text: str,
    calls: list[ToolCall],
    usage: RoundUsage | None,
    provider: str,
    model: str,
    error: str | None = None,
) -> int:
    """Write one assistant row (one provider round) and return its row id.

    ``provider`` / ``model`` are recorded on the row rather than read back
    from the session later: the session's pair can change between turns, so
    only the value in force *at this turn* is a truthful attribution. When
    the round reported the model it actually served, that wins over the
    requested ``model``.

    Parameters
    ----------
    usage : RoundUsage or None
        The round's usage; ``None`` for a round that never finished (an
        error or an abort), whose row then carries no tokens.
    error : str or None, optional
        Why the turn failed, if it did. Stored alongside whatever text did
        arrive, so a refresh replays the failure instead of showing a user
        message with no reply.
    """
    now = _now_iso()
    with get_session() as session:
        row = _require_session(session, session_id)
        row.updated_at = now
        session.add(row)
        message = ChatMessageRow(
            session_id=session_id,
            role="assistant",
            content=text,
            tool_calls=json.dumps(
                [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls]
            ),
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            provider=provider,
            model=(usage.model if usage and usage.model else model),
            error=error,
            created_at=now,
        )
        session.add(message)
        session.commit()
        session.refresh(message)
        return message.id


def _persist_tool_result(session_id: int, call: ToolCall, result: str) -> None:
    """Write one tool-result row."""
    with get_session() as session:
        session.add(
            ChatMessageRow(
                session_id=session_id,
                role="tool",
                content=result,
                tool_call_id=call.id,
                created_at=_now_iso(),
            )
        )
        session.commit()


BUDGET_STOP_MESSAGE = (
    "This chat has reached its monthly budget. You can still read everything "
    "that's already generated."
)
"""The assistant row a :class:`~whygraph.chat.harness.BudgetStop` writes
(``error = "budget_exceeded"``), so a refresh replays why the turn ended."""

_ROUND_FLUSH_EVENTS: tuple[type, ...] = (TextDelta, RoundUsage, RoundLimit, BudgetStop)
"""Harness events that, once a round's :class:`RoundUsage` has arrived,
start something new and so flush that round's buffer as its row. The turn's
end flushes too. A new terminal or round-opening event joins this tuple."""


_UNREACHABLE_CAUSES = frozenset({"APIConnectionError", "APITimeoutError"})
"""The SDK exceptions (``anthropic`` and ``openai`` name them alike) that mean
the provider could not be reached, rather than that it answered with an error."""


def _sse_error_code(exc: BaseException) -> str:
    """The ``code`` of an in-band ``error`` frame for a failed turn.

    ``no_llm_key`` when the adapter found no key for the provider,
    ``llm_unavailable`` when the provider could not be reached, else
    ``provider_error``. ``tests/test_error_codes.py`` reads the literals
    returned here.
    """
    if isinstance(exc, LlmKeyMissing):
        return "no_llm_key"
    cause: BaseException | None = exc
    while cause is not None:
        if type(cause).__name__ in _UNREACHABLE_CAUSES:
            return "llm_unavailable"
        cause = cause.__cause__
    return "provider_error"


def _error_frame(exc: BaseException, provider: str, message: str) -> tuple[str, str]:
    """The ``error`` frame for a failed turn and the text its row keeps.

    A missing key gets a neutral sentence instead of the adapter's, which
    names environment variables and ``whygraph.toml`` keys; the playground
    words it from ``code`` and ``provider`` (and the portal's mode).
    """
    code = _sse_error_code(exc)
    frame: dict[str, str] = {"type": "error", "code": code}
    if code == "no_llm_key":
        message = f"no API key is set for {provider}"
        frame["provider"] = provider
    frame["message"] = message
    return _frame(frame), message


def _turn_frames(session_id: int, provider: str, model: str) -> Iterator[str]:
    """Run one turn, yielding SSE frames and persisting rows as they land.

    Each provider round is flushed as **one** assistant row (plus its tool
    rows), so the stored transcript has the shape the harness saw:
    assistant(+calls) → tool results → assistant(+calls) → … → assistant,
    with every row carrying its own round's tokens and served model. A round
    ends at its :class:`RoundUsage`; the tool events after it still belong
    to it, and the next of :data:`_ROUND_FLUSH_EVENTS` (or the turn's end)
    writes it.
    """
    # The portal bound a usage sink in the request task; this body runs in
    # fresh context copies, so the session goes on the shared scope object.
    set_scope_field(chat_session_id=session_id)
    try:
        client = make_chat_client(provider, model=model)
    except LlmError as exc:
        frame, message = _error_frame(exc, provider, str(exc))
        # Same contract as the mid-turn handlers below: the failure is a row,
        # not just a frame, so a refresh still shows why nothing was answered.
        _persist_assistant_turn(
            session_id,
            text="",
            calls=[],
            usage=None,
            provider=provider,
            model=model,
            error=message,
        )
        yield frame
        return

    registry = ToolRegistry()
    history = _load_history(session_id)

    # One round's worth of buffered state. `round_usage` is set when the
    # round's provider call ends; its tool events still follow, and the next
    # flushing event (or the end of the turn) writes it as rows.
    text_parts: list[str] = []
    round_calls: list[ToolCall] = []
    round_results: list[tuple[ToolCall, str]] = []
    round_usage: RoundUsage | None = None
    last_done: TurnDone | None = None
    final_message_id: int | None = None
    budget_stop: BudgetStop | None = None

    def _flush_round(error: str | None = None) -> int | None:
        """Persist the buffered round: assistant row, then its tool rows.

        An ``error`` forces a row even with nothing buffered — a provider
        failure before the first token would otherwise leave the user
        message with no reply at all. So does a finished round that
        reported tokens, so the rows add up to the turn's total.
        """
        nonlocal text_parts, round_calls, round_results, round_usage
        reported = round_usage is not None and (
            round_usage.input_tokens or round_usage.output_tokens
        )
        if not (text_parts or round_calls or error or reported):
            round_usage = None
            return None
        message_id = _persist_assistant_turn(
            session_id,
            text="".join(text_parts),
            calls=round_calls,
            usage=round_usage,
            provider=provider,
            model=model,
            error=error,
        )
        for call, result in round_results:
            _persist_tool_result(session_id, call, result)
        text_parts = []
        round_calls = []
        round_results = []
        round_usage = None
        return message_id

    try:
        for event in run_turn(client=client, history=history, registry=registry):
            if round_usage is not None and isinstance(event, _ROUND_FLUSH_EVENTS):
                # The previous round is finished and something new starts.
                _flush_round()

            if isinstance(event, TextDelta):
                text_parts.append(event.text)
                yield _frame({"type": "text_delta", "text": event.text})

            elif isinstance(event, ToolCallStarted):
                round_calls.append(event.call)
                yield _frame(
                    {
                        "type": "tool_call",
                        "id": event.call.id,
                        "name": event.call.name,
                        "arguments": event.call.arguments,
                    }
                )

            elif isinstance(event, ToolResultReady):
                round_results.append((event.call, event.result))
                yield _frame(
                    {
                        "type": "tool_result",
                        "id": event.call.id,
                        "name": event.call.name,
                        "result": event.result[:DISPLAY_RESULT_CHARS],
                    }
                )

            elif isinstance(event, RoundUsage):
                # Closes the current round; not a frame of its own (the turn
                # total rides `done`).
                round_usage = event

            elif isinstance(event, RoundLimit):
                yield _frame({"type": "round_limit", "rounds": event.rounds})

            elif isinstance(event, BudgetStop):
                # The previous round (if any) was flushed above; the budget
                # row is written after the loop, as the turn's last row.
                budget_stop = event

            else:  # TurnDone - the end-of-turn marker, carrying the total
                last_done = event

        final_message_id = _flush_round()
        if budget_stop is not None:
            final_message_id = _persist_assistant_turn(
                session_id,
                text=BUDGET_STOP_MESSAGE,
                calls=[],
                usage=None,
                provider=provider,
                model=model,
                error=BUDGET_EXCEEDED,
            )
    except LlmError as exc:
        _log.warning("chat turn failed for session %s: %s", session_id, exc)
        # Whatever arrived before the failure is worth keeping — the user can
        # see how far the model got, and the retry re-sends cleanly. The error
        # rides the same row so a refresh replays it.
        frame, message = _error_frame(exc, provider, str(exc))
        _flush_round(error=message)
        yield frame
        return
    except GeneratorExit:
        # Client disconnected (Stop button, closed tab). Persist what we have
        # and re-raise so the server tears the response down cleanly.
        _flush_round(error="Stopped.")
        raise
    except Exception as exc:  # noqa: BLE001 -- must not surface as a hung stream
        _log.exception("chat turn crashed for session %s", session_id)
        message = f"{type(exc).__name__}: {exc}"
        _flush_round(error=message)
        yield _frame({"type": "error", "code": "provider_error", "message": message})
        return

    if budget_stop is not None:
        yield _frame(
            {
                "type": "budget_exceeded",
                "scope": budget_stop.scope,
                "message_id": final_message_id,
                "message": BUDGET_STOP_MESSAGE,
            }
        )
    yield _frame(
        {
            "type": "done",
            "message_id": final_message_id,
            "input_tokens": last_done.input_tokens if last_done else None,
            "output_tokens": last_done.output_tokens if last_done else None,
            "finish_reason": last_done.finish_reason if last_done else None,
        }
    )


@router.post("/sessions/{session_id}/messages")
def send_message(
    session_id: int, body: SendMessageBody, request: Request
) -> StreamingResponse:
    """Send a user message and stream the assistant's turn as SSE.

    The user row is persisted (and the session titled) **before** streaming
    starts, so the 404 for an unknown session and any config error still
    surface as real HTTP failures. Everything after the first frame is
    in-band — see the module docstring.

    A project an exhausted hard-stopped budget blocks for the caller (the
    bound context's ``llm_block``) is refused before anything is written:
    :class:`~whygraph.core.usage.UsageBlocked`, which the portal answers
    with ``403 budget_exceeded`` (M2f-2 plan section 4.7).
    """
    content = body.content.strip()
    if not content:
        raise ServeError(400, "content must not be empty", code="bad_content")

    with get_session() as session:
        row = _require_session(session, session_id, _owner_filter(request))
        provider, model = row.provider, row.model

    ctx = current_project()
    if ctx is not None and ctx.llm_block == BUDGET_EXCEEDED:
        raise UsageBlocked(ctx.llm_block_scope)

    _persist_user_message(session_id, content)

    return StreamingResponse(
        _turn_frames(session_id, provider, model),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Defensive: some proxies buffer event-streams without this.
            "X-Accel-Buffering": "no",
        },
    )


__all__ = ["router"]
