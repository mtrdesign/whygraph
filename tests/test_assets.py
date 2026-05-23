"""Tests for ``whygraph.assets`` — bundled Claude Code asset installer."""

from __future__ import annotations

from pathlib import Path

import pytest

from whygraph import assets


# ---- packaged tree: the shipping smoke test ------------------------------


def test_packaged_claude_code_assets_present() -> None:
    """All 11 bundled .md files are reachable via importlib.resources.

    Guards against a packaging regression: if hatch stopped shipping
    ``src/whygraph/assets/claude-code/`` for any reason, this test
    fails before the CLI ever runs.
    """
    root = assets.packaged_claude_code_assets()
    expected = (
        "agents/planner.md",
        "agents/researcher.md",
        "agents/synthesizer.md",
        "agents/implementor.md",
        "commands/rationale.md",
        "commands/whygraph-plan.md",
        "commands/whygraph-implement.md",
        "skills/ask-why/SKILL.md",
        "skills/implement-plan/SKILL.md",
        "skills/plan-change/SKILL.md",
        "skills/pre-edit/SKILL.md",
    )
    for rel in expected:
        leaf = root
        for part in rel.split("/"):
            leaf = leaf / part
        assert leaf.is_file(), f"missing packaged asset: {rel}"


# ---- install_claude_code_assets: fresh project ---------------------------


def _make_source(tmp_path: Path) -> Path:
    """Build a tiny on-disk asset tree mirroring the real layout."""
    src = tmp_path / "src"
    (src / "agents").mkdir(parents=True)
    (src / "agents" / "x.md").write_text("X-AGENT", encoding="utf-8")
    (src / "commands").mkdir()
    (src / "commands" / "do-it.md").write_text("DO-IT", encoding="utf-8")
    (src / "skills" / "y").mkdir(parents=True)
    (src / "skills" / "y" / "SKILL.md").write_text("Y-SKILL", encoding="utf-8")
    return src


def test_install_writes_to_dot_claude(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()

    result = assets.install_claude_code_assets(project, source=src)

    assert (project / ".claude" / "agents" / "x.md").read_text(
        encoding="utf-8"
    ) == "X-AGENT"
    assert (project / ".claude" / "commands" / "do-it.md").read_text(
        encoding="utf-8"
    ) == "DO-IT"
    assert (project / ".claude" / "skills" / "y" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "Y-SKILL"
    assert len(result.written) == 3
    assert result.skipped == []
    assert result.overwritten == []


def test_install_creates_parents(tmp_path: Path) -> None:
    """``.claude/`` and all nested dirs are mkdir'd as needed."""
    src = _make_source(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    assert not (project / ".claude").exists()

    assets.install_claude_code_assets(project, source=src)

    assert (project / ".claude" / "skills" / "y").is_dir()


# ---- install_claude_code_assets: idempotency ------------------------------


def test_install_skips_existing(tmp_path: Path) -> None:
    """Without ``force``, files that exist on disk are left alone."""
    src = _make_source(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".claude" / "agents").mkdir(parents=True)
    user_edit = project / ".claude" / "agents" / "x.md"
    user_edit.write_text("USER EDIT", encoding="utf-8")

    result = assets.install_claude_code_assets(project, source=src)

    assert user_edit.read_text(encoding="utf-8") == "USER EDIT"
    assert user_edit in result.skipped
    # The other two files are fresh writes.
    assert len(result.written) == 2
    assert result.overwritten == []


def test_install_force_overwrites(tmp_path: Path) -> None:
    """With ``force=True``, pre-existing files are replaced and tallied."""
    src = _make_source(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".claude" / "agents").mkdir(parents=True)
    user_edit = project / ".claude" / "agents" / "x.md"
    user_edit.write_text("USER EDIT", encoding="utf-8")

    result = assets.install_claude_code_assets(project, source=src, force=True)

    assert user_edit.read_text(encoding="utf-8") == "X-AGENT"
    assert user_edit in result.overwritten
    assert result.skipped == []
    # The other two files weren't pre-existing, so they're in `written`.
    assert len(result.written) == 2


def test_install_re_run_is_a_noop(tmp_path: Path) -> None:
    """Second run with no edits and no force => everything skipped."""
    src = _make_source(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    assets.install_claude_code_assets(project, source=src)

    result = assets.install_claude_code_assets(project, source=src)

    assert len(result.skipped) == 3
    assert result.written == []
    assert result.overwritten == []


# ---- install_claude_code_assets: real packaged source --------------------


def test_install_from_packaged_source(tmp_path: Path) -> None:
    """No source override — real packaged assets land in the target tree.

    End-to-end check that ``packaged_claude_code_assets()`` returns
    something the installer can walk.
    """
    project = tmp_path / "proj"
    project.mkdir()
    result = assets.install_claude_code_assets(project)

    assert (project / ".claude" / "agents" / "planner.md").is_file()
    assert (project / ".claude" / "commands" / "rationale.md").is_file()
    assert (project / ".claude" / "skills" / "pre-edit" / "SKILL.md").is_file()
    # 4 agents + 3 commands + 4 skills = 11 files.
    assert len(result.written) == 11
