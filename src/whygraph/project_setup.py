"""Project initialization as a library.

:func:`initialize_project` is what the portal's Initialize step does to a
repository once its choices are made - ``.gitignore`` entries, the
auto-rescan git hooks, the agent MCP config entries and the bundled agent
assets. (It was lifted out of the 1.x CLI init command, removed in 2.0.0;
the portal is its only caller.)

It is Click-free and prints nothing: it returns an
:class:`InitializeResult` describing what happened, and the portal's HTTP
layer decides how to present it. DB bootstrap is deliberately **not**
part of it: the portal bootstraps the DB under the project context, and
the config lives in the portal DB.

Every agent gets the HTTP entry for the portal's ``/mcp/<slug>`` endpoint
(:class:`~whygraph.agents.HttpMcp`), written with the file-safety rules of
:func:`whygraph.agents.apply_http_entry`.

Notes
-----
The portal markers (``.whygraph/portal.json`` and ``.whygraph/portal.env``)
are written only when ``marker`` is given, only as the last step, and only
when every earlier step succeeded, so a failed initialize leaves no marker.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import agents as agents_mod
from . import assets as assets_mod
from . import hooks as _hooks
from .agents import (
    AgentAction,
    FileOutcome,
    HttpMcp,
)
from .core.gitignore import ensure_gitignore_entries
from .core.safe_paths import UnsafePathError, check_inside
from .hooks import HooksResult

GITIGNORE_ENTRIES = ("whygraph.toml", ".whygraph/", ".codegraph/")
"""Entries ``initialize_project`` keeps in the project's ``.gitignore``."""

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

PORTAL_JSON = Path(".whygraph") / "portal.json"
"""Marker that makes ``whygraph scan`` refuse (relative to the repo root)."""

PORTAL_ENV = Path(".whygraph") / "portal.env"
"""Marker parsed - never sourced - by the git hook helper."""


@dataclass(frozen=True, slots=True)
class PortalMarker:
    """What the portal markers record for a managed project.

    Attributes
    ----------
    slug : str
        The project slug (``^[a-z0-9][a-z0-9-]{0,62}$``).
    port : int
        The portal port.

    Raises
    ------
    ValueError
        If ``slug`` or ``port`` would not survive the hook helper's
        validation, so an invalid marker can never be written.
    """

    slug: str
    port: int

    def __post_init__(self) -> None:
        if not _SLUG_RE.match(self.slug):
            raise ValueError(f"invalid project slug: {self.slug!r}")
        if not isinstance(self.port, int) or not 0 < self.port < 65536:
            raise ValueError(f"invalid portal port: {self.port!r}")


@dataclass(frozen=True)
class InitializeResult:
    """What :func:`initialize_project` did (or, in a dry run, would do).

    Attributes
    ----------
    dry_run : bool
        Whether nothing was written. A dry run reports agent and asset
        files only; ``gitignore_added``, ``hooks`` and the marker are
        left untouched and empty.
    gitignore_added : tuple[str, ...]
        ``.gitignore`` entries newly written (empty when all present).
    hooks : HooksResult or None
        The hook reconcile outcome, or ``None`` when it was skipped
        (dry run) or failed (see ``hooks_error``).
    hooks_error : str or None
        Why hooks were skipped. Best-effort: never fails the call.
    agent_files : tuple[FileOutcome, ...]
        One outcome per agent config file touched.
    asset_files : tuple[FileOutcome, ...]
        One outcome per bundled asset file (``write`` / ``skip`` /
        ``overwrite``).
    assets : dict[str, InstallResult]
        Raw asset-install result per agent name.
    configured_agents : tuple[str, ...]
        Agents whose config entry is in place (written or already up to
        date) - the set the portal records as the project's agents.
    marker_written : bool
        Whether the portal markers were written.
    """

    dry_run: bool = False
    gitignore_added: tuple[str, ...] = ()
    hooks: HooksResult | None = None
    hooks_error: str | None = None
    agent_files: tuple[FileOutcome, ...] = ()
    asset_files: tuple[FileOutcome, ...] = ()
    assets: dict[str, assets_mod.InstallResult] = field(default_factory=dict)
    configured_agents: tuple[str, ...] = ()
    marker_written: bool = False

    @property
    def needs_confirmation(self) -> tuple[FileOutcome, ...]:
        """Agent files that are git-tracked and awaiting confirmation."""
        return tuple(f for f in self.agent_files if f.status == "needs_confirmation")

    @property
    def refused(self) -> tuple[FileOutcome, ...]:
        """Agent files left untouched because they could not be merged safely."""
        return tuple(f for f in self.agent_files if f.status == "refused")


def initialize_project(
    root: Path,
    *,
    agents: Sequence[str],
    hooks: bool | Sequence[str],
    mcp: HttpMcp,
    marker: PortalMarker | None,
    force: bool = False,
    confirm_tracked: Iterable[str | Path] = (),
    agent_actions: Mapping[str, AgentAction] | None = None,
    dry_run: bool = False,
    contained: bool = False,
) -> InitializeResult:
    """Wire a repository for WhyGraph: gitignore, hooks, agent configs, assets.

    Steps run in this order, each independent of the others' outcomes
    except the marker: ``.gitignore``, git hooks, one MCP config entry per
    agent, the bundled asset tree per agent, and finally the portal
    markers.

    Parameters
    ----------
    root : Path
        Repository root.
    agents : Sequence[str]
        Agents to configure (names or aliases). Duplicates collapse.
    hooks : bool or Sequence[str]
        The ``[scan].hooks`` value: ``True`` all hooks, ``False`` / empty
        none, or explicit hook names. An unknown name is reported in
        :attr:`InitializeResult.hooks_error`, never raised.
    mcp : HttpMcp
        The portal endpoint to register.
    marker : PortalMarker or None
        When set, the portal markers are written as the last step
        (``None`` writes none).
    force : bool
        Overwrite existing asset files (a 1.x ``.claude/`` refresh).
    confirm_tracked : Iterable[str or Path]
        Relative paths of git-tracked agent config files the caller has
        confirmed may be rewritten. Without it such a file yields
        ``needs_confirmation`` and the markers are withheld until a
        re-call succeeds.
    agent_actions : Mapping[str, {"migrate", "remove"}] or None
        Per-agent choice. ``migrate`` (the default for every agent in
        ``agents``) writes the entry; ``remove`` strips the ``whygraph``
        entry instead and installs no assets. Agents named here but not
        in ``agents`` are processed too, which is how the wizard removes
        a detected entry for an agent the user did not select.
    dry_run : bool
        Compute and return the per-file outcomes without writing,
        backing up or creating anything.
    contained : bool
        Refuse to follow a symlink out of the repository: before any
        step runs (dry run included), every path the call could read or
        write in the work tree - ``.gitignore``, the hook helper, each
        agent config file and ``.whygraph/backups/``, each bundled asset
        destination, the markers and their temp files - goes through
        :func:`whygraph.core.safe_paths.check_inside`. The portal passes
        ``True`` (repo content is untrusted there).

    Returns
    -------
    InitializeResult
        What each step did.

    Raises
    ------
    whygraph.agents.UnknownAgentError
        For an unknown agent name.
    whygraph.core.safe_paths.UnsafePathError
        With ``contained``, when any of those paths is a symlink or
        resolves outside ``root``. Nothing has been written.
    OSError
        If a step fails to write. Nothing after the failing step runs,
        so no marker is written.
    """
    root = Path(root)
    actions: dict[str, AgentAction] = {}
    for name in agents:
        actions.setdefault(agents_mod.resolve_agent(name).name, "migrate")
    for name, action in (agent_actions or {}).items():
        actions[agents_mod.resolve_agent(name).name] = action
    if contained:
        for path in _touched_paths(root, actions, marker):
            check_inside(root, path)

    gitignore_added: tuple[str, ...] = ()
    hooks_result: HooksResult | None = None
    hooks_error: str | None = None
    if not dry_run:
        gitignore_added = tuple(ensure_gitignore_entries(root, GITIGNORE_ENTRIES))
        try:
            names = _hooks.resolve_hook_names(hooks)
            hooks_result = _hooks.sync_hooks(root, names)
        except _hooks.HooksError as exc:
            hooks_error = str(exc)

    agent_files: list[FileOutcome] = []
    asset_files: list[FileOutcome] = []
    asset_results: dict[str, assets_mod.InstallResult] = {}
    configured: list[str] = []
    for name, action in actions.items():
        target = agents_mod.AGENTS[name]
        if action == "remove":
            outcome = agents_mod.remove_entry(
                target, root, confirm_tracked=confirm_tracked, dry_run=dry_run
            )
            agent_files.append(outcome)
            continue

        outcome = agents_mod.apply_http_entry(
            target, root, mcp, confirm_tracked=confirm_tracked, dry_run=dry_run
        )
        agent_files.append(outcome)
        if outcome.status in ("write", "overwrite", "skip"):
            configured.append(name)
        if target.has_assets:
            installed = assets_mod.install_assets(
                target, root, force=force, dry_run=dry_run
            )
            asset_results[name] = installed
            asset_files.extend(_asset_outcomes(root, name, installed))

    result = InitializeResult(
        dry_run=dry_run,
        gitignore_added=gitignore_added,
        hooks=hooks_result,
        hooks_error=hooks_error,
        agent_files=tuple(agent_files),
        asset_files=tuple(asset_files),
        assets=asset_results,
        configured_agents=tuple(configured),
    )

    if marker is not None and not dry_run and not result.needs_confirmation:
        _remove_stale_scan_lock(root)
        write_portal_marker(root, marker)
        return replace(result, marker_written=True)
    return result


def _touched_paths(
    root: Path, actions: Mapping[str, AgentAction], marker: PortalMarker | None
) -> list[Path]:
    """Every work-tree path :func:`initialize_project` may read or write."""
    paths = [root / ".gitignore", root / _hooks.HELPER_RELPATH]
    paths.append(root / ".whygraph" / "backups")  # agent-file backups
    for name, action in actions.items():
        target = agents_mod.AGENTS[name]
        if target.scope == "project":
            paths.append(agents_mod.config_path_for(target, root))
        if action == "migrate" and target.has_assets:
            assert target.assets_dest is not None
            dest = root.joinpath(*target.assets_dest)
            paths.append(dest)
            paths.extend(
                _asset_destinations(assets_mod.packaged_assets_for(target), dest)
            )
    if marker is not None:
        for rel in (PORTAL_ENV, PORTAL_JSON):
            paths += [root / rel, root / rel.with_name(rel.name + ".tmp")]
    return paths


def _asset_destinations(src, dest: Path) -> list[Path]:
    """Mirror a bundled asset tree onto ``dest`` without touching ``dest``."""
    out: list[Path] = []
    for entry in src.iterdir():
        if entry.is_dir():
            out.append(dest / entry.name)
            out.extend(_asset_destinations(entry, dest / entry.name))
        elif entry.is_file():
            out.append(dest / entry.name)
    return out


def _asset_outcomes(
    root: Path, agent: str, result: assets_mod.InstallResult
) -> list[FileOutcome]:
    """Translate an :class:`~whygraph.assets_mod.InstallResult` to outcomes."""
    out: list[FileOutcome] = []
    for paths, status in (
        (result.written, "write"),
        (result.overwritten, "overwrite"),
        (result.skipped, "skip"),
    ):
        for p in paths:
            rel = Path(p).relative_to(root).as_posix()
            out.append(FileOutcome(rel, status, agent))  # type: ignore[arg-type]
    return sorted(out, key=lambda o: o.file)


def _remove_stale_scan_lock(root: Path) -> None:
    """Delete a leftover 1.x ``.whygraph/scan.lock/`` (nothing reads it in 2.0).

    Done when the markers are written: from then on the hook helper asks
    the portal to scan and never takes the lock.
    """
    lock = root / ".whygraph" / "scan.lock"
    if lock.is_dir():
        shutil.rmtree(lock, ignore_errors=True)


def write_portal_marker(root: Path, marker: PortalMarker) -> None:
    """Write ``portal.env`` then ``portal.json``, each atomically.

    Parameters
    ----------
    root : Path
        Repository root. The caller has checked the marker paths.
    marker : PortalMarker
        The validated slug and port.
    """
    _atomic_write(root / PORTAL_ENV, f"slug={marker.slug}\nport={marker.port}\n")
    _atomic_write(
        root / PORTAL_JSON,
        json.dumps({"slug": marker.slug, "port": marker.port}) + "\n",
    )


def read_portal_marker(root: Path) -> tuple[PortalMarker | None, str | None]:
    """Read ``.whygraph/portal.json`` the way ``whygraph scan`` trusts it.

    Only the validated ``slug`` and ``port`` are returned (never free
    text), so a message built from them cannot carry anything else from
    the file. A marker that is a symlink, tracked by git (someone
    committed it) or malformed is ignored, with a warning.

    Parameters
    ----------
    root : Path
        Repository root.

    Returns
    -------
    tuple of (PortalMarker or None, str or None)
        The marker when the file exists and is valid, and a warning when
        a file exists but is ignored. ``(None, None)`` when there is no
        marker at all.
    """
    path = root / PORTAL_JSON
    if not os.path.lexists(path):
        return None, None
    rel = PORTAL_JSON.as_posix()
    try:
        check_inside(root, path)
    except UnsafePathError:
        return None, f"ignoring {rel}: it is a symbolic link"
    if agents_mod.is_git_tracked(root, rel):
        return (
            None,
            f"ignoring {rel}: it is tracked by git (it must never be committed)",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        slug, port = data["slug"], data["port"]
        if not isinstance(slug, str) or isinstance(port, bool):
            raise TypeError
        return PortalMarker(slug=slug, port=port), None
    except (OSError, UnicodeDecodeError, ValueError, TypeError, KeyError):
        return None, f"ignoring {rel}: it is not a valid portal marker"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


__all__ = [
    "GITIGNORE_ENTRIES",
    "HttpMcp",
    "InitializeResult",
    "PORTAL_ENV",
    "PORTAL_JSON",
    "PortalMarker",
    "initialize_project",
    "read_portal_marker",
    "write_portal_marker",
]
