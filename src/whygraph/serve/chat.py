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

import contextvars
import json
import logging
import queue
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import col, delete, func, select

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
    run_turn,
)
from whygraph.chat.tools import ToolRegistry
from whygraph.core import Config, ConfigError, get_config
from whygraph.core.context import current_project
from whygraph.core.usage import BUDGET_EXCEEDED, UsageBlocked, set_scope_field
from whygraph.db import get_session
from whygraph.db.models import ChatMessage as ChatMessageRow
from whygraph.db.models import ChatSession as ChatSessionRow
from whygraph.services.llm import LlmError, LlmKeyMissing
from whygraph.services.llm.exceptions import is_unreachable
from whygraph.services.llm.chat import (
    CHAT_PROVIDERS,
    EFFORT_LEVELS,
    ChatMessage,
    ModelInfo,
    TextDelta,
    ToolCall,
    TurnDone,
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
    effort: str | None = None


class UpdateSessionBody(BaseModel):
    """Body for ``PATCH /api/chat/sessions/{id}``. Every field optional.

    Provider, model and effort are mutable so the header dropdowns can
    switch mid-conversation; the change applies from the next turn. An
    explicit ``"effort": null`` resets the session to the ``[chat].effort``
    default.
    """

    title: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None


class SendMessageBody(BaseModel):
    """Body for ``POST /api/chat/sessions/{id}/messages`` (and ``.../edit``)."""

    content: str


class BranchBody(BaseModel):
    """Body for ``POST /api/chat/sessions/{id}/branch``."""

    message_id: int


def _check_effort(effort: str | None) -> None:
    """400 ``bad_effort`` unless ``effort`` is unset or one of the levels."""
    if effort is not None and effort not in EFFORT_LEVELS:
        raise ServeError(
            400,
            f"{effort!r} is not an effort level; available: {EFFORT_LEVELS}",
            code="bad_effort",
        )


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _session_dict(row: ChatSessionRow, *, message_count: int | None = None) -> dict:
    """Serialize a session row for the sidebar / detail responses.

    ``streaming`` is true while a reply is being written in the session.
    """
    payload = {
        "id": row.id,
        "title": row.title,
        "provider": row.provider,
        "model": row.model,
        "effort": row.effort,
        "streaming": _is_streaming(_lease_key(row.id)),
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
        "finish_reason": row.finish_reason,
        "effort": row.effort,
        "parent_id": row.parent_id,
        "created_at": row.created_at,
    }


def _frame(payload: dict) -> str:
    """Encode one SSE frame: ``data: <json>\\n\\n``."""
    return f"data: {json.dumps(payload)}\n\n"


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


def _is_configured(provider: str, config: Config | None = None) -> bool:
    """Whether ``provider`` has a stored key in the merged config.

    Parameters
    ----------
    provider : str
        A chat provider tag.
    config : Config, optional
        The merged config to read; defaults to the bound project's.

    Returns
    -------
    bool
        ``True`` when ``[llm.<provider>].api_key`` is set. Ambient
        environment variables never count.
    """
    llm = (config or get_config()).llm
    section = getattr(llm, provider, None)
    return bool(section is not None and getattr(section, "api_key", None))


def default_chat_provider(config: Config) -> str | None:
    """The provider a new chat session starts on.

    Parameters
    ----------
    config : Config
        The merged config of the project.

    Returns
    -------
    str or None
        The configured Chat provider when it has a stored key, else the
        first of ``CHAT_PROVIDERS`` that has one, else ``None``.
    """
    try:
        configured = config.model_for("chat").provider
    except ConfigError:
        configured = None
    if configured and _is_configured(configured, config):
        return configured
    return next((p for p in CHAT_PROVIDERS if _is_configured(p, config)), None)


def _require_key(provider: str) -> None:
    """409 ``no_llm_key`` unless ``provider`` has a stored key (raised before any write)."""
    if not _is_configured(provider):
        raise ServeError(
            409,
            f"no stored key for the chat provider ({provider})",
            code="no_llm_key",
        )


@router.get("/providers")
def providers() -> list[dict]:
    """The four chat providers, each flagged configured and default or not.

    Keyless providers are listed with ``configured: false`` so the UI can
    show an existing session's own provider; the UI hides the rest. Exactly
    one entry (at most) has ``default: true``: where a new session starts.
    Every entry carries ``default_effort``, the merged ``[chat].effort``.
    """
    config = get_config()
    default = default_chat_provider(config)
    return [
        {
            "provider": provider,
            "configured": _is_configured(provider, config),
            "default_model": config.model_for("chat", provider=provider).model,
            "default": provider == default,
            # The merged `[chat].effort`: what a chat with no level runs at.
            "default_effort": config.chat.effort,
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
    _check_effort(body.effort)
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
    if body.provider:
        _require_key(body.provider)
    else:
        provider = default_chat_provider(get_config())
        if provider is None:
            raise ServeError(
                409, "no LLM key is configured for chat", code="no_llm_key"
            )
        default_model = get_config().model_for("chat", provider=provider).model
    model = body.model or default_model

    now = _now_iso()
    row = ChatSessionRow(
        title=(body.title or DEFAULT_TITLE)[:TITLE_MAX_CHARS],
        provider=provider,
        model=model,
        effort=body.effort,
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
    """One session plus its visible transcript, for replay after a restart.

    ``messages`` is the **active path**: root to ``active_leaf_id``, every
    row as stored (errors included - only the model's history is
    sanitized). ``versions`` gives each path row that has siblings its
    ``{index, count, siblings}``. ``message_count`` counts every row of the
    session, on any branch.
    """
    with get_session() as session:
        row = _require_session(session, session_id, _owner_filter(request))
        rows = _session_rows(session, session_id)
        leaf = _leaf_id(row, rows)
        path = _path_ids(rows, leaf)
        return {
            **_session_dict(row, message_count=len(rows)),
            "active_leaf_id": leaf,
            "versions": _versions(rows, path),
            "messages": [_message_dict(rows[i]) for i in path],
        }


@router.patch("/sessions/{session_id}")
def update_session(session_id: int, body: UpdateSessionBody, request: Request) -> dict:
    """Update a session's title, provider, model and/or effort.

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
            _require_key(body.provider)
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

        if "effort" in body.model_fields_set:
            _check_effort(body.effort)
            row.effort = body.effort

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
    parent delete. Refused (``409 turn_in_progress``) while a reply is being
    written: the lease is taken for the transaction.
    """
    with get_session() as session:
        _require_session(session, session_id, _owner_filter(request))
    key = _lease_key(session_id)
    holder = object()
    _acquire(key, holder)
    try:
        with get_session() as session:
            row = _require_session(session, session_id)
            session.execute(
                delete(ChatMessageRow).where(
                    col(ChatMessageRow.session_id) == session_id
                )
            )
            session.delete(row)
            session.commit()
    finally:
        _release(key, holder)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# The conversation tree
# ---------------------------------------------------------------------------


def _session_rows(session, session_id: int) -> dict[int, ChatMessageRow]:
    """Every row of one session, by id (tree queries never cross sessions)."""
    return {
        row.id: row
        for row in session.exec(
            select(ChatMessageRow).where(ChatMessageRow.session_id == session_id)
        ).all()
    }


def _path_ids(rows: dict[int, ChatMessageRow], leaf_id: int | None) -> list[int]:
    """The ids from the root to ``leaf_id``, following ``parent_id``.

    Stops at a missing parent or a parent whose id is not lower (parents are
    always written first), so corrupt data cannot loop.
    """
    path: list[int] = []
    current = leaf_id
    while current is not None and current in rows:
        if path and current >= path[-1]:
            break
        path.append(current)
        current = rows[current].parent_id
    path.reverse()
    return path


def _leaf_id(row: ChatSessionRow, rows: dict[int, ChatMessageRow]) -> int | None:
    """The session's active leaf, or its newest row when the leaf is unset."""
    if row.active_leaf_id is not None and row.active_leaf_id in rows:
        return row.active_leaf_id
    return max(rows) if rows else None


def _children(rows: dict[int, ChatMessageRow]) -> dict[int | None, list[int]]:
    """Child ids per parent id (``None`` holds the roots), ascending."""
    children: dict[int | None, list[int]] = defaultdict(list)
    for row_id in sorted(rows):
        children[rows[row_id].parent_id].append(row_id)
    return children


def _versions(rows: dict[int, ChatMessageRow], path: list[int]) -> dict[str, dict]:
    """``{"<id>": {index, count, siblings}}`` for each path row with siblings.

    ``index`` is 1-based and ``siblings`` ascending, so the UI can show
    ``‹ 2/3 ›`` and branch to a neighbour.
    """
    children = _children(rows)
    versions: dict[str, dict] = {}
    for row_id in path:
        siblings = children[rows[row_id].parent_id]
        if len(siblings) > 1:
            versions[str(row_id)] = {
                "index": siblings.index(row_id) + 1,
                "count": len(siblings),
                "siblings": siblings,
            }
    return versions


def _deepest(rows: dict[int, ChatMessageRow], start: int) -> int:
    """The deepest descendant of ``start``, taking the newest child each level."""
    children = _children(rows)
    current = start
    while children.get(current):
        current = children[current][-1]
    return current


# ---------------------------------------------------------------------------
# The chat turn
# ---------------------------------------------------------------------------


PERSISTED_REPLAY_KEYS = frozenset({"reasoning_content"})
"""The provider replay keys an assistant row keeps across turns.

DeepSeek's ``reasoning_content`` must be passed back on later turns, so it
is stored. Anthropic content blocks never are: the per-turn system prompt
re-render would invalidate their signatures. OpenRouter's
``reasoning_details`` are echoed within a turn only."""


def _persisted_replay(replay) -> str | None:
    """The JSON a row stores for a round's replay payload, or ``None``."""
    kept = {k: v for k, v in (replay or {}).items() if k in PERSISTED_REPLAY_KEYS}
    return json.dumps(kept) if kept else None


def _sanitize_history(rows: list[tuple[ChatMessage, str | None]]) -> list[ChatMessage]:
    """Drop the rows a provider must never see again from a loaded history.

    A row that was fine to show the user can still poison every later turn
    of the session: Anthropic rejects an empty non-final assistant message,
    and every provider rejects a tool call with no result or a result with
    no call. So, for the model's view only (the transcript route still shows
    every row):

    * a budget-stop row is dropped - its text is our notice, not model output;
    * an assistant row keeps only the tool calls that a tool row answers
      before the next user or assistant row, and a tool row whose call its
      assistant row did not announce is dropped;
    * an assistant row left with no text, no calls and no replay payload is
      dropped (an error before the first token, a round that reported only
      tokens, a "Stopped." row with nothing in it).

    An errored row that has text keeps it; its ``error`` never reaches the
    model, because only ``content`` is sent.

    Parameters
    ----------
    rows : list of (ChatMessage, str or None)
        The loaded messages in order, each with its row's ``error``.

    Returns
    -------
    list[ChatMessage]
        The history safe to send.
    """
    kept: list[tuple[ChatMessage, str | None]] = [
        (m, e) for m, e in rows if e != BUDGET_EXCEEDED
    ]
    history: list[ChatMessage] = []
    i = 0
    while i < len(kept):
        message, _error = kept[i]
        i += 1
        if message.role == "tool":
            continue  # reached only when no assistant row announced it
        if message.role != "assistant":
            history.append(message)
            continue
        results: list[ChatMessage] = []
        while i < len(kept) and kept[i][0].role == "tool":
            results.append(kept[i][0])
            i += 1
        announced = {c.id for c in message.tool_calls}
        results = [r for r in results if r.tool_call_id in announced]
        answered = {r.tool_call_id for r in results}
        calls = tuple(c for c in message.tool_calls if c.id in answered)
        if not (message.content or calls or message.replay):
            continue
        history.append(
            ChatMessage(
                role="assistant",
                content=message.content,
                tool_calls=calls,
                replay=message.replay,
            )
        )
        history.extend(results)
    return history


def _merge_continuations(
    rows: list[tuple[ChatMessage, str | None]],
) -> list[tuple[ChatMessage, str | None]]:
    """Join a reply that was continued after the output limit back into one.

    A continuation is written as its own row right after the truncated one,
    with no tool rows between them; the model saw (and must see again) one
    reply. A row that has an ``error`` is never merged.
    """
    merged: list[tuple[ChatMessage, str | None]] = []
    for message, error in rows:
        if merged:
            previous, previous_error = merged[-1]
            if (
                message.role == "assistant"
                and previous.role == "assistant"
                and not previous.tool_calls
                and error is None
                and previous_error is None
            ):
                merged[-1] = (
                    ChatMessage(
                        role="assistant",
                        content=previous.content + message.content,
                        tool_calls=message.tool_calls,
                        replay=_merge_replay(previous.replay, message.replay),
                    ),
                    None,
                )
                continue
        merged.append((message, error))
    return merged


def _merge_replay(first, second):
    """Concatenate two rows' persisted ``reasoning_content``, in order."""
    parts = [
        r["reasoning_content"]
        for r in (first, second)
        if r and "reasoning_content" in r
    ]
    return {"reasoning_content": "".join(parts)} if parts else None


def _load_history(session_id: int, start_id: int) -> list[ChatMessage]:
    """Read the path from the root to ``start_id`` as port :class:`ChatMessage`s.

    ``start_id`` is the row the new turn answers - the new user row for a
    send or an edit, the question itself for a regenerate - never the
    session's leaf: on a regenerate the leaf still points at the old reply
    until the first new row is written, and walking from it would send that
    reply as the conversation's last message.

    The system prompt is not among them — it is re-rendered per turn (see
    :func:`whygraph.chat.harness.build_system_prompt`). Continued replies
    are merged (:func:`_merge_continuations`), then the rows pass through
    :func:`_sanitize_history`, so an errored or orphaned row never reaches
    the provider.

    Rows are converted to port dataclasses **inside** the session: ``get_session``
    commits on exit, which expires every loaded attribute, so touching a row
    afterwards would raise ``DetachedInstanceError``.
    """
    history: list[tuple[ChatMessage, str | None]] = []
    with get_session() as session:
        rows = _session_rows(session, session_id)
        for row_id in _path_ids(rows, start_id):
            row = rows[row_id]
            calls = tuple(
                ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"])
                for c in json.loads(row.tool_calls)
            )
            history.append(
                (
                    ChatMessage(
                        role=row.role,  # type: ignore[arg-type] -- constrained on write
                        content=row.content,
                        tool_calls=calls,
                        tool_call_id=row.tool_call_id,
                        replay=json.loads(row.replay) if row.replay else None,
                    ),
                    row.error,
                )
            )
    return _sanitize_history(_merge_continuations(history))


def _append_row(session_id: int, message: ChatMessageRow, *, now: str) -> int:
    """Write one row as the session's new leaf and return its id.

    Exclusivity comes from the turn lease (:func:`_acquire`), so moving the
    leaf needs no compare-and-set.
    """
    with get_session() as session:
        row = _require_session(session, session_id)
        session.add(message)
        session.flush()
        row.active_leaf_id = message.id
        row.updated_at = now
        session.add(row)
        session.commit()
        session.refresh(message)
        return message.id


def _persist_user_message(
    session_id: int, content: str, *, edit_of: int | None = None
) -> int:
    """Write the user row and apply first-message titling.

    Persisted **before** the model is called so a provider failure still
    leaves the question in the transcript — the user can retry without
    retyping. A new message hangs under the session's leaf; an edit is a
    sibling of the row it edits (same parent) and never retitles the
    session. Called under the turn lease, so the leaf read here is current.
    """
    now = _now_iso()
    with get_session() as session:
        row = _require_session(session, session_id)
        rows = _session_rows(session, session_id)
        if edit_of is not None:
            parent_id = rows[edit_of].parent_id
        else:
            parent_id = _leaf_id(row, rows)
            if not rows and row.title == DEFAULT_TITLE:
                row.title = content.strip()[:TITLE_MAX_CHARS] or DEFAULT_TITLE
                session.add(row)
                session.commit()
    return _append_row(
        session_id,
        ChatMessageRow(
            session_id=session_id,
            parent_id=parent_id,
            role="user",
            content=content,
            created_at=now,
        ),
        now=now,
    )


def _persist_assistant_turn(
    session_id: int,
    *,
    parent_id: int | None,
    text: str,
    calls: list[ToolCall],
    usage: RoundUsage | None,
    provider: str,
    model: str,
    effort: str | None = None,
    error: str | None = None,
) -> int:
    """Write one assistant row (one provider round) and return its row id.

    ``provider`` / ``model`` are recorded on the row rather than read back
    from the session later: the session's pair can change between turns, so
    only the value in force *at this turn* is a truthful attribution. When
    the round reported the model it actually served, that wins over the
    requested ``model``. The row becomes the session's leaf.

    Parameters
    ----------
    parent_id : int or None
        The row this one follows: the previous row the turn wrote, or the
        question for its first row.
    usage : RoundUsage or None
        The round's usage; ``None`` for a round that never finished (an
        error or an abort), whose row then carries no tokens.
    effort : str or None, optional
        The effort level the turn ran at.
    error : str or None, optional
        Why the turn failed, if it did. Stored alongside whatever text did
        arrive, so a refresh replays the failure instead of showing a user
        message with no reply.
    """
    now = _now_iso()
    return _append_row(
        session_id,
        ChatMessageRow(
            session_id=session_id,
            parent_id=parent_id,
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
            replay=_persisted_replay(usage.replay) if usage else None,
            finish_reason=usage.finish_reason if usage else None,
            effort=effort,
            created_at=now,
        ),
        now=now,
    )


def _persist_tool_result(
    session_id: int, call: ToolCall, result: str, *, parent_id: int | None
) -> int:
    """Write one tool-result row as the session's leaf and return its id."""
    now = _now_iso()
    return _append_row(
        session_id,
        ChatMessageRow(
            session_id=session_id,
            parent_id=parent_id,
            role="tool",
            content=result,
            tool_call_id=call.id,
            created_at=now,
        ),
        now=now,
    )


# ---------------------------------------------------------------------------
# One turn per session: the lease
# ---------------------------------------------------------------------------

_LEASES: dict[tuple[str, int], object] = {}
"""Who holds each session: a running :class:`TurnJob`, or a branch / delete
for the length of its transaction. Keyed on the resolved project DB path,
because slugs repeat across orgs and session ids restart per project DB."""

_LEASES_LOCK = threading.Lock()

_LIVE_TURNS: set[TurnJob] = set()
"""Turns whose worker is running, for :func:`stop_all_turns` at shutdown."""


def _lease_key(session_id: int) -> tuple[str, int]:
    """The lease key for a session of the bound project."""
    return (str(Path(get_config().whygraph_db).resolve()), session_id)


def _acquire(key: tuple[str, int], holder: object) -> None:
    """Take the session's lease, or 409 ``turn_in_progress`` when it is held."""
    with _LEASES_LOCK:
        if key in _LEASES:
            raise ServeError(
                409,
                "a reply is still being written in this chat",
                code="turn_in_progress",
            )
        _LEASES[key] = holder


def _release(key: tuple[str, int], holder: object) -> None:
    """Give the lease back, only if ``holder`` still has it."""
    with _LEASES_LOCK:
        if _LEASES.get(key) is holder:
            del _LEASES[key]


def _is_streaming(key: tuple[str, int]) -> bool:
    """Whether the session's lease is held (a reply is being written)."""
    with _LEASES_LOCK:
        return key in _LEASES


POLL_SEC = 1.0
"""How long :meth:`TurnJob.frames` waits for a frame before yielding an
empty chunk, which bounds how late a disconnect (and so a Stop) is noticed."""

HEARTBEAT_SEC = 15.0
"""Silence after which the stream carries an SSE comment, so an edge proxy
(Cloudflare cuts silent streams at 100 s) keeps the connection."""

_END = object()
"""The sentinel the worker puts last on a turn's frame queue."""


class TurnJob:
    """One chat turn, run on its own worker thread.

    The handler starts the worker and returns at once; the response streams
    :meth:`frames`, which only reads the queue the worker fills. So the turn
    finishes (and its rows land) even when nobody reads the response, and
    the worker - never the response - releases the session's lease.

    Parameters
    ----------
    key : tuple[str, int]
        The lease key.
    session_id : int
        The session.
    provider, model : str
        The session's pair, recorded on every assistant row.
    start_id : int
        The row the turn answers (see :func:`_load_history`).
    effort : str or None
        The resolved effort level.

    Attributes
    ----------
    cancel_event : threading.Event
        Set by :meth:`stop`; the harness checks it.
    client : ChatClient or None
        The provider client, once built, for :meth:`stop` to abort.
    """

    def __init__(
        self,
        key: tuple[str, int],
        session_id: int,
        provider: str,
        model: str,
        *,
        start_id: int = 0,
        effort: str | None = None,
    ) -> None:
        self.key = key
        self.session_id = session_id
        self.provider = provider
        self.model = model
        self.start_id = start_id
        self.parent_id: int | None = start_id
        self.effort = effort
        self.cancel_event = threading.Event()
        self.client = None
        self.queue: queue.Queue = queue.Queue()

    def start(self) -> None:
        """Run :meth:`run` on a daemon thread, in a copy of this context.

        The handler runs in a copy of the request's context, where the
        portal bound the project, strict mode and the usage sink; the copy
        carries them to the worker.
        """
        _LIVE_TURNS.add(self)
        threading.Thread(
            target=contextvars.copy_context().run,
            args=(self.run,),
            name=f"chat-turn-{self.session_id}",
            daemon=True,
        ).start()

    def run(self) -> None:
        """Run the turn, persisting rows and queueing frames; always release."""
        try:
            for frame in _turn_frames(self):
                self.queue.put(frame)
        except Exception:  # noqa: BLE001 -- e.g. the project was removed mid-turn
            _log.warning(
                "chat turn for session %s ended without its rows",
                self.session_id,
                exc_info=True,
            )
        finally:
            # Release before the sentinel: a client that reads the response to
            # its end (the test client) may start the next turn at once.
            _release(self.key, self)
            _LIVE_TURNS.discard(self)
            self.queue.put(_END)

    def frames(self) -> Iterator[str]:
        """The response body: queued frames, heartbeats, and empty chunks.

        Every ``next()`` returns within about :data:`POLL_SEC` (an empty
        chunk writes nothing), so a disconnect is noticed promptly. When the
        response is abandoned the generator only signals :meth:`stop`: it may
        be finalized on the event-loop thread, so no join and no DB work
        happen here.
        """
        last = time.monotonic()
        try:
            while True:
                try:
                    item = self.queue.get(timeout=POLL_SEC)
                except queue.Empty:
                    if time.monotonic() - last >= HEARTBEAT_SEC:
                        last = time.monotonic()
                        yield ": heartbeat\n\n"
                    else:
                        yield ""
                    continue
                if item is _END:
                    return
                last = time.monotonic()
                yield item
        except GeneratorExit:
            self.stop()
            raise

    def stop(self) -> None:
        """Ask the turn to stop: set the cancel event and abort the stream."""
        self.cancel_event.set()
        abort = getattr(self.client, "abort", None)
        if abort is None:
            return
        try:
            abort()
        except Exception:  # noqa: BLE001 -- runs during generator finalization
            _log.warning("aborting the chat stream failed", exc_info=True)


def stop_all_turns() -> None:
    """Stop every running chat turn (portal shutdown)."""
    for job in list(_LIVE_TURNS):
        job.stop()


BUDGET_STOP_MESSAGE = (
    "This chat has reached its monthly budget. You can still read everything "
    "that's already generated."
)
"""The assistant row a :class:`~whygraph.chat.harness.BudgetStop` writes
(``error = "budget_exceeded"``), so a refresh replays why the turn ended."""

_ROUND_FLUSH_EVENTS: tuple[type, ...] = (
    TextDelta,
    RoundUsage,
    RoundLimit,
    Continuation,
    EmptyReply,
    BudgetStop,
)
"""Harness events that, once a round's :class:`RoundUsage` has arrived,
start something new and so flush that round's buffer as its row. The turn's
end flushes too. A new terminal or round-opening event joins this tuple."""


def _sse_error_code(exc: BaseException) -> str:
    """The ``code`` of an in-band ``error`` frame for a failed turn.

    ``no_llm_key`` when the adapter found no key for the provider,
    ``effort_unsupported`` when the model rejected the effort parameter,
    ``llm_unavailable`` when the provider could not be reached or the
    stream was cut (:func:`~whygraph.services.llm.exceptions.is_unreachable`),
    else ``provider_error``. ``tests/test_error_codes.py`` reads the
    literals returned here.
    """
    if isinstance(exc, LlmKeyMissing):
        return "no_llm_key"
    if getattr(exc, "code", None) == "effort_unsupported":
        return "effort_unsupported"
    if is_unreachable(exc):
        return "llm_unavailable"
    return "provider_error"


def _error_message(exc: BaseException) -> str:
    """The text a failed turn shows, with the provider's request id when known."""
    request_id = getattr(exc, "request_id", None)
    message = str(exc)
    return f"{message} (request id: {request_id})" if request_id else message


def _cause_names(exc: BaseException) -> str:
    """The class names along ``exc``'s ``__cause__`` chain, for a log line."""
    names: list[str] = []
    cause: BaseException | None = exc
    while cause is not None and len(names) < 10:
        names.append(type(cause).__name__)
        cause = cause.__cause__
    return " <- ".join(names)


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


def _turn_frames(job: TurnJob) -> Iterator[str]:
    """Run one turn on the worker, yielding SSE frames and persisting rows.

    Each provider round is flushed as **one** assistant row (plus its tool
    rows), so the stored transcript has the shape the harness saw:
    assistant(+calls) → tool results → assistant(+calls) → … → assistant,
    with every row carrying its own round's tokens, served model, stop
    reason and effort, and each row hanging under the previous one. A round
    ends at its :class:`RoundUsage`; the tool events after it still belong
    to it, and the next of :data:`_ROUND_FLUSH_EVENTS` (or the turn's end)
    writes it. A stopped turn keeps what arrived, marked ``"Stopped."``.
    """
    session_id, provider, model = job.session_id, job.provider, job.model
    # The portal bound a usage sink in the request task; this body runs in a
    # context copy on the worker, so the session goes on the shared scope.
    set_scope_field(chat_session_id=session_id)

    def _write_assistant(**fields) -> int:
        job.parent_id = _persist_assistant_turn(
            session_id,
            parent_id=job.parent_id,
            provider=provider,
            model=model,
            effort=job.effort,
            **fields,
        )
        return job.parent_id

    try:
        client = make_chat_client(provider, model=model)
    except LlmError as exc:
        frame, message = _error_frame(exc, provider, str(exc))
        # Same contract as the mid-turn handlers below: the failure is a row,
        # not just a frame, so a refresh still shows why nothing was answered.
        _write_assistant(text="", calls=[], usage=None, error=message)
        yield frame
        return
    job.client = client

    registry = ToolRegistry()
    history = _load_history(session_id, job.start_id)

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
        message_id = _write_assistant(
            text="".join(text_parts),
            calls=round_calls,
            usage=round_usage,
            error=error,
        )
        for call, result in round_results:
            job.parent_id = _persist_tool_result(
                session_id, call, result, parent_id=job.parent_id
            )
        text_parts = []
        round_calls = []
        round_results = []
        round_usage = None
        return message_id

    try:
        for event in run_turn(
            client=client,
            history=history,
            registry=registry,
            effort=job.effort,
            cancel=job.cancel_event,
        ):
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

            elif isinstance(event, Continuation):
                yield _frame({"type": "continuation", "attempt": event.attempt})

            elif isinstance(event, EmptyReply):
                yield _frame({"type": "empty_reply"})

            elif isinstance(event, ContextTrimmed):
                yield _frame(
                    {"type": "context_trimmed", "rounds_elided": event.rounds_elided}
                )

            elif isinstance(event, BudgetStop):
                # The previous round (if any) was flushed above; the budget
                # row is written after the loop, as the turn's last row.
                budget_stop = event

            elif isinstance(event, TurnDone):  # the end-of-turn marker, the total
                last_done = event

        final_message_id = _flush_round()
        if budget_stop is not None:
            final_message_id = _write_assistant(
                text=BUDGET_STOP_MESSAGE, calls=[], usage=None, error=BUDGET_EXCEEDED
            )
    except TurnCancelled:
        # Stop or a closed tab: keep what arrived, marked so a refresh shows it.
        _flush_round(error="Stopped.")
        return
    except LlmError as exc:
        if job.cancel_event.is_set():
            _flush_round(error="Stopped.")
            return
        _log.warning("chat turn failed for session %s: %s", session_id, exc)
        # Whatever arrived before the failure is worth keeping — the user can
        # see how far the model got, and the retry re-sends cleanly. The error
        # rides the same row so a refresh replays it.
        frame, message = _error_frame(exc, provider, _error_message(exc))
        _flush_round(error=message)
        yield frame
        return
    except Exception as exc:  # noqa: BLE001 -- must not surface as a hung stream
        if job.cancel_event.is_set():
            _flush_round(error="Stopped.")
            return
        _log.warning(
            "chat turn crashed for session %s: %s",
            session_id,
            _cause_names(exc),
            exc_info=True,
        )
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


def _resolve_effort(session_effort: str | None) -> str | None:
    """The effort a turn runs at: the session's, else ``[chat].effort``, else none."""
    return session_effort or get_config().chat.effort


def _preflight(
    request: Request,
    session_id: int,
    message_id: int | None = None,
    *,
    role: str | None = None,
    turn: bool = True,
) -> ChatSessionRow:
    """Check everything a turn or a branch needs, before anything is written.

    1. The session exists and is the caller's (404 otherwise, as everywhere).
    2. ``message_id``, when given, is a row of **this** session (and of
       ``role`` when given); 404 ``not_found`` otherwise.
    3. For a turn: an exhausted hard-stopped budget refuses it
       (:class:`~whygraph.core.usage.UsageBlocked`, which the portal answers
       with ``403 budget_exceeded``), and the provider needs a stored key.

    Returns
    -------
    ChatSessionRow
        The session row (expunged; read its plain columns only).
    """
    with get_session() as session:
        row = _require_session(session, session_id, _owner_filter(request))
        if message_id is not None:
            message = session.get(ChatMessageRow, message_id)
            if (
                message is None
                or message.session_id != session_id
                or (role is not None and message.role != role)
            ):
                raise ServeError(
                    404, f"message {message_id} not found", code="not_found"
                )
        session.expunge(row)
    if turn:
        ctx = current_project()
        if ctx is not None and ctx.llm_block == BUDGET_EXCEEDED:
            raise UsageBlocked(ctx.llm_block_scope)
        _require_key(row.provider)
    return row


def _start_turn(
    request: Request,
    session_id: int,
    *,
    content: str | None = None,
    edit_of: int | None = None,
    regenerate_of: int | None = None,
) -> StreamingResponse:
    """Start a turn: preflight, lease, user row, worker; then stream it.

    The order is the contract: nothing is written before the preflight and
    the lease, and any failure before the worker starts gives the lease
    back. From then on the worker owns it.
    """
    target = edit_of if edit_of is not None else regenerate_of
    row = _preflight(request, session_id, target, role="user")
    key = _lease_key(session_id)
    job = TurnJob(key, session_id, row.provider, row.model)
    _acquire(key, job)
    try:
        if regenerate_of is not None:
            # A sibling reply under the same question. The leaf moves when the
            # first row is written, so a turn that writes nothing leaves the
            # old reply on the path.
            job.start_id = job.parent_id = regenerate_of
        else:
            user_id = _persist_user_message(session_id, content or "", edit_of=edit_of)
            job.start_id = job.parent_id = user_id
        job.effort = _resolve_effort(row.effort)
        job.start()
    except BaseException:
        _release(key, job)
        _LIVE_TURNS.discard(job)
        raise
    return StreamingResponse(
        job.frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Defensive: some proxies buffer event-streams without this.
            "X-Accel-Buffering": "no",
        },
    )


def _content(body: SendMessageBody) -> str:
    """The message text, or 400 ``bad_content`` when it is blank."""
    content = body.content.strip()
    if not content:
        raise ServeError(400, "content must not be empty", code="bad_content")
    return content


@router.post("/sessions/{session_id}/messages")
def send_message(
    session_id: int, body: SendMessageBody, request: Request
) -> StreamingResponse:
    """Send a user message and stream the assistant's turn as SSE.

    The user row is persisted (and the session titled) **before** streaming
    starts, so the 404 for an unknown session and any config error still
    surface as real HTTP failures. Everything after the first frame is
    in-band — see the module docstring. A second turn while one is running
    is ``409 turn_in_progress``.

    A project an exhausted hard-stopped budget blocks for the caller (the
    bound context's ``llm_block``) is refused before anything is written:
    :class:`~whygraph.core.usage.UsageBlocked`, which the portal answers
    with ``403 budget_exceeded`` (M2f-2 plan section 4.7).
    """
    return _start_turn(request, session_id, content=_content(body))


@router.post("/sessions/{session_id}/messages/{message_id}/edit")
def edit_message(
    session_id: int, message_id: int, body: SendMessageBody, request: Request
) -> StreamingResponse:
    """Edit a user message: a new sibling version of it, answered as SSE.

    The original and everything after it stay reachable through the version
    arrows. An edit never retitles the session.
    """
    return _start_turn(request, session_id, content=_content(body), edit_of=message_id)


@router.post("/sessions/{session_id}/messages/{message_id}/regenerate")
def regenerate_reply(
    session_id: int, message_id: int, request: Request
) -> StreamingResponse:
    """Answer a user message again: a new sibling reply, streamed as SSE.

    ``message_id`` is the question. The session's current provider, model
    and effort apply. The old reply stays reachable through the arrows.
    """
    return _start_turn(request, session_id, regenerate_of=message_id)


@router.post("/sessions/{session_id}/branch")
def branch_session(session_id: int, body: BranchBody, request: Request) -> dict:
    """Show another version: move the leaf to the newest end under a row.

    The leaf becomes the deepest descendant of ``message_id``, taking the
    newest child at each level. Refused while a reply is being written.
    Returns the transcript.
    """
    _preflight(request, session_id, body.message_id, turn=False)
    key = _lease_key(session_id)
    holder = object()
    _acquire(key, holder)
    try:
        with get_session() as session:
            row = _require_session(session, session_id)
            row.active_leaf_id = _deepest(
                _session_rows(session, session_id), body.message_id
            )
            session.add(row)
            session.commit()
    finally:
        _release(key, holder)
    return get_transcript(session_id, request)


__all__ = ["router", "stop_all_turns"]
