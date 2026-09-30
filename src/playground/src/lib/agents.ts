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

/**
 * The entry to paste into an agent's MCP config to connect to a project, derived
 * from the project's MCP URL. It mirrors `agents.render_http_snippet` (the same
 * per-agent port forms as the files Initialize writes): Claude Code and VS Code
 * interpolate the port, Cursor and Codex get it literally.
 */
export function mcpSnippet(agent: AgentInfo["id"], mcpUrl: string): string {
  let host = "127.0.0.1";
  let port = "8765";
  let path = "/mcp";
  try {
    const url = new URL(mcpUrl);
    host = url.hostname;
    port = url.port || (url.protocol === "https:" ? "443" : "80");
    path = url.pathname;
  } catch {
    // Fall through with the defaults; the snippet is still well-formed.
  }
  const at = (p: string) => `http://${host}:${p}${path}`;
  const json = (value: unknown) => JSON.stringify(value, null, 2) + "\n";
  switch (agent) {
    case "claude":
      return json({
        mcpServers: { whygraph: { type: "http", url: at(`\${WHYGRAPH_PORT:-${port}}`) } },
      });
    case "cursor":
      return json({ mcpServers: { whygraph: { url: at(port) } } });
    case "vscode":
      return json({
        inputs: [
          { id: "whygraph-port", type: "promptString", description: "WhyGraph portal port", default: port },
        ],
        servers: { whygraph: { type: "http", url: at("${input:whygraph-port}") } },
      });
    case "codex":
      return `[mcp_servers.whygraph]\nurl = ${JSON.stringify(at(port))}\n`;
  }
}
