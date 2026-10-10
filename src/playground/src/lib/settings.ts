// The words and the small decisions the settings pages share (plan section 4.13):
// which layer a value is inherited from, what a key card says, what a key test
// answered, and which tasks of the org defaults resolve to a provider without a key.

import type { ConfigDict, KeyScope, KeyTestResult, SecretStatus } from "../api";
import { KEYED_PROVIDERS, TASKS, splitModel, type ModelPick, type TaskName } from "./configForm";
import { providerLabel } from "./labels";

const obj = (v: unknown): Record<string, unknown> =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
const str = (v: unknown): string => (typeof v === "string" ? v : "");

const production = (mode: string | null | undefined) => mode === "production";

/** "from the organization" (production) / "from portal defaults" (local). */
export function inheritedFrom(mode: string | null | undefined): string {
  return production(mode) ? "from the organization" : "from portal defaults";
}

/** "Using the organization key" / "Using the portal default key". */
export function inheritedKeyText(mode: string | null | undefined): string {
  return production(mode) ? "Using the organization key" : "Using the portal default key";
}

/** "Using the organization's token" / "Using the portal default token". */
export function inheritedTokenText(mode: string | null | undefined): string {
  return production(mode) ? "Using the organization's token" : "Using the portal default token";
}

/** The Remove action of a project key that an inherited key would replace. */
export function revertKeyLabel(mode: string | null | undefined): string {
  return production(mode) ? "Revert to the organization key" : "Revert to the portal default key";
}

/** What takes over when a project key is removed (`hint` only for org configurers). */
export function revertTakesOver(mode: string | null | undefined, inheritedSet: boolean, hint?: string | null): string {
  if (!inheritedSet) return "No key will be used.";
  const who = production(mode) ? "The organization's key" : "The portal default key";
  return `${who}${hint ? ` (${hint})` : ""} will be used.`;
}

/** Whether a config layer sets its own endpoint for `provider` (`base_url` or `host`). */
export function endpointOverride(layer: ConfigDict, provider: string): boolean {
  const table = obj(obj(layer.llm)[provider.replace(/-/g, "_")]);
  return !!(str(table.base_url) || str(table.host));
}

/**
 * The status line of a project's key card (BUG-11): its own key, the inherited one,
 * or none - and when none, whether that is because the project points the provider
 * at its own endpoint, where an inherited key never follows.
 */
export function projectKeyStatus({
  provider,
  own,
  scope,
  inheritedHint,
  endpoint,
  mode,
}: {
  provider: string;
  own: SecretStatus | undefined;
  scope: KeyScope | undefined;
  /** The org key's tail, for `org.configure` holders only. */
  inheritedHint?: string | null;
  endpoint: boolean;
  mode: string | null | undefined;
}): string {
  if (own?.set || scope === "project") return keySetText(own);
  if (scope === "org") return `${inheritedKeyText(mode)}${inheritedHint ? ` ${inheritedHint}` : ""}`;
  if (scope === "environment") return "Using a key from the portal's environment";
  if (endpoint) return `No key: this project uses its own ${providerLabel(provider)} endpoint`;
  return "No key";
}

/** "Set ...a1b2" for whoever may change the key, "Set" for everyone else. */
export function keySetText(status: SecretStatus | undefined): string {
  const text = status?.hint ? `Set ${status.hint}` : "Set";
  return status?.unreadable ? `${text} - unreadable, enter it again` : text;
}

/** A key test's answer in words; GitHub tokens name GitHub. */
export function keyTestText(result: KeyTestResult, github = false): string {
  switch (result) {
    case "ok":
      return github ? "Token works" : "Key works";
    case "rejected":
      return github ? "GitHub rejected this token" : "The provider rejected this key";
    case "rate_limited":
      return "Rate limited - try again later";
    case "unreachable":
      return github ? "Couldn't reach GitHub" : "Couldn't reach the provider";
    case "no_repo_access":
      return "This token can't read the repository";
    default:
      return github ? "GitHub gave an unexpected answer" : "The provider gave an unexpected answer";
  }
}

/** "Last used 3 days ago" / "Not used yet" from an ISO time or `null`. */
export function lastUsedText(iso: string | null, ago: (iso: string) => string | null): string {
  if (!iso) return "Not used yet";
  const when = ago(iso);
  return when ? `Last used ${when}` : "Not used yet";
}

/** The provider and model a pick resolves to, written for a person. */
export function pickText(pick: ModelPick): string {
  if (!pick.provider && !pick.model) return "";
  if (!pick.provider) return pick.model;
  return pick.model ? `${pick.model} (${providerLabel(pick.provider)})` : `${providerLabel(pick.provider)}'s default`;
}

function taskPick(layer: ConfigDict, task: TaskName): ModelPick {
  const table = obj(layer[task]);
  const provider = str(table.provider);
  const model = str(table.model);
  return provider ? { provider, model } : splitModel(model);
}

/** The provider a task of `layer` runs on (its own, else the default model's, else Anthropic). */
export function taskProvider(layer: ConfigDict, task: TaskName): string {
  return taskPick(layer, task).provider || splitModel(str(obj(layer.llm).model)).provider || "anthropic";
}

/**
 * The org / global page's warning lines (SET-6): each task whose provider needs a
 * key the defaults do not hold, e.g. "Chat uses OpenAI, which has no key".
 */
export function tasksWithoutKey(layer: ConfigDict, llmKeys: Record<string, SecretStatus | undefined>): string[] {
  const lines: string[] = [];
  for (const t of TASKS) {
    const provider = taskProvider(layer, t.id);
    if (!KEYED_PROVIDERS.includes(provider) || llmKeys[provider]?.set) continue;
    lines.push(`${t.label} uses ${providerLabel(provider)}, which has no key`);
  }
  return lines;
}

/**
 * The placeholder of an empty model row: the inherited value and where it comes
 * from (SET-3). `task` is null for the default model.
 */
export function inheritedModelText({
  task,
  projectLayer,
  orgLayer,
  mode,
}: {
  task: TaskName | null;
  /** The project's layer, or `null` on the org / global page. */
  projectLayer: ConfigDict | null;
  orgLayer: ConfigDict | null;
  mode: string | null | undefined;
}): string {
  const orgDefault = splitModel(str(obj(orgLayer?.llm).model));
  if (task === null) {
    if (projectLayer && orgLayer && pickText(orgDefault)) return `Inherited: ${pickText(orgDefault)} ${inheritedFrom(mode)}`;
    return "Provider default";
  }
  if (projectLayer) {
    const orgTask = orgLayer ? taskPick(orgLayer, task) : { provider: "", model: "" };
    if (pickText(orgTask)) return `Inherited: ${pickText(orgTask)} ${inheritedFrom(mode)}`;
    if (str(obj(projectLayer.llm).model)) return "Uses this project's default model";
    if (pickText(orgDefault)) return `Inherited: ${pickText(orgDefault)} ${inheritedFrom(mode)}`;
  }
  return "Uses the default model";
}

/** The placeholder of an empty endpoint: the inherited value, or the built-in default. */
export function inheritedEndpointText(
  inherited: string,
  fallback: string,
  mode: string | null | undefined,
): string {
  return inherited ? `Inherited: ${inherited} ${inheritedFrom(mode)}` : fallback;
}
