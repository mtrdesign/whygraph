"""Per-line squash attribution (Stage 2 of the squash-merge recovery plan).

Builds a real squash scenario on disk: a feature branch whose commits
authored ``sample.py``'s lines, collapsed into one squash commit on
``main``. With the original commits recovered as ``on_default_branch=0``
rows and linked through a PR's ``commit_titles``,
``whygraph_evidence_for`` must re-blame the queried lines at the PR's
``head_sha`` and surface each original commit as ``source="pr-origin"``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from whygraph.analyze import CommitEvidence
from whygraph.db import get_session
from whygraph.db.models import Commit, PullRequest
from whygraph.mcp.evidence import (
    _attribute_squash_origins,
    _should_replace,
    whygraph_evidence_for,
)
from whygraph.mcp.targets import Target
from whygraph.services.git import Repository


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _origin_row(sha: str, *, subject: str, committed_at: str) -> Commit:
    """A recovered PR-origin commit row (on_default_branch=0)."""
    return Commit(
        sha=sha,
        parent_shas="",
        author_name="Feature Dev",
        author_email="dev@example.com",
        authored_at=committed_at,
        committed_at=committed_at,
        subject=subject,
        body="",
        files_changed=1,
        insertions=1,
        deletions=0,
        scanned_at="2026-05-01T00:00:00+00:00",
        on_default_branch=0,
    )


def _squash_row(sha: str, *, committed_at: str) -> Commit:
    """The squash commit on the main walk (small — not file-bulk, so this
    fixture also guards the §4.8 'commit-rich but not file-bulk' trigger)."""
    return Commit(
        sha=sha,
        parent_shas="",
        author_name="Maintainer",
        author_email="maint@example.com",
        authored_at=committed_at,
        committed_at=committed_at,
        subject="Squash-merge feature",
        body="",
        files_changed=1,
        insertions=3,
        deletions=0,
        scanned_at="2026-05-01T00:00:00+00:00",
        on_default_branch=1,
    )


def _build_squash_repo(root: Path) -> dict[str, str]:
    """A repo whose ``main`` HEAD is a squash of a 2-commit feature branch.

    ``sample.py`` ends as three lines: feat1 wrote lines 1-2, feat2 added
    line 3; the squash commit on ``main`` reproduces all three at once.
    The feature tip is pinned under ``refs/whygraph/pull/1`` (and its
    branch deleted) to mirror what the enricher leaves behind.

    Returns a dict of ``squash`` / ``feat1`` / ``feat2`` → sha.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "commit.gpgsign", "false")

    # Base commit on main — no sample.py yet.
    (root / "README.md").write_text("base\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "base")

    # Feature branch: two commits authoring sample.py line by line.
    _git(root, "checkout", "-q", "-b", "feature")
    (root / "sample.py").write_text("line one\nline two\n")
    _git(root, "add", "sample.py")
    _git(root, "commit", "-q", "-m", "feat: first two lines")
    feat1 = _git(root, "rev-parse", "HEAD").strip()
    (root / "sample.py").write_text("line one\nline two\nline three\n")
    _git(root, "add", "sample.py")
    _git(root, "commit", "-q", "-m", "feat: third line")
    feat2 = _git(root, "rev-parse", "HEAD").strip()

    # Squash commit on main: the same final sample.py in one commit.
    _git(root, "checkout", "-q", "main")
    (root / "sample.py").write_text("line one\nline two\nline three\n")
    _git(root, "add", "sample.py")
    _git(root, "commit", "-q", "-m", "Squash-merge feature")
    squash = _git(root, "rev-parse", "HEAD").strip()

    # Pin the feature tip the way the enricher does, then drop the branch.
    _git(root, "update-ref", "refs/whygraph/pull/1", feat2)
    _git(root, "branch", "-q", "-D", "feature")

    return {"squash": squash, "feat1": feat1, "feat2": feat2}


def _seed_squash_pr(shas: dict[str, str]) -> None:
    with get_session() as session:
        session.add(
            _squash_row(shas["squash"], committed_at="2026-04-01T00:00:00+00:00")
        )
        session.add(
            _origin_row(
                shas["feat1"],
                subject="feat: first two lines",
                committed_at="2026-03-01T00:00:00+00:00",
            )
        )
        session.add(
            _origin_row(
                shas["feat2"],
                subject="feat: third line",
                committed_at="2026-03-02T00:00:00+00:00",
            )
        )
        session.add(
            PullRequest(
                number=1,
                title="Feature",
                state="MERGED",
                created_at="2026-03-01T00:00:00+00:00",
                updated_at="2026-04-01T00:00:00+00:00",
                merged_at="2026-04-01T00:00:00+00:00",
                merge_commit_sha=shas["squash"],
                head_sha=shas["feat2"],
                base_ref="main",
                html_url="https://example.test/pr/1",
                labels="[]",
                fetched_at="2026-04-02T00:00:00+00:00",
                commit_titles=(
                    f'[{{"oid": "{shas["feat1"]}", "headline": "feat: first two lines"}},'
                    f' {{"oid": "{shas["feat2"]}", "headline": "feat: third line"}}]'
                ),
            )
        )


def test_evidence_attributes_squash_lines_to_origin_commits(
    tmp_path: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A line owned by the squash commit yields pr-origin entries whose
    commits match ``git blame <head_sha>`` of the same range."""
    shas = _build_squash_repo(tmp_path / "repo")
    _seed_squash_pr(shas)

    monkeypatch.chdir(tmp_path / "repo")
    result = whygraph_evidence_for(path="sample.py", line_start=1, line_end=3)

    by_source: dict[str, set[str]] = {}
    for item in result["evidence"]:
        by_source.setdefault(item["source"], set()).add(item["commit"]["sha"])

    # The squash commit itself surfaces via direct HEAD blame...
    assert shas["squash"] in by_source.get("blame", set())
    # ...and the two originals surface via per-line squash attribution.
    assert by_source.get("pr-origin") == {shas["feat1"], shas["feat2"]}

    # Cross-check against git blame at head_sha directly.
    blame = Repository(tmp_path / "repo").blame("sample.py", 1, 3, rev=shas["feat2"])
    blamed = {h.sha for h in blame if not h.is_uncommitted}
    assert blamed == {shas["feat1"], shas["feat2"]}


def test_attribute_squash_origins_degrades_on_bad_head_sha(
    tmp_path: Path,
    whygraph_db_initialized: Path,
) -> None:
    """An unresolvable head_sha (GC'd ref) is swallowed — no error, no
    hunks — rather than failing the evidence collection."""
    shas = _build_squash_repo(tmp_path / "repo")
    with get_session() as session:
        session.add(
            _squash_row(shas["squash"], committed_at="2026-04-01T00:00:00+00:00")
        )
        session.add(
            _origin_row(
                shas["feat1"], subject="x", committed_at="2026-03-01T00:00:00+00:00"
            )
        )
        session.add(
            PullRequest(
                number=2,
                title="Feature",
                state="MERGED",
                created_at="2026-03-01T00:00:00+00:00",
                updated_at="2026-04-01T00:00:00+00:00",
                merged_at="2026-04-01T00:00:00+00:00",
                merge_commit_sha=shas["squash"],
                head_sha="0" * 40,  # not a real object
                base_ref="main",
                html_url="https://example.test/pr/2",
                labels="[]",
                fetched_at="2026-04-02T00:00:00+00:00",
                commit_titles=f'[{{"oid": "{shas["feat1"]}", "headline": "x"}}]',
            )
        )

    repo = Repository(tmp_path / "repo")
    target = Target(path="sample.py", line_start=1, line_end=3, qualified_name=None)
    with get_session() as session:
        hunks = _attribute_squash_origins(
            repo, target, blame_shas={shas["squash"]}, session=session
        )
    assert hunks == []


def test_unenriched_squash_yields_no_pr_origin(
    tmp_path: Path,
    whygraph_db_initialized: Path,
) -> None:
    """A squash PR with no recovered origin rows is not attributed (the
    trigger requires >= 1 on_default_branch=0 commit)."""
    shas = _build_squash_repo(tmp_path / "repo")
    with get_session() as session:
        session.add(
            _squash_row(shas["squash"], committed_at="2026-04-01T00:00:00+00:00")
        )
        # No origin rows inserted for this PR.
        session.add(
            PullRequest(
                number=3,
                title="Feature",
                state="MERGED",
                created_at="2026-03-01T00:00:00+00:00",
                updated_at="2026-04-01T00:00:00+00:00",
                merged_at="2026-04-01T00:00:00+00:00",
                merge_commit_sha=shas["squash"],
                head_sha=shas["feat2"],
                base_ref="main",
                html_url="https://example.test/pr/3",
                labels="[]",
                fetched_at="2026-04-02T00:00:00+00:00",
                commit_titles=f'[{{"oid": "{shas["feat1"]}", "headline": "x"}}]',
            )
        )

    repo = Repository(tmp_path / "repo")
    target = Target(path="sample.py", line_start=1, line_end=3, qualified_name=None)
    with get_session() as session:
        hunks = _attribute_squash_origins(
            repo, target, blame_shas={shas["squash"]}, session=session
        )
    assert hunks == []


# ---- source priority (plan unit test 6) -------------------------------------


def _ev(sha: str, source: str) -> CommitEvidence:
    return CommitEvidence(
        _origin_row(sha, subject="x", committed_at="x"), source=source
    )


def test_pr_origin_beats_area_but_loses_to_blame() -> None:
    # pr-origin supersedes area / blame-walked / predecessor-blame...
    assert _should_replace(_ev("s", "area"), "pr-origin") is True
    assert _should_replace(_ev("s", "blame-walked"), "pr-origin") is True
    # ...but a direct HEAD blame hit wins over pr-origin.
    assert _should_replace(_ev("s", "blame"), "pr-origin") is False
    # And pr-origin is not displaced by the weaker labels.
    assert _should_replace(_ev("s", "pr-origin"), "area") is False
