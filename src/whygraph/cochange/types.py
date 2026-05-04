from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CoChangeNeighbor:
    file_path: str
    cochange_count: int  # commits where both this and the target file changed
    target_commits_total: int  # denominator — commits touching the target

    @property
    def percent(self) -> float:
        if self.target_commits_total <= 0:
            return 0.0
        return 100.0 * self.cochange_count / self.target_commits_total


@dataclass(frozen=True)
class CoChangeReport:
    target_file: str
    head_sha: str  # the HEAD this report was computed against; "" if unknown
    commits_considered: int  # total commits touching the target file at HEAD
    neighbors: list[CoChangeNeighbor]  # already sorted + truncated + denylist-filtered
    truncated: int  # neighbors dropped past top_k


@dataclass(frozen=True)
class VolatilityReport:
    target_file: str
    head_sha: str
    commits_total: int
    commits_90d: int
    commits_180d: int
    commits_365d: int
    distinct_authors: int
    days_since_last_change: int | None  # None when there's no history
