// The four agents the portal can wire (`agents.py`), with what the Initialize
// step tells the user about each one (plan section 4.4.1).

export interface AgentInfo {
  id: "claude" | "cursor" | "vscode" | "codex";
  label: string;
  /** The MCP config file the portal writes, relative to the repo root. */
  file: string;
  /** How the entry behaves when the file is committed. */
  commit: string;
  /** A caveat worth seeing before initializing, if any. */
  note?: string;
}

export const AGENTS: AgentInfo[] = [
  {
    id: "claude",
    label: "Claude Code",
    file: ".mcp.json",
    commit: "Commit-safe: the URL reads the port from ${WHYGRAPH_PORT:-8765}.",
    note: "Claude Code asks you to approve the project's MCP server the first time it opens the repo.",
  },
  {
    id: "cursor",
    label: "Cursor",
    file: ".cursor/mcp.json",
    commit: "Writes the port literally (Cursor has no default for an unset variable).",
  },
  {
    id: "vscode",
    label: "VS Code / Copilot",
    file: ".vscode/mcp.json",
    commit: "Commit-safe: VS Code prompts for the port (default 8765).",
  },
  {
    id: "codex",
    label: "Codex",
    file: ".codex/config.toml",
    commit: "Writes the port literally (Codex documents no interpolation in url).",
    note: "Codex loads .codex/config.toml for trusted projects only - trust this project in Codex for the server to load.",
  },
];

export const agentInfo = (id: string): AgentInfo | undefined => AGENTS.find((a) => a.id === id);

/** Notes that depend on the combination of selected agents. */
export function selectionNotes(selected: readonly string[]): string[] {
  const notes: string[] = [];
  for (const a of AGENTS) {
    if (selected.includes(a.id) && a.note) notes.push(`${a.label}: ${a.note}`);
  }
  if (selected.includes("claude") && selected.includes("vscode")) {
    notes.push(
      "VS Code also reads the workspace .mcp.json, so with both Claude Code and VS Code selected the whygraph server shows up twice in VS Code. That is noisy, not broken.",
    );
  }
  return notes;
}
