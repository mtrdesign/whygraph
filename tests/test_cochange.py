from __future__ import annotations

from pathlib import Path

import pytest

from whygraph.cochange import git as cochange_git
from whygraph.cochange.service import (
    COCHANGE_VERSION,
    DEFAULT_DENYLIST,
    MIN_COCHANGE_COUNT,
    MIN_COMMITS_FOR_DISPLAY,
    CoChangeService,
    cochange_fingerprint,
)
from whygraph.cochange.types import CoChangeNeighbor, CoChangeReport
from whygraph.db import open_whygraph_db


# ---------------------------------------------------------------------------
# CoChangeNeighbor.percent
# ---------------------------------------------------------------------------


def test_neighbor_percent_zero_when_no_target_commits() -> None:
    n = CoChangeNeighbor(file_path="x", cochange_count=0, target_commits_total=0)
    assert n.percent == 0.0


def test_neighbor_percent_basic() -> None:
    n = CoChangeNeighbor(file_path="x", cochange_count=3, target_commits_total=4)
    assert n.percent == 75.0


# ---------------------------------------------------------------------------
# git helpers (real git via fixtures)
# ---------------------------------------------------------------------------


def test_git_head_sha_returns_empty_for_non_repo(tmp_path: Path) -> None:
    assert cochange_git.head_sha(tmp_path) == ""


def test_git_commits_touching_file_returns_newest_first(
    init_git_repo, git_commit
) -> None:
    repo = init_git_repo()
    sha1 = git_commit(repo, "a.py", "v1\n", message="add a")
    git_commit(repo, "b.py", "x\n", message="unrelated")
    sha3 = git_commit(repo, "a.py", "v2\n", message="edit a")

    shas = cochange_git.commits_touching_file(repo, "a.py")
    assert shas == [sha3, sha1]


def test_git_commits_touching_file_empty_for_unknown(
    init_git_repo, git_commit
) -> None:
    repo = init_git_repo()
    git_commit(repo, "a.py", "v1\n", message="add a")
    assert cochange_git.commits_touching_file(repo, "missing.py") == []


def test_git_files_in_commit_lists_changed_files(
    init_git_repo, git_commit
) -> None:
    repo = init_git_repo()
    # Initial commit so there's a parent for the second.
    git_commit(repo, "a.py", "v1\n", message="seed")
    # Two files in one commit — write both, then `git add` + commit.
    (repo / "b.py").write_text("b1\n")
    (repo / "c.py").write_text("c1\n")
    import subprocess

    env = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(repo.parent),
    }
    subprocess.run(
        ["git", "add", "b.py", "c.py"], cwd=str(repo), check=True, env=env
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "two files"], cwd=str(repo), check=True, env=env
    )
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), text=True, env=env
    ).strip()

    files = cochange_git.files_in_commit(repo, head)
    assert sorted(files) == ["b.py", "c.py"]


# ---------------------------------------------------------------------------
# CoChangeService — end-to-end with a real git repo
# ---------------------------------------------------------------------------


def _multi_file_commit(repo: Path, files: dict[str, str], message: str) -> None:
    import subprocess

    env = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(repo.parent),
    }
    for path, content in files.items():
        full = repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        subprocess.run(["git", "add", path], cwd=str(repo), check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=str(repo), check=True, env=env
    )


def test_service_returns_empty_report_when_file_has_no_history(
    init_git_repo, tmp_path: Path
) -> None:
    repo = init_git_repo()
    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        service = CoChangeService(conn, repo)
        report = service.report_for("nonexistent.py")
        assert report.commits_considered == 0
        assert report.neighbors == []
        assert report.truncated == 0
    finally:
        conn.close()


def test_service_aggregates_cochange_neighbors(
    init_git_repo, git_commit, tmp_path: Path
) -> None:
    repo = init_git_repo()
    # 3 commits touch target; 2 of them also touch "other.py".
    _multi_file_commit(
        repo, {"target.py": "v1\n", "other.py": "o1\n"}, "edit both #1"
    )
    _multi_file_commit(repo, {"target.py": "v2\n"}, "edit target only")
    _multi_file_commit(
        repo, {"target.py": "v3\n", "other.py": "o2\n"}, "edit both #2"
    )

    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        service = CoChangeService(conn, repo)
        report = service.report_for("target.py")
        assert report.commits_considered == 3
        assert len(report.neighbors) == 1
        n = report.neighbors[0]
        assert n.file_path == "other.py"
        assert n.cochange_count == 2
        assert n.target_commits_total == 3
    finally:
        conn.close()


def test_service_excludes_target_file_from_neighbors(
    init_git_repo, tmp_path: Path
) -> None:
    repo = init_git_repo()
    _multi_file_commit(
        repo, {"target.py": "v1\n", "x.py": "x1\n"}, "init"
    )
    _multi_file_commit(
        repo, {"target.py": "v2\n", "x.py": "x2\n"}, "again"
    )

    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        service = CoChangeService(conn, repo)
        report = service.report_for("target.py")
        assert "target.py" not in [n.file_path for n in report.neighbors]
    finally:
        conn.close()


def test_service_filters_denylisted_files(
    init_git_repo, tmp_path: Path
) -> None:
    repo = init_git_repo()
    _multi_file_commit(
        repo,
        {
            "target.py": "v1\n",
            "package-lock.json": '{"v": 1}\n',
            "real.py": "r1\n",
        },
        "init",
    )
    _multi_file_commit(
        repo,
        {
            "target.py": "v2\n",
            "package-lock.json": '{"v": 2}\n',
            "real.py": "r2\n",
        },
        "again",
    )

    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        service = CoChangeService(conn, repo)
        report = service.report_for("target.py")
        paths = [n.file_path for n in report.neighbors]
        assert "package-lock.json" not in paths
        assert "real.py" in paths
    finally:
        conn.close()


def test_service_truncates_at_top_k(
    init_git_repo, tmp_path: Path
) -> None:
    repo = init_git_repo()
    # One commit that touches target + 5 other files. Pass min_cochange_count=1
    # because each "other_*.py" only co-changes once — the test is about top_k
    # truncation, not about the noise filter.
    files = {"target.py": "v1\n"}
    for i in range(5):
        files[f"other_{i}.py"] = f"o{i}\n"
    _multi_file_commit(repo, files, "wide commit")

    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        service = CoChangeService(conn, repo)
        report = service.report_for("target.py", top_k=3, min_cochange_count=1)
        assert len(report.neighbors) == 3
        assert report.truncated == 2
    finally:
        conn.close()


def test_service_caches_per_commit_avoiding_repeat_subprocess(
    init_git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_git_repo()
    _multi_file_commit(
        repo, {"target.py": "v1\n", "other.py": "o\n"}, "init"
    )

    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        service = CoChangeService(conn, repo)
        # First call populates the cache.
        service.report_for("target.py")

        # Now monkeypatch files_in_commit to count further calls.
        calls: list[str] = []
        original = cochange_git.files_in_commit

        def _spy(repo_root, sha):
            calls.append(sha)
            return original(repo_root, sha)

        monkeypatch.setattr(
            "whygraph.cochange.service.cochange_git.files_in_commit", _spy
        )
        service.report_for("target.py")
        assert calls == []  # cache hit, no further subprocess
    finally:
        conn.close()


def test_service_below_min_commits_threshold_still_returns_neighbors(
    init_git_repo, tmp_path: Path
) -> None:
    """The service itself doesn't gate on min-commits; that gate lives in the
    prompt renderer (so callers can still inspect raw counts).

    Pass min_cochange_count=1 to disable the *separate* noise filter — the
    point here is the missing min-commits gate, not the noise filter.
    """
    repo = init_git_repo()
    _multi_file_commit(repo, {"target.py": "v1\n", "other.py": "o\n"}, "init")

    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        service = CoChangeService(conn, repo)
        report = service.report_for("target.py", min_cochange_count=1)
        assert report.commits_considered == 1
        assert report.commits_considered < MIN_COMMITS_FOR_DISPLAY
        # Service still surfaces the data; renderer is what suppresses it.
        assert len(report.neighbors) == 1
    finally:
        conn.close()


def test_service_default_filters_single_occurrence_neighbors(
    init_git_repo, tmp_path: Path
) -> None:
    """The default min_cochange_count=2 drops files that only ever co-changed
    once — those are coincidence, not coupling, and dominated the long tail
    in the engage-repo smoke run.
    """
    repo = init_git_repo()
    # one_off.py appears in only one commit with target.py.
    # real_coupling.py appears in two — should survive the filter.
    _multi_file_commit(
        repo, {"target.py": "v1\n", "one_off.py": "x\n", "real_coupling.py": "r1\n"}, "first"
    )
    _multi_file_commit(
        repo, {"target.py": "v2\n", "real_coupling.py": "r2\n"}, "second"
    )

    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        service = CoChangeService(conn, repo)
        report = service.report_for("target.py")  # default filter
        paths = [n.file_path for n in report.neighbors]
        assert "one_off.py" not in paths
        assert "real_coupling.py" in paths
    finally:
        conn.close()


def test_service_min_cochange_count_one_includes_everything(
    init_git_repo, tmp_path: Path
) -> None:
    """The min_cochange_count parameter is an explicit escape hatch for
    callers who want the unfiltered long tail (e.g. debugging)."""
    repo = init_git_repo()
    _multi_file_commit(
        repo, {"target.py": "v1\n", "one_off.py": "x\n"}, "first"
    )

    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        service = CoChangeService(conn, repo)
        report = service.report_for("target.py", min_cochange_count=1)
        paths = [n.file_path for n in report.neighbors]
        assert "one_off.py" in paths
    finally:
        conn.close()


def test_service_truncated_count_reflects_post_filter_total(
    init_git_repo, tmp_path: Path
) -> None:
    """`truncated` should count files that survived the noise filter and got
    cut by top_k — not raw co-occurrence count. Otherwise the prompt header
    "(top N of M)" implies more meaningful coupling than actually exists.
    """
    repo = init_git_repo()
    # Two commits, each touching the same 4 "real" co-changing files plus a
    # different one-off file. Without the filter, total = 6 (4 real + 2
    # one-offs); with the filter, total = 4.
    _multi_file_commit(
        repo,
        {
            "target.py": "v1\n",
            "real_a.py": "a1\n",
            "real_b.py": "b1\n",
            "real_c.py": "c1\n",
            "real_d.py": "d1\n",
            "one_off_1.py": "x\n",
        },
        "first",
    )
    _multi_file_commit(
        repo,
        {
            "target.py": "v2\n",
            "real_a.py": "a2\n",
            "real_b.py": "b2\n",
            "real_c.py": "c2\n",
            "real_d.py": "d2\n",
            "one_off_2.py": "y\n",
        },
        "second",
    )

    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        service = CoChangeService(conn, repo)
        report = service.report_for("target.py", top_k=2)
        # 4 real co-changers survive the filter; top_k=2 keeps 2; so truncated=2,
        # NOT 4 (which would include the two one-offs that got filtered).
        assert len(report.neighbors) == 2
        assert report.truncated == 2
    finally:
        conn.close()


def test_service_min_cochange_count_constant_is_two() -> None:
    """The default exists so users get sensible behavior out of the box.
    Documented + tested so accidental changes get caught at PR review.
    """
    assert MIN_COCHANGE_COUNT == 2


# ---------------------------------------------------------------------------
# cochange_fingerprint
# ---------------------------------------------------------------------------


def _report(
    *, head_sha: str = "abc123", target_file: str = "x.py"
) -> CoChangeReport:
    return CoChangeReport(
        target_file=target_file,
        head_sha=head_sha,
        commits_considered=0,
        neighbors=[],
        truncated=0,
    )


def test_fingerprint_stable_for_same_inputs() -> None:
    a = cochange_fingerprint(_report())
    b = cochange_fingerprint(_report())
    assert a == b
    assert len(a) == 64


def test_fingerprint_changes_when_head_changes() -> None:
    a = cochange_fingerprint(_report(head_sha="aaa"))
    b = cochange_fingerprint(_report(head_sha="bbb"))
    assert a != b


def test_fingerprint_changes_when_target_file_changes() -> None:
    a = cochange_fingerprint(_report(target_file="x.py"))
    b = cochange_fingerprint(_report(target_file="y.py"))
    assert a != b


def test_fingerprint_includes_version_constant() -> None:
    """Bumping COCHANGE_VERSION should invalidate every fingerprint.

    We can't easily patch a module-level constant string baked into the hash,
    so this test verifies the fingerprint encoding includes the version
    literal — bumping the constant in source gives a different hash.
    """
    expected_payload = f"cochange|{COCHANGE_VERSION}|abc123|x.py"
    import hashlib

    expected = hashlib.sha256(expected_payload.encode("utf-8")).hexdigest()
    assert cochange_fingerprint(_report()) == expected


# ---------------------------------------------------------------------------
# Denylist
# ---------------------------------------------------------------------------


def test_denylist_default_contains_lockfiles() -> None:
    assert "package-lock.json" in DEFAULT_DENYLIST
    assert "yarn.lock" in DEFAULT_DENYLIST
    assert "uv.lock" in DEFAULT_DENYLIST
