"""SQLite-backed cache for ``whygraph_rationale_brief`` LLM output.

Keys a cached card by ``(path, line_start, line_end, provider, model)``
and invalidates by an *evidence fingerprint* — the sha256 of the sorted
commit SHAs returned by :func:`whygraph.mcp.evidence.collect_evidence`.
A new commit landing on the blamed lines changes the fingerprint and
forces a regeneration on the next call; the stale row is overwritten by
:func:`store_cached`.

Notes
-----
:attr:`whygraph.core.config.RationaleConfig.model` can be ``None``
(meaning *use whatever model the provider's adapter defaults to*). The
cache PK still needs a deterministic ``model`` token at lookup time —
*before* the LLM call returns and reports its actual model identity — so
``None`` is translated to the literal string ``"default"`` via
:func:`_model_key`. The LLM-reported identity is persisted separately in
:attr:`RationaleCache.actual_model` so rows keyed under ``"default"``
keep their provenance. Pinning ``rationale.model`` in ``whygraph.toml``
gives the cleanest per-model cache semantics.

The fingerprint is computed only over commit SHAs — PR/issue updates
that don't change the underlying commit set do not invalidate the cache.
That matches :func:`collect_evidence`'s own derivation: PRs and issues
are looked up *from* the commit set.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Sequence

from whygraph.analyze import CommitEvidence, Rationale
from whygraph.db import get_session
from whygraph.db.models import RationaleCache

from .targets import Target


_DEFAULT_MODEL_TAG = "default"


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
        LLM provider tag from :attr:`RationaleConfig.provider`.
    config_model
        Configured model name (or ``None`` for the provider default);
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
        rationale = Rationale(
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
        cached_at = row.cached_at
    return rationale, cached_at


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
