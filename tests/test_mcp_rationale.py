"""Tests for the ``whygraph_rationale_brief`` MCP tool.

The evidence collection (blame + DB join) runs for real; only the LLM
round-trip is stubbed — ``RationaleGenerator`` is monkeypatched so no
provider SDK or ``claude`` CLI is touched.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from whygraph.analyze import AnalyzeError, CommitEvidence, Rationale
from whygraph.db import get_session
from whygraph.db.models import Commit
from whygraph.mcp.errors import WhyGraphError
from whygraph.mcp.rationale import whygraph_rationale_brief
from whygraph.services.codegraph import SymbolContext
from whygraph.services.git import Repository


def _db_commit(
    sha: str,
    *,
    committed_at: str,
    parent_shas: str = "",
    llm_description: str | None = "Mechanical diff summary.",
) -> Commit:
    """A WhyGraph ``commit`` row with sensible defaults for tests."""
    return Commit(
        sha=sha,
        parent_shas=parent_shas,
        author_name="Test User",
        author_email="tester@example.com",
        authored_at=committed_at,
        committed_at=committed_at,
        subject="a change",
        body="",
        files_changed=1,
        insertions=1,
        deletions=0,
        scanned_at="2026-05-01T00:00:00+00:00",
        llm_description=llm_description,
    )


class _FakeGenerator:
    """Stub for :class:`RationaleGenerator` — returns a canned card."""

    @classmethod
    def from_config(cls, config: object) -> "_FakeGenerator":
        return cls()

    def generate(
        self,
        evidence: Sequence[CommitEvidence],
        *,
        symbol_context: SymbolContext | None = None,
    ) -> Rationale:
        return Rationale(
            purpose="Holds two sample lines.",
            why="Built up across two commits.",
            constraints=("keep it small",),
            tradeoffs=(),
            risks=("none worth noting",),
            model="fake-1",
            provider="fake",
        )


def _seed_two_commits(repo_root: Path) -> None:
    """Seed the WhyGraph DB with the two commits of ``temp_git_repo``."""
    newest, oldest = list(Repository(repo_root).commits)
    with get_session() as session:
        session.add(_db_commit(oldest.sha, committed_at="2026-01-01T00:00:00+00:00"))
        session.add(_db_commit(newest.sha, committed_at="2026-02-01T00:00:00+00:00"))


def test_rationale_brief_returns_card(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr(
        "whygraph.mcp.rationale.RationaleGenerator", _FakeGenerator
    )

    result = whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)

    assert result["purpose"] == "Holds two sample lines."
    assert result["constraints"] == ["keep it small"]
    assert result["tradeoffs"] == []
    assert result["risks"] == ["none worth noting"]
    assert result["model"] == "fake-1"
    assert result["provider"] == "fake"
    assert result["evidence_count"]["commits"] == 2
    assert result["target"]["path"] == "sample.py"


def test_rationale_brief_errors_without_evidence(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No commits seeded — the blamed SHAs map to nothing in the DB.
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr(
        "whygraph.mcp.rationale.RationaleGenerator", _FakeGenerator
    )

    with pytest.raises(WhyGraphError, match="no historical evidence"):
        whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)


def test_rationale_brief_wraps_generator_failure(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)

    class _FailingGenerator:
        @classmethod
        def from_config(cls, config: object) -> "_FailingGenerator":
            return cls()

        def generate(
            self,
            evidence: Sequence[CommitEvidence],
            *,
            symbol_context: SymbolContext | None = None,
        ) -> Rationale:
            raise AnalyzeError("model unavailable")

    monkeypatch.setattr(
        "whygraph.mcp.rationale.RationaleGenerator", _FailingGenerator
    )

    with pytest.raises(WhyGraphError, match="rationale generation failed"):
        whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)


# ---- lazy LLM-description backfill --------------------------------------


def _install_stub_descriptor(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch ``LlmDescriptor`` with a recording stub; return its diff log."""
    from whygraph.analyze import Description

    seen: list[str] = []

    class _StubDescriptor:
        @classmethod
        def from_config(cls, _cfg: object) -> "_StubDescriptor":
            return cls()

        def describe(self, diff: str) -> Description:
            seen.append(diff)
            return Description(
                text="backfilled summary",
                model="stub-1",
                provider="stub",
                input_tokens=1,
                output_tokens=2,
            )

    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _StubDescriptor)
    return seen


def _seed_two_commits_with_nulls(repo_root: Path) -> tuple[str, str]:
    """Seed both commits with NULL ``llm_description`` so backfill kicks in.

    Returns ``(newest_sha, oldest_sha)`` for use by assertions.
    """
    newest, oldest = list(Repository(repo_root).commits)
    with get_session() as session:
        session.add(
            _db_commit(
                oldest.sha,
                committed_at="2026-01-01T00:00:00+00:00",
                llm_description=None,
            )
        )
        session.add(
            _db_commit(
                newest.sha,
                committed_at="2026-02-01T00:00:00+00:00",
                parent_shas=oldest.sha,
                llm_description=None,
            )
        )
    return newest.sha, oldest.sha


def test_rationale_brief_backfills_on_cache_miss(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    newest_sha, oldest_sha = _seed_two_commits_with_nulls(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _FakeGenerator)
    seen = _install_stub_descriptor(monkeypatch)

    result = whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)

    # Rationale still came back — the card is the canned one.
    assert result["purpose"] == "Holds two sample lines."
    # Backfill ran for both commits AND was persisted.
    assert len(seen) == 2
    with get_session() as session:
        for sha in (newest_sha, oldest_sha):
            assert session.get(Commit, sha).llm_description == "backfilled summary"


def test_rationale_brief_skips_backfill_on_cache_hit(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.analyze import Rationale as RationaleVO  # local alias for clarity
    from whygraph.db.models import RationaleCache
    from whygraph.mcp.rationale_cache import _fingerprint

    newest_sha, oldest_sha = _seed_two_commits_with_nulls(temp_git_repo)

    # Build the same evidence shape collect_evidence would, just to derive
    # the fingerprint the cache row needs.
    from whygraph.analyze import CommitEvidence as Ev

    fp = _fingerprint(
        [
            Ev(_db_commit(newest_sha, committed_at="x")),
            Ev(_db_commit(oldest_sha, committed_at="x")),
        ]
    )
    # Seed the cache with a row that matches the target + provider/model the
    # rationale tool will look up. Provider/model come from get_config().rationale —
    # in tests with no whygraph.toml present, both default to the literals below.
    import json

    from whygraph.core import get_config

    cfg = get_config().rationale
    model_key = cfg.model if cfg.model else "default"
    with get_session() as session:
        session.add(
            RationaleCache(
                path="sample.py",
                line_start=1,
                line_end=3,
                provider=cfg.provider,
                model=model_key,
                evidence_fingerprint=fp,
                cached_at="2026-03-01T00:00:00+00:00",
                purpose="cached purpose",
                why="cached why",
                constraints=json.dumps(["c1"]),
                tradeoffs=json.dumps([]),
                risks=json.dumps(["r1"]),
                input_tokens=10,
                output_tokens=20,
                actual_provider=cfg.provider,
                actual_model="cached-model",
            )
        )

    monkeypatch.chdir(temp_git_repo)
    # If either of these is invoked, the cache-hit short-circuit failed.
    sentinel = {"generator_called": False}

    class _MustNotRun(_FakeGenerator):
        @classmethod
        def from_config(cls, _cfg: object) -> "_MustNotRun":
            sentinel["generator_called"] = True
            return cls()

    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _MustNotRun)
    seen = _install_stub_descriptor(monkeypatch)

    result = whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)

    assert result["purpose"] == "cached purpose"
    assert sentinel["generator_called"] is False
    # Crucially, the descriptor was NOT invoked — backfill stayed behind the
    # cache lookup and the rows remain NULL.
    assert seen == []
    with get_session() as session:
        for sha in (newest_sha, oldest_sha):
            assert session.get(Commit, sha).llm_description is None
    # Silence unused-import diagnostics for RationaleVO (kept for type clarity).
    _ = RationaleVO


def test_rationale_brief_backfill_failure_does_not_block_rationale(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.services.llm import LlmError

    _seed_two_commits_with_nulls(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _FakeGenerator)

    class _AlwaysFails:
        @classmethod
        def from_config(cls, _cfg: object):
            raise LlmError("no provider configured")

    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _AlwaysFails)

    # Rationale still succeeds — backfill is best-effort.
    result = whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)
    assert result["purpose"] == "Holds two sample lines."
