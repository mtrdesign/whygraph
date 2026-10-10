// The words Usage & cost uses for what a call was (USE-5): "What" labels in the Calls
// table, and the Calls filter's own options, in one place.

/** A call's task as the "What" column reads it. */
const WHAT: Record<string, string> = {
  analyze: "Describe",
  rationale: "Rationale card",
  chat: "Chat",
};

/** Where a call came from, short enough for a table cell. */
const WHERE: Record<string, string> = {
  scan: "Scan",
  explorer: "Explorer",
  chat: "Chat",
  mcp: "MCP",
  agent: "Linked agent",
};

/** `Describe` / `Rationale card` / `Chat` for a ledger task; an unknown one is shown as sent. */
export function whatLabel(task: string): string {
  return WHAT[task] ?? task;
}

/** `Scan` / `Explorer` / `MCP` ... for a ledger source; an unknown one is shown as sent. */
export function whereLabel(source: string): string {
  return WHERE[source] ?? source;
}
