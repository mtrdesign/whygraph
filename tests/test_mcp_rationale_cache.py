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
from sqlmodel import select

from whygraph.analyze import CommitEvidence, Item, ItemRef, Rationale
from whygraph.db import get_session
from whygraph.db.models import Commit, RationaleCache
from whygraph.mcp.rationale import whygraph_rationale_brief
from whygraph.mcp.rationale_cache import (
    _fingerprint,
    decode_item,
    decode_items,
    lookup_card,
    lookup_cached,
    lookup_exact,
    lookup_overlapping,
    ring0_fingerprint,
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
        paths: frozenset[str] | None = None,
        file_lines: Sequence[str] | None = None,
        target_range: tuple[int, int] | None = None,
        **_: object,
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

    assert lookup_cached(target, evidence, "fake", None, min_depth="medium") is None


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
    hit = lookup_cached(target, evidence, "fake", None, min_depth="medium")
    assert hit is not None and hit.deferred_shas == "aaa bbb"
    assert hit.rationale.purpose == "p"
    exact = lookup_exact(target, "fake", None)
    assert exact is not None and exact.deferred_shas == "aaa bbb"


def test_update_deferred_sets_the_list_when_the_row_is_unchanged(
    whygraph_db_initialized: Path,
) -> None:
    target, evidence, _ = _stored_card(whygraph_db_initialized, deferred_shas="aaa bbb")
    seen = lookup_cached(target, evidence, "fake", None, min_depth="medium")
    assert seen is not None
    assert update_deferred(target, "fake", None, seen=seen, deferred_shas="bbb")
    after = lookup_cached(target, evidence, "fake", None, min_depth="medium")
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
    seen = lookup_cached(target, evidence, "fake", None, min_depth="medium")
    assert seen is not None and seen.deferred_shas == "aaa"
    store_cached(target, evidence, rationale, "fake", None)  # final, same second
    assert not update_deferred(target, "fake", None, seen=seen, deferred_shas="bbb")
    after = lookup_cached(target, evidence, "fake", None, min_depth="medium")
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
    seen = lookup_cached(target, evidence, "fake", None, min_depth="medium")
    assert seen is not None
    # a final store lands in the same second, then a stale completion tries to write
    store_cached(target, evidence, rationale, "fake", None)
    newer = replace(rationale, purpose="completed")
    assert store_cached(target, evidence, newer, "fake", None, only_if=seen) is None
    after = lookup_cached(target, evidence, "fake", None, min_depth="medium")
    assert after is not None and after.rationale.purpose == "p"
    assert after.deferred_shas == ""
    # against the row as it now is, the write goes through
    assert (
        store_cached(target, evidence, newer, "fake", None, only_if=after) is not None
    )
    final = lookup_cached(target, evidence, "fake", None, min_depth="medium")
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


# ---- Ring 0 fingerprints, depth and symbol following (plan 62 section 13.5) --


def _ev(sha: str, at: str, source: str = "blame") -> CommitEvidence:
    """One evidence item over a fresh commit row (``sha`` padded to 40 hex)."""
    return CommitEvidence(
        commit=_db_commit(sha.ljust(40, "0"), committed_at=at), source=source
    )


_RATIONALE = Rationale(
    purpose="p",
    why="w",
    constraints=(),
    tradeoffs=(),
    risks=(),
    model="m",
    provider="fake",
)

_LINES = [
    _ev("b1", "2026-01-10T00:00:00+00:00"),
    _ev("b2", "2026-01-02T00:00:00+00:00"),
]
_C1 = _ev("c1", "2026-01-05T00:00:00+00:00", "area")


def _area(n: int, day: int) -> CommitEvidence:
    return _ev(f"a{n}", f"2026-02-{day:02d}T00:00:00+00:00", "area")


_BUILT = "2026-01-15T00:00:00+00:00"
"""When :func:`_store` builds a card: after ``_LINES``, before ``_area``."""


def _store(
    target: Target,
    evidence: Sequence[CommitEvidence],
    *,
    depth: int = 1,
    context_shas: str = "",
    seen=None,  # noqa: ANN001
    purpose: str = "p",
    built: str = _BUILT,
) -> str | None:
    """Store a card as if built at ``built`` (its ``cached_at``)."""
    from unittest import mock

    with mock.patch("whygraph.mcp.rationale_cache._now_iso", lambda: built):
        return store_cached(
            target,
            evidence,
            replace(_RATIONALE, purpose=purpose),
            "fake",
            None,
            depth=depth,
            context_shas=context_shas,
            seen=seen,
        )


def _lookup(target: Target, evidence, min_depth: str = "medium"):  # noqa: ANN001, ANN202
    return lookup_card(target, evidence, "fake", None, min_depth=min_depth)


_T = Target(path="a.py", line_start=10, line_end=20, qualified_name="mod.f")


def test_ring0_fingerprint_ignores_area_items_and_order() -> None:
    lines = list(_LINES)
    assert ring0_fingerprint(lines) == ring0_fingerprint([*reversed(lines), _C1])
    assert ring0_fingerprint(lines) != ring0_fingerprint(lines[:1])
    # The legacy whole-bundle hash still sees every item.
    assert _fingerprint(lines) != _fingerprint([*lines, _C1])
    assert ring0_fingerprint([_C1]) == _fingerprint([])


def test_a_commit_on_other_lines_counts_as_context_not_a_miss(
    whygraph_db_initialized: Path,
) -> None:
    """Acceptance 1: 0 commits after the card was built is fresh, 1-2 are
    context_stale, 3 is a miss."""
    evidence = [*_LINES, _C1]
    assert _store(_T, evidence, context_shas=_C1.commit.sha) is not None

    fresh = _lookup(_T, evidence)
    assert fresh.card is not None and fresh.card.context_stale is False
    assert fresh.card.depth == 1 and fresh.card.context_shas == _C1.commit.sha
    older = _ev("o1", "2026-01-01T00:00:00+00:00", "area")
    assert _lookup(_T, [*evidence, older]).card.context_stale is False

    one = [_area(1, 1), *evidence]
    two = [_area(2, 2), *one]
    three = [_area(3, 3), *two]
    assert _lookup(_T, one).card.context_stale is True
    assert _lookup(_T, two).card.context_stale is True
    miss = _lookup(_T, three)
    assert miss.card is None
    assert miss.seen is not None and miss.seen.depth == 1
    # A change to the target's own lines is a miss whatever the context.
    lines_changed = _lookup(_T, [_ev("b3", "2026-03-01T00:00:00+00:00"), *evidence])
    assert lines_changed.card is None and lines_changed.seen is not None


def test_a_low_row_is_never_context_stale(whygraph_db_initialized: Path) -> None:
    _store(_T, _LINES, depth=0)
    three = [_area(1, 1), _area(2, 2), _area(3, 3), *_LINES]
    hit = _lookup(_T, three, "low")
    assert hit.card is not None and hit.card.context_stale is False


def test_context_counts_only_commits_after_the_card_was_built(
    whygraph_db_initialized: Path,
) -> None:
    _store(_T, _LINES, context_shas="ab" * 20)
    before = _ev("o1", "2026-01-14T00:00:00+00:00", "area")  # newer than b1
    assert _lookup(_T, [*_LINES, before]).card.context_stale is False
    undated = _ev("u1", "", "area")
    assert _lookup(_T, [*_LINES, undated]).card.context_stale is False
    rendered = CommitEvidence(
        commit=_db_commit("ab" * 20, committed_at="2026-02-01T00:00:00+00:00"),
        source="area",
    )
    assert _lookup(_T, [*_LINES, rendered]).card.context_stale is False
    after = _ev("n1", "2026-01-16T00:00:00+00:00", "area")
    assert _lookup(_T, [*_LINES, after]).card.context_stale is True


def test_a_stored_context_commit_never_moves_the_reference(
    whygraph_db_initialized: Path,
) -> None:
    """Older than the card: not counted, even when newer than its context."""
    gone = _ev("c9", "2026-01-11T00:00:00+00:00", "area")
    _store(_T, [*_LINES, gone], context_shas=gone.commit.sha)
    between = [_ev(f"m{n}", f"2026-01-1{n}T00:00:00+00:00", "area") for n in (2, 3, 4)]
    hit = _lookup(_T, [*_LINES, *between])
    assert hit.card is not None and hit.card.context_stale is False


def test_a_card_whose_context_rendered_nothing_does_not_loop(
    whygraph_db_initialized: Path,
) -> None:
    """Ring 1 had candidates but rendered none: the next request still hits."""
    candidates = [_area(3, 3), _area(2, 2), _area(1, 1)]
    evidence = [*_LINES, *candidates]
    _store(_T, evidence, context_shas="", built="2026-02-10T00:00:00+00:00")

    again = _lookup(_T, evidence)
    assert again.card is not None and again.card.context_stale is False
    later = [_area(n, day) for n, day in ((5, 12), (4, 11))]
    two = _lookup(_T, [*later, *evidence])
    assert two.card is not None and two.card.context_stale is True
    three = _lookup(_T, [_area(6, 13), *later, *evidence])
    assert three.card is None


def test_a_deeper_card_satisfies_a_shallower_request(
    whygraph_db_initialized: Path,
) -> None:
    """Acceptance 3: a low request takes a medium card; a medium one rejects low."""
    _store(_T, _LINES, depth=1)
    assert _lookup(_T, _LINES, "low").card is not None
    assert _lookup(_T, _LINES, "medium").card is not None
    too_shallow = _lookup(_T, _LINES, "high")
    assert too_shallow.card is None and too_shallow.seen.depth == 1

    other = replace(_T, line_start=30, line_end=40, qualified_name=None)
    _store(other, _LINES, depth=0)
    low_only = _lookup(other, _LINES, "medium")
    assert low_only.card is None and low_only.seen.depth == 0
    assert _lookup(other, _LINES, "low").card is not None


def test_a_shallower_store_never_overwrites_a_deeper_card(
    whygraph_db_initialized: Path,
) -> None:
    """Concurrent misses: the high card stands whichever request stores last."""
    assert _store(_T, _LINES, depth=2, purpose="high") is not None
    assert _store(_T, _LINES, depth=0, purpose="low") is None
    row = lookup_exact(_T, "fake", None)
    assert (row.rationale.purpose, row.depth) == ("high", 2)
    # Low first, then high: high replaces it.
    other = replace(_T, line_start=30, line_end=40)
    assert _store(other, _LINES, depth=0, purpose="low") is not None
    assert _store(other, _LINES, depth=2, purpose="high") is not None
    assert lookup_exact(other, "fake", None).depth == 2


def test_a_shallower_store_replaces_the_row_it_saw_or_a_dead_one(
    whygraph_db_initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store(_T, _LINES, depth=2, purpose="high")
    seen = lookup_exact(_T, "fake", None)
    monkeypatch.setattr(
        "whygraph.mcp.rationale_cache._now_iso", lambda: "2027-01-01T00:00:00+00:00"
    )
    assert _store(_T, _LINES, depth=0, purpose="low", seen=seen) is not None
    assert lookup_exact(_T, "fake", None).depth == 0
    # A deeper row whose lines changed is dead: any card replaces it.
    other = replace(_T, line_start=30, line_end=40)
    _store(other, _LINES, depth=3, purpose="xhigh")
    changed = [_ev("b3", "2026-03-01T00:00:00+00:00"), *_LINES]
    assert _store(other, changed, depth=1, purpose="new lines") is not None
    assert lookup_exact(other, "fake", None).rationale.purpose == "new lines"


def test_a_legacy_row_is_checked_the_old_way_and_replaced_by_any_card(
    whygraph_db_initialized: Path,
) -> None:
    """Acceptance 7: a legacy row hits on its whole-bundle fingerprint only."""
    evidence = [*_LINES, _C1]
    with get_session() as session:
        session.add(
            RationaleCache(
                path=_T.path,
                line_start=_T.line_start,
                line_end=_T.line_end,
                provider="fake",
                model="default",
                evidence_fingerprint=_fingerprint(evidence),
                cached_at="2026-01-01T00:00:00+00:00",
                purpose="legacy",
                why="w",
                constraints="[]",
                tradeoffs="[]",
                risks="[]",
                qualified_name=_T.qualified_name,
            )
        )
    hit = _lookup(_T, evidence)
    assert hit.card is not None and hit.card.depth is None
    assert hit.card.level == "medium" and hit.card.context_stale is False
    assert _lookup(_T, evidence, "high").card is None
    # Any change to the bundle (an area commit too) is a miss, as before.
    assert _lookup(_T, [_area(1, 1), *evidence]).card is None
    # A legacy row never follows its symbol.
    moved = replace(_T, line_start=50, line_end=60)
    assert _lookup(moved, evidence).card is None
    assert lookup_exact(_T, "fake", None).rationale.purpose == "legacy"
    # And a shallow card replaces it.
    assert _store(_T, evidence, depth=0) is not None
    assert lookup_exact(_T, "fake", None).depth == 0


def test_a_card_follows_its_symbol_to_new_lines(whygraph_db_initialized: Path) -> None:
    """Acceptance 2: same symbol, same line-level commits, new range: the row moves."""
    _store(_T, _LINES, purpose="the card")
    moved = replace(_T, line_start=14, line_end=24)

    found = _lookup(moved, _LINES)

    assert found.card is not None and found.card.rationale.purpose == "the card"
    assert (found.card.line_start, found.card.line_end) == (14, 24)
    assert lookup_exact(_T, "fake", None) is None
    assert lookup_exact(moved, "fake", None).rationale.purpose == "the card"
    with get_session() as session:
        assert len(session.exec(select(RationaleCache)).all()) == 1


def test_following_replaces_the_stale_row_at_the_new_key(
    whygraph_db_initialized: Path,
) -> None:
    _store(_T, _LINES, purpose="the card")
    moved = replace(_T, line_start=14, line_end=24)
    _store(moved, [_ev("zz", "2025-01-01T00:00:00+00:00")], purpose="stale here")

    found = _lookup(moved, _LINES)

    assert found.card is not None and found.card.rationale.purpose == "the card"
    with get_session() as session:
        rows = [(r.line_start, r.purpose) for r in session.exec(select(RationaleCache))]
    assert rows == [(14, "the card")]


def test_following_needs_the_name_and_ring0_and_ranks_candidates(
    whygraph_db_initialized: Path,
) -> None:
    _store(_T, _LINES, purpose="near")  # 10-20
    _store(replace(_T, line_start=80, line_end=90), _LINES, purpose="far")
    _store(
        replace(_T, line_start=40, line_end=45, qualified_name="mod.g"),
        _LINES,
        purpose="other symbol",
    )
    _store(
        replace(_T, line_start=60, line_end=70),
        [_ev("zz", "2025-01-01T00:00:00+00:00")],
        purpose="other lines",
    )
    # No name (the platform's explicit-range branch): no following.
    unnamed = replace(_T, line_start=12, line_end=22, qualified_name=None)
    assert _lookup(unnamed, _LINES).card is None
    # Different line-level commits: no following.
    other = [_ev("b9", "2026-01-11T00:00:00+00:00")]
    assert _lookup(replace(_T, line_start=12, line_end=22), other).card is None
    # The nearest same-name row with the same Ring 0 moves.
    found = _lookup(replace(_T, line_start=12, line_end=22), _LINES)
    assert found.card.rationale.purpose == "near"
    assert lookup_exact(replace(_T, line_start=80, line_end=90), "fake", None)


def test_a_followed_row_too_shallow_moves_and_is_the_seen_row(
    whygraph_db_initialized: Path,
) -> None:
    _store(_T, _LINES, depth=0, purpose="low")
    moved = replace(_T, line_start=14, line_end=24)
    found = _lookup(moved, _LINES, "medium")
    assert found.card is None
    assert found.seen is not None and found.seen.line_start == 14
    assert lookup_exact(moved, "fake", None).rationale.purpose == "low"


def test_store_cached_round_trips_depth_context_and_coverage(
    whygraph_db_initialized: Path,
) -> None:
    from whygraph.analyze import CardCoverage

    coverage = CardCoverage(line_commits=2, context_commits=1, trimmed=("x_y",))
    store_cached(
        _T,
        _LINES,
        replace(_RATIONALE, coverage=coverage),
        "fake",
        None,
        depth=2,
        context_shas="abc1234",
    )
    row = lookup_exact(_T, "fake", None)
    assert (row.depth, row.context_shas, row.level) == (2, "abc1234", "high")
    assert row.rationale.coverage == coverage
    assert row.evidence_fingerprint == ring0_fingerprint(_LINES)
