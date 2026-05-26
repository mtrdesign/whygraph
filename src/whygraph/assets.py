"""Install bundled agent assets into a project.

The package ships a per-agent tree of bundled asset files (markdown for
sub-agents, slash commands, skills, rules, prompt files, etc.) under
:mod:`whygraph.assets`. They live there — rather than in a sibling
``plugins/`` directory — so they ride along in the wheel under the
existing ``[tool.hatch.build.targets.wheel].packages = ["src/whygraph"]``
config, exactly like ``src/whygraph/analyze/prompts/``.

Each :class:`whygraph.agents.AgentTarget` declares its own
``assets_subdir`` (source directory under ``src/whygraph/assets/``) and
``assets_dest`` (destination directory relative to the project root).
Agents without bundled assets leave both fields ``None`` — see
:attr:`whygraph.agents.AgentTarget.has_assets`.

This module exposes a small installer that copies the per-agent tree
into the matching destination directory. It is invoked from
``whygraph init --agent <name>``.

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

from .agents import AgentTarget


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Outcome of one :func:`install_assets` call.

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


def packaged_assets_for(target: AgentTarget) -> Traversable:
    """Return the packaged asset directory for ``target`` as a resource.

    Parameters
    ----------
    target : AgentTarget
        Agent whose bundled tree to locate. Must have
        :attr:`AgentTarget.assets_subdir` set.

    Returns
    -------
    Traversable
        Handle to the bundled asset tree. Iterate with
        :meth:`Traversable.iterdir` and read files with
        :meth:`Traversable.read_text`.

    Raises
    ------
    ValueError
        If ``target.assets_subdir`` is ``None``. Callers should branch
        on :attr:`AgentTarget.has_assets` before invoking this.

    Notes
    -----
    Loaded the same way as :func:`whygraph.analyze.prompt._packaged_prompts_dir`
    — via ``importlib.resources.files("whygraph") / "assets" / <subdir>``.
    The asset subdirectory does not need to be an importable Python
    package, so hyphenated names like ``claude-code`` are fine;
    ``importlib`` accepts arbitrary path components after the package
    anchor.
    """
    if target.assets_subdir is None:
        raise ValueError(
            f"agent {target.name!r} has no bundled assets "
            "(assets_subdir is None)"
        )
    return resources.files("whygraph") / "assets" / target.assets_subdir


def install_assets(
    target: AgentTarget,
    project_root: Path,
    *,
    force: bool = False,
    source: Traversable | Path | None = None,
) -> InstallResult:
    """Copy ``target``'s bundled asset tree into the project.

    Mirrors the source layout under the destination —
    ``<source>/agents/x.md`` lands at
    ``<project_root>/<target.assets_dest>/agents/x.md`` and so on.
    Parent directories are created as needed.

    Parameters
    ----------
    target : AgentTarget
        Agent whose bundled tree to install. Must have
        :attr:`AgentTarget.has_assets` ``True``.
    project_root : Path
        Directory in which to create / populate the destination tree.
        Usually the user's repository root (i.e. ``Path.cwd()`` from
        the CLI).
    force : bool, default False
        If ``True``, overwrite target files that already exist. The
        default leaves existing files alone so user edits survive a
        re-install.
    source : Traversable or Path, optional
        Asset tree to copy from. ``None`` (default) uses the packaged
        tree returned by :func:`packaged_assets_for`. Tests inject a
        ``tmp_path`` here.

    Returns
    -------
    InstallResult
        Per-file outcome (written / skipped / overwritten).

    Raises
    ------
    ValueError
        If ``target`` has no bundled assets configured.
    """
    if not target.has_assets:
        raise ValueError(
            f"agent {target.name!r} has no bundled assets to install"
        )
    src: Traversable | Path = (
        source if source is not None else packaged_assets_for(target)
    )
    assert target.assets_dest is not None  # for type checkers; has_assets guarantees this
    dest_root = project_root.joinpath(*target.assets_dest)
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
    "InstallResult",
    "install_assets",
    "packaged_assets_for",
]
