"""SQLModel for the ``chat_message`` table.

One row per message in a chat transcript, including **tool rows** — the
tool activity is part of the record, so reloading a session replays the
tool cards the user saw live, not just the prose.

System prompts are deliberately never persisted: they are re-rendered per
turn from live repo facts (see :mod:`whygraph.chat.harness`), so storing a
stale copy would only invite drift.

Notes
-----
:class:`whygraph.services.llm.chat.ChatMessage` shares this class's name.
Each is the natural name in its own layer, and the table name must derive
from the model class. Code holding both aliases *this* one::

    from whygraph.db.models import ChatMessage as ChatMessageRow
"""

from __future__ import annotations

from sqlalchemy import Text, text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class ChatMessage(WhygraphTable, table=True):
    """One persisted message in a chat transcript.

    Attributes
    ----------
    id : int or None
        Autoincrement primary key. Also the ``message_id`` the SSE
        ``done`` frame reports back for the completed assistant turn.
    session_id : int
        Owning :class:`~whygraph.db.models.ChatSession`; indexed because
        every read is "all messages for one session, in id order".
    role : str
        ``"user"``, ``"assistant"``, or ``"tool"``. ``"system"`` never
        appears (see the module notes).
    content : str
        Markdown for user / assistant rows; the JSON-serialized tool
        result for tool rows.
    tool_calls : str
        JSON list of ``{id, name, arguments}`` for an assistant row that
        requested tools; ``"[]"`` otherwise. JSON-as-text follows the
        convention documented in :mod:`whygraph.db.models`.
    tool_call_id : str or None
        On a tool row, the call id this result answers. ``None`` for
        every other role.
    input_tokens : int or None
        Prompt-token usage reported for an assistant turn, when the
        provider gives it.
    output_tokens : int or None
        Completion-token usage, same caveat.
    provider : str or None
        Provider that produced an assistant row. ``None`` for user and
        tool rows.
    model : str or None
        Model that produced an assistant row. ``None`` for user and tool
        rows.
    error : str or None
        Why this assistant turn failed — a provider error message, or
        ``"Stopped."`` for a user abort. ``None`` on success. Persisted so
        the failure survives a refresh instead of living only in the SSE
        stream; an error that arrives before the first token still writes a
        row (with empty ``content``) so no user message is left unanswered.
    created_at : str
        ISO-8601 UTC timestamp, stored as text.

    Notes
    -----
    ``provider`` / ``model`` are recorded **per row**, not just on the
    session, because the UI lets the model be switched mid-conversation.
    Without them a transcript containing two models would attribute every
    turn to whichever one happened to be selected last.
    """

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(index=True, foreign_key="chat_session.id")
    role: str = Field(sa_type=Text)
    content: str = Field(sa_type=Text)
    tool_calls: str = Field(
        default="[]",
        sa_type=Text,
        sa_column_kwargs={"server_default": text("'[]'")},
    )
    tool_call_id: str | None = Field(default=None, sa_type=Text)
    input_tokens: int | None = Field(default=None)
    output_tokens: int | None = Field(default=None)
    provider: str | None = Field(default=None, sa_type=Text)
    model: str | None = Field(default=None, sa_type=Text)
    error: str | None = Field(default=None, sa_type=Text)
    created_at: str = Field(sa_type=Text)
