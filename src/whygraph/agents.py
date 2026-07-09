"""Agent registration: where to wire ``whygraph-mcp`` for each LLM agent.

WhyGraph's MCP server is a standalone console script (``whygraph-mcp``,
declared in ``pyproject.toml``). To consume it, an LLM agent (Claude
Code, Cursor, VS Code / Copilot, Codex) needs an entry in its own MCP
configuration file. The location and format of that file vary by agent,
so this module centralises:

* the registry of supported agents and their config-file conventions
  (:data:`AGENTS`, :func:`resolve_agent`),
* the snippet an agent expects (:func:`render_snippet`), and
* a safe merge-write for project-scoped configs
  (:func:`write_snippet`).

All supported agents are **project-scoped** — we never write to
user-global locations (``~/.codex/``, ``~/.cursor/``, etc.). Agents
without a project-level config (e.g. Claude Desktop) are not supported.

Notes
-----
The launch command embedded in every snippet is just ``whygraph-mcp``
— no ``uv run``, no path resolution. This assumes the user installed
WhyGraph with ``uv tool install whygraph`` / ``pipx install whygraph``
so that the console script is on PATH.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import tomli_w

MCP_SERVER_NAME = "whygraph"
MCP_COMMAND = "whygraph-mcp"

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
        renderer :func:`render_snippet` uses.
    description : str
        Short one-line description shown in ``whygraph init --help``.
    assets_subdir : str or None
        Name of the source directory under ``src/whygraph/assets/`` that
        holds this agent's bundled asset tree, or ``None`` if the agent
        has no bundled assets. Used by :func:`whygraph.assets.install_assets`.
    assets_dest : tuple[str, ...] or None
        Path components of the destination directory (relative to the
        project root) where the asset tree is copied. ``()`` means "drop
        at the repo root". ``None`` mirrors :attr:`assets_subdir` —
        agent has no bundled assets.
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


def render_snippet(target: AgentTarget) -> str:
    """Render the registration snippet for ``target`` as a string.

    JSON snippets are pretty-printed with two-space indentation and a
    trailing newline. The TOML snippet is hand-rendered — it's small,
    fixed, and writing it by hand avoids pulling in a TOML writer
    dependency for the print-only path.

    Parameters
    ----------
    target : AgentTarget
        The agent whose snippet format to render.

    Returns
    -------
    str
        The snippet, ready to print or write.
    """
    if target.format == "json":
        payload = {
            "mcpServers": {
                MCP_SERVER_NAME: {"command": MCP_COMMAND},
            }
        }
        return json.dumps(payload, indent=2) + "\n"
    return f'[mcp_servers.{MCP_SERVER_NAME}]\ncommand = "{MCP_COMMAND}"\n'


def write_snippet(target: AgentTarget, project_root: Path) -> Path:
    """Merge the WhyGraph MCP entry into ``target``'s config file.

    Only valid for project-scoped targets — user-scoped paths are out
    of scope (see the module docstring). Supports both JSON and TOML
    formats; the branch is chosen by ``target.format``.

    Behavior (identical across formats):

    * If the file does not exist, write a minimal config containing
      only the WhyGraph entry.
    * If the file exists and parses, the WhyGraph entry is added/
      replaced under ``mcpServers`` (JSON) or ``[mcp_servers]`` (TOML);
      other servers and top-level keys are preserved.
    * If the file exists but is unparseable, a fresh minimal config
      replaces it. This is a conscious trade-off: we surface the new
      config rather than refuse to proceed. Users who'd rather merge by
      hand can render the snippet with ``render_snippet()`` instead.

    Parameters
    ----------
    target : AgentTarget
        The agent to wire.
    project_root : Path
        Repository root used to anchor project-scoped paths.

    Returns
    -------
    Path
        The absolute path that was written.

    Raises
    ------
    ValueError
        If ``target`` is user-scoped.

    Notes
    -----
    Comments and incidental formatting in the existing config file are
    not preserved across the read-modify-write cycle (``tomllib`` strips
    comments on parse; ``json.load`` collapses whitespace). Users who
    care about preserving their hand-formatted config should render the
    snippet with ``render_snippet()`` and merge it manually.
    """
    if target.scope != "project":
        raise ValueError(
            f"agent {target.name!r} is user-scoped; use render_snippet() instead"
        )

    path = config_path_for(target, project_root)
    if target.format == "json":
        return _write_json_snippet(path)
    return _write_toml_snippet(path)


def _write_json_snippet(path: Path) -> Path:
    """JSON-formatted ``mcpServers`` merge for ``.mcp.json`` / ``.cursor/mcp.json`` / ``.vscode/mcp.json``."""
    existing: dict = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                existing = loaded
        except (json.JSONDecodeError, OSError):
            existing = {}

    servers = existing.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[MCP_SERVER_NAME] = {"command": MCP_COMMAND}
    existing["mcpServers"] = servers

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
        f.write("\n")
    return path


def _write_toml_snippet(path: Path) -> Path:
    """TOML-formatted ``[mcp_servers.*]`` merge for ``.codex/config.toml``."""
    existing: dict = {}
    if path.exists():
        try:
            with path.open("rb") as f:
                loaded = tomllib.load(f)
            if isinstance(loaded, dict):
                existing = loaded
        except (tomllib.TOMLDecodeError, OSError):
            existing = {}

    servers = existing.get("mcp_servers")
    if not isinstance(servers, dict):
        servers = {}
    servers[MCP_SERVER_NAME] = {"command": MCP_COMMAND}
    existing["mcp_servers"] = servers

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        tomli_w.dump(existing, f)
    return path


def is_write_supported(target: AgentTarget) -> bool:
    """Return ``True`` if :func:`write_snippet` accepts ``target``.

    Convenience for callers that need to branch on print-vs-write
    without catching :class:`ValueError`. All project-scoped targets
    (JSON or TOML) are writeable.
    """
    return target.scope == "project"


__all__ = [
    "AGENTS",
    "AgentTarget",
    "MCP_COMMAND",
    "MCP_SERVER_NAME",
    "UnknownAgentError",
    "config_path_for",
    "is_write_supported",
    "known_agent_names",
    "render_snippet",
    "resolve_agent",
    "write_snippet",
]
