"""In-memory value object for one repository tag, plus its ``for-each-ref`` parser.

Tag names come from the repository (and, on a production clone, from
whoever can push a tag upstream), so they are untrusted strings: the
listing reads them as bytes, never passes one to ``git`` argv, and keeps
them only as data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

_SEP = "%00"
_PEELED_SHA = "%(if)%(*objectname)%(then)%(*objectname)%(else)%(objectname)%(end)"
_PEELED_TYPE = "%(if)%(*objecttype)%(then)%(*objecttype)%(else)%(objecttype)%(end)"


@dataclass(frozen=True, slots=True)
class GitTag:
    """One tag as listed by ``git for-each-ref``.

    Attributes
    ----------
    name : str
        The tag's short name (the ref with its namespace prefix removed).
        Bytes that are not UTF-8 are replaced, so any name stores.
    sha : str
        The commit the tag points at, peeled once for an annotated tag.
    tagged_at : str or None
        The tagger date of an annotated tag, else the commit's committer
        date, as a strict ISO 8601 timestamp; ``None`` when git has none.
    subject : str or None
        The first line of the tag message, or of the commit for a
        lightweight tag; ``None`` when empty.
    """

    name: str
    sha: str
    tagged_at: str | None
    subject: str | None

    FOR_EACH_REF_FORMAT: ClassVar[str] = (
        f"%(refname){_SEP}{_PEELED_SHA}{_SEP}{_PEELED_TYPE}{_SEP}"
        f"%(creatordate:iso-strict){_SEP}%(contents:subject){_SEP}"
    )
    """The ``--format`` every listing uses: five NUL-terminated fields per ref.

    The peeled SHA is ``%(if)%(*objectname)%(then)...%(end)``, not
    ``%(*objectname)%(objectname)``, which would concatenate both object
    names for an annotated tag.
    """

    _FIELDS: ClassVar[int] = 5

    @classmethod
    def from_for_each_ref(cls, stdout: bytes, prefix: str) -> tuple[GitTag, ...]:
        """Parse ``git for-each-ref --format=FOR_EACH_REF_FORMAT <prefix>`` output.

        Every field ends in NUL and git ends each record with a newline,
        which therefore starts the next record's first field; a newline
        inside a subject cannot split a record. Tags that do not point at
        a commit (a tagged tree or blob) are skipped.

        Parameters
        ----------
        stdout : bytes
            The raw listing.
        prefix : str
            The namespace listed, ending in ``/`` (e.g. ``"refs/tags/"``);
            stripped from each refname to give the tag name.

        Returns
        -------
        tuple[GitTag, ...]
            The tags, in listing order.
        """
        fields = [f.decode("utf-8", errors="replace") for f in stdout.split(b"\0")]
        tags: list[GitTag] = []
        # The last element is what follows the final NUL (git's newline).
        for i in range(0, len(fields) - cls._FIELDS + 1, cls._FIELDS):
            refname, sha, kind, date, subject = fields[i : i + cls._FIELDS]
            refname = refname.removeprefix("\n")
            name = refname.removeprefix(prefix)
            if kind != "commit" or not name or name == refname:
                continue
            tags.append(
                cls(
                    name=name,
                    sha=sha,
                    tagged_at=date or None,
                    subject=subject.strip() or None,
                )
            )
        return tuple(tags)
