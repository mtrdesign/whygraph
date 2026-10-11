import { formatNumber } from "./format";
import type { ScanRunState, TaskState } from "./scanRun";

// One progress bar for a scan run (M2f-3 plan section 4.9, IMP-5): a weighted
// overall percentage, one status line and a compact checklist, folded from the
// same `ScanRunState` the scan-run page reads. The per-task bars stay on the run
// page ("Show details").

/** The crawler (task) names that belong to each phase, by the phase's title. */
export const PHASE_TASKS: Record<string, string[]> = {
  "Structural crawl": ["git", "github"],
  "PR-origin recovery": ["pr-origins"],
  "Author identity": ["authors"],
  "LLM descriptions": ["analyze"],
};

/** Friendly names for the crawler task labels the scan emits. */
const CRAWLER_LABEL: Record<string, string> = {
  git: "Git history",
  github: "GitHub pull requests and issues",
  "pr-origins": "PR-origin recovery",
  authors: "Author identities",
  analyze: "LLM descriptions",
  codegraph: "CodeGraph index",
};

/** The human name of a crawler task (`git` -> "Git history"); an unknown name as is. */
export const crawlerLabel = (name: string) => CRAWLER_LABEL[name] ?? name;

/**
 * Checklist labels for the phase titles the scan announces. A `--codegraph-only`
 * run announces "Code index" in `start` but titles its `phase` event "CodeGraph":
 * both read "Code index".
 */
export const PHASE_LABEL: Record<string, string> = {
  "Structural crawl": "Git history and GitHub",
  "PR-origin recovery": "Pull request origins",
  "Author identity": "Author identities",
  "LLM descriptions": "Commit descriptions",
  "Code index": "Code index",
  CodeGraph: "Code index",
};

/** The checklist label of a phase title (an unknown title as is). */
export const phaseLabel = (title: string) => PHASE_LABEL[title] ?? title;

/** Phase titles that are the CodeGraph index itself (a linked project's only phase). */
const CODE_INDEX_PHASES = new Set(["Code index", "CodeGraph"]);

/** What a phase is doing, for the status line, when none of its tasks has a count yet. */
const PHASE_STATUS: Record<string, string> = {
  "Structural crawl": "Reading git history",
  "PR-origin recovery": "Recovering pull request origins",
  "Author identity": "Matching author identities",
  "LLM descriptions": "Describing commits",
};

/** What a task is doing and what it counts, for the status line. */
const TASK_STATUS: Record<string, { verb: string; unit?: string }> = {
  git: { verb: "Reading git history", unit: "commits" },
  github: { verb: "Fetching pull requests and issues" },
  "pr-origins": { verb: "Recovering pull request origins" },
  authors: { verb: "Matching author identities" },
  analyze: { verb: "Describing commits", unit: "commits" },
  codegraph: { verb: "Building the code index" },
};

/**
 * The overall bar's shares: the clone / fetch of a production project, the
 * CodeGraph index (it runs alongside the phases; a fixed share from the start that
 * fills only when its task completes or the run ends) and the scan phases, which
 * split theirs equally. A run's percentage is renormalized over the parts it has.
 */
export const WEIGHTS = { clone: 15, codegraph: 20, phases: 65 } as const;

export interface ProgressStep {
  label: string;
  state: "done" | "running" | "pending" | "failed" | "skipped";
  detail?: string;
}

export interface ProgressModel {
  /** 0-100, or `null` (an indeterminate bar) until any total is known. */
  percent: number | null;
  status: string;
  steps: ProgressStep[];
}

interface Phase {
  n: number;
  title: string;
  seen: boolean;
}

/** Every phase the run will go through: seen titles first, then the planned ones. */
function allPhases(state: ScanRunState): Phase[] {
  const planned = state.plannedPhases ?? [];
  const total = Math.max(planned.length, state.phaseTotal ?? 0, state.phases.at(-1)?.phase ?? 0);
  const out: Phase[] = [];
  for (let n = 1; n <= total; n++) {
    const seen = state.phases.find((p) => p.phase === n);
    out.push({ n, title: seen?.title ?? planned[n - 1] ?? `Step ${n}`, seen: !!seen });
  }
  return out;
}

/** The scan phases (not the code index) and whether the run is CodeGraph only. */
function shape(state: ScanRunState) {
  const phases = allPhases(state);
  const scan = phases.filter((p) => !CODE_INDEX_PHASES.has(p.title));
  const codegraphOnly = phases.length > 0 && scan.length === 0;
  const codegraphTask = state.tasks.find((t) => t.name === "codegraph") ?? null;
  const source = state.result ?? state.summary;
  const codegraphResult = source?.crawlers?.find((c) => c.name === "codegraph") ?? null;
  // The portal never passes `--no-codegraph`: every run with scan phases refreshes
  // the code index beside them, so its share and its (pending) row are there from
  // the start, not only once a task or result names it. A result that lists its
  // crawlers without CodeGraph (a headless `--no-codegraph` scan) drops both.
  const resultWithout = !!source?.crawlers?.length && codegraphResult === null;
  const hasCodegraph =
    codegraphOnly || codegraphTask !== null || codegraphResult !== null || (scan.length > 0 && !resultWithout);
  return { scan, codegraphOnly, codegraphTask, codegraphResult, hasCodegraph };
}

const sumOf = (tasks: TaskState[]) => {
  const known = tasks.filter((t) => t.total !== null && t.total > 0);
  const total = known.reduce((n, t) => n + (t.total ?? 0), 0);
  const completed = known.reduce((n, t) => n + Math.min(t.completed, t.total ?? 0), 0);
  return { known: known.length > 0, completed, total };
};

const tasksOf = (state: ScanRunState, title: string) =>
  state.tasks.filter((t) => (PHASE_TASKS[title] ?? []).includes(t.name));

/** How far one scan phase is, 0-1: done once a later phase started or the run ended well. */
function phaseFraction(state: ScanRunState, phase: Phase): number {
  if (state.finished === "ok") return 1;
  if (!phase.seen) return 0;
  if ((state.phase ?? 0) > phase.n) return 1;
  const { known, completed, total } = sumOf(tasksOf(state, phase.title));
  return known ? completed / total : 0;
}

/**
 * The pure weighted percentage of a run (0-100, `null` while no total is known).
 * It can go down when a later task announces its total; the reducer keeps the
 * high-water mark in `ScanRunState.maxPercent`, and `progressModel` shows that.
 */
export function rawPercent(state: ScanRunState): number | null {
  if (state.finished === "ok") return 100;
  const anyTotal = state.tasks.some((t) => t.total !== null && t.total > 0);
  if (!anyTotal) return null;
  const { scan, hasCodegraph, codegraphTask } = shape(state);
  let weight = 0;
  let done = 0;
  if (state.sync) {
    weight += WEIGHTS.clone;
    const cloned = state.sync.status === "ok" || state.phase !== null;
    done += cloned ? WEIGHTS.clone : 0;
  }
  if (hasCodegraph) {
    weight += WEIGHTS.codegraph;
    const t = codegraphTask;
    const fraction = t && t.total ? Math.min(t.completed / t.total, 1) : 0;
    done += WEIGHTS.codegraph * fraction;
  }
  if (scan.length > 0) {
    weight += WEIGHTS.phases;
    const each = WEIGHTS.phases / scan.length;
    for (const p of scan) done += each * Math.min(phaseFraction(state, p), 1);
  }
  if (weight === 0) return null;
  // Never 100 before the run says it ended.
  return Math.min(99, Math.floor((done / weight) * 100));
}

function countText(completed: number, total: number, unit?: string): string {
  return `${formatNumber(completed)} of ${formatNumber(total)}${unit ? ` ${unit}` : ""}`;
}

function statusLine(state: ScanRunState, fullName: string | undefined): string {
  if (state.finished === "ok") return "Done";
  if (state.finished === "cancelled") return "The scan was cancelled";
  if (state.finished === "interrupted") return "The scan was interrupted";
  if (state.finished) return "The scan failed";
  const sync = state.sync;
  const name = sync?.fullName ?? fullName;
  if (sync?.status === "cloning") return `Cloning ${name ?? "the repository"}`;
  if (sync?.status === "fetching") return name ? `Fetching ${name} from GitHub` : "Fetching from GitHub";
  const { codegraphOnly, codegraphTask } = shape(state);
  const title = state.phaseTitle;
  if (title === null) {
    if (codegraphOnly && codegraphTask) return TASK_STATUS.codegraph.verb;
    return "Waiting for the scan to start";
  }
  if (CODE_INDEX_PHASES.has(title)) return TASK_STATUS.codegraph.verb;
  const tasks = tasksOf(state, title);
  const active = tasks.find((t) => t.total === null || t.completed < t.total) ?? tasks.at(-1);
  if (active) {
    const what = TASK_STATUS[active.name] ?? { verb: crawlerLabel(active.name) };
    return active.total ? `${what.verb} - ${countText(active.completed, active.total, what.unit)}` : what.verb;
  }
  return PHASE_STATUS[title] ?? phaseLabel(title);
}

function checklist(state: ScanRunState): ProgressStep[] {
  const steps: ProgressStep[] = [];
  const ended = state.finished !== null;
  const failedRun = ended && state.finished !== "ok";
  const source = state.result ?? state.summary;
  const crawlers = source?.crawlers ?? [];

  const sync = state.sync;
  if (sync) {
    const clone = sync.status === "cloning" || sync.cloned === true;
    steps.push({
      label: clone ? "Clone the repository" : "Fetch from GitHub",
      state:
        sync.status === "failed"
          ? "failed"
          : sync.status === "ok" || state.phase !== null
            ? "done"
            : failedRun
              ? "failed"
              : "running",
    });
  }

  const { scan, hasCodegraph, codegraphTask, codegraphResult } = shape(state);
  for (const p of scan) {
    const names = PHASE_TASKS[p.title] ?? [];
    const crawlerFailed = crawlers.some((c) => names.includes(c.name) && c.status === "failed");
    let s: ProgressStep["state"];
    if (!p.seen) s = ended ? "skipped" : "pending";
    else if (crawlerFailed) s = "failed";
    else if (failedRun && !state.result && p.n === state.phase) s = "failed";
    else if (ended || (state.phase ?? 0) > p.n) s = "done";
    else s = "running";
    const counts = sumOf(tasksOf(state, p.title));
    steps.push({
      label: phaseLabel(p.title),
      state: s,
      ...(counts.known && p.seen ? { detail: countText(counts.completed, counts.total) } : {}),
    });
  }

  if (hasCodegraph) {
    const t = codegraphTask;
    const finishedTask = !!t && t.total !== null && t.total > 0 && t.completed >= t.total;
    let s: ProgressStep["state"];
    if (codegraphResult?.status === "failed") s = "failed";
    else if (finishedTask || codegraphResult?.status === "ok" || state.finished === "ok") s = "done";
    else if (failedRun) s = t ? "failed" : "skipped";
    else s = t ? "running" : "pending";
    steps.push({ label: "Code index", state: s });
  }
  return steps;
}

/**
 * The wizard's one-bar view of a run: the weighted percentage (never below the
 * reducer's high-water mark), a status line ("Reading git history - 1,240 of
 * 5,300 commits") and a checklist with human labels. `fullName` names the
 * repository while a production import clones (the `cloning` frame carries it too).
 */
export function progressModel(state: ScanRunState, opts: { fullName?: string } = {}): ProgressModel {
  const raw = rawPercent(state);
  const percent =
    state.finished === "ok" ? 100 : raw === null && state.maxPercent === null ? null : Math.max(raw ?? 0, state.maxPercent ?? 0);
  return { percent, status: statusLine(state, opts.fullName), steps: checklist(state) };
}
