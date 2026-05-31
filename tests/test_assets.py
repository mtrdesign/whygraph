"""Tests for ``whygraph.assets`` — bundled agent asset installer."""

from __future__ import annotations

from pathlib import Path

import pytest

from whygraph import agents, assets


# ---- packaged tree: the shipping smoke test ------------------------------


def test_packaged_claude_assets_present() -> None:
    """All 9 bundled .md files are reachable via importlib.resources.

    Guards against a packaging regression: if hatch stopped shipping
    ``src/whygraph/assets/claude-code/`` for any reason, this test
    fails before the CLI ever runs.
    """
    root = assets.packaged_assets_for(agents.resolve_agent("claude"))
    expected = (
        "CLAUDE.md",
        "agents/planner.md",
        "agents/researcher.md",
        "agents/synthesizer.md",
        "agents/implementor.md",
        "skills/whygraph-plan/SKILL.md",
        "skills/whygraph-implement/SKILL.md",
        "skills/rationale/SKILL.md",
        "skills/pre-edit/SKILL.md",
    )
    for rel in expected:
        leaf = root
        for part in rel.split("/"):
            leaf = leaf / part
        assert leaf.is_file(), f"missing packaged asset: {rel}"


def test_packaged_cursor_assets_present() -> None:
    """All bundled Cursor assets are reachable via importlib.resources.

    Covers the full ported surface: 4 rules (skills equivalent), 3
    commands (slash commands), and 4 subagents — mirroring the
    ``claude-code/`` tree where Cursor has a native concept.
    """
    root = assets.packaged_assets_for(agents.resolve_agent("cursor"))
    expected = (
        "rules/whygraph-pre-edit.mdc",
        "rules/whygraph-ask-why.mdc",
        "rules/whygraph-plan-change.mdc",
        "rules/whygraph-implement-plan.mdc",
        "commands/rationale.md",
        "commands/whygraph-plan.md",
        "commands/whygraph-implement.md",
        "rules/whygraph-codegraph.mdc",
        "agents/planner.md",
        "agents/researcher.md",
        "agents/synthesizer.md",
        "agents/implementor.md",
    )
    for rel in expected:
        leaf = root
        for part in rel.split("/"):
            leaf = leaf / part
        assert leaf.is_file(), f"missing packaged asset: {rel}"


def test_packaged_codex_assets_present() -> None:
    """All bundled Codex assets are reachable via importlib.resources.

    Codex has no separate skill / slash-command surface, so the asset
    tree is minimal: one ``AGENTS.md`` at the repo root (append-merged)
    plus 4 subagents under ``.codex/agents/``. The source layout mirrors
    the destination — ``.codex/`` is nested inside the source so the
    installer (assets_dest = repo root) places files at the correct
    Codex-expected paths.
    """
    root = assets.packaged_assets_for(agents.resolve_agent("codex"))
    expected = (
        "AGENTS.md",
        ".codex/agents/planner.toml",
        ".codex/agents/researcher.toml",
        ".codex/agents/synthesizer.toml",
        ".codex/agents/implementor.toml",
    )
    for rel in expected:
        leaf = root
        for part in rel.split("/"):
            leaf = leaf / part
        assert leaf.is_file(), f"missing packaged asset: {rel}"


def test_packaged_vscode_assets_present() -> None:
    """All bundled VS Code Copilot assets are reachable via importlib.resources.

    Covers the full ported surface: 1 top-level instructions file
    (append-merged), 4 description-driven instructions (skills
    equivalent), 3 prompt files (slash commands equivalent), and 4
    custom agents — mirroring the ``claude-code/`` tree against VS
    Code's 2026 Copilot conventions.
    """
    root = assets.packaged_assets_for(agents.resolve_agent("vscode"))
    expected = (
        "copilot-instructions.md",
        "instructions/pre-edit.instructions.md",
        "instructions/ask-why.instructions.md",
        "instructions/plan-change.instructions.md",
        "instructions/implement-plan.instructions.md",
        "prompts/rationale.prompt.md",
        "prompts/whygraph-plan.prompt.md",
        "prompts/whygraph-implement.prompt.md",
        "agents/planner.agent.md",
        "agents/researcher.agent.md",
        "agents/synthesizer.agent.md",
        "agents/implementor.agent.md",
    )
    for rel in expected:
        leaf = root
        for part in rel.split("/"):
            leaf = leaf / part
        assert leaf.is_file(), f"missing packaged asset: {rel}"


def test_packaged_assets_for_rejects_unconfigured_agent() -> None:
    """Agents with ``assets_subdir=None`` raise ``ValueError``.

    All registered agents now ship assets; use a synthetic target to
    exercise the defensive guard.
    """
    bare = agents.AgentTarget(
        name="synthetic-bare",
        aliases=(),
        relative_path=("ignored",),
        scope="project",
        format="json",
        description="synthetic target with no assets configured",
    )
    assert not bare.has_assets
    with pytest.raises(ValueError, match="no bundled assets"):
        assets.packaged_assets_for(bare)


# ---- install_assets: fresh project ---------------------------------------


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


def _claude_target() -> agents.AgentTarget:
    """Return the Claude Code agent target — the only one with assets in v1."""
    return agents.resolve_agent("claude")


def test_install_writes_to_dest(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()

    result = assets.install_assets(_claude_target(), project, source=src)

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
    """The destination dir and all nested dirs are mkdir'd as needed."""
    src = _make_source(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    assert not (project / ".claude").exists()

    assets.install_assets(_claude_target(), project, source=src)

    assert (project / ".claude" / "skills" / "y").is_dir()


# ---- install_assets: idempotency -----------------------------------------


def test_install_skips_existing(tmp_path: Path) -> None:
    """Without ``force``, files that exist on disk are left alone."""
    src = _make_source(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".claude" / "agents").mkdir(parents=True)
    user_edit = project / ".claude" / "agents" / "x.md"
    user_edit.write_text("USER EDIT", encoding="utf-8")

    result = assets.install_assets(_claude_target(), project, source=src)

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

    result = assets.install_assets(_claude_target(), project, source=src, force=True)

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
    assets.install_assets(_claude_target(), project, source=src)

    result = assets.install_assets(_claude_target(), project, source=src)

    assert len(result.skipped) == 3
    assert result.written == []
    assert result.overwritten == []


# ---- install_assets: real packaged source --------------------------------


def test_install_from_packaged_source(tmp_path: Path) -> None:
    """No source override — real packaged assets land in the target tree.

    End-to-end check that ``packaged_assets_for()`` returns something
    the installer can walk.
    """
    project = tmp_path / "proj"
    project.mkdir()
    result = assets.install_assets(_claude_target(), project)

    assert (project / ".claude" / "agents" / "planner.md").is_file()
    assert (project / ".claude" / "skills" / "rationale" / "SKILL.md").is_file()
    assert (project / ".claude" / "skills" / "pre-edit" / "SKILL.md").is_file()
    # 4 agents + 4 skills + CLAUDE.md (merge file, written fresh) = 9 files.
    assert len(result.written) == 9


# ---- install_assets: defensive guard -------------------------------------


def test_install_assets_rejects_unconfigured_agent(tmp_path: Path) -> None:
    """Calling install_assets on an agent with no bundled tree raises."""
    bare = agents.AgentTarget(
        name="synthetic-bare",
        aliases=(),
        relative_path=("ignored",),
        scope="project",
        format="json",
        description="synthetic target with no assets configured",
    )
    assert not bare.has_assets
    with pytest.raises(ValueError, match="no bundled assets"):
        assets.install_assets(bare, tmp_path)


# ---- install_assets: append-merge for shared instruction files -----------


def _make_merge_source(tmp_path: Path) -> Path:
    """Build a source tree with one merge file at the root + one normal file.

    Layout::

        src/
        ├── merge-me.md            ← merge file (root-level)
        └── sidecar/keepme.md      ← normal file (skip-or-overwrite path)
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "merge-me.md").write_text("BUNDLED BODY", encoding="utf-8")
    (src / "sidecar").mkdir()
    (src / "sidecar" / "keepme.md").write_text("KEEPME", encoding="utf-8")
    return src


def _merge_target() -> agents.AgentTarget:
    """Build an ephemeral AgentTarget that uses the append-merge path."""
    return agents.AgentTarget(
        name="test-merge",
        aliases=(),
        relative_path=("ignored",),
        scope="project",
        format="json",
        description="ephemeral test target",
        assets_subdir="ignored",  # source is injected via the source= arg
        assets_dest=("dest",),
        assets_merge_files=("merge-me.md",),
    )


def test_merge_writes_fresh_file_with_markers(tmp_path: Path) -> None:
    """When the destination file doesn't exist, write the wrapped block fresh."""
    src = _make_merge_source(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()

    result = assets.install_assets(_merge_target(), project, source=src)

    dest = project / "dest" / "merge-me.md"
    content = dest.read_text(encoding="utf-8")
    assert assets.BEGIN_MARKER in content
    assert assets.END_MARKER in content
    assert "BUNDLED BODY" in content
    assert dest in result.written
    # The sidecar normal file also lands.
    assert (project / "dest" / "sidecar" / "keepme.md").is_file()


def test_merge_replaces_between_markers_on_re_run(tmp_path: Path) -> None:
    """Re-running with markers present replaces the block in place.

    Confirms idempotency: running twice doesn't grow the file with
    repeated WhyGraph blocks, and any user content outside the markers
    is preserved verbatim.
    """
    src = _make_merge_source(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    # First run plants the block.
    assets.install_assets(_merge_target(), project, source=src)

    # Add user content outside the markers (above and below).
    dest = project / "dest" / "merge-me.md"
    seeded = "user prefix\n\n" + dest.read_text(encoding="utf-8") + "user suffix\n"
    dest.write_text(seeded, encoding="utf-8")

    # Re-run: block should be re-merged in place, user content preserved.
    result = assets.install_assets(_merge_target(), project, source=src)

    content = dest.read_text(encoding="utf-8")
    assert content.count(assets.BEGIN_MARKER) == 1
    assert content.count(assets.END_MARKER) == 1
    assert content.startswith("user prefix\n")
    assert content.endswith("user suffix\n")
    assert "BUNDLED BODY" in content
    assert dest in result.overwritten


def test_merge_appends_when_existing_has_no_markers(tmp_path: Path) -> None:
    """If the destination exists without markers, append the wrapped block."""
    src = _make_merge_source(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    dest = project / "dest" / "merge-me.md"
    dest.parent.mkdir(parents=True)
    dest.write_text("user content\n", encoding="utf-8")

    result = assets.install_assets(_merge_target(), project, source=src)

    content = dest.read_text(encoding="utf-8")
    assert content.startswith("user content\n")
    assert assets.BEGIN_MARKER in content
    assert assets.END_MARKER in content
    assert "BUNDLED BODY" in content
    # User content is before the block, not replaced.
    user_idx = content.find("user content")
    begin_idx = content.find(assets.BEGIN_MARKER)
    assert user_idx < begin_idx
    assert dest in result.overwritten


def test_merge_idempotent_byte_for_byte(tmp_path: Path) -> None:
    """Two back-to-back installs produce byte-identical files."""
    src = _make_merge_source(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    assets.install_assets(_merge_target(), project, source=src)
    dest = project / "dest" / "merge-me.md"
    first = dest.read_text(encoding="utf-8")

    assets.install_assets(_merge_target(), project, source=src)
    second = dest.read_text(encoding="utf-8")

    assert first == second
