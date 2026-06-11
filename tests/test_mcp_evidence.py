"""Tests for the ``whygraph_evidence_for`` MCP tool and its collector.

Each test seeds an isolated WhyGraph DB and blames a chunk of a throwaway
git repo, so the blame → commit → PR → issue join runs end to end.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlmodel import select

from whygraph.db import get_session
from whygraph.db.models import Commit, CommitFileChange, Issue, PRIssueLink, PullRequest
from whygraph.mcp.errors import WhyGraphError
from whygraph.mcp.evidence import _pr_dict, whygraph_evidence_for
from whygraph.scan.refactor_score import BORING_THRESHOLD
from whygraph.services.git import Repository


def _run_git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _db_commit(
    sha: str,
    *,
    subject: str = "a change",
    committed_at: str,
    parent_shas: str = "",
    files_changed: int = 1,
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
        files_changed=files_changed,
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


# ---- _pr_dict serialization ---------------------------------------------


def test_pr_dict_includes_decoded_commit_titles_and_comments() -> None:
    pr = _db_pr(number=1, merge_commit_sha="squashsha")
    pr.commit_titles = '[{"oid": "abc123", "headline": "first", "author_name": "Jane"}]'
    pr.comments = '[{"author": "alice", "body": "lgtm", "created_at": "x"}]'

    out = _pr_dict(pr)

    assert out["commit_titles"] == [
        {"oid": "abc123", "headline": "first", "author_name": "Jane"}
    ]
    assert out["comments"] == [{"author": "alice", "body": "lgtm", "created_at": "x"}]


def test_pr_dict_malformed_commit_titles_and_comments_yield_empty_lists() -> None:
    pr = _db_pr(number=1, merge_commit_sha="squashsha")
    pr.commit_titles = "not json"
    pr.comments = "{}"  # an object, not a list

    out = _pr_dict(pr)

    assert out["commit_titles"] == []
    assert out["comments"] == []


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
        session.add(_db_commit(oldest.sha, committed_at="2026-01-01T00:00:00+00:00"))
        session.add(_db_commit(newest.sha, committed_at="2026-02-01T00:00:00+00:00"))
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
        session.add(_db_commit(newest.sha, committed_at="2026-02-01T00:00:00+00:00"))

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
        session.add(_db_commit(oldest.sha, committed_at="2026-01-01T00:00:00+00:00"))
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


def test_evidence_for_bulk_commit_uses_per_file_description(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bulk commit is described per-file against the queried path; the
    file-specific text is returned and cached on the file-change row, while
    the commit row keeps its cheap stub."""
    newest, oldest = list(Repository(temp_git_repo).commits)
    bulk_stub = "Bulk commit touching 50 files — per-file on demand."
    with get_session() as session:
        session.add(_db_commit(oldest.sha, committed_at="2026-01-01T00:00:00+00:00"))
        # The newest commit is "bulk" (files_changed over the default
        # threshold of 30) and carries only the stub at the commit level.
        session.add(
            _db_commit(
                newest.sha,
                committed_at="2026-02-01T00:00:00+00:00",
                parent_shas=oldest.sha,
                files_changed=50,
                llm_description=bulk_stub,
            )
        )
        # The scan recorded that the bulk commit touched sample.py.
        session.add(
            CommitFileChange(
                commit_sha=newest.sha,
                path="sample.py",
                change_type="M",
                lines_added=1,
                lines_deleted=0,
            )
        )

    seen = _install_stub_descriptor(monkeypatch)
    monkeypatch.chdir(temp_git_repo)

    result = whygraph_evidence_for(path="sample.py", line_start=1, line_end=3)

    by_sha = {item["commit"]["sha"]: item["commit"] for item in result["evidence"]}
    # The returned description for the bulk commit is the per-file text,
    # not the stub.
    assert by_sha[newest.sha]["llm_description"] == "backfilled summary"
    # Exactly one descriptor call, and it was scoped to sample.py.
    assert len(seen) == 1
    assert "sample.py" in seen[0]

    with get_session() as session:
        # The commit row keeps its stub — per-file text lives on the
        # file-change row instead.
        commit_row = session.get(Commit, newest.sha)
        assert commit_row.llm_description == bulk_stub
        fc = session.exec(
            select(CommitFileChange)
            .where(CommitFileChange.commit_sha == newest.sha)
            .where(CommitFileChange.path == "sample.py")
        ).first()
        assert fc.llm_description == "backfilled summary"
        assert fc.llm_description_model == "stub:stub-1"


def test_evidence_for_merges_area_history_when_blame_is_thin(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the blame-derived list has slack under ``limit``, the remainder
    is filled from ``commit_file_change`` rows — including commits that
    touched the *predecessor* path of a renamed file."""
    newest, oldest = list(Repository(temp_git_repo).commits)
    # An older commit that touched a now-renamed predecessor file. It is
    # NOT blamed because the predecessor file no longer exists at HEAD.
    predecessor_sha = "deadbeef" * 5  # 40 chars
    with get_session() as session:
        session.add(_db_commit(oldest.sha, committed_at="2026-01-01T00:00:00+00:00"))
        session.add(_db_commit(newest.sha, committed_at="2026-02-01T00:00:00+00:00"))
        session.add(
            _db_commit(
                predecessor_sha,
                subject="legacy edit",
                committed_at="2024-06-01T00:00:00+00:00",
            )
        )
        # Rename chain: legacy_sample.py → sample.py
        session.add(
            CommitFileChange(
                commit_sha=predecessor_sha,
                path="legacy_sample.py",
                change_type="M",
                renamed_from=None,
                similarity=None,
                lines_added=1,
                lines_deleted=0,
            )
        )
        session.add(
            CommitFileChange(
                commit_sha=oldest.sha,
                path="sample.py",
                change_type="R",
                renamed_from="legacy_sample.py",
                similarity=100,
                lines_added=0,
                lines_deleted=0,
            )
        )

    monkeypatch.chdir(temp_git_repo)
    result = whygraph_evidence_for(path="sample.py", line_start=1, line_end=3)

    shas = [item["commit"]["sha"] for item in result["evidence"]]
    # Newest first; the predecessor-touching commit comes through the
    # area-history merge despite never appearing in blame.
    assert newest.sha in shas
    assert oldest.sha in shas
    assert predecessor_sha in shas


def test_evidence_for_does_not_duplicate_when_blame_and_area_agree(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SHA produced by both blame and the area-history index appears once."""
    newest, oldest = list(Repository(temp_git_repo).commits)
    with get_session() as session:
        session.add(_db_commit(oldest.sha, committed_at="2026-01-01T00:00:00+00:00"))
        session.add(_db_commit(newest.sha, committed_at="2026-02-01T00:00:00+00:00"))
        # Both blamed commits also have file-change rows for sample.py.
        session.add(
            CommitFileChange(
                commit_sha=newest.sha,
                path="sample.py",
                change_type="M",
                lines_added=1,
                lines_deleted=0,
            )
        )
        session.add(
            CommitFileChange(
                commit_sha=oldest.sha,
                path="sample.py",
                change_type="A",
                lines_added=2,
                lines_deleted=0,
            )
        )

    monkeypatch.chdir(temp_git_repo)
    result = whygraph_evidence_for(path="sample.py", line_start=1, line_end=3)

    shas = [item["commit"]["sha"] for item in result["evidence"]]
    assert sorted(shas) == sorted({newest.sha, oldest.sha})


def _bootstrap_repo(repo: Path) -> None:
    """Init a throwaway repo with deterministic identity configured."""
    repo.mkdir()
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "tester@example.com")
    _run_git(repo, "config", "user.name", "Test User")
    _run_git(repo, "config", "commit.gpgsign", "false")


def _seed_commit_row(
    session,
    *,
    sha: str,
    parent_shas: str,
    subject: str,
    committed_at: str,
    refactor_score: int = 0,
) -> None:
    row = Commit(
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
        scanned_at="2026-05-26T00:00:00+00:00",
        llm_description="diff summary",
    )
    row.refactor_score = refactor_score
    session.add(row)


def test_evidence_for_walks_past_refactor_heavy_commit(
    tmp_path: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A boring commit between today's line and its real author is walked
    past so the original author surfaces under ``source="blame-walked"``."""
    repo = tmp_path / "repo"
    _bootstrap_repo(repo)

    sample = repo / "sample.py"
    sample.write_text("x = 1\n")
    _run_git(repo, "add", "sample.py")
    _run_git(repo, "commit", "-m", "feat: author x")
    original_sha = _run_git(repo, "rev-parse", "HEAD").strip()

    sample.write_text("x = 42\n")
    _run_git(repo, "add", "sample.py")
    _run_git(repo, "commit", "-m", "refactor: bulk-rename constants")
    boring_sha = _run_git(repo, "rev-parse", "HEAD").strip()

    with get_session() as session:
        _seed_commit_row(
            session,
            sha=original_sha,
            parent_shas="",
            subject="feat: author x",
            committed_at="2026-01-01T00:00:00+00:00",
        )
        _seed_commit_row(
            session,
            sha=boring_sha,
            parent_shas=original_sha,
            subject="refactor: bulk-rename constants",
            committed_at="2026-02-01T00:00:00+00:00",
            refactor_score=BORING_THRESHOLD + 10,
        )

    monkeypatch.chdir(repo)
    result = whygraph_evidence_for(path="sample.py", line_start=1, line_end=1)

    by_sha = {item["commit"]["sha"]: item for item in result["evidence"]}
    assert by_sha[boring_sha]["source"] == "blame"
    assert original_sha in by_sha
    assert by_sha[original_sha]["source"] == "blame-walked"


def test_evidence_for_surfaces_predecessor_blame_via_rename(
    tmp_path: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rename that also significantly rewrites the file hides the original
    author from plain blame. Predecessor-blame re-blames the old path at the
    rename event's parent and surfaces the original commit anyway."""
    repo = tmp_path / "repo"
    _bootstrap_repo(repo)

    (repo / "old.py").write_text("x = 1\n")
    _run_git(repo, "add", "old.py")
    _run_git(repo, "commit", "-m", "feat: author x")
    original_sha = _run_git(repo, "rev-parse", "HEAD").strip()

    # Rename + heavy rewrite in one commit. Git's blame -M -C similarity
    # detector should NOT follow the rename back because the contents are
    # essentially different.
    _run_git(repo, "rm", "old.py")
    (repo / "new.py").write_text(
        "# completely different file\n"
        "def fetch_users():\n"
        "    return database.query('SELECT * FROM users')\n"
    )
    _run_git(repo, "add", "new.py")
    _run_git(repo, "commit", "-m", "refactor: replace constant with users module")
    rewrite_sha = _run_git(repo, "rev-parse", "HEAD").strip()

    with get_session() as session:
        _seed_commit_row(
            session,
            sha=original_sha,
            parent_shas="",
            subject="feat: author x",
            committed_at="2026-01-01T00:00:00+00:00",
        )
        _seed_commit_row(
            session,
            sha=rewrite_sha,
            parent_shas=original_sha,
            subject="refactor: replace constant with users module",
            committed_at="2026-02-01T00:00:00+00:00",
        )
        # An explicit rename record drives predecessor-blame even though
        # git's blame -M -C wouldn't follow the move on its own.
        session.add(
            CommitFileChange(
                commit_sha=rewrite_sha,
                path="new.py",
                change_type="R",
                renamed_from="old.py",
                similarity=10,
                lines_added=3,
                lines_deleted=1,
            )
        )

    monkeypatch.chdir(repo)
    result = whygraph_evidence_for(path="new.py", line_start=1, line_end=1)

    sources = {item["commit"]["sha"]: item["source"] for item in result["evidence"]}
    assert original_sha in sources
    assert sources[original_sha] == "predecessor-blame"


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
