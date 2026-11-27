// Rationale depth (plan 62 section 14): the levels, the one copy constant the info
// panel and the settings hints both read, and the formatting of the estimate.
// `high` and `xhigh` are not offered (plan 62 R49), so the UI lists Auto, Low and Medium.

import { formatUsd } from "./format";

export const DEPTH_LEVELS = ["low", "medium"] as const;
export type DepthLevel = (typeof DEPTH_LEVELS)[number];
/** What a project's `[rationale].depth` may hold (the ceiling never `auto`). */
export type DepthSetting = "auto" | DepthLevel;

/** Every level the backend knows, for ranking a value the UI does not offer yet. */
const RANK = ["low", "medium", "high", "xhigh"] as const;

export const rankOf = (level: string): number => RANK.indexOf(level as (typeof RANK)[number]);

export const DEFAULT_DEPTH: DepthSetting = "auto";
/** `[rationale].max_depth` and the org ceiling default. */
export const DEFAULT_CEILING: DepthLevel = "medium";

export const isLevel = (value: unknown): value is DepthLevel =>
  typeof value === "string" && (DEPTH_LEVELS as readonly string[]).includes(value);

/** The short name: "Medium". A level the UI does not offer reads as itself. */
export function depthShortName(level: string): string {
  if (level === "auto") return "Auto";
  if (level === "xhigh") return "Extra high";
  return level.charAt(0).toUpperCase() + level.slice(1);
}

export interface DepthRow {
  /** The option's and the panel row's name: "Low - Lines". */
  name: string;
  /** What the level answers. */
  question: string;
  /** What it reads. */
  reads: string;
  /** Model calls per card. */
  calls: number;
}

/**
 * The words of the info panel and of the hint under the Depth select: one place,
 * so the two never disagree (R31). The rows follow plan 62 section 14.1 (what a level
 * answers, what it reads, how many model calls it makes).
 */
export const DEPTH_COPY = {
  title: "What each depth level includes",
  auto: {
    name: "Auto",
    description: "Picks a level per request: the task, how important the code is, and the budget. Never above the ceiling.",
  },
  levels: {
    low: {
      name: "Low - Lines",
      question: "Who wrote these lines, and why?",
      reads:
        "Reads the commits that own them and the one that created them, the main PRs in full and the author's review replies.",
      calls: 1,
    },
    medium: {
      name: "Medium - Context",
      question: "What has been happening around them?",
      reads: "Adds more PRs in full and the file's recent history in short form.",
      calls: 1,
    },
  } satisfies Record<DepthLevel, DepthRow>,
  footer: "Every level reads the LLM description of each commit it includes.",
} as const;

/** "1 model call." / "2 model calls." */
export const callsText = (calls: number): string => `${calls} model ${calls === 1 ? "call" : "calls"}.`;

/** One level's full sentence: the question, what it reads, the calls. */
export function levelDescription(level: DepthLevel): string {
  const row = DEPTH_COPY.levels[level];
  return `${row.question} ${row.reads} ${callsText(row.calls)}`;
}

/** The hint under the Depth select for the chosen value. */
export function depthHint(value: string): string {
  return isLevel(value) ? levelDescription(value) : DEPTH_COPY.auto.description;
}

/** The option label of a depth or ceiling choice. */
export function depthOptionName(value: string): string {
  return isLevel(value) ? DEPTH_COPY.levels[value].name : value === "auto" ? DEPTH_COPY.auto.name : depthShortName(value);
}

/** A card price: whole dollars from $10, else cents or three places for a fraction of a cent ("$0.019"). */
function usd(value: number): string {
  if (value >= 10) return formatUsd(value);
  return `$${value.toFixed(3).replace(/0$/, "")}`;
}

/** A price range as the panel words it: "$0.05-0.09", or one figure when both ends read alike. */
export function formatCostRange(range: { low: number; high: number } | null | undefined): string | null {
  if (!range) return null;
  const low = usd(range.low);
  const high = usd(range.high);
  return low === high ? low : `${low}-${high.slice(1)}`;
}
