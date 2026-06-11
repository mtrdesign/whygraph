"""Tests for :mod:`whygraph.scan.pr_origin_enricher` (squash-merge recovery).

Covers the balanced enrichment gate, the end-to-end ``work()`` loop with a
stubbed repository (so no network / real git is needed), and the §4.10
``on_default_branch`` query guards that keep recovered origin commits out
of the main-walk-only queries.
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.progress import Progress

from whygraph.db import get_session
from whygraph.db.models import Commit, CommitFileChange, PullRequest
from whygraph.scan.pr_origin_enricher import (
    PROriginEnricher,
    _select_candidates,
)
from whygraph.services.git.commit import Commit as CommitDC
from whygraph.services.git.commit import DiffStats


def _commit(sha: str, *, files_changed: int = 1, on_default_branch: int = 1) -> Commit:
    return Commit(
        sha=sha,
        parent_shas="",
        author_name="Test User",
        author_email="tester@example.com",
        authored_at="2026-01-01T00:00:00+00:00",
        committed_at="2026-01-01T00:00:00+00:00",
        subject=sha,
        body="",
        files_changed=files_changed,
        insertions=1,
        deletions=0,
        scanned_at="2026-01-02T00:00:00+00:00",
        on_default_branch=on_default_branch,
    )


def _pr(
    number: int,
    *,
    oids: list[str],
    merge_commit_sha: str | None = None,
    merged: bool = True,
) -> PullRequest:
    return PullRequest(
        number=number,
        title=f"PR {number}",
        state="MERGED" if merged else "OPEN",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        merged_at="2026-01-02T00:00:00+00:00" if merged else None,
        merge_commit_sha=merge_commit_sha,
        head_sha=f"head{number}",
        base_ref="main",
        html_url=f"https://example.test/pr/{number}",
        labels="[]",
        fetched_at="2026-01-02T00:00:00+00:00",
        commit_titles=json.dumps([{"oid": o, "headline": o} for o in oids]),
    )


def _dc(oid: str, *, files_changed: int = 2) -> CommitDC:
    return CommitDC(
        sha=oid,
        parent_shas=(),
        author_name="Feature Dev",
        author_email="dev@example.com",
        authored_at="2025-12-01T00:00:00+00:00",
        committed_at="2025-12-01T00:00:00+00:00",
        subject=f"original {oid}",
        body="full body",
        stats=DiffStats(files_changed=files_changed, insertions=3, deletions=1),
    )


class _StubRepo:
    """Duck-typed stand-in for :class:`Repository` — no network, no git.

    Records the refspecs passed to :meth:`fetch_refs` and serves
    :meth:`commit_metadata` from a preloaded oid → :class:`CommitDC` map.
    An oid absent from the map raises, mirroring an unknown/GC'd object.
    """

    def __init__(self, metadata: dict[str, CommitDC]) -> None:
        self._metadata = metadata
        self.fetched: list[list[str]] = []

    def fetch_refs(self, refspecs: list[str], *, remote: str | None = None) -> None:
        self.fetched.append(list(refspecs))

    def commit_metadata(self, ref: str) -> CommitDC:
        from whygraph.services.git import GitError

        try:
            return self._metadata[ref]
        except KeyError as exc:  # pragma: no cover - defensive
            raise GitError(f"unknown commit {ref}") from exc


# --- balanced gate (plan §3.3 / unit test 4) ---------------------------------


def test_gate_enriches_file_bulk_squash(whygraph_db_initialized: Path) -> None:
    """Squash (oids absent) + file-bulk merge commit → enrich."""
    with get_session() as session:
        # The squash commit IS on the main walk and is file-bulk (> threshold).
        session.add(_commit("squash1", files_changed=40))
        session.add(_pr(1, oids=["o1", "o2"], merge_commit_sha="squash1"))
        session.commit()
        candidates = _select_candidates(
            session, min_commits=5, large_commit_file_count=30
        )

    assert len(candidates) == 1
    assert candidates[0].number == 1
    assert sorted(candidates[0].new_oids) == ["o1", "o2"]


def test_gate_enriches_commit_rich_squash(whygraph_db_initialized: Path) -> None:
    """Squash + many collapsed commits (>= min_commits) → enrich, even when
    the merge commit touched few files."""
    with get_session() as session:
        session.add(_commit("squash2", files_changed=2))  # not file-bulk
        session.add(
            _pr(2, oids=[f"c{i}" for i in range(5)], merge_commit_sha="squash2")
        )
        session.commit()
        candidates = _select_candidates(
            session, min_commits=5, large_commit_file_count=30
        )

    assert len(candidates) == 1
    assert candidates[0].number == 2


def test_gate_skips_below_both_thresholds(whygraph_db_initialized: Path) -> None:
    """Squash but small (few files, few commits) → skip."""
    with get_session() as session:
        session.add(_commit("squash3", files_changed=2))
        session.add(_pr(3, oids=["x1", "x2"], merge_commit_sha="squash3"))
        session.commit()
        candidates = _select_candidates(
            session, min_commits=5, large_commit_file_count=30
        )

    assert candidates == []


def test_gate_skips_merged_as_own_commits(whygraph_db_initialized: Path) -> None:
    """A plain merge / rebase — the PR's oids are already on the main walk —
    is skipped regardless of size (no history was lost)."""
    with get_session() as session:
        # All of the PR's oids are present in `commit` on the main walk.
        for oid in (f"m{i}" for i in range(8)):
            session.add(_commit(oid, files_changed=1))
        session.add(_commit("merge4", files_changed=50))
        session.add(_pr(4, oids=[f"m{i}" for i in range(8)], merge_commit_sha="merge4"))
        session.commit()
        candidates = _select_candidates(
            session, min_commits=5, large_commit_file_count=30
        )

    assert candidates == []


def test_gate_ignores_unmerged_prs(whygraph_db_initialized: Path) -> None:
    """An open PR is never a candidate even if it is large."""
    with get_session() as session:
        session.add(_commit("tip5", files_changed=40))
        session.add(
            _pr(
                5, oids=[f"u{i}" for i in range(9)], merge_commit_sha=None, merged=False
            )
        )
        session.commit()
        candidates = _select_candidates(
            session, min_commits=5, large_commit_file_count=30
        )

    assert candidates == []


# --- end-to-end work() (plan integration test 7) -----------------------------


def test_work_inserts_origin_rows_and_fetches_only_candidates(
    whygraph_db_initialized: Path,
) -> None:
    """One batched fetch of only the candidate refspec; origin rows land
    with on_default_branch=0 and full git metadata."""
    with get_session() as session:
        session.add(_commit("squash6", files_changed=40))
        session.add(_pr(6, oids=["a6", "b6"], merge_commit_sha="squash6"))
        session.commit()

    repo = _StubRepo({"a6": _dc("a6"), "b6": _dc("b6")})
    enricher = PROriginEnricher(
        Progress(), repository=repo, min_commits=5, large_commit_file_count=30
    )
    enricher.run()

    assert enricher.error is None
    # Exactly one fetch, carrying only PR #6's targeted refspec (no wildcard).
    assert repo.fetched == [["refs/pull/6/head:refs/whygraph/pull/6"]]

    with get_session() as session:
        a6 = session.get(Commit, "a6")
        b6 = session.get(Commit, "b6")
        a6_flag = a6.on_default_branch
        a6_body = a6.body
        a6_files = a6.files_changed
        b6_flag = b6.on_default_branch
        squash_flag = session.get(Commit, "squash6").on_default_branch

    assert a6_flag == 0 and b6_flag == 0
    assert a6_body == "full body"  # full message from git, not just headline
    assert a6_files == 2
    assert squash_flag == 1  # the squash commit stays on the main walk


def test_work_no_candidates_makes_no_fetch(whygraph_db_initialized: Path) -> None:
    """When nothing is gated, the enricher never touches the network."""
    with get_session() as session:
        session.add(_commit("squash7", files_changed=2))
        session.add(_pr(7, oids=["s1", "s2"], merge_commit_sha="squash7"))
        session.commit()

    repo = _StubRepo({})
    enricher = PROriginEnricher(
        Progress(), repository=repo, min_commits=5, large_commit_file_count=30
    )
    enricher.run()

    assert enricher.error is None
    assert repo.fetched == []


def test_work_dedups_oid_shared_across_prs(whygraph_db_initialized: Path) -> None:
    """An oid attached to two squash PRs is inserted exactly once (no PK
    collision) — the many-to-many edge §4.3 relies on."""
    with get_session() as session:
        session.add(_commit("squashA", files_changed=40))
        session.add(_commit("squashB", files_changed=40))
        session.add(_pr(8, oids=["shared", "onlyA"], merge_commit_sha="squashA"))
        session.add(_pr(9, oids=["shared", "onlyB"], merge_commit_sha="squashB"))
        session.commit()

    repo = _StubRepo(
        {"shared": _dc("shared"), "onlyA": _dc("onlyA"), "onlyB": _dc("onlyB")}
    )
    enricher = PROriginEnricher(
        Progress(), repository=repo, min_commits=5, large_commit_file_count=30
    )
    enricher.run()

    assert enricher.error is None
    with get_session() as session:
        from sqlmodel import func, select

        shared_count = session.exec(
            select(func.count(Commit.sha)).where(Commit.sha == "shared")
        ).one()
    assert shared_count == 1


def test_work_skips_scan_when_fetch_fails(whygraph_db_initialized: Path) -> None:
    """A fetch failure (GC'd ref / no network) degrades gracefully — the
    scan does not error and no origin rows are written."""
    from whygraph.services.git import GitError

    class _FailingRepo(_StubRepo):
        def fetch_refs(self, refspecs, *, remote=None):  # type: ignore[override]
            raise GitError("network down")

    with get_session() as session:
        session.add(_commit("squashF", files_changed=40))
        session.add(_pr(10, oids=["f1", "f2"], merge_commit_sha="squashF"))
        session.commit()

    repo = _FailingRepo({"f1": _dc("f1"), "f2": _dc("f2")})
    enricher = PROriginEnricher(
        Progress(), repository=repo, min_commits=5, large_commit_file_count=30
    )
    enricher.run()

    assert enricher.error is None
    with get_session() as session:
        assert session.get(Commit, "f1") is None
        assert session.get(Commit, "f2") is None


# --- §4.10 main-walk-only query guards (unit test 5) -------------------------


def test_boring_shas_excludes_origin_commits(whygraph_db_initialized: Path) -> None:
    """``_boring_shas_in`` ignores on_default_branch=0 rows even when their
    refactor_score crosses the boring threshold."""
    from whygraph.mcp.evidence import BORING_THRESHOLD, _boring_shas_in

    with get_session() as session:
        main = _commit("boring_main", on_default_branch=1)
        main.refactor_score = BORING_THRESHOLD + 5
        origin = _commit("boring_origin", on_default_branch=0)
        origin.refactor_score = BORING_THRESHOLD + 5
        session.add(main)
        session.add(origin)
        session.commit()

    result = _boring_shas_in({"boring_main", "boring_origin"})
    assert result == {"boring_main"}


def test_area_history_excludes_origin_commits(whygraph_db_initialized: Path) -> None:
    """area-history excludes on_default_branch=0 commits even if a
    commit_file_change row points at the queried path (defensive guard)."""
    from whygraph.mcp.path_history import area_history_commits

    with get_session() as session:
        session.add(_commit("area_main", on_default_branch=1))
        session.add(_commit("area_origin", on_default_branch=0))
        session.add(
            CommitFileChange(
                commit_sha="area_main",
                path="src/x.py",
                change_type="M",
                renamed_from=None,
                similarity=None,
                lines_added=1,
                lines_deleted=0,
            )
        )
        # Artificial: origin commits normally get no file-change rows; this
        # forces the join to reach one so the explicit guard is exercised.
        session.add(
            CommitFileChange(
                commit_sha="area_origin",
                path="src/x.py",
                change_type="M",
                renamed_from=None,
                similarity=None,
                lines_added=1,
                lines_deleted=0,
            )
        )
        session.commit()

    items = area_history_commits("src/x.py", include_renames=False)
    shas = {item.commit.sha for item in items}
    assert shas == {"area_main"}
