"""Install bundled agent assets into a project.

The package ships a tree of Claude Code agent / command / skill files
under :mod:`whygraph.assets`. They live there — rather than in a
sibling ``plugins/`` directory — so they ride along in the wheel under
the existing ``[tool.hatch.build.targets.wheel].packages = ["src/whygraph"]``
config, exactly like ``src/whygraph/analyze/prompts/``.

This module exposes a tiny installer that copies that tree into a
target project's ``.claude/`` directory. It is invoked from
``whygraph init --agent claude``.

Notes
-----
The default policy is **skip-if-exists**: a target file that already
exists is left alone. Pass ``force=True`` to overwrite. This matches
the "user edits are sacred" default of most scaffolding tools and
mirrors the spirit of :func:`whygraph.agents.write_snippet`, which
merges into an existing config rather than clobbering it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

CLAUDE_CODE_SUBDIRS: tuple[str, ...] = ("agents", "commands", "skills")
"""Subdirectories under ``.claude/`` that whygraph populates."""

_CLAUDE_DIR = ".claude"


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Outcome of one :func:`install_claude_code_assets` call.

    Attributes
    ----------
    written : list[Path]
        Target paths that did not exist and were created.
    skipped : list[Path]
        Target paths that already existed and were left alone
        (``force=False``).
    overwritten : list[Path]
        Target paths that existed and were replaced (``force=True``).
    """

    written: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    overwritten: list[Path] = field(default_factory=list)


def packaged_claude_code_assets() -> Traversable:
    """Return the packaged ``assets/claude-code/`` directory as a resource.

    Returns
    -------
    Traversable
        Handle to the bundled asset tree. Iterate with
        :meth:`Traversable.iterdir` and read files with
        :meth:`Traversable.read_text`.

    Notes
    -----
    Loaded the same way as :func:`whygraph.analyze.prompt._packaged_prompts_dir`
    — via ``importlib.resources.files("whygraph") / "assets" / "claude-code"``.
    Because the data dir does not need to be an importable Python
    package, the name uses a dash (matching the user-facing agent id
    ``claude-code``); ``importlib`` accepts arbitrary path components
    after the package anchor.
    """
    return resources.files("whygraph") / "assets" / "claude-code"


def install_claude_code_assets(
    project_root: Path,
    *,
    force: bool = False,
    source: Traversable | Path | None = None,
) -> InstallResult:
    """Copy the bundled Claude Code asset tree into ``<project_root>/.claude/``.

    Mirrors the source layout under the destination — files at
    ``source/agents/x.md`` land at ``project_root/.claude/agents/x.md``
    and so on. Parent directories are created as needed.

    Parameters
    ----------
    project_root : Path
        Directory in which to create / populate ``.claude/``. Usually
        the user's repository root (i.e. ``Path.cwd()`` from the CLI).
    force : bool, default False
        If ``True``, overwrite target files that already exist. The
        default leaves existing files alone so user edits survive a
        re-install.
    source : Traversable or Path, optional
        Asset tree to copy from. ``None`` (default) uses the packaged
        tree returned by :func:`packaged_claude_code_assets`. Tests
        inject a ``tmp_path`` here.

    Returns
    -------
    InstallResult
        Per-file outcome (written / skipped / overwritten).
    """
    src: Traversable | Path = (
        source if source is not None else packaged_claude_code_assets()
    )
    dest_root = project_root / _CLAUDE_DIR
    result = InstallResult()
    _copy_tree(src, dest_root, force=force, result=result)
    return result


def _copy_tree(
    src: Traversable | Path,
    dest: Path,
    *,
    force: bool,
    result: InstallResult,
) -> None:
    """Recursively copy ``src`` into ``dest``, recording each file's fate.

    Walks ``src`` via :meth:`Traversable.iterdir` (works for both
    ``Path`` and packaged ``Traversable`` sources) and mirrors its
    structure under ``dest``. The fate of each file (written / skipped
    / overwritten) is appended to ``result``.
    """
    for entry in src.iterdir():
        target = dest / entry.name
        if entry.is_dir():
            _copy_tree(entry, target, force=force, result=result)
            continue
        if not entry.is_file():
            continue
        already_exists = target.exists()
        if already_exists and not force:
            result.skipped.append(target)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry.read_text(encoding="utf-8"), encoding="utf-8")
        if already_exists:
            result.overwritten.append(target)
        else:
            result.written.append(target)


__all__ = [
    "CLAUDE_CODE_SUBDIRS",
    "InstallResult",
    "install_claude_code_assets",
    "packaged_claude_code_assets",
]
