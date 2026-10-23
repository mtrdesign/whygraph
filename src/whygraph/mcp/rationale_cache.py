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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

from sqlalchemy import delete
from sqlmodel import Session, col, select

from whygraph.analyze import CommitEvidence, Rationale
from whygraph.db import get_session
from whygraph.db.models import RationaleCache

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


def _rationale_from_row(row: RationaleCache) -> Rationale:
    """Rebuild the :class:`~whygraph.analyze.Rationale` a cache row holds."""
    return Rationale(
        purpose=row.purpose,
        why=row.why,
        constraints=tuple(json.loads(row.constraints)),
        tradeoffs=tuple(json.loads(row.tradeoffs)),
        risks=tuple(json.loads(row.risks)),
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
    """

    rationale: Rationale
    cached_at: str
    line_start: int
    line_end: int
    qualified_name: str | None
    evidence_fingerprint: str

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
) -> tuple[Rationale, str] | None:
    """Return ``(rationale, cached_at)`` for ``target`` or ``None`` on miss.

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
    tuple of (Rationale, str), or None
        Reconstructed :class:`~whygraph.analyze.Rationale` and the ISO
        timestamp the row was originally cached at. ``None`` if no row
        matches the key or its fingerprint is stale.
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
        rationale = _rationale_from_row(row)
        cached_at = row.cached_at
    return rationale, cached_at


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
) -> str:
    """Upsert a freshly generated rationale; return its ``cached_at``.

    The PK ``(provider, model)`` columns mirror the *configured* LLM
    identity passed in by the caller — they must match what
    :func:`lookup_cached` will use, since the lookup happens *before*
    the LLM is invoked. The LLM-reported model identity is persisted
    separately in :attr:`RationaleCache.actual_model` so rows keyed
    under the ``"default"`` model tag retain provenance.
    """
    fp = _fingerprint(evidence)
    model_key = _model_key(config_model)
    cached_at = _now_iso()
    with get_session() as session:
        session.merge(
            RationaleCache(
                path=target.path,
                line_start=target.line_start,
                line_end=target.line_end,
                provider=provider,
                model=model_key,
                evidence_fingerprint=fp,
                cached_at=cached_at,
                purpose=rationale.purpose,
                why=rationale.why,
                constraints=json.dumps(list(rationale.constraints)),
                tradeoffs=json.dumps(list(rationale.tradeoffs)),
                risks=json.dumps(list(rationale.risks)),
                input_tokens=rationale.input_tokens,
                output_tokens=rationale.output_tokens,
                actual_provider=rationale.provider,
                actual_model=rationale.model,
                qualified_name=target.qualified_name,
            )
        )
    return cached_at


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
