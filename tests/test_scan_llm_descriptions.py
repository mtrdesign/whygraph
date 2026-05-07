import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console
from rich.progress import Progress

from whygraph.scan import db as db_module
from whygraph.scan import llm_descriptions as llm_module
from whygraph.scan.git import Commit
from whygraph.scan.llm_descriptions import (
    LlmConfig,
    LlmError,
    _PROMPT_TEMPLATE,
    claude_cli_available,
    commits_to_describe,
    describe_pair,
    get_pair_diff,
    run_phase,
)


def _silent_progress() -> Progress:
    return Progress(console=Console(quiet=True))


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _make_repo_with_n_commits(tmp_path: Path, n: int) -> tuple[Path, list[str]]:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    shas: list[str] = []
    for i in range(n):
        (tmp_path / f"f{i}.txt").write_text(f"v{i}\n")
        _git(tmp_path, "add", f"f{i}.txt")
        _git(tmp_path, "commit", "-q", "-m", f"commit {i}")
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        shas.append(result.stdout.strip())
    return tmp_path, shas


class _FakeResult:
    def __init__(
        self, returncode: int = 0, stdout: str = "", stderr: str = ""
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _sample_commit(sha: str) -> Commit:
    return Commit(
        sha=sha,
        parent_shas=[],
        author_name="A",
        author_email="a@x.com",
        authored_at="2026-01-01T00:00:00+00:00",
        committed_at="2026-01-01T00:00:00+00:00",
        subject=f"commit {sha[:7]}",
        body="",
        files_changed=1,
        insertions=1,
        deletions=0,
    )


def test_prompt_template_only_has_diff_placeholder() -> None:
    # Defensive check: no smuggled human-written fields.
    assert "{diff}" in _PROMPT_TEMPLATE
    assert "{subject}" not in _PROMPT_TEMPLATE
    assert "{body}" not in _PROMPT_TEMPLATE
    assert "{title}" not in _PROMPT_TEMPLATE


def test_prompt_template_frames_audience_as_llm_future_self() -> None:
    """The description is an LLM-internal artifact. The prompt must not
    pitch it as documentation for humans/developers, and must communicate
    the two anchors (token efficiency, no ambiguity)."""
    lower = _PROMPT_TEMPLATE.lower()
    # Audience reframing.
    assert "future self" in lower
    assert "no human reads" in lower
    # Anchors are stated explicitly.
    assert "token efficiency" in lower
    assert "no ambiguity" in lower
    # No prescriptive format/identifier rules — those are the explicit
    # anti-pattern this rewrite drops.
    assert "verbatim identifiers" not in lower
    assert "before→after" not in _PROMPT_TEMPLATE
    assert "no hedging" not in lower


def test_get_pair_diff_returns_unified_diff(tmp_path: Path) -> None:
    root, shas = _make_repo_with_n_commits(tmp_path, 2)
    diff = get_pair_diff(root, shas[0], shas[1])
    assert "diff --git" in diff
    assert "f1.txt" in diff


def test_commits_to_describe_skips_filled_and_drops_last(tmp_path: Path) -> None:
    root, shas = _make_repo_with_n_commits(tmp_path, 3)
    db_path = tmp_path / "whygraph.db"
    with db_module.Database(db_path) as db:
        for sha in shas:
            db.upsert_commit(_sample_commit(sha))
        # Mark the middle commit as already described.
        db.set_llm_description(shas[1], "already done", "test-model")

        pairs = commits_to_describe(db, root, "main")
    # Only commit-0 needs describing (pairs to commit-1).
    # commit-1 is filled. commit-2 is the last, never gets described.
    assert pairs == [(shas[0], shas[1])]


def test_commits_to_describe_handles_single_commit(tmp_path: Path) -> None:
    root, shas = _make_repo_with_n_commits(tmp_path, 1)
    db_path = tmp_path / "whygraph.db"
    with db_module.Database(db_path) as db:
        db.upsert_commit(_sample_commit(shas[0]))
        assert commits_to_describe(db, root, "main") == []


def test_commits_to_describe_limit_keeps_only_most_recent_pairs(
    tmp_path: Path,
) -> None:
    """walk_first_parent yields oldest→newest, so the last N pairs are
    the most recent. With 5 commits there are 4 pairs; limit=2 should
    keep the two newest."""
    root, shas = _make_repo_with_n_commits(tmp_path, 5)
    db_path = tmp_path / "whygraph.db"
    with db_module.Database(db_path) as db:
        for sha in shas:
            db.upsert_commit(_sample_commit(sha))
        pairs = commits_to_describe(db, root, "main", limit=2)
    # Last two pairs cover commits 2→3 and 3→4 (HEAD is index 4, never
    # described because it has no child).
    assert pairs == [(shas[2], shas[3]), (shas[3], shas[4])]


def test_commits_to_describe_limit_larger_than_history_returns_all(
    tmp_path: Path,
) -> None:
    root, shas = _make_repo_with_n_commits(tmp_path, 3)
    db_path = tmp_path / "whygraph.db"
    with db_module.Database(db_path) as db:
        for sha in shas:
            db.upsert_commit(_sample_commit(sha))
        pairs = commits_to_describe(db, root, "main", limit=99)
    assert pairs == [(shas[0], shas[1]), (shas[1], shas[2])]


def test_commits_to_describe_limit_zero_returns_empty(tmp_path: Path) -> None:
    root, shas = _make_repo_with_n_commits(tmp_path, 3)
    db_path = tmp_path / "whygraph.db"
    with db_module.Database(db_path) as db:
        for sha in shas:
            db.upsert_commit(_sample_commit(sha))
        assert commits_to_describe(db, root, "main", limit=0) == []


def test_describe_pair_truncates_oversize_diff() -> None:
    captured = {}

    def fake_run(args, *, input, **kw):
        captured["input"] = input
        return _FakeResult(returncode=0, stdout="ok")

    big = "x" * 60_000
    with patch("whygraph.llm_subprocess.subprocess.run", side_effect=fake_run):
        describe_pair(big, LlmConfig(model="m", max_diff_chars=50_000))
    prompt = captured["input"]
    assert "[truncated:" in prompt
    assert len(prompt) < 55_000
    assert prompt.count("x") < 51_000


def test_run_phase_writes_back(tmp_path: Path) -> None:
    root, shas = _make_repo_with_n_commits(tmp_path, 3)
    db_path = tmp_path / "whygraph.db"
    with db_module.Database(db_path) as db:
        for sha in shas:
            db.upsert_commit(_sample_commit(sha))

    with patch.object(llm_module, "describe_pair", return_value="canned description"):
        with _silent_progress() as progress:
            task_id = progress.add_task("llm", total=None, start=False)
            progress.start_task(task_id)
            summary = run_phase(
                db_path, root, "main", LlmConfig(model="haiku-test"), progress, task_id
            )

    assert "2 described" in summary
    with db_module.Database(db_path) as db:
        cur = db._conn.cursor()
        cur.execute(
            "SELECT sha, llm_description, llm_description_model FROM commits ORDER BY sha"
        )
        rows = cur.fetchall()
    by_sha = {sha: (desc, model) for sha, desc, model in rows}
    assert by_sha[shas[0]] == ("canned description", "haiku-test")
    assert by_sha[shas[1]] == ("canned description", "haiku-test")
    # Last commit has no successor → stays NULL.
    assert by_sha[shas[2]] == (None, None)


def test_run_phase_persists_per_row_on_failure(tmp_path: Path) -> None:
    root, shas = _make_repo_with_n_commits(tmp_path, 4)
    db_path = tmp_path / "whygraph.db"
    with db_module.Database(db_path) as db:
        for sha in shas:
            db.upsert_commit(_sample_commit(sha))

    call_count = {"n": 0}

    def flaky(diff, config):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise LlmError("simulated failure")
        return f"desc {call_count['n']}"

    with patch.object(llm_module, "describe_pair", side_effect=flaky):
        with _silent_progress() as progress:
            task_id = progress.add_task("llm", total=None, start=False)
            progress.start_task(task_id)
            summary = run_phase(
                db_path,
                root,
                "main",
                LlmConfig(max_workers=1),
                progress,
                task_id,
            )

    # 3 pairs (4 commits → 3 pairs); 2nd failed; 1st and 3rd succeed.
    assert "2 described" in summary
    assert "1 failed" in summary
    with db_module.Database(db_path) as db:
        cur = db._conn.cursor()
        cur.execute(
            "SELECT sha, llm_description FROM commits ORDER BY committed_at"
        )
        rows = dict(cur.fetchall())
    assert rows[shas[0]] is not None  # first pair succeeded
    assert rows[shas[1]] is None  # second pair failed (still NULL → retried next scan)
    assert rows[shas[2]] is not None  # third pair succeeded
    assert rows[shas[3]] is None  # last commit has no successor


def test_run_phase_no_pairs_returns_summary(tmp_path: Path) -> None:
    root, shas = _make_repo_with_n_commits(tmp_path, 1)
    db_path = tmp_path / "whygraph.db"
    with db_module.Database(db_path) as db:
        db.upsert_commit(_sample_commit(shas[0]))
    with _silent_progress() as progress:
        task_id = progress.add_task("llm", total=None, start=False)
        progress.start_task(task_id)
        summary = run_phase(db_path, root, "main", LlmConfig(), progress, task_id)
    assert "0 to describe" in summary


def test_claude_cli_available_truthy_or_falsy() -> None:
    # Just exercise the function — result depends on the test environment.
    assert claude_cli_available() in (True, False)
