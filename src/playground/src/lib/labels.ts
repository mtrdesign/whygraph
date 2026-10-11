// One place for the words the UI uses for roles, agents, providers, sources, run
// triggers and the inherited config layer, so no screen invents its own.

import type { MemberRole, ProjectRole, ScanRunRow } from "../api";
import { AGENTS } from "./agents";

const ORG_ROLE: Record<MemberRole, string> = {
  owner: "Owner",
  admin: "Admin",
  member: "Member",
};

const PROJECT_ROLE: Record<ProjectRole, string> = {
  admin: "Admin",
  contributor: "Contributor",
  viewer: "Viewer",
};

const PROVIDER: Record<string, string> = {
  openrouter: "OpenRouter",
  openai: "OpenAI",
  anthropic: "Anthropic",
  deepseek: "DeepSeek",
  ollama: "Ollama",
};

const SOURCE: Record<string, string> = {
  local: "Local folder",
  github: "GitHub",
  platform: "Platform",
};

const TRIGGER: Record<string, string> = {
  initial: "Initial",
  manual: "Manual",
  describe: "Describe",
  hook: "Git hook",
  poll: "Poll",
  sync: "Sync",
  push: "Push",
  reconcile: "Reconcile",
};

/** `Owner` / `Admin` / `Member`; an unknown role is shown as sent. */
export function orgRoleLabel(role: string): string {
  return ORG_ROLE[role as MemberRole] ?? role;
}

/** `Admin` / `Contributor` / `Viewer`; an unknown role is shown as sent. */
export function projectRoleLabel(role: string): string {
  return PROJECT_ROLE[role as ProjectRole] ?? role;
}

/** `Claude Code` / `Cursor` ... for an agent id. */
export function agentLabel(id: string): string {
  return AGENTS.find((a) => a.id === id)?.label ?? id;
}

/** `OpenRouter` / `OpenAI` ... for a provider id. */
export function providerLabel(id: string): string {
  return PROVIDER[id] ?? id;
}

/** `Local folder` / `GitHub` / `Platform` for a project source. */
export function sourceLabel(source: string): string {
  return SOURCE[source] ?? source;
}

/**
 * The layer a project inherits from: the portal's defaults locally, the
 * organization in production (`short` drops the "defaults" word locally).
 */
export function inheritedLayerLabel(mode: string | null | undefined, short = false): string {
  if (mode === "production") return "Organization";
  return short ? "Portal" : "Portal defaults";
}

/** `Initial` / `Manual` / `Git hook` ... for a run. */
export function triggerLabel(run: Pick<ScanRunRow, "trigger" | "kind">): string {
  return TRIGGER[run.trigger] ?? (run.kind === "sync" ? "Sync" : run.trigger);
}
