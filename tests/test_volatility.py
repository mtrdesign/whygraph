from __future__ import annotations

import time
from pathlib import Path

from whygraph.cochange.service import (
    VOLATILITY_VERSION,
    VolatilityService,
    volatility_fingerprint,
)
from whygraph.cochange.types import VolatilityReport


def test_empty_report_when_file_has_no_history(init_git_repo) -> None:
    repo = init_git_repo()
    svc = VolatilityService(repo)
    report = svc.report_for("never.py")
    assert report.commits_total == 0
    assert report.commits_90d == 0
    assert report.distinct_authors == 0
    assert report.days_since_last_change is None


def test_single_commit_volatility(init_git_repo, git_commit) -> None:
    repo = init_git_repo()
    git_commit(repo, "a.py", "v1\n", message="add a")

    # Pin clock to "right after the commit" so days_since_last_change is small
    # and the 90/180/365 buckets all include it.
    svc = VolatilityService(repo, now=lambda: time.time())
    report = svc.report_for("a.py")
    assert report.commits_total == 1
    assert report.commits_90d == 1
    assert report.commits_180d == 1
    assert report.commits_365d == 1
    assert report.distinct_authors == 1
    assert report.days_since_last_change is not None
    assert report.days_since_last_change <= 1


def test_time_buckets_split_old_vs_recent(init_git_repo, git_commit) -> None:
    repo = init_git_repo()
    git_commit(repo, "a.py", "v1\n", message="commit")

    # Pretend "now" is 200 days after the commit's wall-clock time.
    real_now = time.time()
    pinned_now = real_now + 200 * 86400
    svc = VolatilityService(repo, now=lambda: pinned_now)
    report = svc.report_for("a.py")
    # 200d > 90d but ≤ 365d.
    assert report.commits_90d == 0
    assert report.commits_180d == 0
    assert report.commits_365d == 1
    assert report.commits_total == 1
    assert report.days_since_last_change == 200


def test_distinct_authors_dedup(init_git_repo, git_commit) -> None:
    repo = init_git_repo()
    # The git_commit fixture always uses the same author name "Test", so two
    # commits → 1 distinct author. That's exactly the dedup we want to verify.
    git_commit(repo, "a.py", "v1\n", message="add")
    git_commit(repo, "a.py", "v2\n", message="edit")

    svc = VolatilityService(repo, now=lambda: time.time())
    report = svc.report_for("a.py")
    assert report.commits_total == 2
    assert report.distinct_authors == 1


# ---------------------------------------------------------------------------
# volatility_fingerprint
# ---------------------------------------------------------------------------


def _report(*, head_sha: str = "h1", target_file: str = "a.py") -> VolatilityReport:
    return VolatilityReport(
        target_file=target_file,
        head_sha=head_sha,
        commits_total=0,
        commits_90d=0,
        commits_180d=0,
        commits_365d=0,
        distinct_authors=0,
        days_since_last_change=None,
    )


def test_fingerprint_deterministic() -> None:
    assert volatility_fingerprint(_report()) == volatility_fingerprint(_report())


def test_fingerprint_changes_when_head_changes() -> None:
    a = volatility_fingerprint(_report(head_sha="h1"))
    b = volatility_fingerprint(_report(head_sha="h2"))
    assert a != b


def test_fingerprint_independent_of_clock_derived_fields() -> None:
    """Hashing inputs (head + file) instead of report values keeps the
    fingerprint stable as wall-clock advances. Two reports with different
    `commits_90d` and `days_since_last_change` but the same HEAD + file should
    produce the same fingerprint — that's the whole point of the
    inputs-not-values design.
    """
    base = _report()
    drifted = VolatilityReport(
        target_file=base.target_file,
        head_sha=base.head_sha,
        commits_total=42,
        commits_90d=10,
        commits_180d=20,
        commits_365d=30,
        distinct_authors=5,
        days_since_last_change=99,
    )
    assert volatility_fingerprint(base) == volatility_fingerprint(drifted)


def test_fingerprint_includes_version_constant() -> None:
    import hashlib

    expected_payload = f"volatility|{VOLATILITY_VERSION}|h1|a.py"
    expected = hashlib.sha256(expected_payload.encode("utf-8")).hexdigest()
    assert volatility_fingerprint(_report()) == expected
