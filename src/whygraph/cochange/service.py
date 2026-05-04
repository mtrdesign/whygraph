from __future__ import annotations

import fnmatch
import hashlib
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Callable

from whygraph.cochange import git as cochange_git
from whygraph.cochange.types import (
    CoChangeNeighbor,
    CoChangeReport,
    VolatilityReport,
)

DEFAULT_TOP_K = 8

# Below this commit count, percentages are noise. The validation pass on the
# engage repo found that symbols with 1–2 commits produced "100% co-change"
# for files that simply happened to be in those commits — meaningless coupling
# claims. Skip rendering when the denominator is too small to support a claim.
MIN_COMMITS_FOR_DISPLAY = 3

# Files that share only a single commit with the target are coincidence, not
# coupling — they dominated the long tail in the engage smoke run (e.g. "top
# 8 of 1216" because 1216 files had ever appeared in any of 12 commits).
# Filtering at ≥2 keeps the denominator interpretable: anything in the count
# has co-changed at least twice and is plausibly a real coupling.
MIN_COCHANGE_COUNT = 2

# Bumped whenever the cochange algorithm changes (denylist, ranking, top_k
# meaning, MIN_COCHANGE_COUNT default). Folded into the fingerprint so cached
# rationales invalidate.
#   v1 → v2: introduced MIN_COCHANGE_COUNT default of 2, dropping
#            single-occurrence coincidences from the neighbors + truncation
#            count.
COCHANGE_VERSION = "v2"
VOLATILITY_VERSION = "v1"

# Files that change with everything else (lockfiles), are noise (editor
# configs), or are generated artifacts — they would dominate co-change rankings
# without telling the LLM anything useful about intent coupling.
DEFAULT_DENYLIST: tuple[str, ...] = (
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "go.sum",
    ".gitignore",
    ".editorconfig",
    "*.min.js",
    "*.generated.*",
    "dist/*",
    "build/*",
)


def _matches_denylist(file_path: str, denylist: tuple[str, ...]) -> bool:
    name = file_path.split("/")[-1]
    for pattern in denylist:
        if fnmatch.fnmatch(file_path, pattern) or fnmatch.fnmatch(name, pattern):
            return True
    return False


# ---------------------------------------------------------------------------
# CoChangeService
# ---------------------------------------------------------------------------


class CoChangeService:
    """Computes co-change neighbors for a file, with a per-commit SQLite cache.

    The cache (`commit_files` + `commit_cache_meta`) is keyed by commit sha,
    not by `(file, head)`. Commits are immutable, so a row never invalidates;
    the same cached rows benefit every symbol whose file appears in that
    commit's diff.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        repo_root: Path,
        *,
        denylist: tuple[str, ...] = DEFAULT_DENYLIST,
    ) -> None:
        self._conn = conn
        self._repo_root = repo_root
        self._denylist = denylist

    def report_for(
        self,
        target_file: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        min_cochange_count: int = MIN_COCHANGE_COUNT,
    ) -> CoChangeReport:
        head = cochange_git.head_sha(self._repo_root)
        commits = cochange_git.commits_touching_file(self._repo_root, target_file)
        if not commits:
            return CoChangeReport(
                target_file=target_file,
                head_sha=head,
                commits_considered=0,
                neighbors=[],
                truncated=0,
            )

        for sha in commits:
            self._ensure_commit_cached(sha)

        cooccurrence = self._aggregate(commits, target_file)
        # Filter BEFORE sort+truncate so the truncation denominator reflects
        # only files that survived the noise gate. Otherwise users see "top 8
        # of 1216" where 1200+ are 1-shot coincidences — accurate but
        # misleading.
        meaningful = [
            (path, count)
            for path, count in cooccurrence.items()
            if count >= min_cochange_count
        ]
        ranked = sorted(meaningful, key=lambda item: (-item[1], item[0]))
        kept_pairs = ranked[:top_k]
        truncated = max(0, len(ranked) - top_k)
        neighbors = [
            CoChangeNeighbor(
                file_path=path,
                cochange_count=count,
                target_commits_total=len(commits),
            )
            for path, count in kept_pairs
        ]
        return CoChangeReport(
            target_file=target_file,
            head_sha=head,
            commits_considered=len(commits),
            neighbors=neighbors,
            truncated=truncated,
        )

    def _ensure_commit_cached(self, sha: str) -> None:
        row = self._conn.execute(
            "SELECT 1 FROM commit_cache_meta WHERE commit_sha = ?",
            (sha,),
        ).fetchone()
        if row is not None:
            return
        files = cochange_git.files_in_commit(self._repo_root, sha)
        # Even on empty/failed fetch we still record the meta row so the next
        # call doesn't re-shell out for the same sha.
        if files:
            self._conn.executemany(
                "INSERT OR IGNORE INTO commit_files(commit_sha, file_path) "
                "VALUES (?, ?)",
                [(sha, f) for f in files],
            )
        self._conn.execute(
            "INSERT OR IGNORE INTO commit_cache_meta(commit_sha, cached_at) "
            "VALUES (?, ?)",
            (sha, int(time.time())),
        )

    def _aggregate(self, commits: list[str], target_file: str) -> Counter[str]:
        counts: Counter[str] = Counter()
        if not commits:
            return counts
        # Single query over all commits keeps this O(rows-touched), not
        # O(commits) round-trips.
        placeholders = ",".join("?" * len(commits))
        rows = self._conn.execute(
            f"SELECT file_path FROM commit_files "
            f"WHERE commit_sha IN ({placeholders})",
            tuple(commits),
        ).fetchall()
        for row in rows:
            other = row[0] if not isinstance(row, sqlite3.Row) else row["file_path"]
            if other == target_file:
                continue
            if _matches_denylist(other, self._denylist):
                continue
            counts[other] += 1
        return counts


# ---------------------------------------------------------------------------
# VolatilityService
# ---------------------------------------------------------------------------


class VolatilityService:
    """Per-file volatility — no cache, just a single `git log` call.

    File-level scope (not line-range): faster, robust to refactors, and the
    rationale-relevant signal is "is this area churning" not "is this exact
    range churning."
    """

    def __init__(
        self,
        repo_root: Path,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._repo_root = repo_root
        self._now = now

    def report_for(self, target_file: str) -> VolatilityReport:
        head = cochange_git.head_sha(self._repo_root)
        records = cochange_git.commits_with_metadata_for_file(
            self._repo_root, target_file
        )
        if not records:
            return VolatilityReport(
                target_file=target_file,
                head_sha=head,
                commits_total=0,
                commits_90d=0,
                commits_180d=0,
                commits_365d=0,
                distinct_authors=0,
                days_since_last_change=None,
            )
        now = self._now()
        cutoff_90 = now - 90 * 86400
        cutoff_180 = now - 180 * 86400
        cutoff_365 = now - 365 * 86400
        last_change = max(r.author_time for r in records)
        days_since = max(0, int((now - last_change) // 86400))
        return VolatilityReport(
            target_file=target_file,
            head_sha=head,
            commits_total=len(records),
            commits_90d=sum(1 for r in records if r.author_time >= cutoff_90),
            commits_180d=sum(1 for r in records if r.author_time >= cutoff_180),
            commits_365d=sum(1 for r in records if r.author_time >= cutoff_365),
            distinct_authors=len({r.author for r in records}),
            days_since_last_change=days_since,
        )


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------
#
# Both fingerprints hash the *inputs that derive the report*, not the report
# values themselves. That decouples cache invalidation from wall-clock time:
# `days_since_last_change` advances every day, but the underlying commit set
# is fixed at a given HEAD — so a fingerprint over `(version, head_sha,
# target_file)` is stable until the repo actually moves forward.


def cochange_fingerprint(report: CoChangeReport) -> str:
    payload = "|".join([
        "cochange",
        COCHANGE_VERSION,
        report.head_sha,
        report.target_file,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def volatility_fingerprint(report: VolatilityReport) -> str:
    payload = "|".join([
        "volatility",
        VOLATILITY_VERSION,
        report.head_sha,
        report.target_file,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
