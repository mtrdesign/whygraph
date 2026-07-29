"""SQLModel for the ``chat_session`` table.

One row per conversation in the ``whygraph serve`` Chat view. The session
owns the provider + model choice for its whole lifetime — a session is
not re-pointed at a different model mid-conversation, because the
transcript would then mix two models' tool-calling conventions. Starting
a new chat is the way to switch.

Sessions live in the ordinary WhyGraph DB (not a separate store) so they
survive a server restart with no extra wiring: ``create_app`` already
calls ``ensure_initialized()``, which runs Alembic to head.
"""

from __future__ import annotations

from sqlalchemy import Text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class ChatSession(WhygraphTable, table=True):
    """One chat conversation's identity and settings.

    Attributes
    ----------
    id : int or None
        Autoincrement primary key.
    title : str
        Display name in the session sidebar. Created as ``"New chat"``
        and replaced by the first user message (truncated) unless the
        user has renamed it — an explicit rename always wins thereafter.
    provider : str
        Chat provider tag: ``"anthropic"``, ``"openai"``, ``"deepseek"``,
        or ``"openrouter"``.
    model : str
        The concrete model id used, resolved at session-creation time so
        the transcript records what actually answered (not whatever the
        config happens to say later).
    created_at : str
        ISO-8601 UTC timestamp, stored as text (the convention every
        other WhyGraph timestamp column follows).
    updated_at : str
        Bumped on every message write; the session list orders by this so
        the most recently active conversation sits on top.
    """

    id: int | None = Field(default=None, primary_key=True)
    title: str = Field(sa_type=Text)
    provider: str = Field(sa_type=Text)
    model: str = Field(sa_type=Text)
    created_at: str = Field(sa_type=Text)
    updated_at: str = Field(sa_type=Text)
