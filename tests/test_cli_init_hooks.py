"""End-to-end tests for ``whygraph init``'s git-hook reconcile (plan §4.6).

Drives the real command through :class:`click.testing.CliRunner` against a
throwaway git repo, with the DB and preflight stubbed out (same approach as
``tests/test_init_agents.py``) so these tests are about hook reconciliation
and nothing else.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from whygraph.cli.commands.init import init_cmd
from whygraph.hooks import HELPER_RELPATH, HOOK_NAMES, SENTINEL


@pytest.fixture
def stub_init(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Neutralise the heavy init steps — DB bootstrap and host preflight."""

    def _fake_db() -> Path:
        db = tmp_path / ".whygraph" / "whygraph.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.touch()
        return db

    monkeypatch.setattr("whygraph.cli.commands.init._ensure_db_initialized", _fake_db)
    monkeypatch.setattr("whygraph.cli.commands.init._run_preflight", lambda: None)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def _init(repo: Path, *args: str):
    # `init_cmd` directly, not the `whygraph` group: the group callback runs
    # `configure_logging`, which replaces the root logger's handlers and would
    # silently disable `caplog` for every test that runs after this file.
    runner = CliRunner()
    return runner.invoke(init_cmd, list(args), catch_exceptions=False)


def _run_in(repo: Path, monkeypatch: pytest.MonkeyPatch, *args: str):
    monkeypatch.chdir(repo)
    return _init(repo, *args)


def _managed(repo: Path) -> set[str]:
    """Hook names currently carrying the managed block."""
    hooks_dir = repo / ".git" / "hooks"
    return {
        name
        for name in HOOK_NAMES
        if (hooks_dir / name).exists() and SENTINEL in (hooks_dir / name).read_text()
    }


def test_init_yes_installs_all_four_hooks(
    stub_init, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 35."""
    result = _run_in(repo, monkeypatch, "--yes")

    assert result.exit_code == 0, result.output
    assert _managed(repo) == set(HOOK_NAMES)
    assert (repo / HELPER_RELPATH).exists()
    assert "Installed auto-rescan git hooks" in result.output


def test_existing_opt_out_is_not_resurrected(
    stub_init, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 36 / property 2 — a prior `hooks = false` survives a re-run."""
    (repo / "whygraph.toml").write_text("[scan]\nhooks = false\n")

    result = _run_in(repo, monkeypatch, "--yes")

    assert result.exit_code == 0, result.output
    assert _managed(repo) == set()
    assert not (repo / HELPER_RELPATH).exists()


def test_flipping_to_false_removes_hooks_and_helper(
    stub_init, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 37 — `init` is the reconciler, so it uninstalls too."""
    _run_in(repo, monkeypatch, "--yes")
    assert _managed(repo) == set(HOOK_NAMES)

    (repo / "whygraph.toml").write_text("[scan]\nhooks = false\n")
    result = _run_in(repo, monkeypatch, "--yes")

    assert result.exit_code == 0, result.output
    assert _managed(repo) == set()
    assert not (repo / HELPER_RELPATH).exists()
    assert "Removed git hooks" in result.output


def test_shrinking_the_list_drops_the_others(
    stub_init, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 38 (D7 end-to-end) — the removal half, through the real command."""
    _run_in(repo, monkeypatch, "--yes")

    (repo / "whygraph.toml").write_text('[scan]\nhooks = ["post-commit"]\n')
    result = _run_in(repo, monkeypatch, "--yes")

    assert result.exit_code == 0, result.output
    assert _managed(repo) == {"post-commit"}
    # The helper stays — post-commit still dispatches to it.
    assert (repo / HELPER_RELPATH).exists()


def test_typo_in_hook_name_warns_and_installs_nothing(
    stub_init, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 14 / 41 — a bad name is a warning, not a failed init."""
    (repo / "whygraph.toml").write_text('[scan]\nhooks = ["post-comit"]\n')

    result = _run_in(repo, monkeypatch, "--yes")

    assert result.exit_code == 0, result.output
    assert "post-comit" in result.output
    assert _managed(repo) == set()
    # The rest of init still completed.
    assert "Initialized WhyGraph database" in result.output


def test_unwritable_hooks_dir_warns_but_init_succeeds(
    stub_init, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 16 / 42 — best-effort (§4.6 property 1)."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hooks_dir.chmod(0o500)
    try:
        result = _run_in(repo, monkeypatch, "--yes")
    finally:
        hooks_dir.chmod(0o700)

    assert result.exit_code == 0, result.output
    assert "Skipped git hooks" in result.output
    # The DB, config and gitignore work all completed regardless.
    assert (repo / "whygraph.toml").exists()
    assert (repo / "whygraph.example.toml").exists()


def test_not_a_git_repo_warns_but_init_succeeds(
    stub_init, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`whygraph init` outside a repo still bootstraps everything else."""
    plain = tmp_path / "plain"
    plain.mkdir()

    result = _run_in(plain, monkeypatch, "--yes")

    assert result.exit_code == 0, result.output
    assert "Skipped git hooks" in result.output
    assert (plain / "whygraph.toml").exists()
