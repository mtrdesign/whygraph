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
from whygraph.mcp._shared import WhyGraphError
from whygraph.mcp.rationale import whygraph_rationale_brief
from whygraph.services.codegraph import SymbolContext
from whygraph.services.git import Repository


def _db_commit(sha: str, *, committed_at: str) -> Commit:
    """A WhyGraph ``commit`` row with sensible defaults for tests."""
    return Commit(
        sha=sha,
        parent_shas="",
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
        llm_description="Mechanical diff summary.",
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
