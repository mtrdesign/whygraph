"""Tests for the ``whygraph_evidence_for`` MCP tool and its collector.

Each test seeds an isolated WhyGraph DB and blames a chunk of a throwaway
git repo, so the blame → commit → PR → issue join runs end to end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whygraph.db import get_session
from whygraph.db.models import Commit, Issue, PRIssueLink, PullRequest
from whygraph.mcp.errors import WhyGraphError
from whygraph.mcp.evidence import whygraph_evidence_for
from whygraph.services.git import Repository


def _db_commit(
    sha: str,
    *,
    subject: str = "a change",
    committed_at: str,
    parent_shas: str = "",
    llm_description: str | None = "Mechanical diff summary.",
) -> Commit:
    """A WhyGraph ``commit`` row with sensible defaults for tests."""
    return Commit(
        sha=sha,
        parent_shas=parent_shas,
        author_name="Test User",
        author_email="tester@example.com",
        authored_at=committed_at,
        committed_at=committed_at,
        subject=subject,
        body="",
        files_changed=1,
        insertions=1,
        deletions=0,
        scanned_at="2026-05-01T00:00:00+00:00",
        llm_description=llm_description,
    )


def _db_pr(*, number: int, merge_commit_sha: str) -> PullRequest:
    """A WhyGraph ``pull_request`` row linked to ``merge_commit_sha``."""
    return PullRequest(
        number=number,
        title="A pull request",
        body="PR body.",
        state="merged",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-02-01T00:00:00+00:00",
        merged_at="2026-02-01T00:00:00+00:00",
        merge_commit_sha=merge_commit_sha,
        head_sha="headsha",
        base_ref="main",
        author="octocat",
        html_url=f"https://example.com/pr/{number}",
        labels='["enhancement"]',
        fetched_at="2026-02-02T00:00:00+00:00",
    )


def _db_issue(*, number: int) -> Issue:
    """A WhyGraph ``issue`` row for tests."""
    return Issue(
        number=number,
        title="An issue",
        body="Issue body.",
        state="closed",
        created_at="2025-12-01T00:00:00+00:00",
        updated_at="2026-02-01T00:00:00+00:00",
        author="reporter",
        html_url=f"https://example.com/issue/{number}",
        labels='["bug"]',
        fetched_at="2026-02-02T00:00:00+00:00",
    )


def test_evidence_for_joins_commits_prs_and_issues(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    newest, oldest = list(Repository(temp_git_repo).commits)
    with get_session() as session:
        session.add(
            _db_commit(oldest.sha, committed_at="2026-01-01T00:00:00+00:00")
        )
        session.add(
            _db_commit(newest.sha, committed_at="2026-02-01T00:00:00+00:00")
        )
        session.add(_db_pr(number=5, merge_commit_sha=newest.sha))
        session.add(_db_issue(number=9))
        session.add(PRIssueLink(pr_number=5, issue_number=9, link_kind="closes"))

    monkeypatch.chdir(temp_git_repo)
    result = whygraph_evidence_for(path="sample.py", line_start=1, line_end=3)

    assert result["target"] == {
        "path": "sample.py",
        "line_start": 1,
        "line_end": 3,
        "qualified_name": None,
    }
    evidence = result["evidence"]
    # Newest commit first.
    assert [item["commit"]["sha"] for item in evidence] == [
        newest.sha,
        oldest.sha,
    ]
    assert [pr["number"] for pr in evidence[0]["pull_requests"]] == [5]
    assert [issue["number"] for issue in evidence[0]["issues"]] == [9]
    assert evidence[0]["issues"][0]["labels"] == ["bug"]
    # The older commit owns line 3 only — no PR/issue links.
    assert evidence[1]["pull_requests"] == []
    assert evidence[1]["issues"] == []


def test_evidence_for_skips_blamed_sha_absent_from_db(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    newest, _oldest = list(Repository(temp_git_repo).commits)
    with get_session() as session:
        # Only the newest commit is scanned; the older one is absent.
        session.add(
            _db_commit(newest.sha, committed_at="2026-02-01T00:00:00+00:00")
        )

    monkeypatch.chdir(temp_git_repo)
    result = whygraph_evidence_for(path="sample.py", line_start=1, line_end=3)

    assert [item["commit"]["sha"] for item in result["evidence"]] == [newest.sha]


def test_evidence_for_errors_when_db_not_initialized(
    temp_git_repo: Path,
    whygraph_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(temp_git_repo)
    with pytest.raises(WhyGraphError, match="whygraph scan"):
        whygraph_evidence_for(path="sample.py", line_start=1, line_end=3)


def test_evidence_for_rejects_missing_target() -> None:
    with pytest.raises(WhyGraphError, match="pass either"):
        whygraph_evidence_for()


def test_evidence_for_rejects_both_targeting_modes() -> None:
    with pytest.raises(WhyGraphError, match="not both"):
        whygraph_evidence_for(
            path="sample.py", line_start=1, line_end=3, qualified_name="pkg.f"
        )


# ---- lazy LLM-description backfill --------------------------------------


def _install_stub_descriptor(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch ``LlmDescriptor`` with a recording stub; return its diff log."""
    from whygraph.analyze import Description

    seen: list[str] = []

    class _StubDescriptor:
        @classmethod
        def from_config(cls, _cfg: object) -> "_StubDescriptor":
            return cls()

        def describe(self, diff: str) -> Description:
            seen.append(diff)
            return Description(
                text="backfilled summary",
                model="stub-1",
                provider="stub",
                input_tokens=1,
                output_tokens=2,
            )

    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _StubDescriptor)
    return seen


def test_evidence_for_backfills_null_llm_description(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit whose ``llm_description`` is NULL gets a fresh summary
    generated, returned, and persisted to the DB."""
    newest, oldest = list(Repository(temp_git_repo).commits)
    with get_session() as session:
        # The oldest commit already has a description — must NOT be re-described.
        session.add(
            _db_commit(oldest.sha, committed_at="2026-01-01T00:00:00+00:00")
        )
        # The newest commit's description is NULL — backfill should fill it in.
        session.add(
            _db_commit(
                newest.sha,
                committed_at="2026-02-01T00:00:00+00:00",
                parent_shas=oldest.sha,
                llm_description=None,
            )
        )

    seen = _install_stub_descriptor(monkeypatch)
    monkeypatch.chdir(temp_git_repo)

    result = whygraph_evidence_for(path="sample.py", line_start=1, line_end=3)

    by_sha = {item["commit"]["sha"]: item["commit"] for item in result["evidence"]}
    assert by_sha[newest.sha]["llm_description"] == "backfilled summary"
    assert by_sha[oldest.sha]["llm_description"] == "Mechanical diff summary."
    # The pre-described commit was skipped — only one descriptor call total.
    assert len(seen) == 1
    # Persisted, so the next read does not re-call the LLM.
    with get_session() as session:
        row = session.get(Commit, newest.sha)
        assert row.llm_description == "backfilled summary"
        assert row.llm_description_model == "stub:stub-1"


def test_evidence_for_silent_noop_when_analyze_misconfigured(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Misconfigured analyze provider must not blow up the evidence response —
    the JSON simply keeps ``llm_description: null``."""
    from whygraph.services.llm import LlmError

    newest, oldest = list(Repository(temp_git_repo).commits)
    with get_session() as session:
        session.add(
            _db_commit(
                oldest.sha,
                committed_at="2026-01-01T00:00:00+00:00",
                llm_description=None,
            )
        )
        session.add(
            _db_commit(
                newest.sha,
                committed_at="2026-02-01T00:00:00+00:00",
                parent_shas=oldest.sha,
                llm_description=None,
            )
        )

    class _AlwaysFails:
        @classmethod
        def from_config(cls, _cfg: object):
            raise LlmError("no provider configured")

    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _AlwaysFails)
    monkeypatch.chdir(temp_git_repo)

    result = whygraph_evidence_for(path="sample.py", line_start=1, line_end=3)

    for item in result["evidence"]:
        assert item["commit"]["llm_description"] is None
    with get_session() as session:
        for sha in (newest.sha, oldest.sha):
            assert session.get(Commit, sha).llm_description is None
