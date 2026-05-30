"""Tests for the ``whygraph hooks`` command group.

Exercise install / uninstall / status against a real (throwaway) git repo
via Click's ``CliRunner.isolated_filesystem``: the managed dispatcher is
sentinel-guarded, idempotent, and never clobbers a foreign hook, and the
generated shell is syntactically valid.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from click.testing import CliRunner

from whygraph.cli.commands.hooks import (
    HELPER_RELPATH,
    HOOK_NAMES,
    SENTINEL,
    hooks_cmd,
)


def _git_init() -> None:
    subprocess.run(["git", "init", "-q"], check=True)


def _install(runner: CliRunner):
    return runner.invoke(hooks_cmd, ["install"])


def test_install_creates_helper_and_hooks() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        _git_init()
        result = _install(runner)
        assert result.exit_code == 0, result.output

        helper = Path(HELPER_RELPATH)
        assert helper.exists()
        assert os.access(helper, os.X_OK)

        for name in HOOK_NAMES:
            hook = Path(".git/hooks") / name
            assert hook.exists(), name
            assert SENTINEL in hook.read_text()
            assert os.access(hook, os.X_OK)


def test_install_is_idempotent() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        _git_init()
        _install(runner)
        _install(runner)  # second run must not stack blocks
        for name in HOOK_NAMES:
            text = (Path(".git/hooks") / name).read_text()
            assert text.count(SENTINEL) == 1, name


def test_install_appends_to_foreign_hook() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        _git_init()
        foreign = Path(".git/hooks/post-commit")
        foreign.write_text("#!/bin/sh\necho custom-hook\n")

        _install(runner)

        text = foreign.read_text()
        assert "echo custom-hook" in text  # foreign content preserved
        assert SENTINEL in text  # ours appended


def test_uninstall_removes_ours_keeps_foreign() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        _git_init()
        foreign = Path(".git/hooks/post-commit")
        foreign.write_text("#!/bin/sh\necho custom-hook\n")

        _install(runner)
        result = runner.invoke(hooks_cmd, ["uninstall"])
        assert result.exit_code == 0, result.output

        # Foreign hook kept, our block stripped.
        text = foreign.read_text()
        assert "echo custom-hook" in text
        assert SENTINEL not in text
        # Hooks WhyGraph created outright are removed, as is the helper.
        assert not (Path(".git/hooks") / "post-merge").exists()
        assert not Path(HELPER_RELPATH).exists()


def test_status_reports_states() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        _git_init()
        before = runner.invoke(hooks_cmd, ["status"])
        assert before.exit_code == 0
        assert "missing" in before.output

        _install(runner)
        after = runner.invoke(hooks_cmd, ["status"])
        assert "managed" in after.output


def test_not_a_git_repo_errors() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():  # no git init
        result = _install(runner)
        assert result.exit_code != 0
        assert "not a git repository" in result.output


def test_generated_shell_is_valid() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        _git_init()
        _install(runner)
        # `sh -n` parses without executing — catches quoting/syntax errors.
        for path in [Path(HELPER_RELPATH), *(Path(".git/hooks") / n for n in HOOK_NAMES)]:
            check = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
            assert check.returncode == 0, f"{path}: {check.stderr}"
