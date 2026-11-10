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

Overrides: a committed ``.whygraph-overrides/`` (:data:`OVERLAY_DIR`)
mirrors the bundled trees under each agent's ``assets_subdir`` and can
**replace** a bundled file (same path), **append** to a ``.md`` / ``.mdc``
one (``X.append.md``) or **disable** one (a line in
``.whygraph-overrides/disabled``). The installer applies it to the bundled
tree first (the *effective* tree) and runs the table above against that,
so an installed override is never "edited". A disabled file is retired
like a file WhyGraph no longer ships. A replaced file keeps a copy of the
bundled text it replaced under ``.whygraph/asset-bases/<agent>/``
(:data:`ASSET_BASES_RELPATH`) and its hash in the manifest's ``bases``
map, so a later bundled change is reported with the upstream diff until
``acknowledge_upstream`` is passed. The overlay is untrusted: it is read
without following links and never raises, and a part it cannot read is
**held** (the installed file is left as it is) with an
:class:`OverlayWarning`.
"""

from __future__ import annotations

import difflib
import errno
import hashlib
import json
import os
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib import resources
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath

from .agents import AGENTS, AgentTarget, is_git_tracked
from .core.safe_paths import UnsafePathError, check_inside

BEGIN_MARKER = "<!-- BEGIN whygraph -->"
"""Opening sentinel of the WhyGraph-owned block in an append-merged file."""

END_MARKER = "<!-- END whygraph -->"
"""Closing sentinel of the WhyGraph-owned block in an append-merged file."""


MANIFEST_RELPATH = Path(".whygraph") / "assets.json"
"""The upgrade manifest (relative to the project root; gitignored with ``.whygraph/``)."""

ASSET_BASES_RELPATH = Path(".whygraph") / "asset-bases"
"""Copies of the bundled files the overlay replaces, as last installed.

Relative to the project root (gitignored with ``.whygraph/``). The copy
for manifest key ``<agent>/<rel>`` lives at ``<agent>/<rel>`` below it,
where ``<agent>`` is :attr:`whygraph.agents.AgentTarget.name`
(``claude``, never ``claude-code``). It is what the "WhyGraph changed this
file since your override was written" diff starts from.
"""


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


OVERLAY_DIR = Path(".whygraph-overrides")
"""The committed overlay (relative to the project root; never gitignored)."""

OVERLAY_FILE_MAX = 64 * 1024
"""Bytes per overlay file (the largest bundled file is 32,216 bytes)."""

OVERLAY_FILES_MAX = 100
"""Scandir entries per agent subtree, directories counted (the largest tree has 26 files)."""

OVERLAY_TOTAL_MAX = 1024 * 1024
"""Bytes per agent subtree (the largest tree is 213,846 bytes)."""

OVERLAY_DEPTH_MAX = 8
"""Folder depth below an agent subfolder."""

OVERLAY_WARNINGS_MAX = 50
"""Warnings kept per Initialize; one ``truncated`` warning follows when more were found."""


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
    overrides : dict[Path, str]
        Target path -> ``"replace"``, ``"append"`` or ``"replace+append"``
        for every installed file built from ``.whygraph-overrides/``,
        whatever its outcome.
    upstream_diffs : dict[Path, str]
        For a replaced file whose bundled copy changed since the override
        was last acknowledged (*behind*): a unified diff of the earlier
        bundled copy -> the current one, or (path in
        ``upstream_base_missing``) of the override -> the current bundled
        copy.
    upstream_base_missing : set[Path]
        The paths in ``upstream_diffs`` whose earlier bundled copy is not
        kept, so the diff starts from the override instead.
    disabled : list[Path]
        The paths in ``removed`` or ``retired`` that are there because
        ``.whygraph-overrides/disabled`` switched them off.
    overlay_warnings : list[OverlayWarning]
        Non-blocking notes about ``.whygraph-overrides/`` for this agent,
        without duplicates.
    """

    written: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    overwritten: list[Path] = field(default_factory=list)
    needs_confirmation: list[Path] = field(default_factory=list)
    diffs: dict[Path, str] = field(default_factory=dict)
    removed: list[Path] = field(default_factory=list)
    retired: list[Path] = field(default_factory=list)
    overrides: dict[Path, str] = field(default_factory=dict)
    upstream_diffs: dict[Path, str] = field(default_factory=dict)
    upstream_base_missing: set[Path] = field(default_factory=set)
    disabled: list[Path] = field(default_factory=list)
    overlay_warnings: list[OverlayWarning] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class OverlayWarning:
    """One non-blocking note about ``.whygraph-overrides/``.

    Attributes
    ----------
    path : str
        Repo-relative path with ``/`` separators, JSON-safe (a name that
        is not valid UTF-8 or holds a control character is shown with
        backslash escapes), or ``.whygraph-overrides/disabled:<line>``.
    kind : str
        One of ``unsafe``, ``too_large``, ``not_text``, ``unknown_path``,
        ``no_longer_bundled``, ``append_unsupported``,
        ``cannot_disable_merged``, ``overlay_unreadable``,
        ``marker_in_text``, ``still_referenced``, ``overridden_but_disabled``,
        ``truncated``.
    detail : str
        Extra context (``still_referenced``: ``"<install path of the
        disabled file> is used by <first install path that names it>"``,
        plus ``" and N more"``); empty otherwise.
    """

    path: str
    kind: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class _Effective:
    """What one bundled file installs as once the overlay is applied."""

    text: str  # what gets installed (the block body for a merged file)
    bundled: str  # the bundled text it was built from
    override: str | None  # None | "replace" | "append" | "replace+append"


@dataclass(frozen=True, slots=True)
class _Overlay:
    """One agent's view of ``.whygraph-overrides/`` (see :func:`_read_overlay`).

    ``files`` maps an overlay rel (below ``subdir``, ``/`` separators,
    append files under their own name) to its normalized text;
    ``disabled`` holds ``(line number, rel)`` for this agent's
    ``disabled`` lines (``subdir/`` and one trailing ``/`` removed, ``""``
    for the whole agent); ``held`` the rels or folder rels to leave as
    they are; ``hold_all`` holds every file of the agent.
    """

    subdir: str
    files: dict[str, str] = field(default_factory=dict)
    disabled: list[tuple[int, str]] = field(default_factory=list)
    held: set[str] = field(default_factory=set)
    hold_all: bool = False
    warnings: list[OverlayWarning] = field(default_factory=list)


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
        treated as user-edited. A ``"bases"`` map (same key shape: the
        hash of the bundled copy each override replaced) is added only
        when the file holds a non-empty valid one; a malformed map is
        ignored and malformed entries are dropped, so callers read it with
        ``manifest.get("bases", {})``.
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
        out: dict = {"version": version, "files": dict(files)}
        bases = _valid_bases(data.get("bases"))
        if bases:
            out["bases"] = bases
        return out
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ):
        return None


def _valid_bases(raw: object) -> dict[str, str]:
    """The well-formed entries of a manifest ``bases`` map (``{}`` if not a map)."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        name, sep, rel = key.partition("/")
        if not sep or name not in AGENTS or not _safe_rel(rel):
            continue
        if len(value) == 64 and all(c in "0123456789abcdef" for c in value):
            out[key] = value
    return out


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through ``<name>.tmp`` and a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_manifest(
    project_root: Path, version: str | None, files: dict, *, bases: dict
) -> None:
    data: dict = {"version": version, "files": dict(sorted(files.items()))}
    if bases:
        data["bases"] = dict(sorted(bases.items()))
    _atomic_write(project_root / MANIFEST_RELPATH, json.dumps(data, indent=2) + "\n")


def set_manifest_version(project_root: Path, version: str | None) -> None:
    """Rewrite the manifest's recorded version (no-op without a manifest)."""
    manifest = read_manifest(project_root)
    if manifest is not None:
        _write_manifest(
            project_root, version, manifest["files"], bases=manifest.get("bases", {})
        )


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
    acknowledge_upstream: bool = False,
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
        but touch nothing on disk, the manifest and the asset-bases copies
        included. Used by the "Update agent files" preview.
    acknowledge_upstream : bool, default False
        Record the current bundled copy as the base of every replaced
        file that is *behind* (its bundled copy changed since the base
        was recorded), so the next call no longer reports it. Without it
        a behind file keeps its base and is reported again. The upstream
        diff is reported either way. The portal passes ``True`` only when
        "Update agent files" is ticked.

    Returns
    -------
    InstallResult
        Per-file outcome (written / skipped / overwritten /
        needs_confirmation / removed, with diffs), the overrides, the
        upstream diffs of behind replacements, the files switched off and
        the overlay warnings.

    Raises
    ------
    ValueError
        If ``target`` has no bundled assets configured.

    Notes
    -----
    The source is the *effective* tree: the bundled tree with
    ``.whygraph-overrides/`` applied (see the module notes). When the
    overlay root, this agent's subfolder or ``disabled`` cannot be read,
    nothing is installed, retired or recorded for this agent and only the
    warning is returned; a single unreadable entry holds just the file it
    targets (left as is, never retired, its manifest and base entries
    kept).

    After the effective tree, the files WhyGraph no longer ships and the
    files ``disabled`` switches off (see :func:`retired_assets`) are
    deleted when unedited, flagged ``needs_confirmation`` when edited
    (deleted with ``force``), and left alone when a symlink or not a
    regular file.

    The asset-bases copies (:data:`ASSET_BASES_RELPATH`) are written and
    deleted without a containment check of their own: the caller checks
    them first, as it does every destination
    (``whygraph.project_setup._touched_paths``). When git tracks
    ``.whygraph/asset-bases`` they are neither read, written nor deleted.
    """
    if not target.has_assets:
        raise ValueError(f"agent {target.name!r} has no bundled assets to install")
    src: Traversable | Path = (
        source if source is not None else packaged_assets_for(target)
    )
    assert (
        target.assets_dest is not None
    )  # for type checkers; has_assets guarantees this
    overlay = _read_overlay(project_root, target)
    if overlay.hold_all:
        # Hold rule: no write, no retire, no manifest or bases change.
        return InstallResult(overlay_warnings=list(dict.fromkeys(overlay.warnings)))
    dest_root = project_root.joinpath(*target.assets_dest)
    merge_set = frozenset(target.assets_merge_files)
    manifest = read_manifest(project_root)
    old_files: dict[str, str] = manifest["files"] if manifest else {}
    old_bases: dict[str, str] = manifest.get("bases", {}) if manifest else {}
    mine = f"{target.name}/"
    known_old = {k[len(mine) :] for k in old_files if k.startswith(mine)}
    known_old.update(RETIRED_ASSETS.get(target.assets_subdir or "", {}))
    effective, held, warnings = _effective_tree(
        src, overlay, merge_set, known_old, target.assets_dest
    )
    plan = _Plan(
        agent=target.name,
        root=project_root,
        force=force,
        dry_run=dry_run,
        old=old_files,
    )
    result = plan.result
    result.overlay_warnings.extend(warnings)
    _copy_tree(effective, dest_root, merge_set=merge_set, plan=plan)
    for rel in held:
        if f"{mine}{rel}" in plan.old:
            plan.new[f"{mine}{rel}"] = plan.old[f"{mine}{rel}"]
    for rel, eff in sorted(effective.items()):
        if eff.override is not None:
            result.overrides[dest_root.joinpath(*rel.split("/"))] = eff.override

    bundled = _bundled_paths(src)
    disabled = bundled - set(effective) - held - merge_set
    retired = retired_assets(
        target,
        plan.old,
        source=src,
        present=set(effective) | held,
        extra_known=_disabled_overrides(src, overlay, disabled),
    )
    for rel, known in retired.items():
        _retire(rel, known, dest_root, plan)
    for rel in sorted(disabled):
        path = dest_root.joinpath(*rel.split("/"))
        if path in result.removed or path in result.retired:
            result.disabled.append(path)

    bases = _track_bases(
        project_root,
        target,
        effective,
        held,
        old_files,
        old_bases,
        scope=bundled | set(retired),
        result=result,
        dry_run=dry_run,
        acknowledge=acknowledge_upstream,
    )
    if not dry_run:
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
            or bases != old_bases
        ):
            _write_manifest(project_root, version, files, bases=bases)
    return result


def _disabled_overrides(
    src: Traversable | Path, overlay: _Overlay, disabled: set[str]
) -> dict[str, frozenset[str]]:
    """The hash a disabled rel with an override would have without the disable."""
    out: dict[str, frozenset[str]] = {}
    for rel in sorted(disabled):
        replacement = overlay.files.get(rel)
        appended = overlay.files.get(_append_rel(rel))
        if PurePosixPath(rel).suffix not in _APPEND_SUFFIXES:
            appended = None
        if replacement is None and appended is None:
            continue
        text, _ = _overridden_text(_bundled_text(src, rel), replacement, appended)
        out[rel] = frozenset({_sha(text)})
    return out


def _read_base(path: Path) -> str | None:
    """A base copy's text, or ``None`` when it is missing, a link or unreadable."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _track_bases(
    project_root: Path,
    target: AgentTarget,
    effective: dict[str, _Effective],
    held: set[str],
    old_files: dict[str, str],
    old_bases: dict[str, str],
    *,
    scope: set[str],
    result: InstallResult,
    dry_run: bool,
    acknowledge: bool,
) -> dict[str, str]:
    """Report behind replacements and return the manifest's new ``bases`` map.

    A replaced rel is *behind* when its recorded base differs from the
    current bundled copy, or (no base yet) when the old manifest hash
    differs from both the bundled and the effective text. Base copies
    are touched only for rels in ``scope`` (what the caller's containment
    check covered), only when not ``dry_run`` and only when git does not
    track :data:`ASSET_BASES_RELPATH`.
    """
    mine = f"{target.name}/"
    assert target.assets_dest is not None
    dest_root = project_root.joinpath(*target.assets_dest)
    mirror = project_root / ASSET_BASES_RELPATH / target.name
    merge_set = frozenset(target.assets_merge_files)
    tracked: list[bool] = []

    def copies_usable() -> bool:
        if not tracked:
            tracked.append(is_git_tracked(project_root, ASSET_BASES_RELPATH.as_posix()))
        return not tracked[0]

    bases = {k: v for k, v in old_bases.items() if not k.startswith(mine)}
    for rel in held:
        if f"{mine}{rel}" in old_bases:
            bases[f"{mine}{rel}"] = old_bases[f"{mine}{rel}"]
    for rel, eff in sorted(effective.items()):
        if eff.override not in ("replace", "replace+append") or rel not in scope:
            continue
        key = f"{mine}{rel}"
        dest = dest_root.joinpath(*rel.split("/"))
        copy = mirror.joinpath(*rel.split("/"))
        new_sha = _sha(eff.bundled)
        old_sha = old_bases.get(key)
        old_file = old_files.get(key)
        if old_sha is not None:
            behind = old_sha != new_sha
        else:
            # The manifest hashes a merged block's body without its last newlines.
            strip = rel in merge_set
            known = {
                _sha(t.rstrip("\n") if strip else t) for t in (eff.bundled, eff.text)
            }
            behind = old_file is not None and old_file not in known
        if behind:
            shown = dest.relative_to(project_root).as_posix()
            earlier = None
            if old_sha is not None and copies_usable():
                earlier = _read_base(copy)
                if earlier is not None and _sha(earlier) != old_sha:
                    earlier = None
            if earlier is None:
                result.upstream_diffs[dest] = _diff(shown, eff.text, eff.bundled)
                result.upstream_base_missing.add(dest)
            else:
                result.upstream_diffs[dest] = _diff(shown, earlier, eff.bundled)
        kept = old_sha if old_sha is not None else old_file
        if behind and not acknowledge and kept is not None:
            # Keep the notice: the base stays where it was (no copy written).
            bases[key] = kept
            continue
        bases[key] = new_sha
        if dry_run or not copies_usable():
            continue
        current = _read_base(copy)
        if current is None or _sha(current) != new_sha:
            _atomic_write(copy, eff.bundled)

    if not dry_run:
        for key in sorted(old_bases):
            if not key.startswith(mine) or key in bases:
                continue
            rel = key[len(mine) :]
            if rel not in scope:
                continue  # dropped from the map; no file is touched
            copy = mirror.joinpath(*rel.split("/"))
            if copies_usable() and not copy.is_symlink() and copy.is_file():
                copy.unlink()
    return bases


def _copy_tree(
    effective: dict[str, _Effective],
    dest: Path,
    *,
    merge_set: frozenset[str],
    plan: _Plan,
) -> None:
    """Install the effective tree into ``dest``, recording each file's fate.

    Each rel lands at ``dest/<rel>`` (a destination always comes from a
    bundled path, never from the overlay). The fate of each file is
    recorded on ``plan.result`` and its manifest hash on ``plan.new``;
    rels in ``merge_set`` go through :func:`_merge_block`.
    """
    for rel_str, eff in sorted(effective.items()):
        target = dest.joinpath(*rel_str.split("/"))
        key = f"{plan.agent}/{rel_str}"
        if rel_str in merge_set:
            _merge_block(eff.text, target, key=key, plan=plan)
            continue
        bundled = eff.text
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
    present: set[str] | None = None,
    extra_known: Mapping[str, frozenset[str]] | None = None,
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
    present : set[str], optional
        The rels that stay installed (the effective tree plus the held
        rels). When given, a bundled rel outside it (a file
        ``.whygraph-overrides/disabled`` switches off) is a candidate too,
        known by its bundled hash whether or not the manifest has it, and
        only rels in ``present`` are left out as "still shipped".
    extra_known : Mapping[str, frozenset[str]], optional
        More hashes per rel to treat as unedited (for a disabled rel that
        also has an override: the text it would have had).

    Returns
    -------
    dict[str, frozenset[str]]
        Bundle-relative path (``/`` separators) -> the SHA-256s of the
        content WhyGraph installed there: the manifest hash and every
        :data:`RETIRED_ASSETS` hash. Paths still bundled (or in
        ``present``), append-merged files and paths with a ``..``, ``.``,
        empty or backslashed part are left out.
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
    keep = bundled if present is None else present
    if present is not None:
        for rel in sorted(bundled - present - merge_set):
            if _safe_rel(rel):
                found.setdefault(rel, set()).add(_sha(_bundled_text(src, rel)))
    for rel, hashes in (extra_known or {}).items():
        found.setdefault(rel, set()).update(hashes)
    return {
        rel: frozenset(hashes)
        for rel, hashes in sorted(found.items())
        if rel not in keep and rel not in merge_set and _safe_rel(rel)
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
    body: str,
    dest_path: Path,
    *,
    key: str,
    plan: _Plan,
) -> None:
    """Append-merge ``body`` (the effective block body) into ``dest_path``.

    The body is wrapped in :data:`BEGIN_MARKER` /
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
    bundled = body.rstrip("\n")
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


# ---- overrides: the committed overlay over the bundled tree ---------------

_DISABLED_NAME = "disabled"
_APPEND = ".append"
_APPEND_SUFFIXES = frozenset({".md", ".mdc"})
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_LINK_ERRNOS = frozenset(
    {errno.ELOOP, errno.ENOTDIR, errno.EMLINK}
)  # O_NOFOLLOW met a link


class _Refused(Exception):
    """An overlay entry the reader will not use; ``kind`` is the warning kind."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class _HoldAll(Exception):
    """The overlay cannot be trusted for this agent: hold every file."""

    def __init__(self, warning: OverlayWarning) -> None:
        super().__init__(warning.kind)
        self.warning = warning


class _CapHit(Exception):
    """A subtree cap (entries, bytes or depth) was hit."""


@dataclass(slots=True)
class _Walk:
    """Accumulator of one agent subtree walk (see :func:`_walk_overlay`)."""

    subdir: str
    files: dict[str, str] = field(default_factory=dict)
    held: set[str] = field(default_factory=set)
    warnings: list[OverlayWarning] = field(default_factory=list)
    entries: int = 0
    size: int = 0

    def refuse(self, rel: str, kind: str) -> None:
        self.warnings.append(OverlayWarning(_overlay_rel(self.subdir, rel), kind))
        self.held.add(rel)
        self.held.update(_append_bases(rel))


def _overlay_rel(*parts: str) -> str:
    """``.whygraph-overrides/<parts>`` as a repo-relative ``/`` path."""
    return "/".join((OVERLAY_DIR.as_posix(), *(p for p in parts if p)))


def _bad_name(name: str) -> bool:
    """Whether a scandir name is not plain UTF-8 text or not a safe path part."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return True
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in name):
        return True
    return not _safe_rel(name)


def _shown_name(name: str) -> str:
    """A JSON-safe form of a name :func:`_bad_name` refused."""
    return name.encode("utf-8", "backslashreplace").decode("ascii", "backslashreplace")


def _append_rel(rel: str) -> str:
    """The overlay rel that appends to bundled ``rel`` (last suffix only)."""
    head, _, name = rel.rpartition("/")
    suffix = PurePosixPath(name).suffix
    stem = name[: len(name) - len(suffix)]
    return f"{head}/{stem}{_APPEND}{suffix}" if head else f"{stem}{_APPEND}{suffix}"


def _append_bases(rel: str) -> set[str]:
    """The bundled rels an overlay rel named like an append could target."""
    head, _, name = rel.rpartition("/")
    prefix = f"{head}/" if head else ""
    out: set[str] = set()
    if name.endswith(_APPEND) and len(name) > len(_APPEND):
        out.add(prefix + name[: -len(_APPEND)])
    suffix = PurePosixPath(name).suffix
    stem = name[: len(name) - len(suffix)]
    if suffix and stem.endswith(_APPEND) and len(stem) > len(_APPEND):
        out.add(prefix + stem[: -len(_APPEND)] + suffix)
    return out


def _under(rel: str, prefix: str) -> bool:
    """Whether ``rel`` is ``prefix`` or below it (``""`` is the whole tree)."""
    return not prefix or rel == prefix or rel.startswith(f"{prefix}/")


def _normalize(text: str) -> str:
    """Drop one leading BOM and turn CRLF / CR into LF."""
    return text.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n")


def _decode(data: bytes) -> str:
    try:
        return _normalize(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise _Refused("not_text") from None


def _same_entry(fst: os.stat_result, entry: os.DirEntry[str]) -> bool:
    st = entry.stat(follow_symlinks=False)
    return (fst.st_dev, fst.st_ino) == (st.st_dev, st.st_ino)


def _open_dir(parent_fd: int, entry: os.DirEntry[str]) -> int:
    """Open folder ``entry`` of ``parent_fd`` without following a link."""
    fd = os.open(entry.name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        fst = os.fstat(fd)
        if not stat.S_ISDIR(fst.st_mode) or not _same_entry(fst, entry):
            raise _Refused("unsafe")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_file(parent_fd: int, entry: os.DirEntry[str]) -> bytes:
    """Read regular file ``entry`` of ``parent_fd`` (at most the per-file cap)."""
    fd = os.open(entry.name, _FILE_FLAGS, dir_fd=parent_fd)
    chunks: list[bytes] = []
    size = 0
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or not _same_entry(fst, entry):
            raise _Refused("unsafe")
        while size <= OVERLAY_FILE_MAX:
            chunk = os.read(fd, OVERLAY_FILE_MAX + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > OVERLAY_FILE_MAX:
        raise _Refused("too_large")
    return b"".join(chunks)


def _listing(dir_fd: int, walk: _Walk) -> list[os.DirEntry[str]]:
    """The entries of ``dir_fd`` in name order, counted against the cap."""
    entries: list[os.DirEntry[str]] = []
    with os.scandir(dir_fd) as it:
        for entry in it:
            walk.entries += 1
            if walk.entries > OVERLAY_FILES_MAX:
                raise _CapHit
            entries.append(entry)
    return sorted(entries, key=lambda e: e.name)


def _walk_overlay(dir_fd: int, prefix: str, depth: int, walk: _Walk) -> None:
    """Read one agent subtree into ``walk``; raises :class:`_CapHit` on a cap."""
    for entry in _listing(dir_fd, walk):
        name = entry.name
        if _bad_name(name):
            # Targets nothing: a name that is not plain text cannot match.
            walk.warnings.append(
                OverlayWarning(
                    _overlay_rel(walk.subdir, prefix + _shown_name(name)), "unsafe"
                )
            )
            continue
        rel = prefix + name
        try:
            if entry.is_symlink():
                raise _Refused("unsafe")
            if entry.is_dir(follow_symlinks=False):
                if depth + 1 > OVERLAY_DEPTH_MAX:
                    raise _CapHit
                fd = _open_dir(dir_fd, entry)
                try:
                    _walk_overlay(fd, f"{rel}/", depth + 1, walk)
                finally:
                    os.close(fd)
            elif entry.is_file(follow_symlinks=False):
                data = _read_file(dir_fd, entry)
                walk.size += len(data)
                if walk.size > OVERLAY_TOTAL_MAX:
                    raise _CapHit
                walk.files[rel] = _decode(data)
            else:
                raise _Refused("unsafe")  # a FIFO, socket or device
        except _Refused as exc:
            walk.refuse(rel, exc.kind)
        except OSError as exc:
            walk.refuse(
                rel, "unsafe" if exc.errno in _LINK_ERRNOS else "overlay_unreadable"
            )


@contextmanager
def _overlay_root(
    project_root: Path,
) -> Iterator[tuple[int, dict[str, os.DirEntry[str]]] | None]:
    """Open ``.whygraph-overrides/`` by exact name; ``None`` when it is absent.

    Yields the root's fd and its entries by exact name. Raises
    :class:`_HoldAll` when the root is unsafe, unreadable or over the
    entry cap; any other ``OSError`` propagates.
    """
    root_warning = OverlayWarning(_overlay_rel(), "overlay_unreadable")
    try:
        project_fd = os.open(project_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except FileNotFoundError:
        yield None
        return
    try:
        with os.scandir(project_fd) as it:
            entry = next((e for e in it if e.name == OVERLAY_DIR.name), None)
        if entry is None:
            yield None
            return
        try:
            check_inside(project_root, project_root / OVERLAY_DIR)
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                raise _Refused("unsafe")
            root_fd = _open_dir(project_fd, entry)
        except (_Refused, OSError, UnsafePathError):
            raise _HoldAll(root_warning) from None
        try:
            entries: dict[str, os.DirEntry[str]] = {}
            with os.scandir(root_fd) as it:
                for count, child in enumerate(it, start=1):
                    if count > OVERLAY_FILES_MAX:
                        raise _HoldAll(OverlayWarning(_overlay_rel(), "too_large"))
                    entries[child.name] = child
            yield root_fd, entries
        finally:
            os.close(root_fd)
    finally:
        os.close(project_fd)


def _read_disabled(root_fd: int, entry: os.DirEntry[str]) -> list[tuple[int, str]]:
    """The ``disabled`` file's lines as ``(1-based number, stripped line)``.

    Blank lines and ``#`` comments are dropped. Raises :class:`_HoldAll`
    when the file is unsafe, unreadable, too large or not UTF-8.
    """
    path = _overlay_rel(_DISABLED_NAME)
    try:
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            raise _Refused("overlay_unreadable")
        text = _decode(_read_file(root_fd, entry))
    except _Refused as exc:
        kind = "overlay_unreadable" if exc.kind == "unsafe" else exc.kind
        raise _HoldAll(OverlayWarning(path, kind)) from None
    except OSError:
        raise _HoldAll(OverlayWarning(path, "overlay_unreadable")) from None
    lines: list[tuple[int, str]] = []
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append((number, line))
    return lines


def _line_body(line: str) -> str:
    """A ``disabled`` line with one trailing ``/`` removed."""
    return line[:-1] if line.endswith("/") else line


def _disabled_path(number: int) -> str:
    return _overlay_rel(f"{_DISABLED_NAME}:{number}")


def _read_overlay(project_root: Path, target: AgentTarget) -> _Overlay:
    """Read ``target``'s part of ``.whygraph-overrides/`` without trusting it.

    Never raises and never follows a symbolic link: the root, the agent
    subfolder and ``disabled`` are matched by exact scandir name, every
    folder and file is opened by an fd relative to its parent with
    ``O_NOFOLLOW`` and checked against its scandir entry, and only
    regular UTF-8 files within the caps are read. A problem with the
    root, the agent subfolder or ``disabled`` (or a subtree cap) sets
    ``hold_all``; a problem with one entry holds the rel it targets.

    Parameters
    ----------
    project_root : Path
        Repository root.
    target : AgentTarget
        Agent whose subfolder (:attr:`AgentTarget.assets_subdir`) to read.

    Returns
    -------
    _Overlay
        Normalized texts, this agent's ``disabled`` lines, the held rels
        and the warnings; empty when the overlay or the subfolder is absent.
    """
    sub = target.assets_subdir or ""
    try:
        with _overlay_root(project_root) as root:
            if root is None:
                return _Overlay(subdir=sub)
            root_fd, entries = root
            warnings: list[OverlayWarning] = []
            disabled: list[tuple[int, str]] = []
            listed = entries.get(_DISABLED_NAME)
            for number, line in _read_disabled(root_fd, listed) if listed else []:
                body = _line_body(line)
                if body == sub:
                    disabled.append((number, ""))
                elif body.startswith(f"{sub}/"):
                    rel = body[len(sub) + 1 :]
                    if _safe_rel(rel):
                        disabled.append((number, rel))
                    else:
                        warnings.append(
                            OverlayWarning(_disabled_path(number), "unsafe")
                        )
            walk = _Walk(subdir=sub)
            agent = entries.get(sub) if sub else None
            if agent is not None:
                sub_warning = OverlayWarning(_overlay_rel(sub), "overlay_unreadable")
                try:
                    if agent.is_symlink() or not agent.is_dir(follow_symlinks=False):
                        raise _Refused("unsafe")
                    fd = _open_dir(root_fd, agent)
                except (_Refused, OSError):
                    raise _HoldAll(sub_warning) from None
                try:
                    _walk_overlay(fd, "", 0, walk)
                except _CapHit:
                    raise _HoldAll(
                        OverlayWarning(_overlay_rel(sub), "too_large")
                    ) from None
                except OSError:
                    raise _HoldAll(sub_warning) from None
                finally:
                    os.close(fd)
            return _Overlay(
                subdir=sub,
                files=walk.files,
                disabled=disabled,
                held=walk.held,
                warnings=warnings + walk.warnings,
            )
    except _HoldAll as exc:
        return _Overlay(subdir=sub, hold_all=True, warnings=[exc.warning])
    except (OSError, ValueError):
        return _Overlay(
            subdir=sub,
            hold_all=True,
            warnings=[OverlayWarning(_overlay_rel(), "overlay_unreadable")],
        )


def overlay_root_warnings(
    project_root: Path, known_subdirs: frozenset[str]
) -> list[OverlayWarning]:
    """Report top-level overlay entries and ``disabled`` lines no agent reads.

    Lists ``.whygraph-overrides/`` the same way as the per-agent reader
    (exact names, never following a link, never raising, the same entry
    cap). Entries for a known agent that is not selected stay silent.

    Parameters
    ----------
    project_root : Path
        Repository root.
    known_subdirs : frozenset[str]
        Every :attr:`whygraph.agents.AgentTarget.assets_subdir` with
        bundled assets.

    Returns
    -------
    list[OverlayWarning]
        ``unknown_path`` for every top-level entry other than ``disabled``
        and a ``known_subdirs`` member and for every ``disabled`` line
        whose first segment is not one of them (``unsafe`` when the name
        or line is not a plain relative path); ``overlay_unreadable`` /
        ``too_large`` / ``not_text`` when the root or ``disabled`` cannot
        be read. ``[]`` when the overlay is absent.
    """
    out: list[OverlayWarning] = []
    try:
        with _overlay_root(project_root) as root:
            if root is None:
                return []
            root_fd, entries = root
            for name in sorted(entries):
                if name == _DISABLED_NAME or name in known_subdirs:
                    continue
                if _bad_name(name):
                    out.append(
                        OverlayWarning(_overlay_rel(_shown_name(name)), "unsafe")
                    )
                else:
                    out.append(OverlayWarning(_overlay_rel(name), "unknown_path"))
            listed = entries.get(_DISABLED_NAME)
            try:
                lines = _read_disabled(root_fd, listed) if listed else []
            except _HoldAll as exc:
                out.append(exc.warning)
                lines = []
            for number, line in lines:
                body = _line_body(line)
                if body.split("/", 1)[0] in known_subdirs:
                    continue
                kind = "unknown_path" if body and _safe_rel(body) else "unsafe"
                out.append(OverlayWarning(_disabled_path(number), kind))
            return out
    except _HoldAll as exc:
        return [*out, exc.warning]
    except (OSError, ValueError):
        return [*out, OverlayWarning(_overlay_rel(), "overlay_unreadable")]


def _bundled_text(src: Traversable | Path, rel: str) -> str:
    node = src
    for part in rel.split("/"):
        node = node / part
    return node.read_text(encoding="utf-8")


def _overridden_text(
    bundled: str, replacement: str | None, appended: str | None
) -> tuple[str, str | None]:
    """Rules 2-4: the text and override kind from a replacement and an append."""
    base = bundled if replacement is None else replacement
    if appended is None:
        return base, None if replacement is None else "replace"
    text = base.rstrip("\n") + "\n\n" + appended.strip("\n") + "\n"
    return text, "append" if replacement is None else "replace+append"


def _has_marker(text: str) -> bool:
    return BEGIN_MARKER in text or END_MARKER in text


def _effective_tree(
    src: Traversable | Path,
    overlay: _Overlay,
    merge_set: frozenset[str],
    known_old: set[str],
    install_prefix: tuple[str, ...],
) -> tuple[dict[str, _Effective], set[str], list[OverlayWarning]]:
    """Apply the overlay rules to a bundled tree.

    Rules, per bundled ``rel``: disabled (a file, folder or whole-agent
    line; a merged file cannot be disabled), replace, append (``.md`` /
    ``.mdc`` only, on top of a replacement), refuse a text holding a
    block marker (the rel is held), warn about an override of a disabled
    rel, warn when a disabled file's install path still appears in an
    installed file, and report overlay entries that match nothing.

    Parameters
    ----------
    src : Traversable or Path
        The bundled tree.
    overlay : _Overlay
        This agent's overlay (:func:`_read_overlay`).
    merge_set : frozenset[str]
        :attr:`AgentTarget.assets_merge_files`.
    known_old : set[str]
        Rels this agent shipped before (manifest keys, prefix stripped,
        plus :data:`RETIRED_ASSETS`): an override of one is
        ``no_longer_bundled`` instead of ``unknown_path``.
    install_prefix : tuple[str, ...]
        :attr:`AgentTarget.assets_dest`, for the install paths that
        ``still_referenced`` looks for.

    Returns
    -------
    tuple[dict[str, _Effective], set[str], list[OverlayWarning]]
        The effective map (disabled and held rels absent), the held
        bundled rels (every one under ``hold_all``) and the warnings:
        the overlay's own first, then this function's, without duplicates.
    """
    bundled = {rel: _bundled_text(src, rel) for rel in sorted(_bundled_paths(src))}
    if overlay.hold_all:
        return {}, set(bundled), list(dict.fromkeys(overlay.warnings))
    warnings: list[OverlayWarning] = list(overlay.warnings)
    sub = overlay.subdir
    files = overlay.files
    held = {rel for rel in bundled if any(_under(rel, h) for h in overlay.held)}

    # Rule 1: disabled lines.
    disabled: dict[str, int] = {}
    for number, line in overlay.disabled:
        hits = [rel for rel in bundled if _under(rel, line)]
        if not hits:
            old = any(_under(rel, line) for rel in known_old)
            kind = "no_longer_bundled" if old else "unknown_path"
            warnings.append(OverlayWarning(_disabled_path(number), kind))
            continue
        for rel in hits:
            if rel in merge_set:
                warnings.append(
                    OverlayWarning(_disabled_path(number), "cannot_disable_merged")
                )
            elif rel not in held:
                disabled.setdefault(rel, number)

    # Rules 2-4, 6 and 7.
    consumed: set[str] = set()
    effective: dict[str, _Effective] = {}
    for rel, text in bundled.items():
        append_rel = _append_rel(rel)
        replacement = files.get(rel)
        appended = files.get(append_rel)
        overrides = [
            (o, t)
            for o, t in ((rel, replacement), (append_rel, appended))
            if t is not None
        ]
        consumed.update(o for o, _ in overrides)
        if rel in held:
            continue
        if rel in disabled:
            for o, _ in overrides:
                warnings.append(
                    OverlayWarning(_overlay_rel(sub, o), "overridden_but_disabled")
                )
            continue
        if appended is not None and PurePosixPath(rel).suffix not in _APPEND_SUFFIXES:
            warnings.append(
                OverlayWarning(_overlay_rel(sub, append_rel), "append_unsupported")
            )
            appended = None
        marked = [
            o
            for o, t in ((rel, replacement), (append_rel, appended))
            if t is not None and _has_marker(t)
        ]
        if marked:
            warnings.extend(
                OverlayWarning(_overlay_rel(sub, o), "marker_in_text") for o in marked
            )
            held.add(rel)
            continue
        eff_text, kind = _overridden_text(text, replacement, appended)
        effective[rel] = _Effective(text=eff_text, bundled=text, override=kind)

    # Rule 5: a disabled file whose install path an installed file names.
    def install_path(rel: str) -> str:
        return "/".join((*install_prefix, *rel.split("/")))

    for rel, number in sorted(disabled.items()):
        needle = install_path(rel)
        users = sorted(
            install_path(r) for r, e in effective.items() if needle in e.text
        )
        if users:
            more = f" and {len(users) - 1} more" if len(users) > 1 else ""
            warnings.append(
                OverlayWarning(
                    _disabled_path(number),
                    "still_referenced",
                    f"{needle} is used by {users[0]}{more}",
                )
            )

    # Leftovers: overlay files that match no bundled file.
    for rel in sorted(set(files) - consumed):
        old = ({rel} | _append_bases(rel)) & known_old
        kind = "no_longer_bundled" if old else "unknown_path"
        warnings.append(OverlayWarning(_overlay_rel(sub, rel), kind))

    return effective, held, list(dict.fromkeys(warnings))


__all__ = [
    "ASSET_BASES_RELPATH",
    "BEGIN_MARKER",
    "END_MARKER",
    "MANIFEST_RELPATH",
    "OVERLAY_DEPTH_MAX",
    "OVERLAY_DIR",
    "OVERLAY_FILES_MAX",
    "OVERLAY_FILE_MAX",
    "OVERLAY_TOTAL_MAX",
    "OVERLAY_WARNINGS_MAX",
    "RETIRED_ASSETS",
    "InstallResult",
    "OverlayWarning",
    "install_assets",
    "manifest_status",
    "overlay_root_warnings",
    "package_version",
    "packaged_assets_for",
    "read_manifest",
    "retired_assets",
    "set_manifest_version",
]
