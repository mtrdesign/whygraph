// The four agents the portal can wire (`agents.py`), with what the Set up step
// tells the user about each one (M2f-3 plan section 4.9: plain words, the real port).

export interface AgentInfo {
  id: "claude" | "cursor" | "vscode" | "codex";
  label: string;
  /** The MCP config file the portal writes, relative to the repo root. */
  file: string;
  /** Everything Initialize adds for the agent, in plain words (IMP-10). */
  adds: string;
  /** How the entry finds the portal, for the portal's real `port` (BUG-16). */
  portNote: (port: number | string) => string;
  /** A caveat worth seeing before initializing, if any. */
  note?: string;
}

/** What a teammate whose portal runs on another port does with a literal port. */
const OTHER_PORT = "A teammate whose portal runs on another port changes it in their copy.";

export const AGENTS: AgentInfo[] = [
  {
    id: "claude",
    label: "Claude Code",
    file: ".mcp.json",
    adds: "Adds .mcp.json and .claude/ for Claude Code.",
    portNote: (port) => `Connects on port ${port}. Safe to commit: each teammate's portal port is used.`,
    note: "Claude Code asks you to approve the project's MCP server the first time it opens the repo.",
  },
  {
    id: "cursor",
    label: "Cursor",
    file: ".cursor/mcp.json",
    adds: "Adds .cursor/mcp.json and .cursor/ for Cursor.",
    portNote: (port) => `Connects on port ${port}. ${OTHER_PORT}`,
  },
  {
    id: "vscode",
    label: "VS Code / Copilot",
    file: ".vscode/mcp.json",
    adds: "Adds .vscode/mcp.json and .github/ for VS Code and Copilot.",
    portNote: (port) => `VS Code asks for the port once (${port} here). Safe to commit.`,
  },
  {
    id: "codex",
    label: "Codex",
    file: ".codex/config.toml",
    adds: "Adds .codex/config.toml, AGENTS.md and .codex/agents/ for Codex.",
    portNote: (port) => `Connects on port ${port}. ${OTHER_PORT}`,
    note: "Codex loads .codex/config.toml for trusted projects only - trust this project in Codex for the server to load.",
  },
];

export const agentInfo = (id: string): AgentInfo | undefined => AGENTS.find((a) => a.id === id);

/** Notes that depend on the combination of selected agents. */
export function selectionNotes(selected: readonly string[]): string[] {
  const notes: string[] = [];
  for (const a of AGENTS) {
    // Each note already names its agent.
    if (selected.includes(a.id) && a.note) notes.push(a.note);
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
