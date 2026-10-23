"""Tests for ``whygraph.assets`` — bundled agent asset installer."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

from whygraph import agents, assets


# ---- packaged tree: the shipping smoke test ------------------------------


def test_packaged_claude_assets_present() -> None:
    """All 11 bundled .md files are reachable via importlib.resources.

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
        "agents/whygraph-reviewer.md",
        "skills/whygraph-plan/SKILL.md",
        "skills/whygraph-implement/SKILL.md",
        "skills/whygraph-review/SKILL.md",
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
    skills (slash commands), and 4 subagents — mirroring the
    ``claude-code/`` tree where Cursor has a native concept.
    """
    root = assets.packaged_assets_for(agents.resolve_agent("cursor"))
    expected = (
        "rules/whygraph-pre-edit.mdc",
        "rules/whygraph-ask-why.mdc",
        "rules/whygraph-plan-change.mdc",
        "rules/whygraph-implement-plan.mdc",
        "skills/rationale/SKILL.md",
        "skills/whygraph-plan/SKILL.md",
        "skills/whygraph-implement/SKILL.md",
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
    """Without ``force``, an edited file (no manifest) is left alone and flagged."""
    src = _make_source(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".claude" / "agents").mkdir(parents=True)
    user_edit = project / ".claude" / "agents" / "x.md"
    user_edit.write_text("USER EDIT", encoding="utf-8")

    result = assets.install_assets(_claude_target(), project, source=src)

    assert user_edit.read_text(encoding="utf-8") == "USER EDIT"
    assert user_edit in result.needs_confirmation
    assert "-USER EDIT" in result.diffs[user_edit]
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
    assert result.skipped == [] and result.needs_confirmation == []
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
    # 5 agents + 5 skills + CLAUDE.md (merge file, written fresh) = 11 files.
    assert len(result.written) == 11


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
    assert dest in result.skipped  # the block is unchanged: nothing to rewrite


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


# ---- upgrade manifest (plan section 6.4) -----------------------------------


def _manifest(project: Path) -> dict:
    return json.loads((project / ".whygraph" / "assets.json").read_text())


def _installed(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Install the fake tree once; return (source, project, x.md)."""
    src = _make_source(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    assets.install_assets(_claude_target(), project, source=src)
    return src, project, project / ".claude" / "agents" / "x.md"


def _bundle_change(src: Path) -> None:
    (src / "agents" / "x.md").write_text("X-AGENT v2", encoding="utf-8")


def test_manifest_round_trip(tmp_path: Path) -> None:
    _, project, _ = _installed(tmp_path)

    manifest = _manifest(project)

    assert manifest["version"] == assets.package_version()
    assert (
        manifest["files"]["claude/agents/x.md"]
        == hashlib.sha256(b"X-AGENT").hexdigest()
    )
    assert set(manifest["files"]) == {
        "claude/agents/x.md",
        "claude/commands/do-it.md",
        "claude/skills/y/SKILL.md",
    }
    assert assets.read_manifest(project) == manifest
    assert assets.manifest_status(project)["outdated"] is False


def test_dry_run_writes_no_manifest(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()

    assets.install_assets(_claude_target(), project, source=src, dry_run=True)

    assert not (project / ".whygraph").exists()


def test_unmodified_file_updates_silently(tmp_path: Path) -> None:
    src, project, x = _installed(tmp_path)
    _bundle_change(src)

    result = assets.install_assets(_claude_target(), project, source=src)

    assert x.read_text() == "X-AGENT v2"
    assert x in result.overwritten and result.needs_confirmation == []
    assert (
        _manifest(project)["files"]["claude/agents/x.md"]
        == hashlib.sha256(b"X-AGENT v2").hexdigest()
    )


def test_edited_file_with_unchanged_bundle_is_skipped(tmp_path: Path) -> None:
    src, project, x = _installed(tmp_path)
    x.write_text("MINE")

    result = assets.install_assets(_claude_target(), project, source=src)

    assert x.read_text() == "MINE"
    assert x in result.skipped and result.needs_confirmation == []


def test_edited_file_with_changed_bundle_needs_confirmation(tmp_path: Path) -> None:
    src, project, x = _installed(tmp_path)
    x.write_text("MINE")
    _bundle_change(src)
    before = _manifest(project)

    result = assets.install_assets(_claude_target(), project, source=src)

    assert x.read_text() == "MINE"
    assert result.needs_confirmation == [x]
    assert "+X-AGENT v2" in result.diffs[x]
    assert _manifest(project)["files"] == before["files"]

    forced = assets.install_assets(_claude_target(), project, source=src, force=True)
    assert x.read_text() == "X-AGENT v2" and x in forced.overwritten


def test_no_manifest_is_treated_as_edited(tmp_path: Path) -> None:
    src, project, x = _installed(tmp_path)
    (project / ".whygraph" / "assets.json").unlink()
    x.write_text("OLD COPY")

    result = assets.install_assets(_claude_target(), project, source=src)

    assert result.needs_confirmation == [x]
    assert x.read_text() == "OLD COPY"
    # The files that already equal the bundle are adopted into a new manifest.
    manifest = _manifest(project)
    assert "claude/commands/do-it.md" in manifest["files"]
    assert "claude/agents/x.md" not in manifest["files"]
    assert manifest["version"] is None  # still stale: no current-version claim


def test_missing_file_is_written_again(tmp_path: Path) -> None:
    src, project, x = _installed(tmp_path)
    x.unlink()

    result = assets.install_assets(_claude_target(), project, source=src)

    assert x in result.written


def test_merge_block_follows_the_table(tmp_path: Path) -> None:
    src = _make_merge_source(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    assets.install_assets(_merge_target(), project, source=src)
    dest = project / "dest" / "merge-me.md"
    key = "test-merge/merge-me.md"
    assert (
        _manifest(project)["files"][key] == hashlib.sha256(b"BUNDLED BODY").hexdigest()
    )

    # Unedited block + changed bundle: replaced silently, user text kept.
    dest.write_text("above\n" + dest.read_text())
    (src / "merge-me.md").write_text("BUNDLED BODY 2\n")
    result = assets.install_assets(_merge_target(), project, source=src)
    assert dest in result.overwritten
    assert dest.read_text().startswith("above\n")
    assert "BUNDLED BODY 2" in dest.read_text()

    # Edited block + changed bundle: confirmation, then force.
    dest.write_text(dest.read_text().replace("BUNDLED BODY 2", "MY BODY"))
    (src / "merge-me.md").write_text("BUNDLED BODY 3\n")
    result = assets.install_assets(_merge_target(), project, source=src)
    assert result.needs_confirmation == [dest]
    assert "MY BODY" in dest.read_text()
    forced = assets.install_assets(_merge_target(), project, source=src, force=True)
    assert dest in forced.overwritten and "BUNDLED BODY 3" in dest.read_text()


def test_manifest_status_reports_outdated(tmp_path: Path) -> None:
    _, project, _ = _installed(tmp_path)
    assert assets.manifest_status(project)["outdated"] is False
    assets.set_manifest_version(project, "0.0.1")

    status = assets.manifest_status(project)

    assert status["installed_version"] == "0.0.1" and status["outdated"] is True


# ---- CodeGraph guidance in the packaged trees (plan 4.6) -------------------

_ASSET_ROOT = Path(assets.__file__).parent / "assets"
_SERVED = (
    "codegraph_explore",
    "codegraph_node",
    "codegraph_search",
    "codegraph_impact",
)


def _asset_texts() -> dict[Path, str]:
    return {p: p.read_text() for p in _ASSET_ROOT.rglob("*") if p.is_file()}


def test_assets_name_only_served_codegraph_tools() -> None:
    import re

    for path, text in _asset_texts().items():
        for line in text.splitlines():
            for name in re.findall(r"codegraph_[a-z]+", line):
                if name in ("codegraph_callers", "codegraph_callees"):
                    assert "not available here" in line, (path, line)
                else:
                    assert name in _SERVED or name == "codegraph_", (path, name)


def test_assets_have_no_cli_or_stale_codegraph_instructions() -> None:
    banned = (
        "codegraph init",
        "whygraph scan",
        "watcher debounces",
        "explore call budget",
        "NEVER call",
    )
    for path, text in _asset_texts().items():
        for phrase in banned:
            assert phrase not in text, (path, phrase)


def test_guidance_blocks_key_on_iserror_and_pin_u6() -> None:
    claude = (_ASSET_ROOT / "claude-code" / "CLAUDE.md").read_text()
    assert "At most one call per question in the main session" in claude
    assert "deferred, load them with ToolSearch" in claude
    for rel in (
        "claude-code/CLAUDE.md",
        "codex/AGENTS.md",
        "vscode/copilot-instructions.md",
        "cursor/rules/whygraph-codegraph.mdc",
    ):
        text = (_ASSET_ROOT / rel).read_text()
        assert "`isError`" in text and "[WhyGraph]" in text, rel
        assert "without `isError` is not a WhyGraph note" in text, rel
        assert "projectPath" in text, rel
    # The deferral clause is Claude Code only.
    for path, text in _asset_texts().items():
        if "claude-code" not in path.parts:
            assert "ToolSearch" not in text, path


# ---- harness frontmatter (plan 6.1) ---------------------------------------

_FRONTMATTER_KEYS = {
    "claude-code": {
        "name", "description", "model", "tools", "disallowedTools", "maxTurns",
        "when_to_use", "user-invocable", "allowed-tools", "argument-hint",
        "disable-model-invocation",
    },
    "cursor": {
        "name", "description", "model", "readonly", "is_background",
        "alwaysApply", "globs", "disable-model-invocation",
    },
    "vscode": {
        "name", "description", "tools", "agents", "user-invocable", "agent",
        "argument-hint", "applyTo",
    },
}  # fmt: skip
_DOCUMENTED_MODELS = {"sonnet", "opus", "haiku", "inherit"}


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    block = text[4 : text.index("\n---\n", 4)]
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line and not line.startswith((" ", "-")):
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    return out


def test_frontmatter_allowlist() -> None:
    """Every asset's frontmatter keys and ``model`` values are documented ones."""
    for tree, keys in _FRONTMATTER_KEYS.items():
        for path in (_ASSET_ROOT / tree).rglob("*"):
            if path.suffix not in {".md", ".mdc"}:
                continue
            fm = _frontmatter(path)
            assert set(fm) <= keys, (path, set(fm) - keys)
            if "model" in fm:
                assert fm["model"] in _DOCUMENTED_MODELS, (path, fm["model"])
            if tree == "vscode" and "agent" in fm:
                assert fm["agent"] in {
                    "agent",
                    "whygraph-planner",
                    "whygraph-implementor",
                }, path


def test_harness_frontmatter_wiring() -> None:
    cc = _ASSET_ROOT / "claude-code"
    assert _frontmatter(cc / "agents/researcher.md")["disallowedTools"] == "Agent"
    assert _frontmatter(cc / "agents/researcher.md")["model"] == "sonnet"
    assert _frontmatter(cc / "agents/synthesizer.md")["tools"] == (
        "Read, Grep, Glob, Bash"
    )
    assert "Write" not in _frontmatter(cc / "agents/planner.md")["disallowedTools"]
    assert _frontmatter(cc / "skills/whygraph-implement/SKILL.md")["allowed-tools"] == (
        "Agent, Read"
    )
    assert _frontmatter(_ASSET_ROOT / "cursor/agents/planner.md")["readonly"] == "false"
    for name in ("rationale", "whygraph-plan", "whygraph-implement"):
        fm = _frontmatter(_ASSET_ROOT / f"cursor/skills/{name}/SKILL.md")
        assert fm["name"] == name and fm["disable-model-invocation"] == "true"
    assert not (_ASSET_ROOT / "cursor/commands").exists()
    vs = _ASSET_ROOT / "vscode"
    assert _frontmatter(vs / "prompts/whygraph-plan.prompt.md")["agent"] == (
        "whygraph-planner"
    )
    assert _frontmatter(vs / "prompts/whygraph-implement.prompt.md")["agent"] == (
        "whygraph-implementor"
    )
    assert "agent" in _frontmatter(vs / "agents/planner.agent.md")["tools"]
    for name in ("researcher", "synthesizer", "implementor"):
        fm = _frontmatter(vs / f"agents/{name}.agent.md")
        assert fm["user-invocable"] == "false" and fm["agents"] == "[]"
    for name in ("researcher", "synthesizer"):
        toml = (_ASSET_ROOT / f"codex/.codex/agents/{name}.toml").read_text()
        assert 'model_reasoning_effort = "medium"' in toml
    assert "/agent " not in (_ASSET_ROOT / "codex/AGENTS.md").read_text()
    assert "## WhyGraph" in (cc / "CLAUDE.md").read_text()


# ---- pipeline parity (plan 6.2, 6.5) --------------------------------------

_AGENT_PATHS = {
    "claude-code": "claude-code/agents/{n}.md",
    "cursor": "cursor/agents/{n}.md",
    "vscode": "vscode/agents/{n}.agent.md",
    "codex": "codex/.codex/agents/{n}.toml",
}
_AGENT_NAMES = ("planner", "synthesizer", "implementor", "researcher")


def _agent_body(tree: str, name: str) -> list[str]:
    """Return an agent's instruction body (no frontmatter) as stripped lines."""
    text = (_ASSET_ROOT / _AGENT_PATHS[tree].format(n=name)).read_text()
    if text.startswith("---\n"):
        text = text.split("\n---\n", 1)[1]
    else:  # codex TOML: the body is the developer_instructions string
        text = text.split('developer_instructions = """\n', 1)[1].rsplit('"""', 1)[0]
    return text.strip().splitlines()


def test_plan_schema_identical() -> None:
    """The fenced plan schema is byte-identical in all 12 agent files."""
    import re

    blocks: dict[tuple[str, str], str] = {}
    for tree in _AGENT_PATHS:
        for name in ("planner", "synthesizer", "implementor"):
            body = "\n".join(_agent_body(tree, name))
            found = re.findall(r"```markdown\n(# Plan:.*?)\n```", body, re.S)
            assert len(found) == 1, (tree, name)
            blocks[(tree, name)] = found[0]
    assert len(set(blocks.values())) == 1, {
        key: block[:60] for key, block in blocks.items()
    }
    assert "## Confidence:" in next(iter(blocks.values()))


# The allowlisted substitution table: platform vocabulary that may differ
# between the four trees. Everything else in an agent body must be identical.
_PARITY_SUBS = [
    (r" \(in Claude Code the schemas may be deferred.*?first\)", ""),
    (r"`/whygraph-plan`|`the planning workflow`", "<RERUN>"),
    (
        r"(?i)\b(?:Agent tool|subagent (?:delegation|handoff)|"
        r"nested agent invocations?|subagent invocations?)\b",
        "<AGENT-TOOL>",
    ),
    (
        r"(?i)\b(?:spawn(?:ed|ing)?|delegat(?:e|es|ed|ing|ion)(?: to)?|"
        r"hand(?:ed|ing)? off(?: to)?|handoff)\b",
        "<DISPATCH>",
    ),
    (r"slash (?:command|prompt)|\bcaller\b", "<INVOKER>"),
]
# A line that differs after substitution must be platform tool vocabulary.
_PARITY_PLATFORM = (
    r"<AGENT-TOOL>|<DISPATCH>|<INVOKER>|\b(?:Read|Write|Grep|Glob|Bash|Edit|read|"
    r"grep|glob|shell|file reads|write-new-file|read-then-edit)\b|"
    r"^Your prompt has this shape:$|then to the synthesizer"
)
# Lines only one tree has: its own fallback note for missing nested delegation.
_PARITY_TREE_ONLY = r"(?i)<DISPATCH> is unavailable|nested (?:subagents|<DISPATCH>)"
_PARITY_TOOLS = {"Read", "Edit", "Write", "Grep", "Glob", "Bash"}


def _parity_norm(line: str) -> str:
    import re

    for pattern, repl in _PARITY_SUBS:
        line = re.sub(pattern, repl, line)
    return line


def _parity_tokens(line: str) -> list[str]:
    import re

    out = []
    for tok in re.findall(r"`([^`]+)`", line):
        words = set(re.findall(r"[A-Za-z]+", tok))
        if (words and words <= _PARITY_TOOLS) or tok.startswith("/agent "):
            continue
        out.append(tok)
    return sorted(out)


@pytest.mark.parametrize("name", _AGENT_NAMES)
def test_body_parity(name: str) -> None:
    """The four trees' agent bodies differ only by allowlisted platform wording.

    A per-line edit that lands in one tree and misses another shows up here: a
    differing line must be platform vocabulary with the same backticked
    identifiers on both sides, or a tree-only delegation fallback note.
    """
    import difflib
    import re

    base = [_parity_norm(line) for line in _agent_body("claude-code", name)]
    for tree in ("cursor", "vscode", "codex"):
        other = [_parity_norm(line) for line in _agent_body(tree, name)]
        matcher = difflib.SequenceMatcher(None, base, other, autojunk=False)
        for op, i1, i2, j1, j2 in matcher.get_opcodes():
            if op == "equal":
                continue
            left, right = base[i1:i2], other[j1:j2]
            where = (name, tree, left, right)
            if not any(line.strip() for line in left + right):
                continue
            if not (left and right):  # one-sided: a tree-only note
                for line in left or right:
                    assert not line.strip() or re.search(_PARITY_TREE_ONLY, line), where
                continue
            assert any(re.search(_PARITY_PLATFORM, line) for line in left + right), (
                where
            )
            tokens = lambda lines: sorted(  # noqa: E731
                tok for line in lines for tok in _parity_tokens(line)
            )
            assert tokens(left) == tokens(right), where


def test_pipeline_streamlining_texts_in_every_tree() -> None:
    """The 6.2 sentences reached all four trees (inline cards, OUT_PATH, budget)."""
    for tree in _AGENT_PATHS:
        planner = "\n".join(_agent_body(tree, "planner"))
        assert "OUT_PATH" in planner and "Saved plan to" in planner, tree
        assert "is_empty: <bool>" not in planner, tree
        assert "constraints: [<verbatim>, ...]" in planner, tree
        assert "(unavailable:" in planner, tree
        researcher = "\n".join(_agent_body(tree, "researcher"))
        assert "budget_exceeded" in researcher, tree
        assert "cached_only=True" in researcher, tree
        assert "(unavailable:" in "\n".join(_agent_body(tree, "synthesizer")), tree
        implementor = "\n".join(_agent_body(tree, "implementor"))
        assert "FIX_ATTEMPTS" in implementor and "git add -A" not in implementor, tree
    for rel in (
        "claude-code/skills/pre-edit/SKILL.md",
        "cursor/rules/whygraph-pre-edit.mdc",
        "vscode/instructions/pre-edit.instructions.md",
        "codex/AGENTS.md",
    ):
        assert "budget_exceeded" in (_ASSET_ROOT / rel).read_text(), rel
    for rel in (
        "claude-code/skills/whygraph-plan/SKILL.md",
        "cursor/skills/whygraph-plan/SKILL.md",
        "vscode/prompts/whygraph-plan.prompt.md",
        "codex/AGENTS.md",
    ):
        assert "OUT_PATH" in (_ASSET_ROOT / rel).read_text(), rel


# ---- the verification lens (plan 6.3) -------------------------------------

# A literal command a Verify line may carry: the repo's own runner, in backticks.
_LITERAL_COMMAND = re.compile(r"`((?:uv run |npm |npx |make |pytest)[^`]*)`")


def _line_starting(lines: list[str], prefix: str) -> str:
    """Return the one line of ``lines`` that starts with ``prefix``."""
    found = [line for line in lines if line.startswith(prefix)]
    assert len(found) == 1, (prefix, found)
    return found[0]


def test_verification_lens_replaces_impact() -> None:
    """`verification` replaces `impact` in every tree and every enumeration."""
    dims = "<verification | constraints_risks | prior_art>"
    for tree in _AGENT_PATHS:
        researcher = _agent_body(tree, "researcher")
        planner = _agent_body(tree, "planner")
        synthesizer = _agent_body(tree, "synthesizer")
        assert "### `verification`" in researcher and "### `impact`" not in researcher
        assert f"DIMENSION: {dims}" in researcher and f"DIMENSION: {dims}" in planner
        for body in (planner, synthesizer):
            assert "=== verification ===" in body and "=== impact ===" not in body
        assert any(line.startswith("**1d. Dependency order.**") for line in planner)
        assert not any("file's role" in line for line in synthesizer), tree
    for path, text in _asset_texts().items():
        assert not re.search(
            r"\(impact(?:,| /)|`impact` (?:researcher|report)", text
        ), path


def test_verify_lines_are_literal_commands() -> None:
    """The lens contract and the synthesizer's step 3 demand literal commands.

    A fixture report filled in from the lens's own output contract yields a
    Verify line per symbol that is a backticked runner command, never prose.
    """
    for tree in _AGENT_PATHS:
        researcher = _agent_body(tree, "researcher")
        lens = _line_starting(researcher, "3. For each working-set symbol")
        assert "literal commands in backticks" in lens and "Never write prose" in lens
        assert _LITERAL_COMMAND.search(lens), tree
        contract = researcher[researcher.index("## Verify candidates") :]
        assert all("`<literal command>`" in line for line in contract[3:5])

        synthesizer = _agent_body(tree, "synthesizer")
        verify = _line_starting(synthesizer, "- **Verify**")
        assert "`verification` report" in verify and "verbatim in backticks" in verify
        assert "Never write a prose Verify" in verify
        assert _LITERAL_COMMAND.search(verify), tree

    # Fixture: fill the Claude Code contract's own template lines; the Verify
    # lines a synthesizer copies from that report are literal commands.
    researcher = _agent_body("claude-code", "researcher")
    contract = researcher[researcher.index("## Verify candidates") :]
    covered, uncovered = (
        line for line in contract[2:5] if line.startswith("- `qualified.name`")
    )
    report = [
        covered.replace(
            "`<literal command>`", "`uv run pytest tests/test_cache.py::test_key -q`"
        ),
        uncovered.replace(
            "`<test file>::<test name>`", "`tests/test_c.py::test_ev`"
        ).replace("`<literal command>`", "`uv run pytest tests/test_c.py::test_ev -q`"),
    ]
    commands = [_LITERAL_COMMAND.findall(line)[-1] for line in report]
    assert commands == [
        "uv run pytest tests/test_cache.py::test_key -q",
        "uv run pytest tests/test_c.py::test_ev -q",
    ]
    for prose in ("- **Verify:** run the tests", "- **Verify:** check it still works"):
        assert not _LITERAL_COMMAND.search(prose)


# ---- /whygraph-review (plan 6.6) -------------------------------------------

_REVIEWER = "whygraph-reviewer"
_REVIEW_SKILL = _ASSET_ROOT / "claude-code" / "skills" / "whygraph-review" / "SKILL.md"
_BASE_PATTERN = "^[A-Za-z0-9][A-Za-z0-9._/^~-]*$"
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

_FIXTURE_DIFF = """\
diff --git a/src/cache.py b/src/cache.py
index 1111111..2222222 100644
--- a/src/cache.py
+++ b/src/cache.py
@@ -13 +13,3 @@ class Cache:
-        return hashlib.sha256((name + path).encode()).hexdigest()
+        key = name + path
+        return hashlib.md5(key.encode()).hexdigest()
+
@@ -40,3 +41,0 @@ def purge(rows):
-    for row in rows:
-        row.delete()
-    return len(rows)
diff --git a/docs/notes.md b/docs/notes.md
new file mode 100644
--- /dev/null
+++ b/docs/notes.md
@@ -0,0 +1 @@
+Cache keys are content-addressable.
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def gone():
-    pass
diff --git a/logo.png b/logo.png
Binary files a/logo.png and b/logo.png differ
"""

_FIXTURE_CARDS = {
    ("src/cache.py", 13, 15): {
        "constraints": [
            "The key is sha256 of qualified_name + file_path, so cards survive "
            "a backend swap."
        ],
        "risks": ["Changing the hash orphans every cached card."],
    },
    ("src/cache.py", 41, 41): {
        "constraints": [],
        "risks": ["Purging without a count breaks the scan summary."],
    },
}


def _diff_hunks(diff: str) -> list[tuple[str, int, int]]:
    """The reviewer's Step 1 rule: one ``(path, start, end)`` per new-side hunk.

    Deleted files (``+++ /dev/null``) and binary files (no ``+++`` line) are
    skipped, as the agent body says.
    """
    hunks: list[tuple[str, int, int]] = []
    path: str | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = None
        elif line.startswith("+++ "):
            new = line[4:]
            path = None if new == "/dev/null" else new.removeprefix("b/")
        elif path and (match := _HUNK_HEADER.match(line)):
            c = int(match.group(1))
            d = 1 if match.group(2) is None else int(match.group(2))
            start, end = (max(c, 1), max(c, 1)) if d == 0 else (c, c + d - 1)
            hunks.append((path, start, end))
    return hunks


def _review_contract() -> list[str]:
    """The fenced output contract in the reviewer's body, as lines."""
    body = "\n".join(_agent_body("claude-code", _REVIEWER))
    found = re.findall(r"```markdown\n(# Review:.*?)\n```", body, re.S)
    assert len(found) == 1
    return found[0].splitlines()


def _review_problems(
    review: str,
    hunks: list[tuple[str, int, int]],
    cards: dict[tuple[str, int, int], dict],
    verdicts: set[str],
) -> list[str]:
    """Check a review against the contract; return what is wrong (empty: valid)."""
    lines = review.strip().splitlines()
    head = re.fullmatch(r"# Review: (\d+) hunks", lines[0]) if lines else None
    if head is None:
        return ["the first line is not '# Review: <n> hunks'"]
    problems: list[str] = []
    body = lines[1:]
    if re.fullmatch(r"## Confidence: (?:high|medium|low) - \S.*", lines[-1]):
        body = lines[1:-1]
    else:
        problems.append("the last line is not the Confidence line")
    sections: list[tuple[str, list[str]]] = []
    for line in body:
        if line.startswith("## "):
            sections.append((line, []))
        elif line.strip():
            if not sections:
                problems.append(f"text before the first hunk: {line}")
            else:
                sections[-1][1].append(line)
    if int(head.group(1)) != len(sections):
        problems.append("<n> is not the number of hunk sections")
    seen: list[tuple[str, int, int]] = []
    for heading, verdict_lines in sections:
        match = re.fullmatch(r"## (\S+):(\d+)-(\d+) \((.+)\)", heading)
        if match is None:
            problems.append(f"bad heading: {heading}")
            continue
        key = (match.group(1), int(match.group(2)), int(match.group(3)))
        seen.append(key)
        if len(verdict_lines) != 1:
            problems.append(f"{heading}: {len(verdict_lines)} verdict lines")
            continue
        verdict = re.fullmatch(r"\*\*(.+?):\*\* (\S.*)", verdict_lines[0])
        if verdict is None or verdict.group(1) not in verdicts:
            problems.append(f"{heading}: not a verdict: {verdict_lines[0]}")
            continue
        field = {"Constraint at risk": "constraints", "Risk triggered": "risks"}
        if verdict.group(1) in field:
            quote = re.match(r'"(.+?)" - \S', verdict.group(2))
            listed = cards.get(key, {}).get(field[verdict.group(1)], [])
            if quote is None or quote.group(1) not in listed:
                problems.append(f"{heading}: not a verbatim card quote")
    if seen != hunks:
        problems.append(f"the hunks {seen} are not the diff's {hunks}")
    return problems


def _fill(template: str, quote: str | None = None) -> str:
    """Fill a contract template line: a quote first, then every other slot."""
    if quote is not None:
        template = re.sub(r'"<[^>]+>"', lambda _: f'"{quote}"', template)
    return re.sub(r"<[^>]+>", "it may", template)


def test_review_output_contract_on_a_fixture_diff() -> None:
    """A review built from the contract's own template lines passes the checker.

    The fixture diff's hunks follow the body's range rule; a paraphrased
    quote, a second verdict, a wrong count or range, or a missing Confidence
    line each fail it.
    """
    body = "\n".join(_agent_body("claude-code", _REVIEWER))
    assert "`<start>` = `c`, `<end>` = `c + d - 1`" in body
    assert "deletion-only hunk (`d` = 0)" in body
    assert "Skip binary files and deleted files" in body

    hunks = _diff_hunks(_FIXTURE_DIFF)
    assert hunks == [
        ("src/cache.py", 13, 15),
        ("src/cache.py", 41, 41),
        ("docs/notes.md", 1, 1),
    ]

    contract = _review_contract()
    verdicts = {
        found.group(1)
        for line in contract
        if (found := re.match(r"\*\*(.+?):\*\*", line))
    }
    assert verdicts == {
        "Constraint at risk",
        "Risk triggered",
        "No rationale",
        "Consistent",
    }
    heading = "## <path>:<start>-<end> (<symbol>)"
    assert {line for line in contract if line.startswith("## <")} == {heading}
    line_for = {v: _line_starting(contract, f"**{v}:**") for v in verdicts}
    confidence = _line_starting(contract, "## Confidence:")
    assert confidence == "## Confidence: <high | medium | low> - <reason>"

    def section(hunk: tuple[str, int, int], symbol: str, line: str) -> str:
        path, start, end = hunk
        top = heading.replace("<path>", path).replace("<start>", str(start))
        top = top.replace("<end>", str(end)).replace("<symbol>", symbol)
        return f"{top}\n{line}\n"

    constraint = _FIXTURE_CARDS[hunks[0]]["constraints"][0]
    risk = _FIXTURE_CARDS[hunks[1]]["risks"][0]
    parts = [
        section(hunks[0], "key", _fill(line_for["Constraint at risk"], constraint)),
        section(hunks[1], "purge", _fill(line_for["Risk triggered"], risk)),
        section(hunks[2], "not indexed", _fill(line_for["No rationale"])),
    ]
    assert contract[0] == "# Review: <n> hunks"
    confidence = confidence.replace("<high | medium | low>", "medium")
    review = "\n".join(["# Review: 3 hunks", "", *parts, _fill(confidence)])
    assert _review_problems(review, hunks, _FIXTURE_CARDS, verdicts) == []

    broken = {
        "paraphrase": review.replace(constraint, "The key must stay a sha256."),
        "two verdicts": review.replace(
            "## src/cache.py:41-41", "**Consistent:** fine\n\n## src/cache.py:41-41"
        ),
        "count": review.replace("# Review: 3 hunks", "# Review: 2 hunks"),
        "range": review.replace("src/cache.py:13-15", "src/cache.py:13-14"),
        "no confidence": review.rsplit("\n## Confidence", 1)[0],
        "unknown verdict": review.replace("**No rationale:**", "**Looks fine:**"),
    }
    for name, text in broken.items():
        assert _review_problems(text, hunks, _FIXTURE_CARDS, verdicts), name


def test_reviewer_wiring() -> None:
    """The skill and agent frontmatter match plan 6.1 / 6.6; Claude Code only."""
    cc = _ASSET_ROOT / "claude-code"
    skill = _frontmatter(_REVIEW_SKILL)
    assert skill["name"] == "whygraph-review"
    assert skill["disable-model-invocation"] == "true"
    assert skill["allowed-tools"] == "Agent, Read"
    assert "--generate" in skill["argument-hint"]
    agent = _frontmatter(cc / "agents" / f"{_REVIEWER}.md")
    assert agent["name"] == _REVIEWER and agent["model"] == "sonnet"
    assert agent["tools"] == (
        "Read, Grep, Glob, Bash, mcp__whygraph__*, mcp__codegraph__*"
    )

    body = "\n".join(_agent_body("claude-code", _REVIEWER))
    skill_text = _REVIEW_SKILL.read_text()
    assert "run only `git diff` commands" in body
    # No LLM spend without --generate: cached cards, an undescribed probe,
    # and no rationale call at all on a linked project (cached_only is not
    # honoured there).
    assert "cached_only=True" in body and "describe=False" in body
    assert "Never pass `force_refresh`" in body
    assert "carries a `platform` block" in body
    assert "do not call `whygraph_rationale_brief` at all" in body
    assert 'subagent_type: "whygraph-reviewer"' in skill_text
    assert "GENERATE: <true | false>" in skill_text
    assert "GENERATE: <true | false>" in body
    assert f"`{_BASE_PATTERN}`" in skill_text and f"`{_BASE_PATTERN}`" in body
    base = re.compile(_BASE_PATTERN)
    for ok in ("main", "origin/main", "HEAD~3", "v2.0.0", "a1b2c3d"):
        assert base.match(ok), ok
    for bad in ("--output=x", "-p", "main;rm", "$(id)", "a b"):
        assert not base.match(bad), bad

    # Claude Code first: the other trees get a reviewer after Step 9.
    for tree in ("cursor", "vscode", "codex"):
        assert not [p for p in (_ASSET_ROOT / tree).rglob("*review*")], tree


# ---- retired assets: files WhyGraph no longer ships -----------------------


def _retire_do_it(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Install the fake tree, then drop ``commands/do-it.md`` from the bundle."""
    src, project, _ = _installed(tmp_path)
    (src / "commands" / "do-it.md").unlink()
    (src / "commands").rmdir()
    return src, project, project / ".claude" / "commands" / "do-it.md"


def test_unedited_retired_file_is_removed_with_its_empty_dir(tmp_path: Path) -> None:
    src, project, do_it = _retire_do_it(tmp_path)

    result = assets.install_assets(_claude_target(), project, source=src)

    assert result.removed == [do_it] and result.needs_confirmation == []
    assert "-DO-IT" in result.diffs[do_it]
    assert not do_it.exists()
    assert not (project / ".claude" / "commands").exists()
    assert (project / ".claude").is_dir()  # the agent's root stays
    assert "claude/commands/do-it.md" not in _manifest(project)["files"]


def test_retired_file_keeps_a_non_empty_dir(tmp_path: Path) -> None:
    src, project, do_it = _retire_do_it(tmp_path)
    mine = project / ".claude" / "commands" / "mine.md"
    mine.write_text("MINE")

    assets.install_assets(_claude_target(), project, source=src)

    assert not do_it.exists() and mine.read_text() == "MINE"


def test_edited_retired_file_needs_confirmation_then_force_removes(
    tmp_path: Path,
) -> None:
    src, project, do_it = _retire_do_it(tmp_path)
    do_it.write_text("MY EDIT")
    before = _manifest(project)["files"]["claude/commands/do-it.md"]

    result = assets.install_assets(_claude_target(), project, source=src)

    assert result.needs_confirmation == [do_it] and result.retired == [do_it]
    assert result.removed == []
    diff = result.diffs[do_it]
    assert "-MY EDIT" in diff and "+++ b/.claude/commands/do-it.md" in diff
    assert not [
        ln
        for ln in diff.splitlines()
        if ln.startswith("+") and ln != "+++ b/.claude/commands/do-it.md"
    ]
    assert do_it.read_text() == "MY EDIT"
    # Kept, so the next Update asks again.
    assert _manifest(project)["files"]["claude/commands/do-it.md"] == before
    again = assets.install_assets(_claude_target(), project, source=src)
    assert again.needs_confirmation == [do_it]

    forced = assets.install_assets(_claude_target(), project, source=src, force=True)
    assert forced.removed == [do_it] and not do_it.exists()
    assert "claude/commands/do-it.md" not in _manifest(project)["files"]


def test_dry_run_reports_a_retired_file_without_removing_it(tmp_path: Path) -> None:
    src, project, do_it = _retire_do_it(tmp_path)
    before = _manifest(project)

    result = assets.install_assets(_claude_target(), project, source=src, dry_run=True)

    assert result.removed == [do_it]
    assert do_it.read_text() == "DO-IT"
    assert _manifest(project) == before


def test_symlink_at_a_retired_path_is_left_alone(tmp_path: Path) -> None:
    src, project, do_it = _retire_do_it(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("DO-IT")
    do_it.unlink()
    do_it.symlink_to(outside)

    result = assets.install_assets(_claude_target(), project, source=src)

    assert result.removed == [] and result.needs_confirmation == []
    assert do_it.is_symlink() and outside.read_text() == "DO-IT"


def test_symlinked_retired_dir_is_left_alone(tmp_path: Path) -> None:
    src, project, do_it = _retire_do_it(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    (project / ".claude" / "commands").rename(elsewhere)
    (project / ".claude" / "commands").symlink_to(elsewhere)

    result = assets.install_assets(_claude_target(), project, source=src)

    assert result.removed == []
    assert (elsewhere / "do-it.md").read_text() == "DO-IT"


def test_missing_retired_file_just_drops_the_entry(tmp_path: Path) -> None:
    src, project, do_it = _retire_do_it(tmp_path)
    do_it.unlink()

    result = assets.install_assets(_claude_target(), project, source=src)

    assert result.removed == [] and result.needs_confirmation == []
    assert "claude/commands/do-it.md" not in _manifest(project)["files"]


def test_manifest_key_with_dot_dot_is_ignored(tmp_path: Path) -> None:
    src, project, _ = _installed(tmp_path)
    victim = tmp_path / "victim.md"
    victim.write_text("X")
    manifest_path = project / ".whygraph" / "assets.json"
    data = json.loads(manifest_path.read_text())
    data["files"]["claude/../../victim.md"] = hashlib.sha256(b"X").hexdigest()
    manifest_path.write_text(json.dumps(data))

    result = assets.install_assets(_claude_target(), project, source=src)

    assert result.removed == [] and victim.read_text() == "X"


def test_pre_manifest_retired_file_uses_the_retired_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _make_source(tmp_path)
    project = tmp_path / "proj"
    old = project / ".claude" / "commands" / "old.md"
    edited = project / ".claude" / "commands" / "older.md"
    old.parent.mkdir(parents=True)
    old.write_text("OLD v1")
    edited.write_text("OLD v1 + mine")
    v1 = frozenset({hashlib.sha256(b"OLD v1").hexdigest()})
    monkeypatch.setattr(
        assets,
        "RETIRED_ASSETS",
        {
            "claude-code": {
                "commands/old.md": v1,
                "commands/older.md": v1,
                # Still bundled: never a candidate.
                "agents/x.md": frozenset({hashlib.sha256(b"X-AGENT").hexdigest()}),
            }
        },
    )

    result = assets.install_assets(_claude_target(), project, source=src)

    assert result.removed == [old] and not old.exists()
    assert result.needs_confirmation == [edited] and edited.exists()
    assert result.retired == [edited]
    assert (project / ".claude" / "agents" / "x.md").read_text() == "X-AGENT"


def test_cursor_retires_its_old_commands() -> None:
    """The three 1.x / 2.0 Cursor commands are retired candidates."""
    cursor = agents.resolve_agent("cursor")
    retired = assets.retired_assets(cursor, {})

    assert set(retired) == {
        "commands/rationale.md",
        "commands/whygraph-implement.md",
        "commands/whygraph-plan.md",
    }
    assert all(len(h) == 1 for h in retired.values())
    root = assets.packaged_assets_for(cursor)
    assert not (root / "commands").is_dir()
    assert (root / "skills" / "rationale" / "SKILL.md").is_file()


def test_cursor_retired_hash_matches_the_v2_command(tmp_path: Path) -> None:
    """A byte-identical v2.0.0 command is removed (when the tag is available)."""
    rel = "src/whygraph/assets/cursor/commands/rationale.md"
    shown = subprocess.run(
        ["git", "show", f"v2.0.0:{rel}"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
    )
    if shown.returncode != 0:
        pytest.skip("the v2.0.0 tag is not available in this checkout")
    project = tmp_path / "proj"
    old = project / ".cursor" / "commands" / "rationale.md"
    old.parent.mkdir(parents=True)
    old.write_bytes(shown.stdout)

    result = assets.install_assets(agents.resolve_agent("cursor"), project)

    assert old in result.removed and not old.exists()
    assert not (project / ".cursor" / "commands").exists()
