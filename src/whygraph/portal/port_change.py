"""Follow a portal port change into the repos it manages (plan sections 4.8, 4.13).

The port is baked into each initialized repo's markers
(``.whygraph/portal.json`` / ``.env``, read by ``whygraph scan`` and the
hook helper) and into agent MCP configs. When the portal starts on a new
port, :func:`reconcile_port` runs once, before the portal serves requests:

* **Markers** of every mounted, initialized project whose ``portal.json``
  port differs are rewritten (both files). They are untracked by
  construction (``.whygraph/`` is gitignored first), and a marker reached
  through a symlink is left alone.
* **Agent configs** of those projects, by the section 4.8 rules: an
  env-interpolated entry (Claude Code ``${WHYGRAPH_PORT:-...}``, VS Code's
  port prompt) is never rewritten - the report says what to export or
  enter instead; a literal entry (Cursor, Codex) is rewritten, only the
  ``whygraph`` key, when the file is **untracked**; a **tracked** (or
  unparseable) file is never rewritten automatically - the report carries
  the exact line to change.
* **Unmounted** roots cannot be checked; when the port differs from the
  one recorded at the previous start (``<data>/portal.port``) they are
  listed so the UI can say they still point at the old port.

The result is kept on :attr:`PortalState.port_change` and shown by
``GET /api/portal/state`` (and per project by ``GET /api/projects/{slug}``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlmodel import col, select

from whygraph import agents as agents_mod
from whygraph.agents import HttpMcp
from whygraph.core.safe_paths import UnsafePathError, check_inside
from whygraph.project_setup import (
    PORTAL_ENV,
    PORTAL_JSON,
    PortalMarker,
    read_portal_marker,
    write_portal_marker,
)

from .context import resolve_root
from .db import get_session
from .models import Project, ProjectAgent
from .paths import BACKUPS_DIR
from .repos import root_status

_log = logging.getLogger(__name__)

LAST_PORT_FILE = "portal.port"
"""File in the data dir holding the port of the previous start."""

_LITERAL_AGENTS = ("cursor", "codex")
"""Agents whose entry carries the port literally (rewritten when untracked)."""


def reconcile_port(data_dir: Path, port: int, agent_host: str) -> dict | None:
    """Rewrite markers and literal agent entries for a new portal port (blocking).

    Parameters
    ----------
    data_dir : Path
        The portal data directory (holds :data:`LAST_PORT_FILE`).
    port : int
        The port the portal is starting on.
    agent_host : str
        The host agents connect to (``PortalOrigins.agent_host``).

    Returns
    -------
    dict or None
        ``None`` when nothing changed and there is nothing to report,
        else ``{"port", "previous_port", "projects": [...], "unmounted":
        [...]}``. Each project item is ``{"project_id", "org_id", "slug",
        "root", "previous_port", "markers": "rewritten" | "skipped",
        "reason"?, "agents": [...]}`` and each unmounted item ``{"project_id",
        "org_id", "slug", "root"}`` (a slug is unique only within an org);
        each agent item is ``{"agent", "file", "action", ...}`` with
        ``action`` one of ``rewritten``, ``up_to_date``, ``env`` (no
        rewrite needed; ``hint`` says what to do), ``manual`` (tracked or
        unparseable; ``line`` is the entry to set, plus ``diff`` or
        ``snippet``) or ``skipped`` (``reason``).
    """
    previous = _read_last_port(data_dir)
    projects: list[dict] = []
    unmounted: list[dict] = []
    with get_session() as session:
        rows = session.exec(
            select(Project).where(col(Project.initialized_at).is_not(None))
        ).all()
        work = []
        for row in rows:
            agents = [
                a.agent
                for a in session.exec(
                    select(ProjectAgent).where(ProjectAgent.project_id == row.id)
                ).all()
            ]
            work.append(
                (row.id, row.org_id, row.slug, resolve_root(row), sorted(agents))
            )

    for project_id, org_id, slug, root, agents in work:
        ids = {"project_id": project_id, "org_id": org_id}
        if root_status(root) != "ok":
            unmounted.append({**ids, "slug": slug, "root": str(root)})
            continue
        marker, _warning = read_portal_marker(root)
        if marker is None or marker.port == port:
            continue
        projects.append(
            {**ids, **_reconcile_project(slug, root, agents, marker, port, agent_host)}
        )

    port_changed = previous is not None and previous != port
    _write_last_port(data_dir, port)
    if not projects and not port_changed:
        return None
    report = {
        "port": port,
        "previous_port": previous,
        "projects": projects,
        "unmounted": unmounted if port_changed or projects else [],
    }
    _log.info(
        "portal port %s: updated %d project(s), %d unmounted",
        port,
        len(projects),
        len(report["unmounted"]),
    )
    return report


def _reconcile_project(
    slug: str,
    root: Path,
    agents: list[str],
    old: PortalMarker,
    port: int,
    agent_host: str,
) -> dict:
    """Rewrite one mounted project's markers and agent entries."""
    item: dict = {
        "slug": slug,
        "root": str(root),
        "previous_port": old.port,
        "markers": "rewritten",
        "agents": [],
    }
    try:
        for rel in (PORTAL_ENV, PORTAL_JSON):
            check_inside(root, rel)
            check_inside(root, rel.with_name(rel.name + ".tmp"))
        write_portal_marker(root, PortalMarker(slug=slug, port=port))
    except (UnsafePathError, OSError) as exc:
        item["markers"] = "skipped"
        item["reason"] = str(exc)

    mcp = HttpMcp(slug=slug, port=port, host=agent_host)
    for name in agents:
        target = agents_mod.AGENTS.get(name)
        if target is None:
            continue
        item["agents"].append(_reconcile_agent(target, root, mcp))
    return item


def _reconcile_agent(target: agents_mod.AgentTarget, root: Path, mcp: HttpMcp) -> dict:
    """Apply the section 4.8 rules to one agent config file."""
    rel = "/".join(target.relative_path)
    entry: dict = {"agent": target.name, "file": rel}
    if target.name not in _LITERAL_AGENTS:
        entry["action"] = "env"
        entry["hint"] = (
            f"export WHYGRAPH_PORT={mcp.port} for your editor"
            if target.name == "claude"
            else f"enter {mcp.port} when VS Code asks for the WhyGraph portal port"
        )
        return entry
    try:
        check_inside(root, rel)
        check_inside(root, BACKUPS_DIR)
    except UnsafePathError as exc:
        entry.update(action="skipped", reason=str(exc))
        return entry
    if not (root / rel).is_file():
        entry.update(action="skipped", reason="file does not exist")
        return entry
    line = _entry_line(target, mcp)
    try:
        outcome = agents_mod.apply_http_entry(target, root, mcp)
    except OSError as exc:
        entry.update(action="skipped", reason=str(exc))
        return entry
    if outcome.status in ("write", "overwrite"):
        entry["action"] = "rewritten"
    elif outcome.status == "skip":
        entry["action"] = "up_to_date"
    else:  # needs_confirmation (tracked) or refused (unparseable)
        entry.update(action="manual", line=line, reason=outcome.reason)
        if outcome.diff:
            entry["diff"] = outcome.diff
        if outcome.snippet:
            entry["snippet"] = outcome.snippet
    return entry


def _entry_line(target: agents_mod.AgentTarget, mcp: HttpMcp) -> str:
    """The one line of ``target``'s config that carries the URL."""
    url = json.dumps(mcp.url_for(target))
    return f"url = {url}" if target.format == "toml" else f'"url": {url}'


def _read_last_port(data_dir: Path) -> int | None:
    try:
        text = (data_dir / LAST_PORT_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _write_last_port(data_dir: Path, port: int) -> None:
    try:
        (data_dir / LAST_PORT_FILE).write_text(f"{port}\n", encoding="utf-8")
    except OSError:
        _log.warning("could not record the portal port in %s", data_dir)


__all__ = ["LAST_PORT_FILE", "reconcile_port"]
