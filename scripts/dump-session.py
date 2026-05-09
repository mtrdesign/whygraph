#!/usr/bin/env python3
"""Render a Claude Code session JSONL as readable markdown.

Usage:
  dump-session.py <path-to-session.jsonl> [agent_id]
  dump-session.py <path-to-session.jsonl> --list-agents

For a main session log: pass <session-uuid>.jsonl from
  ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl

For a subagent transcript: pass agent-<id>.jsonl from
  ~/.claude/projects/<encoded-cwd>/<session-uuid>/subagents/

`agent_id` filters a multi-agent session log to a single subagent.
Subagent JSONLs already contain only that one agent, so the filter
is a no-op there.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def render_block(b: dict) -> str:
    t = b.get("type")
    if t == "text":
        return b.get("text", "")
    if t == "thinking":
        return (
            "\n<details><summary>thinking</summary>\n\n```\n"
            + b.get("thinking", "")
            + "\n```\n</details>\n"
        )
    if t == "tool_use":
        return (
            f"\n**tool_use** `{b.get('name')}` (`{b.get('id','')}`)\n"
            f"```json\n{json.dumps(b.get('input', {}), indent=2)}\n```\n"
        )
    if t == "tool_result":
        c = b.get("content")
        if isinstance(c, list):
            c = "\n".join(x.get("text", "") for x in c if x.get("type") == "text")
        return (
            f"\n**tool_result** (`{b.get('tool_use_id','')}`)\n"
            f"```\n{c}\n```\n"
        )
    return f"\n_<{t}>_\n"


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    path = Path(sys.argv[1])
    filter_agent = sys.argv[2] if len(sys.argv) > 2 else None
    list_only = filter_agent == "--list-agents"

    agents: dict[str, list[dict]] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        ev = json.loads(line)
        if ev.get("type") not in ("user", "assistant"):
            continue
        aid = ev.get("agentId") or "main"
        agents.setdefault(aid, []).append(ev)

    if list_only:
        for aid, evs in agents.items():
            print(f"{aid}: {len(evs)} turns")
        return

    targets = [filter_agent] if filter_agent else list(agents.keys())
    for aid in targets:
        print(f"\n# Agent: {aid}\n")
        for ev in agents.get(aid, []):
            role = ev["message"].get("role", ev["type"])
            ts = ev.get("timestamp", "")
            sidechain = " [sidechain]" if ev.get("isSidechain") else ""
            print(f"\n---\n## {role}{sidechain} — {ts}\n")
            content = ev["message"].get("content")
            if isinstance(content, str):
                print(content)
            elif isinstance(content, list):
                for b in content:
                    print(render_block(b))


if __name__ == "__main__":
    main()
