"""Tests for :mod:`whygraph.analyze.card_bundle` (plan 62 Part B, section 13.2)."""

from __future__ import annotations

import pytest

from whygraph.analyze import CommitEvidence
from whygraph.analyze.card_bundle import (
    CONTEXT_COMMITS,
    FULL_PR_MAX_CHARS,
    FULL_PR_MAX_COUNT,
    LEVELS,
    TOTAL_MAX_CHARS,
    card_bundle,
    level_caps,
)
from whygraph.db.models import Commit, PullRequest


def _commit(sha: str, committed_at: str) -> Commit:
    return Commit(
        sha=sha,
        parent_shas="[]",
        author_name="Jane",
        author_email="jane@example.com",
        authored_at=committed_at,
        committed_at=committed_at,
        subject=f"subject {sha[:4]}",
        body="",
        files_changed=1,
        insertions=1,
        deletions=0,
        scanned_at="2026-05-02T00:00:00Z",
        llm_description="desc",
    )


def _pr(
    number: int,
    *,
    head: str | None = None,
    merge: str | None = None,
    body: str = "body",
) -> PullRequest:
    return PullRequest(
        number=number,
        title=f"PR {number}",
        body=body,
        state="merged",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        merged_at="2026-01-02T00:00:00Z",
        head_sha=head or "deadbeef",
        merge_commit_sha=merge,
        base_ref="main",
        author="octocat",
        html_url=f"https://x/pull/{number}",
        labels="[]",
        commit_titles="[]",
        comments="[]",
        fetched_at="2026-01-02T00:00:00Z",
    )


def _item(
    sha: str,
    day: int,
    source: str = "blame",
    *prs: PullRequest,
    own: bool = True,
) -> CommitEvidence:
    """An evidence item; ``own`` makes a placeholder-head PR its own."""
    for pr in prs:
        if own and pr.head_sha == "deadbeef" and pr.merge_commit_sha is None:
            pr.head_sha = sha
    return CommitEvidence(
        _commit(sha, f"2026-03-{day:02d}T00:00:00Z"),
        pull_requests=prs,
        source=source,
        lines=((1, 2),) if source == "blame" else None,
    )


def _numbers(prs) -> list[int]:
    return [pr.number for pr in prs]


# ---- constants (R45) -------------------------------------------------------


def test_level_constants_are_the_b0_numbers() -> None:
    assert LEVELS == ("low", "medium")
    assert dict(FULL_PR_MAX_COUNT) == {"low": 16, "medium": 16}
    assert dict(FULL_PR_MAX_CHARS) == {"low": 35_000, "medium": 40_000}
    assert dict(TOTAL_MAX_CHARS) == {"low": 158_000, "medium": 163_000}
    assert dict(CONTEXT_COMMITS) == {"low": 0, "medium": 4}


def test_level_values_never_decrease_with_depth() -> None:
    caps = [level_caps(level) for level in LEVELS]
    for shallow, deep in zip(caps, caps[1:]):
        assert deep.full_pr_max_count >= shallow.full_pr_max_count
        assert deep.full_pr_max_chars >= shallow.full_pr_max_chars
        assert deep.total_max_chars >= shallow.total_max_chars
        assert deep.context_commits >= shallow.context_commits


def test_level_caps_raise_a_lowered_deeper_value(monkeypatch) -> None:
    monkeypatch.setitem(FULL_PR_MAX_CHARS, "medium", 1_000)

    assert level_caps("medium").full_pr_max_chars == 35_000


def test_unknown_level_raises() -> None:
    with pytest.raises(ValueError, match="depth level"):
        level_caps("huge")


# ---- rings and units -------------------------------------------------------


def test_every_line_level_item_is_in_ring0_and_area_is_ring1() -> None:
    evidence = [
        _item("a" * 8, 9, "area"),
        _item("b" * 8, 8, "blame"),
        _item("c" * 8, 7, "blame-walked"),
        _item("d" * 8, 6, "predecessor-blame"),
        _item("e" * 8, 5, "area"),
    ]
    for level in LEVELS:
        bundle = card_bundle(evidence, level)
        assert [i.commit.sha[0] for i in bundle.ring0] == ["b", "c", "d"]
        assert [i.commit.sha[0] for i in bundle.ring0_all] == ["b", "c", "d"]
    assert card_bundle(evidence, "low").ring1 == ()
    assert [i.commit.sha[0] for i in card_bundle(evidence, "medium").ring1] == [
        "a",
        "e",
    ]


def test_ring1_takes_the_newest_context_commits_up_to_the_level() -> None:
    evidence = [_item(f"{i:08x}", 20 - i, "area") for i in range(10)]

    bundle = card_bundle(evidence, "medium")

    assert [i.commit.sha for i in bundle.ring1] == [f"{i:08x}" for i in range(4)]
    assert bundle.context_total == 10
    assert len(card_bundle(evidence, "low").ring1) == 0


def _squash_evidence() -> tuple[list[CommitEvidence], PullRequest]:
    squash = _pr(7, head="f2f2f2f2", merge="5555aaaa")
    newer = _item("9999bbbb", 20, "blame", _pr(8))
    s = _item("5555aaaa", 10, "blame", squash)
    f1 = _item("f1f1f1f1", 3, "pr-origin", squash, own=False)
    f2 = _item("f2f2f2f2", 4, "pr-origin", squash)
    return [newer, s, f2, f1], squash


def test_a_squash_unit_renders_as_its_squash_commit() -> None:
    evidence, squash = _squash_evidence()

    for level in ("low", "medium"):
        bundle = card_bundle(evidence, level)
        assert [i.commit.sha for i in bundle.ring0] == ["9999bbbb", "5555aaaa"]
        assert len(bundle.ring0_all) == 4
        assert len(bundle.units) == 2
        assert 7 in _numbers(bundle.full_prs)


def test_a_pr_origin_commit_without_its_squash_commit_is_its_own_unit() -> None:
    """C3a: the squash commit is not in the evidence, so the draft commit stands alone."""
    squash = _pr(7, head="f2f2f2f2", merge="5555aaaa")
    f2 = _item("f2f2f2f2", 4, "pr-origin", squash)
    evidence = [_item("9999bbbb", 20, "blame", _pr(8)), f2]

    for level in LEVELS:
        bundle = card_bundle(evidence, level)

        assert [i.commit.sha for i in bundle.ring0] == ["9999bbbb", "f2f2f2f2"]
        assert [u.head.commit.sha for u in bundle.units] == ["9999bbbb", "f2f2f2f2"]


def test_a_squash_unit_is_dated_by_its_squash_commit() -> None:
    """The oldest dated unit is the squash unit (day 10), not its originals (3-4)."""
    evidence, _ = _squash_evidence()
    evidence.append(_item("0101cccc", 6, "blame-walked", _pr(9)))

    bundle = card_bundle(evidence, "medium")

    # Ring 0 order: blame units newest first, then the oldest dated unit.
    assert [u.head.commit.sha for u in bundle.units] == [
        "9999bbbb",
        "5555aaaa",
        "0101cccc",
    ]


def test_ring0_order_puts_the_oldest_dated_unit_second() -> None:
    evidence = [
        _item("1111aaaa", 30, "blame-walked", _pr(1)),
        _item("2222aaaa", 20, "blame", _pr(2)),
        _item("3333aaaa", 10, "pr-origin", _pr(3)),
        _item("4444aaaa", 5, "predecessor-blame", _pr(4)),
    ]

    bundle = card_bundle(evidence, "medium")

    assert [u.head.commit.sha[:4] for u in bundle.units] == [
        "2222",
        "4444",
        "1111",
        "3333",
    ]
    assert _numbers(bundle.full_prs) == [2, 4, 1, 3]


def test_an_undated_item_never_fills_the_oldest_slot() -> None:
    undated = _item("0000aaaa", 1, "pr-origin", _pr(5))
    undated.commit.committed_at = ""
    evidence = [
        _item("2222aaaa", 20, "blame", _pr(2)),
        _item("1111aaaa", 15, "blame-walked", _pr(1)),
        undated,
    ]

    bundle = card_bundle(evidence, "medium")

    assert [u.head.commit.sha[:4] for u in bundle.units][:2] == ["2222", "1111"]


# ---- full PRs (R34) ---------------------------------------------------------


def test_low_shows_own_prs_of_blame_units_only() -> None:
    evidence = [
        _item("1111aaaa", 30, "blame-walked", _pr(1)),
        _item("2222aaaa", 20, "blame", _pr(2)),
    ]

    low = card_bundle(evidence, "low")
    medium = card_bundle(evidence, "medium")

    assert _numbers(low.full_prs) == [2]
    assert _numbers(low.title_prs) == [1]
    assert low.demoted == ()
    assert _numbers(medium.full_prs) == [2, 1]


def test_roster_only_prs_are_never_full() -> None:
    release = _pr(40)  # holds the commit only through its roster
    item = _item("2222aaaa", 20, "blame", _pr(2))
    item = CommitEvidence(
        item.commit, (*item.pull_requests, release), source="blame", lines=((1, 2),)
    )

    bundle = card_bundle([item], "medium")

    assert _numbers(bundle.full_prs) == [2]
    assert 40 not in _numbers(bundle.title_prs)
    assert bundle.own["2222aaaa"] == frozenset({2})


def test_stopper_1_count_ceiling_stops_at_the_first_pr_past_it(monkeypatch) -> None:
    monkeypatch.setitem(FULL_PR_MAX_COUNT, "low", 2)
    monkeypatch.setitem(FULL_PR_MAX_COUNT, "medium", 2)
    evidence = [_item(f"{i:04x}aaaa", 20 - i, "blame", _pr(i)) for i in range(1, 5)]

    bundle = card_bundle(evidence, "medium")

    assert _numbers(bundle.full_prs) == [1, 2]
    assert _numbers(bundle.demoted) == [3, 4]
    assert _numbers(bundle.title_prs) == [3, 4]


def test_stopper_1_char_budget_stops_at_the_first_block_that_does_not_fit() -> None:
    """Stop-at-first: a small PR after a too-big one is demoted too."""
    sizes = {1: 30_000, 2: 15_000, 3: 10}
    evidence = [_item(f"{n:04x}aaaa", 20 - n, "blame", _pr(n)) for n in sizes]

    bundle = card_bundle(evidence, "medium", pr_chars=lambda pr: sizes[pr.number])

    assert _numbers(bundle.full_prs) == [1]
    assert _numbers(bundle.demoted) == [2, 3]


def test_each_level_is_a_superset_of_the_one_below() -> None:
    """R24: full PRs and Ring 0 commits only grow with depth (stopper 2 aside)."""
    evidence, _ = _squash_evidence()
    evidence += [
        _item("1111aaaa", 2, "blame-walked", _pr(11)),
        _item("2222aaaa", 1, "predecessor-blame", _pr(12)),
        _item("3333aaaa", 25, "area", _pr(13)),
    ]
    sizes = {8: 20_000, 7: 14_000, 11: 4_000, 12: 2_000}
    bundles = [
        card_bundle(evidence, level, pr_chars=lambda pr: sizes.get(pr.number, 10))
        for level in LEVELS
    ]
    for shallow, deep in zip(bundles, bundles[1:]):
        assert set(_numbers(shallow.full_prs)) <= set(_numbers(deep.full_prs))
        assert {i.commit.sha for i in shallow.items} <= {
            i.commit.sha for i in deep.items
        }
