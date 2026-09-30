"""Agent registration: where to wire the WhyGraph MCP server for each LLM agent.

The WhyGraph portal serves each project's MCP endpoint over HTTP
(``/mcp/<slug>``). To consume it, an LLM agent (Claude Code, Cursor,
VS Code / Copilot, Codex) needs an entry in its own MCP configuration
file. The location and format of that file vary by agent, so this module
centralises:

* the registry of supported agents and their config-file conventions
  (:data:`AGENTS`, :func:`resolve_agent`), and
* the HTTP entry itself (:class:`HttpMcp`, :func:`render_http_snippet`,
  :func:`apply_http_entry`, :func:`remove_entry`,
  :func:`detect_entries`), which is careful with existing files: it
  refuses what it cannot parse, backs up before rewriting, and asks
  before touching a git-tracked file.

All supported agents are **project-scoped** — we never write to
user-global locations (``~/.codex/``, ``~/.cursor/``, etc.). Agents
without a project-level config (e.g. Claude Desktop) are not supported.

Notes
-----
The 1.x stdio entry (``{"command": ...}``) is no longer written (removed
in 2.0.0); :func:`detect_entries` still reports one, as ``stdio``, so the
portal can offer to migrate or remove it.
"""

from __future__ import annotations

import copy
import difflib
import json
import subprocess
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import tomli_w

MCP_SERVER_NAME = "whygraph"

Scope = Literal["project", "user"]
Format = Literal["json", "toml"]


class UnknownAgentError(ValueError):
    """Raised when a caller asks for an agent name that isn't registered."""


@dataclass(frozen=True, slots=True)
class AgentTarget:
    """Where and how to register the WhyGraph MCP server for one agent.

    Attributes
    ----------
    name : str
        Canonical agent id (e.g. ``"claude"``, ``"cursor"``).
    aliases : tuple[str, ...]
        Alternate names that resolve to this target (e.g. ``"copilot"``
        is an alias of ``"vscode"``).
    relative_path : tuple[str, ...]
        Path components of the config file relative to the *anchor*
        directory implied by :attr:`scope` (the project root for
        ``"project"``, ``Path.home()`` for ``"user"``). Stored as a
        tuple so resolution is portable across operating systems.
    scope : {"project", "user"}
        Whether the config file lives inside the repo or under the
        user's home directory.
    format : {"json", "toml"}
        Serialization format of the target file. Determines which
        renderer :func:`render_http_snippet` uses.
    description : str
        Short one-line description of the agent and its config file.
    assets_subdir : str or None
        Name of the source directory under ``src/whygraph/assets/`` that
        holds this agent's bundled asset tree, or ``None`` if the agent
        has no bundled assets. Used by :func:`whygraph.assets.install_assets`.
    assets_dest : tuple[str, ...] or None
        Path components of the destination directory (relative to the
        project root) where the asset tree is copied. ``()`` means "drop
        at the repo root". ``None`` mirrors :attr:`assets_subdir` —
        agent has no bundled assets.
    servers_key : str
        Top-level key holding the server table in the config file.
        VS Code's ``.vscode/mcp.json`` uses ``servers`` and rejects
        ``mcpServers``.
    assets_merge_files : tuple[str, ...]
        Paths (relative to :attr:`assets_dest`) of files that should be
        **append-merged** rather than skip-or-overwritten. The installer
        wraps the bundled body in ``<!-- BEGIN whygraph --> ... <!-- END
        whygraph -->`` markers, replacing the block in-place on re-run
        (idempotent) or appending after any existing user content.
        Empty default — only agents with repo-shared instruction files
        (e.g. ``copilot-instructions.md``, ``AGENTS.md``) populate this.
    """

    name: str
    aliases: tuple[str, ...]
    relative_path: tuple[str, ...]
    scope: Scope
    format: Format
    description: str
    assets_subdir: str | None = None
    assets_dest: tuple[str, ...] | None = None
    assets_merge_files: tuple[str, ...] = ()
    servers_key: str = "mcpServers"

    @property
    def has_assets(self) -> bool:
        """Return ``True`` if this agent has a bundled asset tree to install.

        Both :attr:`assets_subdir` and :attr:`assets_dest` must be
        configured. Callers should branch on this before invoking
        :func:`whygraph.assets.install_assets`.
        """
        return self.assets_subdir is not None and self.assets_dest is not None


_CLAUDE = AgentTarget(
    name="claude",
    aliases=(),
    relative_path=(".mcp.json",),
    scope="project",
    format="json",
    description="Claude Code (project-scoped .mcp.json at repo root)",
    assets_subdir="claude-code",
    assets_dest=(".claude",),
    assets_merge_files=("CLAUDE.md",),
)

_CURSOR = AgentTarget(
    name="cursor",
    aliases=(),
    relative_path=(".cursor", "mcp.json"),
    scope="project",
    format="json",
    description="Cursor (.cursor/mcp.json at repo root)",
    assets_subdir="cursor",
    assets_dest=(".cursor",),
)

_VSCODE = AgentTarget(
    name="vscode",
    aliases=("copilot",),
    relative_path=(".vscode", "mcp.json"),
    scope="project",
    format="json",
    description="VS Code / GitHub Copilot (.vscode/mcp.json + .github/ asset tree)",
    assets_subdir="vscode",
    assets_dest=(".github",),
    assets_merge_files=("copilot-instructions.md",),
    servers_key="servers",
)

_CODEX = AgentTarget(
    name="codex",
    aliases=(),
    relative_path=(".codex", "config.toml"),
    scope="project",
    format="toml",
    description="OpenAI Codex (.codex/config.toml + AGENTS.md + .codex/agents/ tree)",
    assets_subdir="codex",
    assets_dest=(),
    assets_merge_files=("AGENTS.md",),
    servers_key="mcp_servers",
)


AGENTS: dict[str, AgentTarget] = {
    t.name: t for t in (_CLAUDE, _CURSOR, _VSCODE, _CODEX)
}


def known_agent_names() -> list[str]:
    """Return all agent names and aliases that :func:`resolve_agent` accepts.

    Returns
    -------
    list[str]
        Sorted list of canonical names + aliases. Suitable for use as
        ``click.Choice(known_agent_names())``.
    """
    names: set[str] = set()
    for target in AGENTS.values():
        names.add(target.name)
        names.update(target.aliases)
    return sorted(names)


def resolve_agent(name: str) -> AgentTarget:
    """Look up an :class:`AgentTarget` by canonical name or alias.

    Parameters
    ----------
    name : str
        Agent identifier as supplied by the user. Case-insensitive.

    Returns
    -------
    AgentTarget
        The matching target.

    Raises
    ------
    UnknownAgentError
        If ``name`` is neither a canonical name nor an alias.
    """
    needle = name.strip().lower()
    for target in AGENTS.values():
        if needle == target.name or needle in target.aliases:
            return target
    raise UnknownAgentError(name)


def config_path_for(target: AgentTarget, project_root: Path) -> Path:
    """Resolve the absolute config-file path for ``target``.

    Parameters
    ----------
    target : AgentTarget
        The agent whose config path is wanted.
    project_root : Path
        Repository root, used as the anchor for project-scoped targets.
        Ignored for user-scoped targets.

    Returns
    -------
    Path
        Absolute path to the config file (whether or not it currently
        exists).
    """
    anchor = project_root if target.scope == "project" else Path.home()
    return anchor.joinpath(*target.relative_path)


# ---------------------------------------------------------------------------
# The HTTP entry (the WhyGraph portal)
# ---------------------------------------------------------------------------

DEFAULT_PORTAL_PORT = 8765
"""The portal's default port, baked into env-interpolated agent URLs."""

_VSCODE_PORT_INPUT_ID = "whygraph-port"

AgentAction = Literal["migrate", "remove"]
FileStatus = Literal["write", "overwrite", "skip", "refused", "needs_confirmation"]


@dataclass(frozen=True, slots=True)
class HttpMcp:
    """How agents reach one project's MCP endpoint on the portal.

    Attributes
    ----------
    slug : str
        Project slug; the endpoint is ``/mcp/<slug>``.
    port : int
        The portal's port. Agents that cannot interpolate a default
        (Cursor, Codex) get it literally; the others use it as the
        default of their interpolation.
    host : str
        Host the portal listens on, as seen by the agent.
    bearer_env_var : str or None
        Reserved for per-agent auth headers (M2). Unused in M1, and kept
        here so adding auth needs no signature change.
    """

    slug: str
    port: int = DEFAULT_PORTAL_PORT
    host: str = "127.0.0.1"
    bearer_env_var: str | None = None

    def url_for(self, agent: AgentTarget | str) -> str:
        """Return the MCP URL in ``agent``'s form.

        Parameters
        ----------
        agent : AgentTarget or str
            The agent (or its name / alias).

        Returns
        -------
        str
            ``http://<host>:<port form>/mcp/<slug>`` where the port form
            is ``${WHYGRAPH_PORT:-<port>}`` for Claude Code,
            ``${input:whygraph-port}`` for VS Code and the literal port
            for Cursor and Codex.
        """
        target = agent if isinstance(agent, AgentTarget) else resolve_agent(agent)
        if target.name == "claude":
            port = f"${{WHYGRAPH_PORT:-{self.port}}}"
        elif target.name == "vscode":
            port = f"${{input:{_VSCODE_PORT_INPUT_ID}}}"
        else:
            port = str(self.port)
        return f"http://{self.host}:{port}/mcp/{self.slug}"


@dataclass(frozen=True, slots=True)
class FileOutcome:
    """What one file write did (or, in a dry run, would do).

    Attributes
    ----------
    file : str
        Path relative to the project root, with ``/`` separators.
    status : {"write", "overwrite", "skip", "refused", "needs_confirmation"}
        ``write`` creates the file, ``overwrite`` changes an existing
        one, ``skip`` leaves it alone (already up to date, or an asset
        that exists without ``force``), ``refused`` means the file could
        not be merged safely and was not touched, and
        ``needs_confirmation`` means it is git-tracked and its path is
        not in ``confirm_tracked``.
    agent : str or None
        Canonical agent name the file belongs to, when it has one.
    reason : str or None
        Why the file was skipped or refused.
    snippet : str or None
        For ``refused``: the exact entry to paste by hand.
    diff : str or None
        For ``overwrite`` and ``needs_confirmation``: a unified diff.
    """

    file: str
    status: FileStatus
    agent: str | None = None
    reason: str | None = None
    snippet: str | None = None
    diff: str | None = None


@dataclass(frozen=True, slots=True)
class DetectedEntry:
    """An existing ``whygraph`` MCP entry found in an agent config file.

    Attributes
    ----------
    agent : str
        Canonical agent name.
    file : str
        Config path relative to the project root.
    key : str
        Dotted location, e.g. ``mcpServers.whygraph``.
    transport : {"stdio", "http", "unknown"}
        ``stdio`` for a ``command`` entry (a 1.x one), ``http`` for a
        ``url`` entry.
    stale : bool
        ``True`` for the 1.x ``mcpServers.whygraph`` in
        ``.vscode/mcp.json``, which VS Code never read.
    """

    agent: str
    file: str
    key: str
    transport: Literal["stdio", "http", "unknown"]
    stale: bool = False


class _Unmergeable(Exception):
    """An existing config that must not be rewritten; carries the reason."""


def _http_entry(target: AgentTarget, mcp: HttpMcp) -> dict:
    """Build the per-agent HTTP server entry."""
    entry: dict = {"url": mcp.url_for(target)}
    if target.name in ("claude", "vscode"):
        entry = {"type": "http", **entry}
    return entry


def _vscode_port_input(mcp: HttpMcp) -> dict:
    """Build the VS Code ``inputs`` entry that prompts for the port."""
    return {
        "id": _VSCODE_PORT_INPUT_ID,
        "type": "promptString",
        "description": "WhyGraph portal port",
        "default": str(mcp.port),
    }


def render_http_snippet(target: AgentTarget, mcp: HttpMcp) -> str:
    """Render the HTTP registration snippet for ``target``.

    Parameters
    ----------
    target : AgentTarget
        The agent whose format to render.
    mcp : HttpMcp
        The endpoint to point at.

    Returns
    -------
    str
        The snippet, ready to print or paste. VS Code's includes the
        ``inputs`` entry its port interpolation needs.
    """
    entry = _http_entry(target, mcp)
    if target.format == "toml":
        return (
            f"[{target.servers_key}.{MCP_SERVER_NAME}]\n"
            f"url = {json.dumps(entry['url'])}\n"
        )
    payload: dict = {}
    if target.name == "vscode":
        payload["inputs"] = [_vscode_port_input(mcp)]
    payload[target.servers_key] = {MCP_SERVER_NAME: entry}
    return json.dumps(payload, indent=2) + "\n"


def _rel(target: AgentTarget) -> str:
    """Config path relative to the project root, ``/``-separated."""
    return "/".join(target.relative_path)


def _load_config(target: AgentTarget, path: Path, *, strict_toml: bool) -> dict | None:
    """Parse ``path``: ``None`` when absent, ``_Unmergeable`` when unusable.

    ``strict_toml`` additionally refuses TOML containing any ``#`` (a
    deliberately conservative comment test): ``tomli_w`` would drop the
    comments on rewrite.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _Unmergeable(f"cannot read the file ({exc})") from exc
    if target.format == "json":
        if not text.strip():
            return {}
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _Unmergeable(
                f"not valid JSON ({exc.msg}); JSON with comments is not supported"
            ) from exc
        if not isinstance(loaded, dict):
            raise _Unmergeable("the top level is not a JSON object")
        return loaded
    if strict_toml and "#" in text:
        raise _Unmergeable("contains a comment; rewriting it would drop the comment")
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise _Unmergeable(f"not valid TOML ({exc})") from exc


def _servers_table(data: dict, key: str) -> dict:
    """Return ``data[key]`` as a dict (empty when absent); refuse a non-table."""
    table = data.get(key)
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise _Unmergeable(f"`{key}` is not a table / object")
    return table


def _drop_stale_vscode(data: dict) -> bool:
    """Delete the 1.x ``mcpServers.whygraph`` VS Code never read."""
    stale = data.get("mcpServers")
    if not isinstance(stale, dict) or MCP_SERVER_NAME not in stale:
        return False
    del stale[MCP_SERVER_NAME]
    if not stale:
        del data["mcpServers"]
    return True


def _merge_http(target: AgentTarget, data: dict, mcp: HttpMcp) -> dict:
    """Return a copy of ``data`` with the HTTP entry merged in."""
    out = copy.deepcopy(data)
    table = _servers_table(out, target.servers_key)
    table[MCP_SERVER_NAME] = _http_entry(target, mcp)
    out[target.servers_key] = table
    if target.name != "vscode":
        return out

    _drop_stale_vscode(out)
    inputs = out.get("inputs")
    if inputs is None:
        inputs = []
    if not isinstance(inputs, list):
        raise _Unmergeable("`inputs` is not a list")
    wanted = _vscode_port_input(mcp)
    for i, item in enumerate(inputs):
        if isinstance(item, dict) and item.get("id") == _VSCODE_PORT_INPUT_ID:
            inputs[i] = wanted
            break
    else:
        inputs.append(wanted)
    if "inputs" in out:
        out["inputs"] = inputs
        return out
    return {"inputs": inputs, **out}


def _dump(target: AgentTarget, data: dict) -> str:
    """Serialize ``data`` in the target's format."""
    if target.format == "json":
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return tomli_w.dumps(data)


def is_git_tracked(project_root: Path, rel: str) -> bool:
    """Return ``True`` when ``rel`` is tracked by git.

    Uses ``git ls-files --error-unmatch``.

    Parameters
    ----------
    project_root : Path
        Repository root; git runs from here.
    rel : str
        Path relative to ``project_root``.

    Returns
    -------
    bool
        ``False`` when the file is untracked, the directory is not a
        repository, or ``git`` is unavailable.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", rel],
            cwd=project_root,
            capture_output=True,
        )
    except OSError:
        return False
    return proc.returncode == 0


def _backup(project_root: Path, rel: str, path: Path) -> Path:
    """Copy ``path`` to ``.whygraph/backups/<flattened rel>.<timestamp>``.

    The relative path is flattened (``/`` becomes ``__``) because
    ``.cursor/mcp.json`` and ``.vscode/mcp.json`` share a file name.
    """
    backups = project_root / ".whygraph" / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    dest = backups / f"{rel.replace('/', '__')}.{stamp}"
    dest.write_bytes(path.read_bytes())
    return dest


def _unified_diff(rel: str, old: str, new: str) -> str:
    """Return a unified diff of ``old`` -> ``new`` labelled with ``rel``."""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
    )


def _commit_change(
    target: AgentTarget,
    project_root: Path,
    new_data: dict,
    existing: dict | None,
    *,
    confirm_tracked: Iterable[str | Path],
    dry_run: bool,
) -> FileOutcome:
    """Apply ``new_data`` to the config file under the shared safety rules.

    Unchanged content is a ``skip`` (parsed comparison, so a committed
    file is never merely reformatted); a tracked file needs its path in
    ``confirm_tracked``; an existing file is backed up before it is
    rewritten; ``dry_run`` reports without touching anything.
    """
    rel = _rel(target)
    path = config_path_for(target, project_root)
    if existing is not None and existing == new_data:
        return FileOutcome(rel, "skip", target.name, reason="already up to date")

    new_text = _dump(target, new_data)
    diff: str | None = None
    status: FileStatus = "write"
    if existing is not None:
        status = "overwrite"
        diff = _unified_diff(rel, path.read_text(encoding="utf-8"), new_text)
        confirmed = {Path(p).as_posix() for p in confirm_tracked}
        if rel not in confirmed and is_git_tracked(project_root, rel):
            return FileOutcome(
                rel,
                "needs_confirmation",
                target.name,
                reason="tracked by git; confirm to rewrite it",
                diff=diff,
            )
    if not dry_run:
        if existing is not None:
            _backup(project_root, rel, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text, encoding="utf-8")
    return FileOutcome(rel, status, target.name, diff=diff)


def apply_http_entry(
    target: AgentTarget,
    project_root: Path,
    mcp: HttpMcp,
    *,
    confirm_tracked: Iterable[str | Path] = (),
    dry_run: bool = False,
) -> FileOutcome:
    """Merge the HTTP ``whygraph`` entry into ``target``'s config file.

    A file that cannot be parsed (JSON with comments included, TOML
    containing any comment) is **refused**, never replaced; the outcome carries the snippet to paste by hand. Other
    servers and keys are preserved. For VS Code the entry goes under
    ``servers``, its port ``inputs`` entry is added, and a stale 1.x
    ``mcpServers.whygraph`` is dropped.

    Parameters
    ----------
    target : AgentTarget
        The agent to wire. Must be project-scoped.
    project_root : Path
        Repository root.
    mcp : HttpMcp
        The endpoint to register.
    confirm_tracked : Iterable[str or Path]
        Relative paths of git-tracked files the caller has confirmed
        may be rewritten.
    dry_run : bool, default False
        Report the outcome without writing (or backing up) anything.

    Returns
    -------
    FileOutcome
        What happened to the file.

    Raises
    ------
    ValueError
        If ``target`` is user-scoped.
    """
    if target.scope != "project":
        raise ValueError(f"agent {target.name!r} is user-scoped")
    path = config_path_for(target, project_root)
    try:
        existing = _load_config(target, path, strict_toml=True)
        new_data = _merge_http(target, existing or {}, mcp)
    except _Unmergeable as exc:
        return FileOutcome(
            _rel(target),
            "refused",
            target.name,
            reason=str(exc),
            snippet=render_http_snippet(target, mcp),
        )
    return _commit_change(
        target,
        project_root,
        new_data,
        existing,
        confirm_tracked=confirm_tracked,
        dry_run=dry_run,
    )


def remove_entry(
    target: AgentTarget,
    project_root: Path,
    *,
    confirm_tracked: Iterable[str | Path] = (),
    dry_run: bool = False,
) -> FileOutcome:
    """Strip the ``whygraph`` entry (and only it) from ``target``'s config.

    Follows the same parse, backup and git-tracked rules as
    :func:`apply_http_entry`. For VS Code the stale 1.x
    ``mcpServers.whygraph`` is stripped too.

    Parameters
    ----------
    target : AgentTarget
        The agent whose entry to remove. Must be project-scoped.
    project_root : Path
        Repository root.
    confirm_tracked : Iterable[str or Path]
        Relative paths of tracked files that may be rewritten.
    dry_run : bool, default False
        Report without writing.

    Returns
    -------
    FileOutcome
        ``skip`` when there was nothing to remove.

    Raises
    ------
    ValueError
        If ``target`` is user-scoped.
    """
    if target.scope != "project":
        raise ValueError(f"agent {target.name!r} is user-scoped")
    rel = _rel(target)
    path = config_path_for(target, project_root)
    try:
        existing = _load_config(target, path, strict_toml=True)
    except _Unmergeable as exc:
        return FileOutcome(rel, "refused", target.name, reason=str(exc))
    if existing is None:
        return FileOutcome(rel, "skip", target.name, reason="file does not exist")

    new_data = copy.deepcopy(existing)
    table = new_data.get(target.servers_key)
    removed = isinstance(table, dict) and table.pop(MCP_SERVER_NAME, None) is not None
    if target.name == "vscode":
        removed = _drop_stale_vscode(new_data) or removed
    if not removed:
        return FileOutcome(rel, "skip", target.name, reason="no whygraph entry")
    return _commit_change(
        target,
        project_root,
        new_data,
        existing,
        confirm_tracked=confirm_tracked,
        dry_run=dry_run,
    )


def detect_entries(project_root: Path) -> list[DetectedEntry]:
    """Find every existing ``whygraph`` entry across the four agent files.

    Reads ``mcpServers.whygraph`` (Claude Code, Cursor),
    ``servers.whygraph`` and the stale 1.x ``mcpServers.whygraph``
    (VS Code) and ``[mcp_servers.whygraph]`` (Codex). Read-only; a file
    that cannot be parsed reports nothing.

    Parameters
    ----------
    project_root : Path
        Repository root.

    Returns
    -------
    list[DetectedEntry]
        One item per entry found, in agent registry order.
    """
    found: list[DetectedEntry] = []
    for target in AGENTS.values():
        try:
            data = _load_config(
                target, config_path_for(target, project_root), strict_toml=False
            )
        except _Unmergeable:
            continue
        if not data:
            continue
        locations = [(target.servers_key, False)]
        if target.name == "vscode":
            locations.append(("mcpServers", True))
        for key, stale in locations:
            table = data.get(key)
            entry = table.get(MCP_SERVER_NAME) if isinstance(table, dict) else None
            if entry is None:
                continue
            transport: Literal["stdio", "http", "unknown"] = "unknown"
            if isinstance(entry, dict):
                if "command" in entry:
                    transport = "stdio"
                elif "url" in entry:
                    transport = "http"
            found.append(
                DetectedEntry(
                    agent=target.name,
                    file=_rel(target),
                    key=f"{key}.{MCP_SERVER_NAME}",
                    transport=transport,
                    stale=stale,
                )
            )
    return found


__all__ = [
    "AGENTS",
    "AgentAction",
    "AgentTarget",
    "DEFAULT_PORTAL_PORT",
    "DetectedEntry",
    "FileOutcome",
    "FileStatus",
    "HttpMcp",
    "MCP_SERVER_NAME",
    "UnknownAgentError",
    "apply_http_entry",
    "config_path_for",
    "detect_entries",
    "is_git_tracked",
    "known_agent_names",
    "remove_entry",
    "render_http_snippet",
    "resolve_agent",
]
