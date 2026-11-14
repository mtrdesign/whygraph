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
from dataclasses import replace
from pathlib import Path

import pytest

from whygraph.analyze import CommitEvidence, Item, ItemRef, Rationale
from whygraph.db import get_session
from whygraph.db.models import Commit, RationaleCache
from whygraph.mcp.rationale import whygraph_rationale_brief
from whygraph.mcp.rationale_cache import (
    _fingerprint,
    decode_item,
    decode_items,
    lookup_cached,
    lookup_exact,
    lookup_overlapping,
    store_cached,
    update_deferred,
)
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


_SOURCED = Item(
    "keep it small", "quoted", (ItemRef("pr", "4", "https://x/pull/4"),), "small"
)


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
        subject: str | None = None,
    ) -> Rationale:
        type(self).calls += 1
        return Rationale(
            purpose="Holds two sample lines.",
            why="Built up across two commits.",
            constraints=(_SOURCED,),
            tradeoffs=(),
            risks=(Item("none worth noting", "inferred"),),
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
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _CountingGenerator)

    first = whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)
    second = whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)

    assert _CountingGenerator.calls == 1
    assert first["cached_at"] == second["cached_at"]
    assert first["purpose"] == second["purpose"] == "Holds two sample lines."
    # The item round-trips through the cache row as an object.
    assert first["constraints"] == second["constraints"] == [_SOURCED.as_dict()]
    assert (
        first["risks"]
        == second["risks"]
        == [{"text": "none worth noting", "basis": "inferred", "refs": []}]
    )
    assert first["model"] == second["model"] == "fake-1"


def test_new_commit_invalidates_cache(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    _CountingGenerator.reset()
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _CountingGenerator)

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


def test_lookup_overlapping_ranks_rows(whygraph_db_initialized: Path) -> None:
    """Same path / provider / model and an overlapping range, ranked:
    the symbol's name, then the smallest containing range, then the
    longest overlap; ties to the newest row."""

    def row(
        line_start: int,
        line_end: int,
        purpose: str,
        *,
        path: str = "a.py",
        provider: str = "fake",
        model: str = "default",
        qualified_name: str | None = None,
        cached_at: str = "2026-01-01T00:00:00+00:00",
    ) -> RationaleCache:
        return RationaleCache(
            path=path,
            line_start=line_start,
            line_end=line_end,
            provider=provider,
            model=model,
            evidence_fingerprint="fp-" + purpose,
            cached_at=cached_at,
            purpose=purpose,
            why="why",
            constraints=json.dumps(["c"]),
            tradeoffs=json.dumps([]),
            risks=json.dumps([]),
            qualified_name=qualified_name,
        )

    def best(line_start: int, line_end: int, qualified_name: str | None = None):
        target = Target("a.py", line_start, line_end, qualified_name)
        return lookup_overlapping(target, "fake", None)

    with get_session() as session:
        session.add(row(10, 30, "outer"))
        session.add(row(12, 20, "inner"))
        session.add(row(15, 40, "overlap long"))
        session.add(row(5, 16, "overlap short", qualified_name="mod.f"))
        session.add(row(14, 18, "other path", path="b.py"))
        session.add(row(14, 18, "other provider", provider="other"))
        session.add(row(14, 18, "other model", model="pinned"))
        session.add(row(50, 60, "disjoint"))

    found = best(14, 18)
    assert found is not None
    assert (found.rationale.purpose, found.line_start, found.line_end) == (
        "inner",
        12,
        20,
    )
    # A row of plain strings (written before items had sources) decodes.
    assert found.rationale.constraints == (Item("c", "unknown"),)
    assert found.evidence_fingerprint == "fp-inner"
    # The symbol's own card first, even though it only overlaps.
    assert best(14, 18, "mod.f").rationale.purpose == "overlap short"
    # Nothing contains 25-35: the longest overlap wins (15-40 over 10-30).
    assert best(25, 35).rationale.purpose == "overlap long"
    assert best(41, 49) is None
    assert best(14, 18, "pinned") is not None  # an unknown name ranks normally

    with get_session() as session:
        session.add(row(1, 2, "older"))
        session.add(row(2, 3, "newer", cached_at="2026-02-01T00:00:00+00:00"))
    assert best(2, 2).rationale.purpose == "newer"
    assert (
        lookup_overlapping(
            Target("a.py", 14, 18, None), "fake", "pinned"
        ).rationale.purpose
        == "other model"
    )


def test_cache_key_follows_config_v2_model_resolution(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row cached under a 1.x config (key ``"default"``) keeps hitting for
    an old file that pins only a deprecated ``[llm.<provider>].model``, while
    setting or changing ``[llm].model`` changes the key."""
    from sqlmodel import select

    from whygraph import core
    from whygraph.core.config import Config

    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    _CountingGenerator.reset()
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _CountingGenerator)

    def _use(raw: dict) -> None:
        raw = {**raw, "whygraph_db": str(whygraph_db_initialized)}
        monkeypatch.setattr(core, "_config", Config.from_dict(raw, temp_git_repo))

    def _brief() -> dict:
        return whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)

    _use({})  # 1.x defaults: nothing pinned
    _brief()
    with get_session() as session:
        keys = {(r.provider, r.model) for r in session.exec(select(RationaleCache))}
    assert keys == {("anthropic", "default")}
    assert _CountingGenerator.calls == 1

    _use({"llm": {"anthropic": {"model": "claude-sonnet-4-5"}}})  # deprecated pin
    _brief()
    assert _CountingGenerator.calls == 1  # old row still hits

    _use({"llm": {"model": "anthropic/claude-sonnet-4-5"}})  # v2 pin
    _brief()
    assert _CountingGenerator.calls == 2  # new key, regenerated

    _use({"llm": {"model": "anthropic/claude-opus-4-7"}})  # changed pin
    _brief()
    assert _CountingGenerator.calls == 3

    _use({})  # back to unpinned: the original row is still there
    _brief()
    assert _CountingGenerator.calls == 3


def _stored_card(
    whygraph_db_initialized: Path, *, deferred_shas: str = ""
) -> tuple[Target, list[CommitEvidence], Rationale]:
    """Store one card for a fixed target and return what the caches key on."""
    del whygraph_db_initialized
    target = Target(path="a.py", line_start=1, line_end=3, qualified_name="a.f")
    evidence = [CommitEvidence(commit=_db_commit("aaa", committed_at="t1"))]
    rationale = Rationale(
        purpose="p",
        why="w",
        constraints=(Item("c", "inferred"),),
        tradeoffs=(),
        risks=(),
        model="m",
        provider="fake",
    )
    store_cached(target, evidence, rationale, "fake", None, deferred_shas=deferred_shas)
    return target, evidence, rationale


def test_lookups_round_trip_deferred_shas(whygraph_db_initialized: Path) -> None:
    """``lookup_cached`` and ``lookup_exact`` both return the row's ``deferred_shas``."""
    target, evidence, _ = _stored_card(whygraph_db_initialized, deferred_shas="aaa bbb")
    hit = lookup_cached(target, evidence, "fake", None)
    assert hit is not None and hit.deferred_shas == "aaa bbb"
    assert hit.rationale.purpose == "p"
    exact = lookup_exact(target, "fake", None)
    assert exact is not None and exact.deferred_shas == "aaa bbb"


def test_update_deferred_sets_the_list_when_the_row_is_unchanged(
    whygraph_db_initialized: Path,
) -> None:
    target, evidence, _ = _stored_card(whygraph_db_initialized, deferred_shas="aaa bbb")
    seen = lookup_cached(target, evidence, "fake", None)
    assert seen is not None
    assert update_deferred(target, "fake", None, seen=seen, deferred_shas="bbb")
    after = lookup_cached(target, evidence, "fake", None)
    assert after is not None and after.deferred_shas == "bbb"
    assert after.cached_at == seen.cached_at


def test_update_deferred_after_a_final_store_in_the_same_second_leaves_it_final(
    whygraph_db_initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same ``cached_at`` and fingerprint: only ``deferred_shas`` tells them apart."""
    monkeypatch.setattr(
        "whygraph.mcp.rationale_cache._now_iso", lambda: "2026-01-01T00:00:00+00:00"
    )
    target, evidence, rationale = _stored_card(
        whygraph_db_initialized, deferred_shas="aaa"
    )
    seen = lookup_cached(target, evidence, "fake", None)
    assert seen is not None and seen.deferred_shas == "aaa"
    store_cached(target, evidence, rationale, "fake", None)  # final, same second
    assert not update_deferred(target, "fake", None, seen=seen, deferred_shas="bbb")
    after = lookup_cached(target, evidence, "fake", None)
    assert after is not None and after.deferred_shas == ""


def test_store_cached_only_if_writes_only_when_the_row_is_unchanged(
    whygraph_db_initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "whygraph.mcp.rationale_cache._now_iso", lambda: "2026-01-01T00:00:00+00:00"
    )
    target, evidence, rationale = _stored_card(
        whygraph_db_initialized, deferred_shas="aaa"
    )
    seen = lookup_cached(target, evidence, "fake", None)
    assert seen is not None
    # a final store lands in the same second, then a stale completion tries to write
    store_cached(target, evidence, rationale, "fake", None)
    newer = replace(rationale, purpose="completed")
    assert store_cached(target, evidence, newer, "fake", None, only_if=seen) is None
    after = lookup_cached(target, evidence, "fake", None)
    assert after is not None and after.rationale.purpose == "p"
    assert after.deferred_shas == ""
    # against the row as it now is, the write goes through
    assert (
        store_cached(target, evidence, newer, "fake", None, only_if=after) is not None
    )
    final = lookup_cached(target, evidence, "fake", None)
    assert final is not None and final.rationale.purpose == "completed"


# ---- the item codec (plan 54 section 5.5) ---------------------------------


def test_decode_item_round_trips_as_dict() -> None:
    assert decode_item(_SOURCED.as_dict()) == _SOURCED
    commit = Item("c", "cited", (ItemRef("commit", "a" * 40),))
    assert decode_item(commit.as_dict()) == commit


def test_decode_item_plain_string_is_unknown() -> None:
    assert decode_item("old words") == Item("old words", "unknown", (), "")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"text": "t", "basis": "made-up"}, Item("t", "unknown")),
        ({"text": "t"}, Item("t", "unknown")),
        (
            {
                "text": "t",
                "basis": "cited",
                "refs": [
                    {"kind": "pr", "id": "3"},
                    {"kind": "tag", "id": "v1"},
                    {"kind": "issue"},
                    "PR #3",
                    {"kind": "issue", "id": "9", "url": 5},
                ],
                "quote": 7,
            },
            Item("t", "cited", (ItemRef("pr", "3"), ItemRef("issue", "9"))),
        ),
        ({"basis": "quoted"}, None),
        ({"text": 3}, None),
        (42, None),
    ],
)
def test_decode_item_tolerates_malformed_objects(value, expected) -> None:
    assert decode_item(value) == expected


def test_decode_items_mixed_and_malformed_columns() -> None:
    raw = json.dumps(["old", _SOURCED.as_dict(), 5])
    assert decode_items(raw) == (Item("old", "unknown"), _SOURCED)
    assert decode_items("not json") == ()
    assert decode_items('{"text": "x"}') == ()
    assert decode_items(None) == ()
