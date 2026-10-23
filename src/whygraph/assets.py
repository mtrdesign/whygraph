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
into the matching destination directory. It is invoked by the portal's
Initialize step (:func:`whygraph.project_setup.initialize_project`).

Notes
-----
User edits are sacred, and upgrades are manifest-driven. Every install
writes ``.whygraph/assets.json`` (:data:`MANIFEST_RELPATH`): the package
version and the SHA-256 of each installed file (for an append-merged
block, of the body between the sentinels). A re-install without
``force`` then decides per file:

* missing: written;
* present and equal to the manifest hash (never edited): overwritten
  silently, or left as is when it already equals the bundled copy;
* present, edited, bundled copy unchanged since the manifest: skipped;
* present, edited, bundled copy changed (or no manifest entry at all, a
  pre-manifest install): ``needs_confirmation`` with a diff, left
  untouched until the caller passes ``force=True``.

Files listed in :attr:`whygraph.agents.AgentTarget.assets_merge_files`
have their bundled content wrapped in ``<!-- BEGIN whygraph -->`` /
``<!-- END whygraph -->`` markers and **append-merged** into the
destination file. This is used for repo-shared instruction files that
users may have authored themselves (e.g. ``.github/copilot-instructions.md``,
``AGENTS.md``). Content outside the markers is never touched; the block
itself follows the same table as a plain file.

Files WhyGraph shipped once but no longer ships (a renamed or dropped
asset) are **retired**: the candidates are the manifest entries for the
agent that are not bundled any more, plus :data:`RETIRED_ASSETS` (every
released version of such a file, for installs that predate the
manifest). An unedited one is deleted (``removed``), an edited one is
``needs_confirmation`` with a removal diff and is deleted only with
``force``, and a symlink or anything that is not a regular file is left
alone.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
from dataclasses import dataclass, field
from importlib import resources
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from importlib.resources.abc import Traversable
from pathlib import Path

from .agents import AgentTarget, is_git_tracked

BEGIN_MARKER = "<!-- BEGIN whygraph -->"
"""Opening sentinel of the WhyGraph-owned block in an append-merged file."""

END_MARKER = "<!-- END whygraph -->"
"""Closing sentinel of the WhyGraph-owned block in an append-merged file."""


MANIFEST_RELPATH = Path(".whygraph") / "assets.json"
"""The upgrade manifest (relative to the project root; gitignored with ``.whygraph/``)."""


RETIRED_ASSETS: dict[str, dict[str, frozenset[str]]] = {
    "cursor": {
        # Cursor's commands became skills (``skills/<name>/SKILL.md``);
        # v1.0.0 to v2.0.0 shipped each of these byte-identical.
        "commands/rationale.md": frozenset(
            {"ecb7e6b745fe46d65a8085e6014cf99ced2ac5ec2d7c8c233f522781f80adf3b"}
        ),
        "commands/whygraph-implement.md": frozenset(
            {"b156c3c20379ae0a8eea39f7aef3496e0d1a62c8065e9e558f29b95ffe63e348"}
        ),
        "commands/whygraph-plan.md": frozenset(
            {"1db74273fd9fb1a2c0dde52a5501a4ab5cb4903960323ffb909094841e3b8613"}
        ),
    },
}
"""Asset files WhyGraph used to ship, for installs without a manifest entry.

Keyed by :attr:`whygraph.agents.AgentTarget.assets_subdir`, then by the
bundle-relative path (``/`` separators), mapping to the SHA-256 of every
released version of that file (as :func:`install_assets` hashes it). A
file on disk that matches one of them was never edited and is removed by
a re-install. **When a bundled asset is renamed or removed, add the
hashes of all its released versions here**, or a project initialized
before the manifest keeps the stale copy forever.
"""


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Outcome of one :func:`install_assets` call.

    Attributes
    ----------
    written : list[Path]
        Target paths that did not exist and were created. For merge
        files (see :attr:`whygraph.agents.AgentTarget.assets_merge_files`),
        a path appears here when the destination file did not exist
        before the merge.
    skipped : list[Path]
        Target paths that already existed and were left alone
        (``force=False``). Merge files are never skipped — they always
        run the (non-destructive) merge.
    overwritten : list[Path]
        Target paths that existed and were replaced (``force=True``).
        For merge files, a path appears here when an existing
        destination file had its WhyGraph block replaced or had a new
        block appended.
    needs_confirmation : list[Path]
        Target paths that exist, were edited by the user and differ from
        the bundled copy that changed (or have no manifest entry): left
        untouched; ``force=True`` replaces them. Also an edited file
        WhyGraph no longer ships (listed in ``retired`` too), which
        ``force=True`` deletes.
    diffs : dict[Path, str]
        A unified diff (installed -> bundled) for every path in
        ``needs_confirmation``, for every overwritten path whose content
        changed, and (installed -> empty) for every removed path.
    removed : list[Path]
        Files WhyGraph no longer ships that were deleted: unedited, or
        edited and ``force=True``. Their now-empty parent directories
        (below the agent's destination root) are removed too.
    retired : list[Path]
        The paths in ``needs_confirmation`` that WhyGraph no longer
        ships (an edited retired file, kept until ``force=True``).
    """

    written: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    overwritten: list[Path] = field(default_factory=list)
    needs_confirmation: list[Path] = field(default_factory=list)
    diffs: dict[Path, str] = field(default_factory=dict)
    removed: list[Path] = field(default_factory=list)
    retired: list[Path] = field(default_factory=list)


def package_version() -> str | None:
    """Return the installed ``whygraph`` version, or ``None`` when unknown."""
    try:
        return _pkg_version("whygraph")
    except PackageNotFoundError:
        return None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_manifest(project_root: Path) -> dict | None:
    """Read ``.whygraph/assets.json``.

    Parameters
    ----------
    project_root : Path
        Repository root. The caller has checked the manifest path.

    Returns
    -------
    dict or None
        ``{"version": str | None, "files": {"<agent>/<relpath>": sha256}}``,
        or ``None`` when the file is missing, not a valid manifest, or
        tracked by git (a repo is untrusted: a committed manifest could
        name the repo's own files as retired assets). The files are then
        treated as user-edited.
    """
    if is_git_tracked(project_root, MANIFEST_RELPATH.as_posix()):
        return None
    try:
        data = json.loads((project_root / MANIFEST_RELPATH).read_text(encoding="utf-8"))
        files = data["files"]
        version = data.get("version")
        if not isinstance(files, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in files.items()
        ):
            return None
        if version is not None and not isinstance(version, str):
            return None
        return {"version": version, "files": dict(files)}
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ):
        return None


def _write_manifest(project_root: Path, version: str | None, files: dict) -> None:
    path = project_root / MANIFEST_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"version": version, "files": dict(sorted(files.items()))}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def set_manifest_version(project_root: Path, version: str | None) -> None:
    """Rewrite the manifest's recorded version (no-op without a manifest)."""
    manifest = read_manifest(project_root)
    if manifest is not None:
        _write_manifest(project_root, version, manifest["files"])


def manifest_status(project_root: Path) -> dict:
    """Report the installed vs packaged agent-file version.

    Parameters
    ----------
    project_root : Path
        Repository root. The caller has checked the manifest path.

    Returns
    -------
    dict
        ``installed_version`` (``None`` for a pre-manifest install),
        ``current_version`` (``None`` when the package version is
        unknown) and ``outdated`` (they differ).
    """
    manifest = read_manifest(project_root)
    installed = manifest["version"] if manifest else None
    current = package_version()
    return {
        "installed_version": installed,
        "current_version": current,
        "outdated": current is not None and installed != current,
    }


def _diff(rel: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            f"{old}\n".splitlines(keepends=True) if old else [],
            f"{new}\n".splitlines(keepends=True) if new else [],
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
    )


@dataclass(slots=True)
class _Plan:
    """Per-install state threaded through :func:`_copy_tree`."""

    agent: str
    root: Path
    force: bool
    dry_run: bool
    old: dict[str, str]
    new: dict[str, str] = field(default_factory=dict)
    result: InstallResult = field(default_factory=InstallResult)

    def decide(self, key: str, current_hash: str | None, bundled_hash: str) -> str:
        """Classify an existing file: ``skip`` / ``overwrite`` / ``confirm``.

        Records the manifest entry the file should carry afterwards.
        """
        old = self.old.get(key)
        if current_hash == bundled_hash:
            self.new[key] = bundled_hash
            return "skip"
        if self.force or (current_hash is not None and old == current_hash):
            self.new[key] = bundled_hash
            return "overwrite"
        if old is not None:
            self.new[key] = old
        return "skip" if old == bundled_hash else "confirm"


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
            f"agent {target.name!r} has no bundled assets (assets_subdir is None)"
        )
    return resources.files("whygraph") / "assets" / target.assets_subdir


def install_assets(
    target: AgentTarget,
    project_root: Path,
    *,
    force: bool = False,
    source: Traversable | Path | None = None,
    dry_run: bool = False,
) -> InstallResult:
    """Copy ``target``'s bundled asset tree into the project.

    Mirrors the source layout under the destination —
    ``<source>/agents/x.md`` lands at
    ``<project_root>/<target.assets_dest>/agents/x.md`` and so on.
    Parent directories are created as needed.

    Files whose source-relative path matches an entry in
    ``target.assets_merge_files`` follow the **append-merge** path
    instead — their body is wrapped in ``<!-- BEGIN whygraph -->`` /
    ``<!-- END whygraph -->`` markers and either written fresh, replaced
    in place, or appended to existing user content. See :func:`_merge_block`.

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
        If ``True``, overwrite target files that already exist, edited
        or not (the "Overwrite my edits too" path). The default follows
        the manifest table in the module notes, so user edits survive a
        re-install unless the caller confirms them.
    source : Traversable or Path, optional
        Asset tree to copy from. ``None`` (default) uses the packaged
        tree returned by :func:`packaged_assets_for`. Tests inject a
        ``tmp_path`` here.
    dry_run : bool, default False
        If ``True``, classify every file exactly as a real call would
        (written / skipped / overwritten / needs_confirmation / removed)
        but touch nothing on disk, the manifest included.
        Used by the "Update agent files" preview.

    Returns
    -------
    InstallResult
        Per-file outcome (written / skipped / overwritten /
        needs_confirmation / removed, with diffs).

    Raises
    ------
    ValueError
        If ``target`` has no bundled assets configured.

    Notes
    -----
    After the bundled tree, the files WhyGraph no longer ships (see
    :func:`retired_assets`) are deleted when unedited, flagged
    ``needs_confirmation`` when edited (deleted with ``force``), and left
    alone when a symlink or not a regular file.
    """
    if not target.has_assets:
        raise ValueError(f"agent {target.name!r} has no bundled assets to install")
    src: Traversable | Path = (
        source if source is not None else packaged_assets_for(target)
    )
    assert (
        target.assets_dest is not None
    )  # for type checkers; has_assets guarantees this
    dest_root = project_root.joinpath(*target.assets_dest)
    merge_set = frozenset(target.assets_merge_files)
    manifest = read_manifest(project_root)
    plan = _Plan(
        agent=target.name,
        root=project_root,
        force=force,
        dry_run=dry_run,
        old=manifest["files"] if manifest else {},
    )
    _copy_tree(src, dest_root, rel_prefix=(), merge_set=merge_set, plan=plan)
    for rel, known in retired_assets(target, plan.old, source=src).items():
        _retire(rel, known, dest_root, plan)
    result = plan.result
    if not dry_run:
        mine = f"{target.name}/"
        files = {k: v for k, v in plan.old.items() if not k.startswith(mine)}
        files.update(plan.new)
        version = package_version()
        if result.needs_confirmation:
            # Something is still stale: do not claim the current version.
            version = manifest["version"] if manifest else None
        if (
            manifest is None
            or files != manifest["files"]
            or version != manifest["version"]
        ):
            _write_manifest(project_root, version, files)
    return result


def _copy_tree(
    src: Traversable | Path,
    dest: Path,
    *,
    rel_prefix: tuple[str, ...],
    merge_set: frozenset[str],
    plan: _Plan,
) -> None:
    """Recursively copy ``src`` into ``dest``, recording each file's fate.

    Walks ``src`` via :meth:`Traversable.iterdir` (works for both
    ``Path`` and packaged ``Traversable`` sources) and mirrors its
    structure under ``dest``. The fate of each file is recorded on
    ``plan.result`` and its manifest hash on ``plan.new``.

    ``rel_prefix`` accumulates path components from the source root, so
    we can identify whether a given file matches an entry in
    ``merge_set`` (which holds paths relative to the source root).
    """
    for entry in src.iterdir():
        target = dest / entry.name
        rel_components = (*rel_prefix, entry.name)
        if entry.is_dir():
            _copy_tree(
                entry,
                target,
                rel_prefix=rel_components,
                merge_set=merge_set,
                plan=plan,
            )
            continue
        if not entry.is_file():
            continue
        rel_str = "/".join(rel_components)
        key = f"{plan.agent}/{rel_str}"
        if rel_str in merge_set:
            _merge_block(entry, target, key=key, plan=plan)
            continue
        bundled = entry.read_text(encoding="utf-8")
        if not target.exists():
            if not plan.dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(bundled, encoding="utf-8")
            plan.new[key] = _sha(bundled)
            plan.result.written.append(target)
            continue
        try:
            current: str | None = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            current = None
        action = plan.decide(
            key, None if current is None else _sha(current), _sha(bundled)
        )
        _record(plan, target, action, current, bundled)
        if action == "overwrite" and not plan.dry_run:
            target.write_text(bundled, encoding="utf-8")


def _bundled_paths(src: Traversable | Path, prefix: str = "") -> set[str]:
    """Every file in a bundled tree, as ``/``-separated relative paths."""
    out: set[str] = set()
    for entry in src.iterdir():
        rel = f"{prefix}{entry.name}"
        if entry.is_dir():
            out |= _bundled_paths(entry, f"{rel}/")
        elif entry.is_file():
            out.add(rel)
    return out


def _safe_rel(rel: str) -> bool:
    """Whether ``rel`` is a plain relative path (no ``..``, ``.`` or empty part)."""
    return all(p not in ("", ".", "..") and "\\" not in p for p in rel.split("/"))


def retired_assets(
    target: AgentTarget,
    manifest_files: dict[str, str],
    *,
    source: Traversable | Path | None = None,
) -> dict[str, frozenset[str]]:
    """Return the files ``target`` used to ship but no longer does.

    Parameters
    ----------
    target : AgentTarget
        Agent with bundled assets.
    manifest_files : dict[str, str]
        The manifest's ``files`` map (``{}`` without a manifest).
    source : Traversable or Path, optional
        The bundled tree (``None``: the packaged one).

    Returns
    -------
    dict[str, frozenset[str]]
        Bundle-relative path (``/`` separators) -> the SHA-256s of the
        content WhyGraph installed there: the manifest hash and every
        :data:`RETIRED_ASSETS` hash. Paths still bundled, append-merged
        files and paths with a ``..``, ``.``, empty or backslashed part
        are left out.
    """
    src = source if source is not None else packaged_assets_for(target)
    bundled = _bundled_paths(src)
    merge_set = frozenset(target.assets_merge_files)
    found: dict[str, set[str]] = {}
    mine = f"{target.name}/"
    for key, sha in manifest_files.items():
        if key.startswith(mine):
            found.setdefault(key[len(mine) :], set()).add(sha)
    for rel, hashes in RETIRED_ASSETS.get(target.assets_subdir or "", {}).items():
        found.setdefault(rel, set()).update(hashes)
    return {
        rel: frozenset(hashes)
        for rel, hashes in sorted(found.items())
        if rel not in bundled and rel not in merge_set and _safe_rel(rel)
    }


def _retire(rel: str, known: frozenset[str], dest_root: Path, plan: _Plan) -> None:
    """Delete (or flag) one file WhyGraph no longer ships, per the module notes."""
    key = f"{plan.agent}/{rel}"
    path = dest_root.joinpath(*rel.split("/"))
    current = path
    while current != plan.root and plan.root in current.parents:
        if current.is_symlink():
            return  # never read or delete through a symlink
        current = current.parent
    if not path.is_file():
        return
    try:
        content: str | None = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        content = None
    if content is not None and BEGIN_MARKER in content:
        return  # an append-merged file: it may hold the user's own text
    result = plan.result
    result.diffs[path] = _diff(
        path.relative_to(plan.root).as_posix(), content or "", ""
    )
    if not plan.force and (content is None or _sha(content) not in known):
        result.needs_confirmation.append(path)
        result.retired.append(path)
        if key in plan.old:
            plan.new[key] = plan.old[key]  # asked again next time
        return
    result.removed.append(path)
    if plan.dry_run:
        return
    path.unlink()
    parent = path.parent
    while parent != dest_root and dest_root in parent.parents:
        if parent.is_symlink() or not parent.is_dir():
            break
        try:
            parent.rmdir()
        except OSError:
            break  # not empty
        parent = parent.parent


def _record(
    plan: _Plan, target: Path, action: str, current: str | None, bundled: str
) -> None:
    """File ``target`` under the :class:`InstallResult` list for ``action``."""
    result = plan.result
    rel = target.relative_to(plan.root).as_posix()
    diff = _diff(rel, current or "", bundled)
    if action == "skip":
        result.skipped.append(target)
    elif action == "overwrite":
        result.overwritten.append(target)
        result.diffs[target] = diff
    else:
        result.needs_confirmation.append(target)
        result.diffs[target] = diff


def _merge_block(
    src_entry: Traversable | Path,
    dest_path: Path,
    *,
    key: str,
    plan: _Plan,
) -> None:
    """Append-merge ``src_entry``'s body into ``dest_path``.

    The bundled body is wrapped in :data:`BEGIN_MARKER` /
    :data:`END_MARKER` markers and:

    * **dest missing** - written fresh; appended to ``result.written``.
    * **dest exists, markers present** - the block follows the same table
      as a plain file (see the module notes): replaced in place when it
      is unedited (``overwritten``), left alone when it already equals
      the bundled body or the user edited it and the bundle did not
      change (``skipped``), and ``needs_confirmation`` when it was edited
      and the bundle changed. ``force`` replaces it.
    * **dest exists, markers missing** - the wrapped block is appended
      to the existing content, separated by a single blank line if the
      file does not already end in one; appended to ``result.overwritten``.

    Content outside the markers is preserved verbatim.
    """
    result = plan.result
    bundled = src_entry.read_text(encoding="utf-8").rstrip("\n")
    block = f"{BEGIN_MARKER}\n{bundled}\n{END_MARKER}\n"
    bundled_hash = _sha(bundled)

    if not dest_path.exists():
        if not plan.dry_run:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_text(block, encoding="utf-8")
        plan.new[key] = bundled_hash
        result.written.append(dest_path)
        return

    existing = dest_path.read_text(encoding="utf-8")
    begin = existing.find(BEGIN_MARKER)
    end = existing.find(END_MARKER)

    if begin != -1 and end != -1 and end > begin:
        body = existing[begin + len(BEGIN_MARKER) : end]
        body = body.removeprefix("\n").removesuffix("\n")
        action = plan.decide(key, _sha(body), bundled_hash)
        _record(plan, dest_path, action, body, bundled)
        if action != "overwrite":
            return
        # Replace the existing WhyGraph block in place. Preserve any
        # content before BEGIN_MARKER and after END_MARKER verbatim.
        end_of_block = end + len(END_MARKER)
        # Consume the newline following END_MARKER too (if any) so the
        # rewritten file doesn't grow a stray blank line on each run.
        if end_of_block < len(existing) and existing[end_of_block] == "\n":
            end_of_block += 1
        new_content = existing[:begin] + block + existing[end_of_block:]
        if not plan.dry_run:
            dest_path.write_text(new_content, encoding="utf-8")
        return

    # No markers - append the block after the existing content,
    # ensuring exactly one blank line of separation.
    separator = (
        ""
        if existing.endswith("\n\n")
        else ("\n" if existing.endswith("\n") else "\n\n")
    )
    if not plan.dry_run:
        dest_path.write_text(existing + separator + block, encoding="utf-8")
    plan.new[key] = bundled_hash
    result.overwritten.append(dest_path)


__all__ = [
    "BEGIN_MARKER",
    "END_MARKER",
    "MANIFEST_RELPATH",
    "RETIRED_ASSETS",
    "InstallResult",
    "install_assets",
    "manifest_status",
    "package_version",
    "packaged_assets_for",
    "read_manifest",
    "retired_assets",
    "set_manifest_version",
]
