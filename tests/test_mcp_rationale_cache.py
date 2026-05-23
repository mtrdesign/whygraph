"""Tests for the SQLite-backed rationale cache.

Exercises :mod:`whygraph.mcp.rationale_cache` end-to-end via the
:func:`whygraph_rationale_brief` MCP tool — the LLM round-trip is
stubbed (``_CountingGenerator``) so we can assert exact call counts
across repeat invocations and an invalidation event.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from whygraph.analyze import CommitEvidence, Rationale
from whygraph.db import get_session
from whygraph.db.models import Commit, RationaleCache
from whygraph.mcp.rationale import whygraph_rationale_brief
from whygraph.mcp.rationale_cache import _fingerprint, lookup_cached
from whygraph.mcp.targets import Target
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


def _seed_two_commits(repo_root: Path) -> None:
    """Seed the WhyGraph DB with the two commits of ``temp_git_repo``."""
    newest, oldest = list(Repository(repo_root).commits)
    with get_session() as session:
        session.add(_db_commit(oldest.sha, committed_at="2026-01-01T00:00:00+00:00"))
        session.add(_db_commit(newest.sha, committed_at="2026-02-01T00:00:00+00:00"))


def _add_third_commit(repo_root: Path) -> None:
    """Land one more commit on ``sample.py`` and seed it into the DB.

    Rewrites line 2 so a blame of lines 1-3 now spans *three* commits
    instead of two — the evidence fingerprint changes and a previously
    cached card must be regenerated.
    """
    sample = repo_root / "sample.py"
    sample.write_text("line one\nline two updated\nline three\n")
    subprocess.run(
        ["git", "add", "sample.py"], cwd=repo_root, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "third commit"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    newest = list(Repository(repo_root).commits)[0]
    with get_session() as session:
        session.add(_db_commit(newest.sha, committed_at="2026-03-01T00:00:00+00:00"))


class _CountingGenerator:
    """Stub for :class:`RationaleGenerator` — counts how often it's called."""

    calls = 0

    @classmethod
    def reset(cls) -> None:
        cls.calls = 0

    @classmethod
    def from_config(cls, config: object) -> "_CountingGenerator":
        return cls()

    def generate(
        self,
        evidence: Sequence[CommitEvidence],
        *,
        symbol_context: SymbolContext | None = None,
    ) -> Rationale:
        type(self).calls += 1
        return Rationale(
            purpose="Holds two sample lines.",
            why="Built up across two commits.",
            constraints=("keep it small",),
            tradeoffs=(),
            risks=("none worth noting",),
            model="fake-1",
            provider="fake",
        )


def test_second_call_returns_cached(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    _CountingGenerator.reset()
    monkeypatch.setattr(
        "whygraph.mcp.rationale.RationaleGenerator", _CountingGenerator
    )

    first = whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)
    second = whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)

    assert _CountingGenerator.calls == 1
    assert first["cached_at"] == second["cached_at"]
    assert first["purpose"] == second["purpose"] == "Holds two sample lines."
    assert first["constraints"] == second["constraints"] == ["keep it small"]
    assert first["model"] == second["model"] == "fake-1"


def test_new_commit_invalidates_cache(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    _CountingGenerator.reset()
    monkeypatch.setattr(
        "whygraph.mcp.rationale.RationaleGenerator", _CountingGenerator
    )

    first = whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)
    _add_third_commit(temp_git_repo)
    second = whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)

    assert _CountingGenerator.calls == 2
    assert first["evidence_count"]["commits"] == 2
    assert second["evidence_count"]["commits"] == 3
    # ``cached_at`` is the row's persisted timestamp; the regenerated
    # entry overwrites the old one, so the second call's stamp is
    # always >= the first.
    assert second["cached_at"] >= first["cached_at"]


def test_lookup_cached_returns_none_on_stale_fingerprint(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale ``evidence_fingerprint`` is treated as a miss, not a hit."""
    from whygraph.mcp.evidence import collect_evidence

    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    target = Target(path="sample.py", line_start=1, line_end=3, qualified_name=None)
    evidence = collect_evidence(target, limit=20)
    assert evidence, "fixture must produce at least one evidence row"

    with get_session() as session:
        session.add(
            RationaleCache(
                path="sample.py",
                line_start=1,
                line_end=3,
                provider="fake",
                model="default",
                evidence_fingerprint="bogus-fingerprint",
                cached_at="2026-01-01T00:00:00+00:00",
                purpose="stale",
                why="stale",
                constraints=json.dumps([]),
                tradeoffs=json.dumps([]),
                risks=json.dumps([]),
                actual_model="fake-1",
            )
        )

    assert lookup_cached(target, evidence, "fake", None) is None


def test_fingerprint_independent_of_evidence_order() -> None:
    """``_fingerprint`` sorts SHAs — order in the input list must not matter."""
    e1 = [
        CommitEvidence(commit=_db_commit("aaa", committed_at="t1")),
        CommitEvidence(commit=_db_commit("bbb", committed_at="t2")),
        CommitEvidence(commit=_db_commit("ccc", committed_at="t3")),
    ]
    e2 = list(reversed(e1))
    assert _fingerprint(e1) == _fingerprint(e2)
