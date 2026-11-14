"""SQLite-backed cache for ``whygraph_rationale_brief`` LLM output.

Keys a cached card by ``(path, line_start, line_end, provider, model)``
and invalidates by an *evidence fingerprint* — the sha256 of the sorted
commit SHAs returned by :func:`whygraph.mcp.evidence.collect_evidence`.
A new commit landing on the blamed lines changes the fingerprint and
forces a regeneration on the next call; the stale row is overwritten by
:func:`store_cached`.

Notes
-----
Callers key on :meth:`whygraph.core.config.Config.cache_identity`
(``"rationale"``), whose model is ``None`` unless one is *pinned* by
``[rationale].model`` or ``[llm].model`` (meaning *use whatever model the
provider defaults to*). The cache PK still needs a deterministic
``model`` token at lookup time — *before* the LLM call returns and
reports its actual model identity — so ``None`` is translated to the
literal string ``"default"`` via :func:`_model_key`. That is also the
1.x token, so a 1.x config (including one that pins only a deprecated
``[llm.<provider>].model``) keeps hitting its old rows, while changing
``[llm].model`` changes the key. The LLM-reported identity is persisted
separately in :attr:`RationaleCache.actual_model` so rows keyed under
``"default"`` keep their provenance.

The fingerprint is computed only over commit SHAs — PR/issue updates
that don't change the underlying commit set do not invalidate the cache.
That matches :func:`collect_evidence`'s own derivation: PRs and issues
are looked up *from* the commit set.

A ``cached_only`` call (``/whygraph-review``) is looser than a generating
one: :func:`lookup_exact` returns the key's row whatever its fingerprint,
and :func:`lookup_overlapping` the best row of the same file whose range
overlaps the request, so a card survives its symbol drifting by a few
lines. Both return a :class:`CachedCard`, which says whether it is stale.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Sequence

from sqlalchemy import delete, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, col, select

from whygraph.analyze import CommitEvidence, Item, ItemRef, Rationale
from whygraph.analyze.rationale import ITEM_BASES, REF_KINDS
from whygraph.core.import_checks import ImportFormatError, clean_text
from whygraph.db import get_session
from whygraph.db.models import RationaleCache

from .notes import _import_time, _lexical_path
from .targets import Target


_DEFAULT_MODEL_TAG = "default"

_PURGE_CHUNK = 500
"""Paths per ``DELETE ... WHERE path IN (...)`` (under SQLite's variable limit)."""


def _fingerprint(evidence: Sequence[CommitEvidence]) -> str:
    """sha256 of the newline-joined, sorted commit SHAs from ``evidence``.

    Sorting decouples the fingerprint from :func:`collect_evidence`'s
    return order, so a stable evidence set hashes to the same value
    regardless of timing or future ordering tweaks.
    """
    payload = "\n".join(sorted(item.commit.sha for item in evidence))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _model_key(config_model: str | None) -> str:
    """Translate a (possibly absent) configured model into a cache-key token."""
    return config_model if config_model else _DEFAULT_MODEL_TAG


def _now_iso() -> str:
    """UTC ISO-8601 timestamp at second resolution.

    Matches the timestamp shape used by :attr:`Commit.committed_at`,
    :attr:`Issue.created_at`, and the rest of the WhyGraph schema.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _decode_ref(value: object) -> ItemRef | None:
    """One stored ref, or ``None`` when it is not a ``{"kind", "id"}`` object."""
    if not isinstance(value, dict):
        return None
    kind, ref_id, url = value.get("kind"), value.get("id"), value.get("url")
    if kind not in REF_KINDS or not isinstance(ref_id, str) or not ref_id:
        return None
    return ItemRef(kind, ref_id, url if isinstance(url, str) and url else None)


def decode_item(value: object) -> Item | None:
    """One stored constraint or risk as an :class:`~whygraph.analyze.Item`.

    Parameters
    ----------
    value : object
        A decoded JSON list element: an object ``{"text", "basis", "refs",
        "quote"}`` (:meth:`Item.as_dict`), or a plain string from a row or
        bundle written before items carried sources.

    Returns
    -------
    Item or None
        A plain string becomes ``Item(text, "unknown", (), "")``. An object
        keeps its basis when it is a known one (else ``"unknown"``) and the
        refs that are well formed. ``None`` for anything else (an object
        without a string ``text``, a number), which the caller skips.
    """
    if isinstance(value, str):
        return Item(value, "unknown")
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        return None
    basis = value.get("basis")
    if basis not in ITEM_BASES:
        basis = "unknown"
    raw_refs = value.get("refs")
    refs = tuple(
        ref
        for ref in map(_decode_ref, raw_refs if isinstance(raw_refs, list) else [])
        if ref is not None
    )
    quote = value.get("quote")
    return Item(value["text"], basis, refs, quote if isinstance(quote, str) else "")


def decode_items(raw: str | None) -> tuple[Item, ...]:
    """A stored JSON list of constraints or risks as items (see :func:`decode_item`).

    Parameters
    ----------
    raw : str or None
        The JSON text of a ``constraints`` / ``risks`` column.

    Returns
    -------
    tuple[Item, ...]
        The items; empty for a missing or malformed column.
    """
    try:
        values = json.loads(raw) if raw else []
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(values, list):
        return ()
    return tuple(item for item in map(decode_item, values) if item is not None)


def _rationale_from_row(row: RationaleCache) -> Rationale:
    """Rebuild the :class:`~whygraph.analyze.Rationale` a cache row holds."""
    return Rationale(
        purpose=row.purpose,
        why=row.why,
        constraints=decode_items(row.constraints),
        tradeoffs=tuple(json.loads(row.tradeoffs)),
        risks=decode_items(row.risks),
        model=row.actual_model or row.model,
        provider=row.actual_provider or row.provider,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
    )


@dataclass(frozen=True, slots=True)
class CachedCard:
    """A cache row read without its fingerprint check (a ``cached_only`` lookup).

    Attributes
    ----------
    rationale : Rationale
        The cached card.
    cached_at : str
        When the row was cached (ISO-8601).
    line_start : int
        First line of the range the card was generated for.
    line_end : int
        Last line of that range.
    qualified_name : str or None
        The symbol the card was generated for, when it was targeted by one.
    evidence_fingerprint : str
        The row's fingerprint (see :func:`_fingerprint`).
    deferred_shas : str
        Space-joined SHAs the card was written without a description for;
        empty means the card is final.
    """

    rationale: Rationale
    cached_at: str
    line_start: int
    line_end: int
    qualified_name: str | None
    evidence_fingerprint: str
    deferred_shas: str = ""

    @classmethod
    def from_row(cls, row: RationaleCache) -> "CachedCard":
        """Detach a :class:`RationaleCache` row from its session."""
        return cls(
            rationale=_rationale_from_row(row),
            cached_at=row.cached_at,
            line_start=row.line_start,
            line_end=row.line_end,
            qualified_name=row.qualified_name,
            evidence_fingerprint=row.evidence_fingerprint,
            deferred_shas=row.deferred_shas,
        )

    def is_stale(self, evidence: Sequence[CommitEvidence]) -> bool:
        """Whether ``evidence`` (of the card's range) differs from what the card saw.

        Parameters
        ----------
        evidence : Sequence[CommitEvidence]
            The current evidence for the card's own range.

        Returns
        -------
        bool
            ``True`` when the fingerprint of ``evidence`` is not the row's,
            i.e. the lines got new commits since the card was written.
        """
        return _fingerprint(evidence) != self.evidence_fingerprint


def lookup_cached(
    target: Target,
    evidence: Sequence[CommitEvidence],
    provider: str,
    config_model: str | None,
) -> CachedCard | None:
    """Return the cached card for ``target`` or ``None`` on miss.

    A hit requires the row's ``evidence_fingerprint`` to match the
    fingerprint of ``evidence``; a row with a stale fingerprint is
    treated as a miss and will be overwritten by the next
    :func:`store_cached` call.

    Parameters
    ----------
    target
        Resolved target (path + line range + optional symbol name).
    evidence
        The evidence sequence as returned by
        :func:`whygraph.mcp.evidence.collect_evidence` — its commit SHAs
        drive the fingerprint check.
    provider
        LLM provider tag from ``Config.cache_identity("rationale")``.
    config_model
        Pinned model name (or ``None`` for the provider default);
        translated to the cache-key token via :func:`_model_key`.

    Returns
    -------
    CachedCard or None
        The row (its rationale, the ISO timestamp it was cached at and its
        ``deferred_shas``). ``None`` if no row matches the key or its
        fingerprint is stale.
    """
    fp = _fingerprint(evidence)
    model_key = _model_key(config_model)
    with get_session() as session:
        row = session.get(
            RationaleCache,
            (target.path, target.line_start, target.line_end, provider, model_key),
        )
        if row is None or row.evidence_fingerprint != fp:
            return None
        return CachedCard.from_row(row)


def lookup_exact(
    target: Target, provider: str, config_model: str | None
) -> CachedCard | None:
    """The row keyed exactly by ``target``, whatever its evidence fingerprint.

    For ``cached_only`` calls only: a stale row is still the rationale the
    lines had before their latest commits. A generating call must use
    :func:`lookup_cached`, which treats a stale row as a miss.

    Parameters
    ----------
    target : Target
        Resolved target; its path and range are the key.
    provider : str
        LLM provider tag from ``Config.cache_identity("rationale")``.
    config_model : str or None
        Pinned model name (or ``None``), as for :func:`lookup_cached`.

    Returns
    -------
    CachedCard or None
        The row, or ``None`` when the key has none.
    """
    model_key = _model_key(config_model)
    with get_session() as session:
        row = session.get(
            RationaleCache,
            (target.path, target.line_start, target.line_end, provider, model_key),
        )
        return None if row is None else CachedCard.from_row(row)


def lookup_overlapping(
    target: Target, provider: str, config_model: str | None
) -> CachedCard | None:
    """The best cached card of ``target``'s file whose range overlaps it.

    For ``cached_only`` calls only, when the exact key has no row: a
    symbol whose lines moved, or a hunk inside a cached symbol, still
    finds the card. Rows are matched on ``path``, ``provider`` and the
    model key (resolved as in :func:`lookup_cached`) with
    ``line_start <= target.line_end AND line_end >= target.line_start``;
    the fingerprint is not checked (see :meth:`CachedCard.is_stale`).

    Parameters
    ----------
    target : Target
        Resolved target (path + line range + optional symbol name).
    provider : str
        LLM provider tag from ``Config.cache_identity("rationale")``.
    config_model : str or None
        Pinned model name (or ``None`` for the provider default).

    Returns
    -------
    CachedCard or None
        The best row, or ``None`` when no row overlaps.

    Notes
    -----
    Ranking, best first:

    1. rows whose ``qualified_name`` is the target's (when it has one);
    2. rows whose range contains the target's, smallest span first;
    3. the rest, by overlap length, longest first.

    Ties go to the newest ``cached_at``. The composite primary key leads
    with ``path``, so the query needs no extra index.
    """
    model_key = _model_key(config_model)
    with get_session() as session:
        rows = session.exec(
            select(RationaleCache).where(
                RationaleCache.path == target.path,
                RationaleCache.provider == provider,
                RationaleCache.model == model_key,
                col(RationaleCache.line_start) <= target.line_end,
                col(RationaleCache.line_end) >= target.line_start,
            )
        ).all()
        if not rows:
            return None

        def rank(row: RationaleCache) -> tuple[int, int, int]:
            named = (
                target.qualified_name is not None
                and row.qualified_name == target.qualified_name
            )
            contains = (
                row.line_start <= target.line_start and row.line_end >= target.line_end
            )
            if contains:
                return (0 if named else 1, 0, row.line_end - row.line_start)
            overlap = min(row.line_end, target.line_end) - max(
                row.line_start, target.line_start
            )
            return (0 if named else 1, 1, -overlap)

        newest_first = sorted(rows, key=lambda row: row.cached_at, reverse=True)
        return CachedCard.from_row(min(newest_first, key=rank))


def store_cached(
    target: Target,
    evidence: Sequence[CommitEvidence],
    rationale: Rationale,
    provider: str,
    config_model: str | None,
    *,
    deferred_shas: str = "",
    only_if: CachedCard | None = None,
) -> str | None:
    """Upsert a freshly generated rationale; return its ``cached_at``.

    The PK ``(provider, model)`` columns mirror the *configured* LLM
    identity passed in by the caller — they must match what
    :func:`lookup_cached` will use, since the lookup happens *before*
    the LLM is invoked. The LLM-reported model identity is persisted
    separately in :attr:`RationaleCache.actual_model` so rows keyed
    under the ``"default"`` model tag retain provenance.

    Parameters
    ----------
    target, evidence, rationale, provider, config_model
        As above.
    deferred_shas : str, optional
        Space-joined SHAs the card was written without a description for;
        empty (the default) stores a final card.
    only_if : CachedCard, optional
        The row the caller read. When given, an existing row is replaced
        only if its ``cached_at``, ``evidence_fingerprint`` and
        ``deferred_shas`` still equal this card's (all three, because
        ``cached_at`` has second resolution and cannot tell a partial row
        from a final one written the same second).

    Returns
    -------
    str or None
        The new ``cached_at``; ``None`` when ``only_if`` was given and the
        row had moved, in which case nothing was written.
    """
    fp = _fingerprint(evidence)
    model_key = _model_key(config_model)
    cached_at = _now_iso()
    values = {
        "path": target.path,
        "line_start": target.line_start,
        "line_end": target.line_end,
        "provider": provider,
        "model": model_key,
        "evidence_fingerprint": fp,
        "cached_at": cached_at,
        "purpose": rationale.purpose,
        "why": rationale.why,
        "constraints": json.dumps([item.as_dict() for item in rationale.constraints]),
        "tradeoffs": json.dumps(list(rationale.tradeoffs)),
        "risks": json.dumps([item.as_dict() for item in rationale.risks]),
        "input_tokens": rationale.input_tokens,
        "output_tokens": rationale.output_tokens,
        "actual_provider": rationale.provider,
        "actual_model": rationale.model,
        "qualified_name": target.qualified_name,
        "deferred_shas": deferred_shas,
    }
    with get_session() as session:
        if only_if is None:
            session.merge(RationaleCache(**values))
            return cached_at
        pk = ("path", "line_start", "line_end", "provider", "model")
        stmt = sqlite_insert(RationaleCache).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=list(pk),
            set_={k: v for k, v in values.items() if k not in pk},
            where=(
                (col(RationaleCache.cached_at) == only_if.cached_at)
                & (
                    col(RationaleCache.evidence_fingerprint)
                    == only_if.evidence_fingerprint
                )
                & (col(RationaleCache.deferred_shas) == only_if.deferred_shas)
            ),
        )
        result = session.exec(stmt)  # type: ignore[call-overload]
        if not result.rowcount:
            return None
    return cached_at


def update_deferred(
    target: Target,
    provider: str,
    config_model: str | None,
    *,
    seen: CachedCard,
    deferred_shas: str,
) -> bool:
    """Set a row's ``deferred_shas`` if it is still the row the caller read.

    A plain ``UPDATE``, never ``session.merge`` (the row's NOT NULL columns
    are not on hand). The ``WHERE`` compares ``cached_at``,
    ``evidence_fingerprint`` and ``deferred_shas`` with ``seen``'s, because
    ``cached_at`` has second resolution and alone cannot tell a partial row
    from a final one written the same second.

    Parameters
    ----------
    target : Target
        Resolved target; its path and range are the key.
    provider : str
        LLM provider tag from ``Config.cache_identity("rationale")``.
    config_model : str or None
        Pinned model name (or ``None``), as for :func:`lookup_cached`.
    seen : CachedCard
        The card the caller read.
    deferred_shas : str
        The new space-joined list (empty marks the card final).

    Returns
    -------
    bool
        ``True`` if the row was updated, ``False`` if it had moved or is gone.
    """
    model_key = _model_key(config_model)
    with get_session() as session:
        result = session.exec(  # type: ignore[call-overload]
            update(RationaleCache)
            .where(
                col(RationaleCache.path) == target.path,
                col(RationaleCache.line_start) == target.line_start,
                col(RationaleCache.line_end) == target.line_end,
                col(RationaleCache.provider) == provider,
                col(RationaleCache.model) == model_key,
                col(RationaleCache.cached_at) == seen.cached_at,
                col(RationaleCache.evidence_fingerprint) == seen.evidence_fingerprint,
                col(RationaleCache.deferred_shas) == seen.deferred_shas,
            )
            .values(deferred_shas=deferred_shas)
        )
        return bool(result.rowcount)


def purge_paths(session: Session, paths: Iterable[str]) -> int:
    """Delete every cached card for ``paths``, whatever its range, provider or model.

    The scan runner calls it for the files it rewrote from symbolic links
    into plain files: a card written while the path was a link may quote
    what the link pointed to, and the evidence fingerprint (commit SHAs)
    would not change to invalidate it.

    Parameters
    ----------
    session : Session
        A session on the project's WhyGraph DB; the caller commits.
    paths : Iterable[str]
        Repository-relative paths, as cards store them.

    Returns
    -------
    int
        The number of rows deleted.
    """
    unique = sorted(set(paths))
    deleted = 0
    for start in range(0, len(unique), _PURGE_CHUNK):
        chunk = unique[start : start + _PURGE_CHUNK]
        result = session.exec(  # type: ignore[call-overload]
            delete(RationaleCache).where(col(RationaleCache.path).in_(chunk))
        )
        deleted += result.rowcount or 0
    return deleted


# ---------------------------------------------------------------------------
# Import from a project-transfer bundle (plan 42 section 5.5)
# ---------------------------------------------------------------------------

_IMPORT_SPAN_MAX = 5_000
_IMPORT_TEXT_MAX = 32_000
_IMPORT_LIST_ITEMS = 50
_IMPORT_ITEM_MAX = 4_000
_IMPORT_REF_MAX = 500
_IMPORT_IDENT_MAX = 200
_IMPORT_FUTURE_SLACK = timedelta(minutes=5)
_IMPORT_INT_MAX = 2**31
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")


@dataclass
class RationaleImportResult:
    """What :func:`import_rows` did (plan 42 section 7.2 ``rationale``).

    Attributes
    ----------
    inserted : int
        Rows newly written.
    existing : int
        Valid rows whose key the target already holds (the target wins), or
        that repeat an earlier record of the same bundle.
    skipped_oversize : int
        Records skipped for an over-long field, too many list items or a bad
        line span.
    skipped_denied : int
        Records whose path is on Chat's deny-list.
    inactive_model : int
        Valid rows whose ``(provider, model)`` differs from the target's
        ``cache_key``: they are stored but no lookup reads them.
    """

    inserted: int = 0
    existing: int = 0
    skipped_oversize: int = 0
    skipped_denied: int = 0
    inactive_model: int = 0

    def as_dict(self) -> dict[str, int]:
        """The counts block as a plain dict."""
        return {
            "inserted": self.inserted,
            "existing": self.existing,
            "inactive_model": self.inactive_model,
            "skipped_oversize": self.skipped_oversize,
            "skipped_denied": self.skipped_denied,
        }


def _as_mapping(record: Any) -> Mapping[str, Any]:
    """A wire model or a plain mapping, as a mapping."""
    if isinstance(record, Mapping):
        return record
    return record.model_dump()


def _import_int(value: object, name: str, *, nullable: bool = False) -> int | None:
    """An int in ``0..2**31`` (or ``None`` when ``nullable``), else a format error."""
    if value is None and nullable:
        return None
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= _IMPORT_INT_MAX
    ):
        raise ImportFormatError(name, "must be an integer between 0 and 2**31")
    return value


def _import_line(value: object, name: str, *, field_name: str) -> str | None:
    """A single-line optional identifier at most 200 chars, or ``None`` when absent."""
    if value is None:
        return None
    text = clean_text(
        value, multiline=False, max_chars=_IMPORT_IDENT_MAX, field=field_name
    )
    if text is None:
        raise ImportFormatError(
            field_name, f"must be at most {_IMPORT_IDENT_MAX} characters"
        )
    return text


class RationaleImport:
    """Check, then insert, a bundle's rationale cards in batches (plan 42 Q11).

    Two passes over the same records, so the caller can stream a large
    member instead of holding it: :meth:`check` format-checks **every**
    record before anything is written and counts the skips and the in-bundle
    repeats; :meth:`rows` re-reads the records and yields the rows to insert;
    :meth:`insert` writes one batch of them. Repeats are found over the whole
    member in :meth:`check`, so a batch never meets another batch's key and
    a rolled-back dry run counts exactly what the apply writes.

    Parameters
    ----------
    now : datetime
        The importer's clock; a ``cached_at`` more than five minutes after it
        refuses the bundle.
    cache_key : tuple of str
        The target's ``(provider, _model_key(model))`` under the config the
        import leaves in force; rows keyed otherwise count as
        ``inactive_model``.

    Attributes
    ----------
    result : RationaleImportResult
        The counts so far.
    """

    def __init__(self, *, now: datetime, cache_key: tuple[str, str]) -> None:
        self.result = RationaleImportResult()
        self._latest = (now if now.tzinfo else now.replace(tzinfo=timezone.utc)) + (
            _IMPORT_FUTURE_SLACK
        )
        self._cache_key = cache_key
        self._paths: dict[str, str | None] = {}  # one lexical check per path

    def _checked(
        self, records: Iterable[Any]
    ) -> Iterator[tuple[int, dict[str, Any] | str | None]]:
        """Each record's position and :func:`_check_rationale` outcome."""
        for index, raw in enumerate(records):
            try:
                yield (
                    index,
                    _check_rationale(_as_mapping(raw), self._latest, self._paths),
                )
            except ImportFormatError as exc:
                exc.index = index
                raise

    def check(self, records: Iterable[Any]) -> set[int]:
        """Pass one: format-check every record and count what will not be inserted.

        Parameters
        ----------
        records : Iterable
            Wire records (``RationaleRecord``) or equivalent mappings, in
            line order.

        Returns
        -------
        set of int
            The 0-based positions to leave out of :meth:`rows`: skipped
            records and repeats of an earlier record's key.

        Raises
        ------
        ImportFormatError
            A wrong type, a malformed path, fingerprint or timestamp, a
            control character in a single-line field, or a ``cached_at`` in
            the future. ``index`` is the 0-based record position.
        """
        result = self.result
        drop: set[int] = set()
        seen: set[tuple[str, int, int, str, str]] = set()
        for index, row in self._checked(records):
            if not isinstance(row, dict):
                if row == "denied":
                    result.skipped_denied += 1
                else:
                    result.skipped_oversize += 1
                drop.add(index)
                continue
            if (row["provider"], _model_key(row["model"])) != self._cache_key:
                result.inactive_model += 1
            key = (
                row["path"],
                row["line_start"],
                row["line_end"],
                row["provider"],
                row["model"],
            )
            if key in seen:
                result.existing += 1
                drop.add(index)
                continue
            seen.add(key)
        return drop

    def rows(self, records: Iterable[Any], drop: set[int]) -> Iterator[dict[str, Any]]:
        """Pass two: the rows to insert, for the records :meth:`check` passed.

        Parameters
        ----------
        records : Iterable
            The same records, in the same order, as :meth:`check` saw.
        drop : set of int
            What :meth:`check` returned.

        Yields
        ------
        dict
            One ``rationale_cache`` row without ``import_id``.
        """
        for index, row in self._checked(records):
            if isinstance(row, dict) and index not in drop:
                yield row

    def insert(
        self, session: Session, rows: Sequence[dict[str, Any]], *, import_id: int
    ) -> None:
        """Insert one batch; a key the target already holds is kept (D3).

        ``INSERT ... ON CONFLICT DO NOTHING``, every row stamped with
        ``import_id`` so :func:`remove_imported` can undo the import. Runs in
        the caller's session and never commits.

        Parameters
        ----------
        session : Session
            A session on the project's WhyGraph DB, inside the caller's write
            transaction.
        rows : sequence of dict
            Rows from :meth:`rows`.
        import_id : int
            The ``project_import.id`` stamped on every inserted row.
        """
        if not rows:
            return
        # One compiled statement run as executemany: compiling a multi-row
        # INSERT per chunk costs more than running it.
        outcome = session.connection().execute(
            sqlite_insert(RationaleCache).on_conflict_do_nothing(),
            [{**row, "import_id": import_id} for row in rows],
        )
        self.result.inserted += outcome.rowcount
        self.result.existing += len(rows) - outcome.rowcount


def import_rows(
    session: Session,
    records: Iterable[Any],
    *,
    import_id: int,
    now: datetime,
    cache_key: tuple[str, str],
) -> RationaleImportResult:
    """Insert imported rationale cards that the target does not hold yet, in one call.

    The target wins (plan 42 D3): a row is written only when its
    ``(path, line_start, line_end, provider, model)`` key is absent
    (``INSERT ... ON CONFLICT DO NOTHING``), and every row it writes carries
    ``import_id`` so :func:`remove_imported` can undo the import. A card's
    evidence fingerprint is the (forgeable) hash of commit SHAs, so imported
    rows are only as trustworthy as the bundle; that is why they are marked.
    The transfer import runs the same steps in batches through
    :class:`RationaleImport`.

    Parameters
    ----------
    session : Session
        A session on the project's WhyGraph DB; the caller commits (or rolls
        back, for a dry run).
    records : Iterable
        Wire records (``RationaleRecord``) or equivalent mappings.
    import_id : int
        The ``project_import.id`` stamped on every inserted row.
    now : datetime
        The importer's clock; a ``cached_at`` more than five minutes after it
        refuses the bundle.
    cache_key : tuple of str
        The target's ``(provider, _model_key(model))`` under the config the
        import leaves in force; rows keyed otherwise count as
        ``inactive_model``.

    Returns
    -------
    RationaleImportResult
        The counts.

    Raises
    ------
    ImportFormatError
        As :meth:`RationaleImport.check`; nothing is written.
    """
    records = list(records)
    job = RationaleImport(now=now, cache_key=cache_key)
    drop = job.check(records)
    job.insert(session, list(job.rows(records, drop)), import_id=import_id)
    return job.result


_SHA_RE = re.compile(r"[0-9a-f]{7,64}")


def _import_deferred(value: Any) -> str:
    """A record's ``deferred_shas``: absent is ``""`` (final); else SHAs, space-joined."""
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > _IMPORT_TEXT_MAX:
        raise ImportFormatError("deferred_shas", "must be space-separated commit SHAs")
    tokens = value.split()
    if not all(_SHA_RE.fullmatch(t) for t in tokens):
        raise ImportFormatError("deferred_shas", "must be space-separated commit SHAs")
    return " ".join(tokens)


def _import_list_element(name: str, element: Any) -> Any:
    """One imported list element as the value to store; ``None`` when too long.

    ``tradeoffs`` stay strings. A constraint or risk is read by the one
    decoder (:func:`decode_item`): a plain string becomes basis ``unknown``,
    an object keeps its basis and well-formed refs, anything else is a format
    error. The stored value is :meth:`Item.as_dict`.
    """
    if name == "tradeoffs":
        return clean_text(
            element, multiline=True, max_chars=_IMPORT_ITEM_MAX, field=name
        )
    item = decode_item(element)
    if item is None:
        raise ImportFormatError(name, "must be a list of text or item objects")
    text = clean_text(item.text, multiline=True, max_chars=_IMPORT_ITEM_MAX, field=name)
    quote = clean_text(
        item.quote, multiline=True, max_chars=_IMPORT_ITEM_MAX, field=name
    )
    if text is None or quote is None:
        return None
    refs = tuple(
        ref
        for ref in item.refs
        if len(ref.id) <= _IMPORT_REF_MAX and len(ref.url or "") <= _IMPORT_REF_MAX
    )
    return Item(text, item.basis, refs, quote).as_dict()


def _check_rationale(
    record: Mapping[str, Any], latest: datetime, paths: dict[str, str | None]
) -> dict[str, Any] | str | None:
    """One record as a row dict; ``None`` to skip (size), ``"denied"`` to skip (path)."""
    raw_path = record.get("path")
    if isinstance(raw_path, str) and raw_path in paths:
        path = paths[raw_path]
    else:
        path = _lexical_path(raw_path, field="path")
        paths[raw_path] = path  # type: ignore[index]
    line_start = _import_int(record.get("line_start"), "line_start")
    line_end = _import_int(record.get("line_end"), "line_end")
    assert line_start is not None and line_end is not None
    provider = _import_line(record.get("provider"), "provider", field_name="provider")
    model = _import_line(record.get("model"), "model", field_name="model")
    if provider is None or model is None:
        raise ImportFormatError("provider", "provider and model are required")
    fingerprint = record.get("evidence_fingerprint")
    if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.fullmatch(fingerprint):
        raise ImportFormatError("evidence_fingerprint", "must be 64 lowercase hex")
    cached_at = _import_time(record.get("cached_at"), "cached_at", latest)
    if cached_at is None:
        raise ImportFormatError("cached_at", "must be an ISO-8601 timestamp")
    input_tokens = _import_int(
        record.get("input_tokens"), "input_tokens", nullable=True
    )
    output_tokens = _import_int(
        record.get("output_tokens"), "output_tokens", nullable=True
    )
    actual_provider = _import_line(
        record.get("actual_provider"), "actual_provider", field_name="actual_provider"
    )
    actual_model = _import_line(
        record.get("actual_model"), "actual_model", field_name="actual_model"
    )
    qualified_name = _import_line(
        record.get("qualified_name"), "qualified_name", field_name="qualified_name"
    )
    texts: dict[str, str | None] = {}
    for name in ("purpose", "why"):
        texts[name] = clean_text(
            record.get(name), multiline=True, max_chars=_IMPORT_TEXT_MAX, field=name
        )
    lists: dict[str, list[Any] | None] = {}
    for name in ("constraints", "tradeoffs", "risks"):
        value = record.get(name)
        if not isinstance(value, list | tuple):
            raise ImportFormatError(name, "must be a list of text")
        items: list[Any] | None = []
        if len(value) > _IMPORT_LIST_ITEMS:
            items = None
        else:
            for element in value:
                cleaned = _import_list_element(name, element)
                if cleaned is None:
                    items = None
                    break
                items.append(cleaned)
        lists[name] = items
    deferred_shas = _import_deferred(record.get("deferred_shas"))
    # Format errors above win over skips; the skips follow.
    if path is None:
        return "denied"
    if (
        not 1 <= line_start <= line_end
        or line_end - line_start > _IMPORT_SPAN_MAX
        or any(v is None for v in (*texts.values(), *lists.values()))
    ):
        return None
    return {
        "path": path,
        "line_start": line_start,
        "line_end": line_end,
        "provider": provider,
        "model": model,
        "evidence_fingerprint": fingerprint,
        "cached_at": cached_at,
        "purpose": texts["purpose"],
        "why": texts["why"],
        "constraints": json.dumps(lists["constraints"]),
        "tradeoffs": json.dumps(lists["tradeoffs"]),
        "risks": json.dumps(lists["risks"]),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "actual_provider": actual_provider,
        "actual_model": actual_model,
        "qualified_name": qualified_name,
        "deferred_shas": deferred_shas,
    }


def remove_imported(session: Session) -> int:
    """Delete every rationale card an import wrote (``import_id IS NOT NULL``).

    Parameters
    ----------
    session : Session
        A session on the project's WhyGraph DB; the caller commits.

    Returns
    -------
    int
        The number of rows deleted.
    """
    result = session.exec(  # type: ignore[call-overload]
        delete(RationaleCache).where(col(RationaleCache.import_id).is_not(None))
    )
    return result.rowcount or 0
