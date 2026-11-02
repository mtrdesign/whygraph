"""SQLModel for the ``imported_chat`` table."""

from __future__ import annotations

from sqlalchemy import Text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class ImportedChat(WhygraphTable, table=True):
    """Maps a chat session's origin key to the session an import created.

    The origin key survives round trips, so re-importing the same session
    is skipped. ``delete_session`` removes the row with its session.

    Attributes
    ----------
    origin_key : str
        The session's origin key. Primary key.
    session_id : int
        The imported :class:`~whygraph.db.models.ChatSession`.
    import_id : int
        The :class:`~whygraph.db.models.ProjectImport` that created it.
    """

    origin_key: str = Field(primary_key=True, sa_type=Text)
    session_id: int = Field(foreign_key="chat_session.id")
    import_id: int = Field(foreign_key="project_import.id")
