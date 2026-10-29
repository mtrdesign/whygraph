import { formatNumber } from "./format";
import { z } from "zod";
import type { ConfigDict, SecretsPatch } from "../api";

// Screen 5's form <-> a v2 config layer. `PUT .../config` replaces the WHOLE stored
// layer, so `valuesToLayer` starts from the layer the server sent and overwrites
// only the keys the form owns; every other key (tuning, an imported `[scan]`
// option, a deprecated `[llm.<p>].model`) survives a save untouched.

export const PROVIDERS = ["anthropic", "openai", "openrouter", "deepseek", "ollama"];
/** Providers that can drive chat (`CHAT_PROVIDERS` in `core/config.py`). */
export const CHAT_PROVIDER_TAGS = ["anthropic", "openai", "deepseek", "openrouter"];
/** Providers that need an API key; `ollama` is key-less. */
export const KEYED_PROVIDERS = ["anthropic", "openai", "openrouter", "deepseek"];
export const HOOK_NAMES = ["post-commit", "post-merge", "post-rewrite", "post-checkout"] as const;
export type HookName = (typeof HOOK_NAMES)[number];

export type TaskName = "analyze" | "rationale" | "chat";
export const TASKS: { id: TaskName; label: string; hint: string }[] = [
  { id: "analyze", label: "Commit descriptions", hint: "Describes each commit's diff during a scan." },
  { id: "rationale", label: "Rationale cards", hint: "Writes the why behind a symbol on demand." },
  { id: "chat", label: "Chat", hint: "The default for new chat sessions. Without a key for it, new chats start on the first provider that has one." },
];

export interface ModelPick {
  provider: string;
  model: string;
}

/** The effort levels a chat can run at (`EFFORT_LEVELS` in `core/config.py`), lowest first. */
export const EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"] as const;
export type EffortLevel = (typeof EFFORT_LEVELS)[number];

/** The chat modes (`MODE_IDS` in `core/config.py`), in the order the select lists them. */
export const MODE_IDS = ["engineer", "lead", "product"] as const;

/** The Chat task's pick: a model, plus `[chat].effort` and `[chat].mode` ("" = unset, inherit). */
export interface ChatPick extends ModelPick {
  effort: string;
  mode: string;
}

/** One `[areas.<name>]` row as typed: paths and labels one per line. */
export interface AreaRow {
  name: string;
  description: string;
  pathsText: string;
  labelsText: string;
}

const lines = (text: string): string[] =>
  text
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l !== "");
const textLines = (v: unknown): string => (Array.isArray(v) ? v.map(String).join("\n") : "");

/** The stored `[areas]` table as rows, in stored order. */
export function areasFromLayer(layer: ConfigDict): AreaRow[] {
  return Object.entries(obj(layer.areas)).map(([name, raw]) => {
    const a = obj(raw);
    return { name, description: str(a.description), pathsText: textLines(a.paths), labelsText: textLines(a.labels) };
  });
}

export interface ConfigFormValues {
  defaultModel: ModelPick;
  analyze: ModelPick;
  rationale: ModelPick;
  chat: ChatPick;
  openaiBaseUrl: string;
  ollamaHost: string;
  forge: boolean;
  hooks: Record<HookName, boolean>;
  /** `[scan].serve_codegraph`: the portal serves CodeGraph tools for this project (default on). */
  serveCodegraph: boolean;
  /** Write-only; blank leaves the stored token alone. */
  githubToken: string;
  /** Write-only per provider; blank leaves the stored key alone. */
  keys: Record<string, string>;
  /** `[rationale].agent_generations_per_hour`, as typed (blank = the default). Org defaults only. */
  agentGenerations: string;
  /** `[analyze].agent_descriptions_per_hour`, as typed (blank = the default). Org defaults only. */
  agentDescriptions: string;
  /** `[areas]`, project layer only. */
  areas: AreaRow[];
}

/**
 * The org limits on agent LLM spend (`ORG_ONLY_KEYS` in `portal/policy.py`): an
 * owner sets them on the org defaults, never per project. Largest allowed value:
 * `MAX_AGENT_LIMIT` in `core/config.py`.
 */
export const MAX_AGENT_LIMIT = 10_000;
export const DEFAULT_AGENT_GENERATIONS = 120;
export const DEFAULT_AGENT_DESCRIPTIONS = 600;
const AGENT_LIMIT_KEYS = {
  rationale: ["agentGenerations", "agent_generations_per_hour"],
  analyze: ["agentDescriptions", "agent_descriptions_per_hour"],
} as const;

const obj = (v: unknown): Record<string, unknown> =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
const str = (v: unknown): string => (typeof v === "string" ? v : "");

/** Split `"provider/model"` on the first slash (`openrouter/anthropic/x` -> openrouter + anthropic/x). */
export function splitModel(value: string): ModelPick {
  const i = value.indexOf("/");
  if (i <= 0) return { provider: "", model: value };
  const provider = value.slice(0, i);
  return PROVIDERS.includes(provider)
    ? { provider, model: value.slice(i + 1) }
    : { provider: "", model: value };
}

function readTask(layer: ConfigDict, task: TaskName): ModelPick {
  const table = obj(layer[task]);
  const provider = str(table.provider);
  const model = str(table.model);
  // 1.x / hand-written files: "provider/model" in `model` with no `provider` key.
  return provider ? { provider, model } : splitModel(model);
}

export function hooksFromLayer(layer: ConfigDict): Record<HookName, boolean> {
  const raw = obj(layer.scan).hooks;
  const all = Object.fromEntries(HOOK_NAMES.map((h) => [h, true])) as Record<HookName, boolean>;
  if (raw === undefined || raw === true) return all;
  if (raw === false) return Object.fromEntries(HOOK_NAMES.map((h) => [h, false])) as Record<HookName, boolean>;
  const list = Array.isArray(raw) ? raw.map(String) : [];
  return Object.fromEntries(HOOK_NAMES.map((h) => [h, list.includes(h)])) as Record<HookName, boolean>;
}

/** `[scan].serve_codegraph`; only an explicit `false` turns the hosted tools off. */
export function serveCodegraphOn(layer: ConfigDict): boolean {
  return obj(layer.scan).serve_codegraph !== false;
}

/** Whether `[scan].forge` turns the GitHub crawl on (`auto` / `github`). */
export function forgeOn(layer: ConfigDict): boolean {
  const forge = str(obj(layer.scan).forge);
  return forge === "auto" || forge === "github";
}

export function layerToValues(layer: ConfigDict): ConfigFormValues {
  const llm = obj(layer.llm);
  return {
    defaultModel: splitModel(str(llm.model)),
    analyze: readTask(layer, "analyze"),
    rationale: readTask(layer, "rationale"),
    chat: { ...readTask(layer, "chat"), effort: str(obj(layer.chat).effort), mode: str(obj(layer.chat).mode) },
    openaiBaseUrl: str(obj(llm.openai).base_url),
    ollamaHost: str(obj(llm.ollama).host),
    forge: forgeOn(layer),
    hooks: hooksFromLayer(layer),
    serveCodegraph: serveCodegraphOn(layer),
    githubToken: "",
    keys: Object.fromEntries(KEYED_PROVIDERS.map((p) => [p, ""])),
    agentGenerations: limitText(obj(layer.rationale).agent_generations_per_hour),
    agentDescriptions: limitText(obj(layer.analyze).agent_descriptions_per_hour),
    areas: areasFromLayer(layer),
  };
}

const limitText = (v: unknown): string => (typeof v === "number" ? String(v) : "");

/** The `[scan].hooks` value for a set of checkboxes (`undefined` = the default, all on). */
export function hooksToValue(hooks: Record<HookName, boolean>): boolean | string[] | undefined {
  const on = HOOK_NAMES.filter((h) => hooks[h]);
  if (on.length === HOOK_NAMES.length) return undefined;
  if (on.length === 0) return false;
  return [...on];
}

function setOrDrop(table: Record<string, unknown>, key: string, value: string | undefined) {
  if (value === undefined || value === "") delete table[key];
  else table[key] = value;
}

/** Drop a table that ended up empty, so a cleared section leaves no `{}` behind. */
function prune(root: Record<string, unknown>, key: string) {
  const t = root[key];
  if (t !== null && typeof t === "object" && Object.keys(t as object).length === 0) delete root[key];
}

export function valuesToLayer(
  base: ConfigDict,
  v: ConfigFormValues,
  opts: { scan: boolean; limits?: boolean; areas?: boolean },
): ConfigDict {
  const layer = structuredClone(base) as Record<string, Record<string, unknown>>;

  const llm = (layer.llm = obj(layer.llm));
  const d = v.defaultModel;
  setOrDrop(llm, "model", d.provider && d.model ? `${d.provider}/${d.model}` : undefined);
  for (const [provider, key, value] of [
    ["openai", "base_url", v.openaiBaseUrl],
    ["ollama", "host", v.ollamaHost],
  ] as const) {
    const table = (llm[provider] = obj(llm[provider]));
    setOrDrop(table, key, value.trim());
    prune(llm, provider);
  }
  prune(layer, "llm");

  for (const task of ["analyze", "rationale", "chat"] as const) {
    const table = (layer[task] = obj(layer[task]));
    const pick = v[task];
    // Explicit `provider` + `model`, never a "provider/model" string: a model id
    // that itself contains a slash (OpenRouter's) must not be re-split.
    setOrDrop(table, "provider", pick.provider);
    setOrDrop(table, "model", pick.model);
    if (task === "chat") {
      setOrDrop(table, "effort", (pick as ChatPick).effort);
      // Project-only on the server; the org-scope form never fills it, so it never sends it.
      setOrDrop(table, "mode", (pick as ChatPick).mode);
    }
    // The org-only limits are written by the org defaults form alone; a project
    // layer is never given them (the server refuses them there).
    if (opts.limits && (task === "rationale" || task === "analyze")) {
      const [field, key] = AGENT_LIMIT_KEYS[task];
      const typed = v[field].trim();
      if (typed === "") delete table[key];
      else table[key] = Number(typed);
    }
    prune(layer, task);
  }

  if (opts.scan) {
    const scan = (layer.scan = obj(layer.scan));
    // Only touch what the user changed: an untouched layer stays as stored (an
    // unset forge is "off" already; an imported hooks list stays a list).
    if (v.forge !== forgeOn(base)) scan.forge = v.forge ? "auto" : "off";
    const before = hooksFromLayer(base);
    if (HOOK_NAMES.some((h) => before[h] !== v.hooks[h])) {
      const hooks = hooksToValue(v.hooks);
      if (hooks === undefined) delete scan.hooks;
      else scan.hooks = hooks;
    }
    if (v.serveCodegraph !== serveCodegraphOn(base)) {
      if (v.serveCodegraph) delete scan.serve_codegraph;
      else scan.serve_codegraph = false;
    }
    prune(layer, "scan");
  }
  // Only the Areas section's save writes `[areas]`; zero rows removes the key.
  if (opts.areas) {
    const stored = obj(base.areas);
    const table: Record<string, unknown> = {};
    for (const row of v.areas) {
      const before = obj(stored[row.name]);
      const area: Record<string, unknown> = {};
      // Keep a stored empty `description` / `labels` key, so an untouched row round-trips exactly.
      if (row.description.trim() !== "" || "description" in before) area.description = row.description.trim();
      area.paths = lines(row.pathsText);
      const labels = lines(row.labelsText);
      if (labels.length > 0 || "labels" in before) area.labels = labels;
      table[row.name.trim()] = area;
    }
    if (Object.keys(table).length === 0) delete (layer as Record<string, unknown>).areas;
    else (layer as Record<string, unknown>).areas = table;
  }
  return layer;
}

/** The secrets part of a save: typed values set, staged removals delete, blanks are untouched. */
export function secretsPatch(
  v: ConfigFormValues,
  removed: readonly string[],
  opts: { github: boolean },
): SecretsPatch | undefined {
  const llm: Record<string, string | null> = {};
  for (const p of KEYED_PROVIDERS) {
    const typed = v.keys[p]?.trim();
    if (typed) llm[p] = typed;
    else if (removed.includes(p)) llm[p] = null;
  }
  const patch: SecretsPatch = {};
  if (Object.keys(llm).length) patch.llm = llm;
  if (opts.github) {
    const token = v.githubToken.trim();
    if (token) patch.github_token = token;
    else if (removed.includes("github")) patch.github_token = null;
  }
  return Object.keys(patch).length ? patch : undefined;
}

// ---- validation -------------------------------------------------------------

const modelText = z.string().regex(/^\S*$/, "No spaces in a model id");

const effortText = z.string().refine((e) => e === "" || (EFFORT_LEVELS as readonly string[]).includes(e), "Unknown effort level");

const pick = () => z.object({ provider: z.string(), model: modelText });

const chatPick = () =>
  z
    .object({ provider: z.string(), model: modelText, effort: effortText, mode: z.string() })
    .refine((p) => p.provider === "" || CHAT_PROVIDER_TAGS.includes(p.provider), {
      message: "This provider cannot run chat",
      path: ["provider"],
    });

const agentLimit = z.string().refine(
  (s) => {
    const t = s.trim();
    return t === "" || (/^[0-9]{1,5}$/.test(t) && Number(t) <= MAX_AGENT_LIMIT);
  },
  `Enter a whole number from 0 to ${formatNumber(MAX_AGENT_LIMIT)}, or leave it blank`,
);

const areaRow = z.object({
  name: z.string().refine((n) => n.trim() !== "", "Give the area a name"),
  description: z.string(),
  pathsText: z.string().refine((t) => lines(t).length > 0, "Add at least one path"),
  labelsText: z.string(),
});

export const configFormSchema = z.object({
  defaultModel: z
    .object({ provider: z.string(), model: modelText })
    .refine((p) => (p.provider === "") === (p.model === ""), {
      message: "Choose a provider and a model together, or neither",
      path: ["model"],
    }),
  analyze: pick(),
  rationale: pick(),
  chat: chatPick(),
  openaiBaseUrl: z
    .string()
    .refine((s) => s.trim() === "" || /^https?:\/\/\S+$/.test(s.trim()), "Enter an http(s) URL"),
  ollamaHost: z
    .string()
    .refine((s) => s.trim() === "" || /^\S+$/.test(s.trim()), "No spaces in a host"),
  forge: z.boolean(),
  hooks: z.object({
    "post-commit": z.boolean(),
    "post-merge": z.boolean(),
    "post-rewrite": z.boolean(),
    "post-checkout": z.boolean(),
  }),
  serveCodegraph: z.boolean(),
  githubToken: z.string(),
  keys: z.record(z.string(), z.string()),
  agentGenerations: agentLimit,
  agentDescriptions: agentLimit,
  areas: z.array(areaRow).refine(
    (rows) => new Set(rows.map((r) => r.name.trim())).size === rows.length,
    "Area names must be unique",
  ),
});
