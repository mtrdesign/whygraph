"""SQLModel for the ``rationale_cache`` table.

One row per cached LLM-generated rationale, keyed by target plus the
``(provider, model)`` identity of the LLM that produced it. Lookups
happen *after* evidence collection so that a change in the blamed-commit
set — a new commit landing on those lines — invalidates the cache via
the ``evidence_fingerprint`` column without needing TTLs.

Notes
-----
The list-shaped rationale fields (``constraints``, ``tradeoffs``,
``risks``) are stored as JSON-encoded strings, matching the convention
already used by :attr:`Commit.parent_shas`, :attr:`PullRequest.labels`,
and :attr:`Issue.labels`. Callers encode/decode at the boundary
(:mod:`whygraph.mcp.rationale_cache`).

``model`` is part of the composite PK; when
:attr:`whygraph.core.config.RationaleConfig.model` is ``None`` the cache
key uses the literal string ``"default"``. The LLM-reported model
identity lands in the separate ``actual_model`` column so rows keyed
under ``"default"`` retain provenance.
"""

from __future__ import annotations

from sqlalchemy import Text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class RationaleCache(WhygraphTable, table=True):
    """Cached :class:`whygraph.analyze.Rationale` for a (target, LLM) pair.

    The composite PK ``(path, line_start, line_end, provider, model)``
    lets two LLMs cache their results for the same target side by side.
    ``qualified_name`` is observational only — a path/line target may
    have no symbol attached, and including it in the PK would split the
    cache between symbol and line-range lookups of the same lines.
    """

    path: str = Field(primary_key=True, sa_type=Text)
    line_start: int = Field(primary_key=True)
    line_end: int = Field(primary_key=True)
    provider: str = Field(primary_key=True, sa_type=Text)
    model: str = Field(primary_key=True, sa_type=Text)

    evidence_fingerprint: str = Field(sa_type=Text)
    cached_at: str = Field(sa_type=Text)

    purpose: str = Field(sa_type=Text)
    why: str = Field(sa_type=Text)
    constraints: str = Field(sa_type=Text)  # JSON-encoded list[str]
    tradeoffs: str = Field(sa_type=Text)  # JSON-encoded list[str]
    risks: str = Field(sa_type=Text)  # JSON-encoded list[str]

    input_tokens: int | None = Field(default=None)
    output_tokens: int | None = Field(default=None)

    actual_provider: str | None = Field(default=None, sa_type=Text)
    actual_model: str | None = Field(default=None, sa_type=Text)
    qualified_name: str | None = Field(default=None, sa_type=Text)
