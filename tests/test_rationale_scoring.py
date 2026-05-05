from __future__ import annotations

import pytest

from whygraph.evidence.types import EvidenceRecord
from whygraph.rationale import CONFIDENCE_CEILING, score_confidence


def _commit(*, ref: str = "abc", author: str | None = "Alice") -> EvidenceRecord:
    return EvidenceRecord(
        id=0,
        node_id="n_a",
        qualified_name="pkg.a",
        source="git_commit",
        ref=ref,
        payload={"subject": "x"} if author is None else {"subject": "x", "author": author},
        collected_at=0,
    )


def _pr() -> EvidenceRecord:
    return EvidenceRecord(
        id=0,
        node_id="n_a",
        qualified_name="pkg.a",
        source="pr",
        ref="42",
        payload={"title": "Refactor X"},
        collected_at=0,
    )


def _issue() -> EvidenceRecord:
    return EvidenceRecord(
        id=0,
        node_id="n_a",
        qualified_name="pkg.a",
        source="issue",
        ref="100",
        payload={"title": "Bug"},
        collected_at=0,
    )


def test_score_zero_when_no_evidence_and_empty_rationale() -> None:
    assert score_confidence(evidence=[], constraints=[], risks=[]) == 0.0


def test_score_only_rationale_content_term() -> None:
    # No evidence, but the rationale has content → only that term fires.
    score = score_confidence(evidence=[], constraints=["must be sync"], risks=[])
    assert score == pytest.approx(0.20 * CONFIDENCE_CEILING)


def test_score_treats_constraints_or_risks_as_content() -> None:
    """has_rationale_content fires when EITHER list is non-empty."""
    a = score_confidence(evidence=[], constraints=["x"], risks=[])
    b = score_confidence(evidence=[], constraints=[], risks=["y"])
    c = score_confidence(evidence=[], constraints=["x"], risks=["y"])
    # All three fire the same single content term — content is binary, not
    # a count.
    assert a == b == c


def test_score_single_commit_single_author() -> None:
    # Terms: has_any_commits (0.20) + commits_norm (1/5 = 0.04)
    #      + authors_norm (1/3 ≈ 0.0667) → 0.3067 raw → × 0.85
    score = score_confidence(
        evidence=[_commit()], constraints=[], risks=[]
    )
    raw = 0.20 + 0.20 * (1 / 5) + 0.20 * (1 / 3)
    assert score == pytest.approx(raw * CONFIDENCE_CEILING)


def test_score_commit_count_saturates_at_five() -> None:
    five = [_commit(ref=str(i), author="A") for i in range(5)]
    six = [_commit(ref=str(i), author="A") for i in range(6)]
    s5 = score_confidence(evidence=five, constraints=[], risks=[])
    s6 = score_confidence(evidence=six, constraints=[], risks=[])
    assert s5 == s6


def test_score_author_count_saturates_at_three() -> None:
    # Hold commit count constant to isolate the author-saturation effect:
    # 5 commits in both cases; only the number of distinct authors varies.
    three_authors = [
        _commit(ref=str(i), author=name)
        for i, name in enumerate(["A", "B", "C", "A", "B"])
    ]
    five_authors = [
        _commit(ref=str(i), author=name)
        for i, name in enumerate(["A", "B", "C", "D", "E"])
    ]
    s3 = score_confidence(evidence=three_authors, constraints=[], risks=[])
    s5 = score_confidence(evidence=five_authors, constraints=[], risks=[])
    assert s3 == s5


def test_score_distinct_authors_dedup() -> None:
    """Five commits, all by Alice → counts as one author."""
    commits = [_commit(ref=str(i), author="Alice") for i in range(5)]
    score = score_confidence(evidence=commits, constraints=[], risks=[])
    raw = 0.20 + 0.20 * 1.0 + 0.20 * (1 / 3)  # 1 author / 3
    assert score == pytest.approx(raw * CONFIDENCE_CEILING)


def test_score_missing_author_doesnt_count() -> None:
    commits = [_commit(ref="a", author=None), _commit(ref="b", author=None)]
    score = score_confidence(evidence=commits, constraints=[], risks=[])
    raw = 0.20 + 0.20 * (2 / 5) + 0.20 * 0.0  # 0 known authors
    assert score == pytest.approx(raw * CONFIDENCE_CEILING)


def test_score_pr_evidence_adds_term() -> None:
    base = score_confidence(evidence=[_commit()], constraints=[], risks=[])
    with_pr = score_confidence(evidence=[_commit(), _pr()], constraints=[], risks=[])
    assert with_pr - base == pytest.approx(0.10 * CONFIDENCE_CEILING)


def test_score_issue_evidence_adds_term() -> None:
    base = score_confidence(evidence=[_commit()], constraints=[], risks=[])
    with_issue = score_confidence(
        evidence=[_commit(), _issue()], constraints=[], risks=[]
    )
    assert with_issue - base == pytest.approx(0.10 * CONFIDENCE_CEILING)


def test_score_max_signals_saturates_at_ceiling() -> None:
    """Every term firing at saturation → raw 1.0 → score lands exactly at
    the 0.85 ceiling."""
    commits = [_commit(ref=str(i), author=name) for i, name in enumerate(
        ["A", "B", "C", "A", "B"]
    )]
    score = score_confidence(
        evidence=[*commits, _pr(), _issue()],
        constraints=["must be sync"],
        risks=["regression risk"],
    )
    assert score == pytest.approx(CONFIDENCE_CEILING)


def test_score_never_exceeds_ceiling() -> None:
    """Even with the most extreme inputs the function stops at the ceiling."""
    many_commits = [
        _commit(ref=str(i), author=f"author_{i}") for i in range(50)
    ]
    score = score_confidence(
        evidence=[*many_commits, _pr(), _pr(), _issue(), _issue()],
        constraints=["a"] * 20,
        risks=["b"] * 20,
    )
    assert score <= CONFIDENCE_CEILING + 1e-9
