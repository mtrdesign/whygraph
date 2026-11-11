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
from whygraph.mcp.evidence import _commit_dict, _pr_dict, whygraph_evidence_for
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


def test_commit_dict_leads_with_the_diff_derived_description() -> None:
    """AC 10: key *order* changed to fix anchoring; the key set must not.

    ``whygraph_evidence_for`` and ``whygraph_area_history`` are registered MCP
    tools the planner subagents consume, so adding or removing a key here would
    be a contract change. Reordering is not.
    """
    payload = _commit_dict(_db_commit("abc", committed_at="2026-01-01T00:00:00+00:00"))
    assert list(payload) == [
        "sha",
        "llm_description",
        "subject",
        "body",
        "author_name",
        "author_email",
        "committed_at",
    ]
    # The model anchors on what it reads first, and the diff-derived field is
    # the one a nonsense commit message cannot corrupt.
    assert list(payload).index("llm_description") < list(payload).index("subject")
    assert payload["llm_description"] == "Mechanical diff summary."
    assert payload["subject"] == "a change"


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
    assert [item["sha"] for item in evidence] == [
        newest.sha,
        oldest.sha,
    ]
    assert [pr["number"] for pr in evidence[0]["pull_requests"]] == [5]
    assert [issue["number"] for issue in evidence[0]["issues"]] == [9]
    assert evidence[0]["issues"][0]["resource"] == "whygraph://issue/9"
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

    assert [item["sha"] for item in result["evidence"]] == [newest.sha]


def test_evidence_for_errors_when_db_not_initialized(
    temp_git_repo: Path,
    whygraph_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(temp_git_repo)
    with pytest.raises(WhyGraphError, match="scan from the WhyGraph portal first"):
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

        def describe(self, diff: str, *, subject: str | None = None) -> Description:
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

    by_sha = {item["sha"]: item for item in result["evidence"]}
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

    by_sha = {item["sha"]: item for item in result["evidence"]}
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

    shas = [item["sha"] for item in result["evidence"]]
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

    shas = [item["sha"] for item in result["evidence"]]
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

    by_sha = {item["sha"]: item for item in result["evidence"]}
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

    sources = {item["sha"]: item["source"] for item in result["evidence"]}
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
        assert item["llm_description"] is None
    with get_session() as session:
        for sha in (newest.sha, oldest.sha):
            assert session.get(Commit, sha).llm_description is None


# ---- result budget: detail, describe, the bounded backfill (plan 5.4) ------


def _seed_pr_world(repo_root: Path) -> tuple[str, str]:
    """Both commits scanned; the newest in PR #5 (long discussion) closing #9."""
    import json

    newest, oldest = list(Repository(repo_root).commits)
    pr = _db_pr(number=5, merge_commit_sha=newest.sha)
    pr.comments = json.dumps(
        [{"author": "alice", "body": "x" * 600, "created_at": "t"}]
        + [{"author": "bob", "body": f"c{n}", "created_at": "t"} for n in range(21)]
    )
    pr.commit_titles = json.dumps(
        [{"oid": f"oid{n}", "headline": f"h{n}", "author_name": "J"} for n in range(31)]
    )
    with get_session() as session:
        session.add(_db_commit(oldest.sha, committed_at="2026-01-01T00:00:00+00:00"))
        session.add(_db_commit(newest.sha, committed_at="2026-02-01T00:00:00+00:00"))
        session.add(pr)
        session.add(_db_issue(number=9))
        session.add(PRIssueLink(pr_number=5, issue_number=9, link_kind="closes"))
    return newest.sha, oldest.sha


def test_evidence_for_defaults_to_the_compact_shape(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    newest, _ = _seed_pr_world(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)

    result = whygraph_evidence_for(path="sample.py", line_start=1, line_end=3)

    assert set(result) == {"target", "evidence", "describing"}
    assert result["describing"] == 0
    item = result["evidence"][0]
    assert list(item) == [
        "sha",
        "llm_description",
        "subject",
        "author",
        "date",
        "lines",
        "source",
        "pull_requests",
        "issues",
    ]
    assert (item["sha"], item["author"]) == (newest, "Test User")
    assert item["date"] == "2026-02-01T00:00:00+00:00"
    assert item["pull_requests"] == [
        {
            "number": 5,
            "title": "A pull request",
            "html_url": "https://example.com/pr/5",
            "resource": "whygraph://pr/5",
        }
    ]
    assert item["issues"] == [
        {
            "number": 9,
            "title": "An issue",
            "html_url": "https://example.com/issue/9",
            "resource": "whygraph://issue/9",
        }
    ]
    blob = json.dumps(result)
    for prose in ("PR body.", "Issue body.", "x" * 50, "oid0", '"text"'):
        assert prose not in blob


def test_evidence_for_full_groups_prose_under_text_and_caps_it(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    newest, oldest = _seed_pr_world(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)

    result = whygraph_evidence_for(
        path="sample.py", line_start=1, line_end=3, detail="full"
    )

    assert set(result) == {
        "target",
        "evidence",
        "describing",
        "pull_requests",
        "issues",
    }
    by_sha = {item["sha"]: item for item in result["evidence"]}
    item = by_sha[newest]
    assert item["text"] == {"body": ""}
    # The item carries only the link refs, the same as compact.
    assert item["pull_requests"] == [
        {
            "number": 5,
            "title": "A pull request",
            "html_url": "https://example.com/pr/5",
            "resource": "whygraph://pr/5",
        }
    ]
    assert item["issues"][0]["resource"] == "whygraph://issue/9"
    assert "text" not in item["issues"][0]
    # The prose lives once, in the top-level maps keyed by number.
    pr = result["pull_requests"]["5"]
    assert pr["text"]["body"] == "PR body."
    comments = pr["text"]["comments"]
    assert len(comments) == 20  # pr_discussion_max_comments
    assert comments[0]["body"] == "x" * 500 + "..."  # pr_comment_max_chars
    assert comments[1] == {"author": "bob", "body": "c0", "created_at": "t"}
    assert len(pr["commit_titles"]) == 30  # pr_roster_max_commits
    assert pr["omitted"] == {"comments": 2, "commit_titles": 1, "body_chars": 0}
    assert result["issues"] == {"9": {"text": {"body": "Issue body."}}}
    # A commit with no PR has nothing to omit.
    assert by_sha[oldest]["pull_requests"] == []


def test_full_pr_without_clipping_carries_no_omitted() -> None:
    from whygraph.mcp.evidence import _evidence_dict, shape_evidence
    from whygraph.analyze import CommitEvidence

    commit = _db_commit("abc", committed_at="2026-01-01T00:00:00+00:00")
    pr = _db_pr(number=1, merge_commit_sha="abc")
    pr.comments = '[{"author": "a", "body": "ok", "created_at": "t"}]'
    item = _evidence_dict(CommitEvidence(commit=commit, pull_requests=(pr,)))

    shaped = shape_evidence([item], "full")

    assert "omitted" not in shaped["pull_requests"]["1"]
    assert shaped["pull_requests"]["1"]["text"]["comments"][0]["body"] == "ok"
    assert shaped["evidence"][0]["lines"] is None  # no lines: null, not missing


def test_full_emits_a_shared_pr_once_and_both_items_point_at_it(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    newest, oldest = list(Repository(temp_git_repo).commits)
    pr = _db_pr(number=5, merge_commit_sha=newest.sha)
    pr.commit_titles = json.dumps(
        [
            {"oid": oldest.sha, "headline": "first", "author_name": "J"},
            {"oid": newest.sha, "headline": "second", "author_name": "J"},
        ]
    )
    pr.body = "The one PR body."
    with get_session() as session:
        session.add(_db_commit(oldest.sha, committed_at="2026-01-01T00:00:00+00:00"))
        session.add(_db_commit(newest.sha, committed_at="2026-02-01T00:00:00+00:00"))
        session.add(pr)
        session.add(_db_issue(number=9))
        session.add(PRIssueLink(pr_number=5, issue_number=9, link_kind="closes"))
    monkeypatch.chdir(temp_git_repo)

    result = whygraph_evidence_for(
        path="sample.py", line_start=1, line_end=3, detail="full"
    )

    assert len(result["evidence"]) == 2
    for item in result["evidence"]:
        assert [p["resource"] for p in item["pull_requests"]] == ["whygraph://pr/5"]
        assert [i["resource"] for i in item["issues"]] == ["whygraph://issue/9"]
    assert list(result["pull_requests"]) == ["5"]
    assert list(result["issues"]) == ["9"]
    blob = json.dumps(result)
    assert blob.count("The one PR body.") == 1
    assert blob.count("Issue body.") == 1
    assert blob.count('"headline": "first"') == 1


def test_full_caps_pr_and_issue_bodies() -> None:
    from whygraph.analyze import CommitEvidence
    from whygraph.mcp.evidence import (
        PROSE_BODY_MAX_CHARS,
        _evidence_dict,
        shape_evidence,
    )

    commit = _db_commit("abc", committed_at="2026-01-01T00:00:00+00:00")
    commit.body = "c" * (PROSE_BODY_MAX_CHARS + 10)
    pr = _db_pr(number=1, merge_commit_sha="abc")
    pr.body = "p" * (PROSE_BODY_MAX_CHARS + 7)
    issue = _db_issue(number=2)
    issue.body = "i" * (PROSE_BODY_MAX_CHARS + 3)
    item = _evidence_dict(
        CommitEvidence(commit=commit, pull_requests=(pr,), issues=(issue,))
    )

    shaped = shape_evidence([item], "full")

    pr_out = shaped["pull_requests"]["1"]
    assert pr_out["text"]["body"] == "p" * PROSE_BODY_MAX_CHARS + "..."
    assert pr_out["omitted"] == {"comments": 0, "commit_titles": 0, "body_chars": 7}
    issue_out = shaped["issues"]["2"]
    assert issue_out["text"]["body"] == "i" * PROSE_BODY_MAX_CHARS + "..."
    assert issue_out["omitted"] == {"body_chars": 3}
    # The commit body is unique per item and stays whole.
    assert shaped["evidence"][0]["text"]["body"] == commit.body


def test_full_keeps_the_first_entry_for_a_repeated_number() -> None:
    from whygraph.mcp.evidence import shape_evidence

    first = {"commit": {"sha": "a"}, "pull_requests": [{"number": 3, "body": "one"}]}
    second = {"commit": {"sha": "b"}, "pull_requests": [{"number": 3, "body": "two"}]}

    shaped = shape_evidence([first, second], "full")

    assert shaped["pull_requests"]["3"]["text"]["body"] == "one"


def test_compact_shape_evidence_adds_no_maps() -> None:
    from whygraph.mcp.evidence import shape_evidence

    item = {"commit": {"sha": "a"}, "pull_requests": [{"number": 3, "body": "x"}]}

    assert set(shape_evidence([item])) == {"evidence"}


def test_the_evidence_tools_default_to_ten_items() -> None:
    import asyncio
    import inspect

    from whygraph.mcp.area_history import whygraph_area_history
    from whygraph.mcp.server import mcp

    for tool in (whygraph_evidence_for, whygraph_area_history):
        params = inspect.signature(tool).parameters
        assert params["limit"].default == 10
        assert (params["detail"].default, params["describe"].default) == (
            "compact",
            True,
        )
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name in ("whygraph_evidence_for", "whygraph_area_history"):
        props = tools[name].inputSchema["properties"]
        assert props["limit"]["default"] == 10
        assert props["detail"]["enum"] == ["compact", "full"]
        assert props["describe"]["default"] is True


def test_describe_false_never_calls_an_llm_and_reports_describing(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    newest, oldest = list(Repository(temp_git_repo).commits)
    with get_session() as session:
        for sha, at in ((oldest.sha, "2026-01-01"), (newest.sha, "2026-02-01")):
            session.add(
                _db_commit(
                    sha, committed_at=f"{at}T00:00:00+00:00", llm_description=None
                )
            )

    class _Refuse:
        @classmethod
        def from_config(cls, _cfg: object) -> None:
            raise AssertionError("an LLM client was built")

    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _Refuse)
    monkeypatch.chdir(temp_git_repo)

    result = whygraph_evidence_for(
        path="sample.py", line_start=1, line_end=3, describe=False
    )

    assert result["describing"] == 2
    assert all(item["llm_description"] is None for item in result["evidence"])


def _undescribed(count: int, *, files_changed: int = 1) -> list:
    """``count`` undescribed in-memory evidence items, newest first."""
    from whygraph.analyze import CommitEvidence

    return [
        CommitEvidence(
            commit=_db_commit(
                f"sha{n}",
                committed_at=f"2026-01-{30 - n:02d}T00:00:00+00:00",
                files_changed=files_changed,
                llm_description=None,
            )
        )
        for n in range(count)
    ]


class _FakeLlm:
    """What the fakes of :func:`_fake_backfill` record and how they behave.

    ``described`` lists each ``describe()`` call's SHA, in call order;
    ``peak`` is the most calls running at once. ``diffs`` maps a SHA to its
    diff (``""`` is an empty diff) or an exception the diff raises;
    ``errors`` maps a SHA to an exception ``describe()`` raises.
    """

    def __init__(self, sleep: float) -> None:
        import threading

        self.sleep = sleep
        self.described: list[str] = []
        self.projects: list[str | None] = []
        self.diffs: dict[str, object] = {}
        self.errors: dict[str, BaseException] = {}
        self.started = threading.Event()
        self.running = 0
        self.peak = 0
        self.lock = threading.Lock()

    def diff(self, sha: str) -> str:
        value = self.diffs.get(sha, "diff")
        if isinstance(value, BaseException):
            raise value
        return str(value)

    def describe(self, sha: str):  # noqa: ANN201
        import time

        from whygraph.analyze import Description
        from whygraph.core.context import current_project

        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.described.append(sha)
            ctx = current_project()
            self.projects.append(None if ctx is None else ctx.slug)
        self.started.set()
        try:
            time.sleep(self.sleep)
            if sha in self.errors:
                raise self.errors[sha]
            return Description(text=f"described {sha}", model="m", provider="fake")
        finally:
            with self.lock:
                self.running -= 1


def _fake_backfill(monkeypatch: pytest.MonkeyPatch, *, sleep: float = 0.0) -> _FakeLlm:
    """Stub the descriptor and the two ``backfill_*`` recipes, minus git.

    The fakes follow the real recipes: their own empty-diff check, then
    ``descriptor.describe(...)`` (the backfill's guard), then the DB write
    and the local row. ``describe`` records the commit's SHA.
    """
    import whygraph.analyze as analyze_pkg
    from whygraph.mcp import evidence as ev

    llm = _FakeLlm(sleep)

    class _Descriptor:
        @classmethod
        def from_config(cls, _cfg: object) -> "_Descriptor":
            return cls()

        def describe(self, diff: str, *, subject: str | None = None):  # noqa: ANN201
            return llm.describe(str(subject))

    def backfill_commit(commit, *, repository, descriptor) -> bool:  # noqa: ANN001
        if commit.llm_description is not None:
            return False
        if not llm.diff(commit.sha).strip():
            return False
        text = descriptor.describe("diff", subject=commit.sha).text
        with get_session() as session:
            row = session.get(Commit, commit.sha)
            if row is not None:
                row.llm_description = text
        commit.llm_description = text
        return True

    def backfill_file(commit, path, *, repository, descriptor) -> str | None:  # noqa: ANN001
        cached = ev._cached_file_description(commit.sha, path)
        if cached is not None:
            return cached
        if not llm.diff(commit.sha).strip():
            return None
        text = descriptor.describe("diff", subject=commit.sha).text
        with get_session() as session:
            for row in session.exec(
                select(CommitFileChange).where(
                    CommitFileChange.commit_sha == commit.sha,
                    CommitFileChange.path == path,
                )
            ):
                row.llm_description = text
        return text

    monkeypatch.setattr(analyze_pkg, "LlmDescriptor", _Descriptor)
    monkeypatch.setattr(analyze_pkg, "backfill_commit_description", backfill_commit)
    monkeypatch.setattr(analyze_pkg, "backfill_file_description", backfill_file)
    return llm


def _join_workers() -> None:
    """Wait for the tracked describe threads, leaving the claims as they are."""
    from whygraph.mcp import evidence as ev

    for thread in list(ev._threads):
        thread.join(5)


def _released() -> bool:
    """Once the workers ended: no claim is held and every slot is free."""
    from whygraph.mcp import evidence as ev

    _join_workers()
    return ev._claims == set() and ev._DESCRIBE_SLOTS._value == 8  # type: ignore[attr-defined]


def test_backfill_describes_at_most_five_commits_newest_first(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.mcp import evidence as ev

    assert (ev.MAX_BACKFILL_PER_CALL, ev.BACKFILL_WALL_SEC) == (5, 20)
    llm = _fake_backfill(monkeypatch)
    monkeypatch.chdir(temp_git_repo)
    items = _undescribed(7)
    started: list[int] = []

    outcome = ev.backfill_evidence_descriptions(
        items, target_path="sample.py", workers=1, on_start=started.append
    )

    assert llm.described == [f"sha{n}" for n in range(5)]
    assert (outcome.left, outcome.described, started) == (2, 5, [5])
    assert outcome.waiting == {"sha5", "sha6"}
    assert [i.commit.llm_description for i in items[5:]] == [None, None]


def test_backfill_starts_no_description_past_the_wall_clock(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow descriptor: the wall returns the call; a running describe finishes."""
    from whygraph.mcp import evidence as ev

    llm = _fake_backfill(monkeypatch, sleep=0.3)
    monkeypatch.chdir(temp_git_repo)

    outcome = ev.backfill_evidence_descriptions(
        _undescribed(4), target_path="sample.py", wall_sec=0.45, workers=1
    )

    # sha1 was still running at the wall: waiting, but it finishes.
    assert outcome.left == 3
    assert outcome.waiting == {"sha1", "sha2", "sha3"}
    assert ev.drain_describes(timeout=5)
    assert llm.described == ["sha0", "sha1"]


def test_evidence_for_reports_what_the_bound_left(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.mcp import evidence as ev

    newest, oldest = list(Repository(temp_git_repo).commits)
    with get_session() as session:
        for sha, at in ((oldest.sha, "2026-01-01"), (newest.sha, "2026-02-01")):
            session.add(
                _db_commit(
                    sha, committed_at=f"{at}T00:00:00+00:00", llm_description=None
                )
            )
    llm = _fake_backfill(monkeypatch)
    monkeypatch.setattr(ev, "MAX_BACKFILL_PER_CALL", 1)
    monkeypatch.chdir(temp_git_repo)

    result = whygraph_evidence_for(path="sample.py", line_start=1, line_end=3)

    assert llm.described == [newest.sha]  # newest first
    assert result["describing"] == 1
    # Acceptance 7: the tool starts no background work.
    assert ev._threads == set()


# ---- plan 49 section 4.1: classification ----------------------------------


def test_backfill_classifies_each_result(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty diff and GitError are terminal; AnalyzeError and a diff timeout wait."""
    from whygraph.analyze import AnalyzeError
    from whygraph.mcp import evidence as ev
    from whygraph.services.git import GitError

    llm = _fake_backfill(monkeypatch)
    llm.diffs = {
        "sha1": "",
        "sha2": GitError("bad object"),
        "sha3": subprocess.TimeoutExpired(["git", "diff"], 1),
    }
    llm.errors = {"sha4": AnalyzeError("provider failed")}
    monkeypatch.chdir(temp_git_repo)
    asked: list[bool] = []

    outcome = ev.backfill_evidence_descriptions(
        _undescribed(5),
        target_path="sample.py",
        max_commits=5,
        workers=1,
        allow=lambda: asked.append(True) or True,
    )

    assert outcome.described == 1
    assert outcome.terminal == {"sha1", "sha2"}
    assert outcome.waiting == {"sha3", "sha4"}
    # Only the two commits that reached an LLM call asked the budget.
    assert llm.described == ["sha0", "sha4"]
    assert len(asked) == 2
    assert _released()


def test_a_bulk_commit_off_the_path_is_terminal(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.mcp import evidence as ev

    llm = _fake_backfill(monkeypatch)
    llm.diffs = {"sha0": ""}
    monkeypatch.chdir(temp_git_repo)
    items = _undescribed(1, files_changed=500)

    outcome = ev.backfill_evidence_descriptions(items, target_path="sample.py")

    assert (outcome.terminal, outcome.waiting, llm.described) == ({"sha0"}, set(), [])


def test_real_recipes_classify_an_empty_diff_and_a_bad_object_as_terminal(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real ``backfill_*`` recipes: no LLM call and no ``allow()`` for either."""
    from whygraph.analyze import CommitEvidence
    from whygraph.mcp import evidence as ev

    _run_git(temp_git_repo, "commit", "--allow-empty", "-m", "empty")
    empty = _run_git(temp_git_repo, "rev-parse", "HEAD").strip()
    parent = _run_git(temp_git_repo, "rev-parse", "HEAD~1").strip()
    with get_session() as session:
        session.add(
            _db_commit(
                empty,
                committed_at="2026-03-01T00:00:00+00:00",
                parent_shas=parent,
                llm_description=None,
            )
        )
    seen = _install_stub_descriptor(monkeypatch)
    monkeypatch.chdir(temp_git_repo)
    asked: list[bool] = []
    items = [
        CommitEvidence(
            commit=_db_commit(
                empty,
                committed_at="2026-03-01T00:00:00+00:00",
                parent_shas=parent,
                llm_description=None,
            )
        ),
        *_undescribed(1),  # "sha0" is no git object
    ]

    outcome = ev.backfill_evidence_descriptions(
        items, target_path="sample.py", allow=lambda: asked.append(True) or True
    )

    assert outcome.terminal == {empty, "sha0"}
    assert (outcome.left, seen, asked) == (0, [], [])


def test_rejected_credentials_stop_the_queue(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.analyze import AnalyzeError
    from whygraph.mcp import evidence as ev
    from whygraph.services.llm import LlmAuthError

    llm = _fake_backfill(monkeypatch)
    rejected = AnalyzeError("describe failed")
    rejected.__cause__ = LlmAuthError("invalid key")
    llm.errors = {"sha0": rejected}
    monkeypatch.chdir(temp_git_repo)

    outcome = ev.backfill_evidence_descriptions(
        _undescribed(3), target_path="sample.py", workers=1
    )

    assert llm.described == ["sha0"]
    assert outcome.waiting == {"sha0", "sha1", "sha2"}


# ---- plan 49 section 4.1: the guard's refusals -----------------------------


class _BlockedSink:
    """A usage sink whose hard-stopped budget is spent after ``after`` checks."""

    def __init__(self, *, after: int = 0) -> None:
        from whygraph.core.usage import UsageScope

        self.scope = UsageScope(source="chat")
        self.after = after
        self.checks = 0

    def record(self, rec: object) -> None:
        pass

    def blocked_scope(self) -> str | None:
        self.checks += 1
        return "member" if self.checks > self.after else None


def test_a_spent_budget_leaves_the_rest_waiting(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard asks the sink before each LLM call (M2f-2 plan section 4.7)."""
    from whygraph.core.usage import use_usage_sink
    from whygraph.mcp import evidence as ev

    llm = _fake_backfill(monkeypatch)
    monkeypatch.chdir(temp_git_repo)

    # Open for the first check, spent from the second on.
    with use_usage_sink(_BlockedSink(after=1)):
        outcome = ev.backfill_evidence_descriptions(
            _undescribed(3), target_path="sample.py", workers=1
        )

    assert llm.described == ["sha0"]
    assert (outcome.described, outcome.waiting) == (1, {"sha1", "sha2"})
    assert outcome.terminal == set()
    assert _released()


def test_the_bound_refuses_past_max_commits(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.mcp import evidence as ev

    llm = _fake_backfill(monkeypatch, sleep=0.1)
    monkeypatch.chdir(temp_git_repo)

    outcome = ev.backfill_evidence_descriptions(
        _undescribed(5), target_path="sample.py", max_commits=2, workers=4
    )

    assert len(llm.described) == 2
    assert (outcome.described, outcome.left, outcome.terminal) == (2, 3, set())
    assert _released()


def test_allow_refusal_leaves_the_commit_waiting(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.mcp import evidence as ev

    llm = _fake_backfill(monkeypatch)
    monkeypatch.chdir(temp_git_repo)
    answers = iter([True, False])

    outcome = ev.backfill_evidence_descriptions(
        _undescribed(2),
        target_path="sample.py",
        workers=1,
        allow=lambda: next(answers),
    )

    assert llm.described == ["sha0"]
    assert (outcome.waiting, outcome.terminal) == ({"sha1"}, set())
    assert _released()


def test_a_held_claim_times_out_into_waiting(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.mcp import evidence as ev

    llm = _fake_backfill(monkeypatch)
    monkeypatch.chdir(temp_git_repo)
    held = (whygraph_db_initialized.resolve(), "sha0", None)
    ev._claims.add(held)
    try:
        outcome = ev.backfill_evidence_descriptions(
            _undescribed(1), target_path="sample.py", wall_sec=0.3
        )
        _join_workers()
    finally:
        ev._claims.discard(held)

    assert (outcome.waiting, outcome.terminal, llm.described) == ({"sha0"}, set(), [])
    assert _released()


def test_a_slot_timeout_leaves_the_commit_waiting(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from whygraph.mcp import evidence as ev

    llm = _fake_backfill(monkeypatch)
    monkeypatch.chdir(temp_git_repo)
    full = threading.BoundedSemaphore(1)
    assert full.acquire()
    monkeypatch.setattr(ev, "_DESCRIBE_SLOTS", full)

    outcome = ev.backfill_evidence_descriptions(
        _undescribed(1), target_path="sample.py", wall_sec=0.3
    )

    _join_workers()
    assert (outcome.waiting, outcome.terminal, llm.described) == ({"sha0"}, set(), [])
    assert ev._claims == set()
    full.release()  # the guard released nothing it did not take


def test_a_stored_description_is_applied_without_allow(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard's re-read: another caller described it, so no budget is asked."""
    from whygraph.mcp import evidence as ev

    with get_session() as session:
        session.add(
            _db_commit(
                "sha0",
                committed_at="2026-01-30T00:00:00+00:00",
                llm_description="written meanwhile",
            )
        )
    llm = _fake_backfill(monkeypatch)
    monkeypatch.chdir(temp_git_repo)
    asked: list[bool] = []
    items = _undescribed(1)  # the local row still has no description

    outcome = ev.backfill_evidence_descriptions(
        items, target_path="sample.py", allow=lambda: asked.append(True) or True
    )

    assert (outcome.described, outcome.left, llm.described, asked) == (1, 0, [], [])
    assert items[0].commit.llm_description == "written meanwhile"
    assert _released()


# ---- plan 49 section 4.1: parallel workers, claims -------------------------


def test_parallel_workers_describe_all_under_the_project_context(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.core.config import Config
    from whygraph.core.context import ProjectContext, use_project
    from whygraph.mcp import evidence as ev

    llm = _fake_backfill(monkeypatch, sleep=0.05)
    ctx = ProjectContext(
        slug="parallel",
        root=temp_git_repo,
        config=Config(whygraph_db=whygraph_db_initialized),
    )

    with use_project(ctx):
        outcome = ev.backfill_evidence_descriptions(
            _undescribed(8), target_path="sample.py", max_commits=8, workers=4
        )

    assert (outcome.described, outcome.left) == (8, 0)
    assert sorted(llm.described) == sorted(f"sha{n}" for n in range(8))
    assert llm.projects == ["parallel"] * 8
    assert llm.peak <= 4


def test_the_default_pool_size_is_analyze_max_workers(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.core import get_config
    from whygraph.mcp import evidence as ev

    llm = _fake_backfill(monkeypatch, sleep=0.2)
    monkeypatch.chdir(temp_git_repo)

    outcome = ev.backfill_evidence_descriptions(
        _undescribed(8), target_path="sample.py", max_commits=8
    )

    assert outcome.left == 0
    assert llm.peak == get_config().analyze.max_workers


def _seed_undescribed(count: int, *, files_changed: int = 1) -> None:
    """DB rows for :func:`_undescribed`'s SHAs, undescribed, touching sample.py."""
    from whygraph.analyze import bulk_commit_stub

    with get_session() as session:
        for item in _undescribed(count, files_changed=files_changed):
            commit = item.commit
            if files_changed > 1:
                commit.llm_description = bulk_commit_stub(files_changed)
            session.add(commit)
            session.add(
                CommitFileChange(
                    commit_sha=commit.sha,
                    path="sample.py",
                    change_type="M",
                    lines_added=1,
                    lines_deleted=0,
                )
            )


def _in_parallel(*calls) -> list:  # noqa: ANN002
    """Run each call on its own thread (one shared global config); their results."""
    import threading

    results: list = [None] * len(calls)

    def run(index: int) -> None:
        results[index] = calls[index]()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(calls))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    return results


def test_two_callers_on_the_same_commits_describe_each_once(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim: the second caller waits, re-reads, and copies the text."""
    from whygraph.mcp import evidence as ev

    _seed_undescribed(4)
    llm = _fake_backfill(monkeypatch, sleep=0.2)
    monkeypatch.chdir(temp_git_repo)
    first, second = _undescribed(4), _undescribed(4)

    outcomes = _in_parallel(
        lambda: ev.backfill_evidence_descriptions(
            first, target_path="sample.py", workers=4
        ),
        lambda: ev.backfill_evidence_descriptions(
            second, target_path="sample.py", workers=4
        ),
    )

    assert sorted(llm.described) == [f"sha{n}" for n in range(4)]
    assert [o.left for o in outcomes] == [0, 0]
    for items in (first, second):
        assert [i.commit.llm_description for i in items] == [
            f"described sha{n}" for n in range(4)
        ]
    assert _released()


@pytest.mark.parametrize("bulk", [False, True])
def test_a_claim_waiter_copies_the_stored_text_onto_its_row(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
    bulk: bool,
) -> None:
    """Normal commit: the commit's text; bulk commit: the per-file text, not the stub."""
    from whygraph.mcp import evidence as ev

    files = 500 if bulk else 1
    _seed_undescribed(1, files_changed=files)
    llm = _fake_backfill(monkeypatch, sleep=0.3)
    monkeypatch.chdir(temp_git_repo)
    first, second = (
        _undescribed(1, files_changed=files),
        _undescribed(1, files_changed=files),
    )
    if bulk:
        for items in (first, second):
            items[0].commit.llm_description = "stub"

    def late_second():  # noqa: ANN202
        # Start once the first caller's describe is running, so this one
        # passes its own cache check and waits on the claim.
        assert llm.started.wait(5)
        return ev.backfill_evidence_descriptions(second, target_path="sample.py")

    outcomes = _in_parallel(
        lambda: ev.backfill_evidence_descriptions(first, target_path="sample.py"),
        late_second,
    )

    assert llm.described == ["sha0"]
    assert [(o.described, o.left) for o in outcomes] == [(1, 0), (1, 0)]
    assert first[0].commit.llm_description == "described sha0"
    assert second[0].commit.llm_description == "described sha0"
    if bulk:
        assert ev._cached_file_description("sha0", "sample.py") == "described sha0"
        with get_session() as session:
            assert session.get(Commit, "sha0").llm_description != "described sha0"


def test_stop_describes_stops_pulling_and_drops_a_late_result(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.mcp import evidence as ev

    _seed_undescribed(3)
    llm = _fake_backfill(monkeypatch, sleep=0.3)
    monkeypatch.chdir(temp_git_repo)

    def stop_soon():  # noqa: ANN202
        assert llm.started.wait(5)
        ev.stop_describes(whygraph_db_initialized)

    outcome, _ = _in_parallel(
        lambda: ev.backfill_evidence_descriptions(
            _undescribed(3), target_path="sample.py", workers=1
        ),
        stop_soon,
    )

    assert llm.described == ["sha0"]
    assert outcome.waiting == {"sha0", "sha1", "sha2"}
    with get_session() as session:
        assert session.get(Commit, "sha0").llm_description is None
    # A later call gets a fresh event (a re-added project works).
    again = ev.backfill_evidence_descriptions(_undescribed(1), target_path="sample.py")
    assert again.left == 0
