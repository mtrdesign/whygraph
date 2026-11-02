"""SQLModel for the ``imported_actor`` table."""

from __future__ import annotations

from sqlalchemy import Text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class ImportedActor(WhygraphTable, table=True):
    """Display name of an actor that only exists in an imported bundle.

    Every uid from a bundle is stored as ``imported:<source uid>``, so an
    imported note never reads as confirmed by a real member of this project.
    Insert-if-absent: which bundle named a uid first does not matter.

    Attributes
    ----------
    uid : str
        ``imported:<source uid>``. Primary key.
    display_name : str
        Single-line name shown with an "imported" badge (at most 100
        characters).
    import_id : int
        The :class:`~whygraph.db.models.ProjectImport` that first named it.
    """

    uid: str = Field(primary_key=True, sa_type=Text)
    display_name: str = Field(sa_type=Text)
    import_id: int = Field(foreign_key="project_import.id")
