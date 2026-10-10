import { useEffect, useReducer } from "react";
import { ApiError, projectApi, type ScanEvent, type ScanRunStatus, type ScanRunSummary } from "../api";
import { PHASE_TASKS, rawPercent } from "./scanProgress";

export { crawlerLabel } from "./scanProgress";

// Live state of one scan run, folded from the events stream. The wizard's
// progress card (`ScanProgress`, through `progressModel`) and the scan-run page
// read the same hook and reducer.

export interface TaskState {
  name: string;
  completed: number;
  total: number | null;
  description: string;
}

/** The events request itself was refused (a run that does not exist, an unsafe project). */
export interface StreamFailure {
  status: number;
  message: string;
  code?: string;
}

export interface ScanRunState {
  phaseTotal: number | null;
  phase: number | null;
  phaseTitle: string | null;
  /** Every phase seen so far, in the order the stream announced them. */
  phases: { phase: number; title: string }[];
  /**
   * The titles the `start` event announced for every phase that will run, in
   * order (`null` for an older run whose `start` has none). Kept apart from
   * `phases`, which only holds the phases the stream has reached.
   */
  plannedPhases: string[] | null;
  tasks: TaskState[];
  /**
   * The runner's sync frames of a production GitHub project: `cloning` (an
   * import's first clone, with the repository's `full_name`) or `fetching`, then
   * `ok` / `failed`; `cloned` is set on the `ok` frame of a clone.
   */
  sync: {
    status: "cloning" | "fetching" | "ok" | "failed";
    moved?: boolean;
    error?: string;
    fullName?: string;
    cloned?: boolean;
  } | null;
  /** A runner-level failure (`{"type":"error"}`), e.g. the repo root is gone. */
  error: string | null;
  /** The child's `result` event. */
  result: Extract<ScanEvent, { type: "result" }> | null;
  /** Set by the terminal `end` frame - the run is finished. */
  finished: ScanRunStatus | null;
  summary: ScanRunSummary | null;
  /** Set when the stream was refused with a 4xx; the hook stops retrying. */
  failure: StreamFailure | null;
  /**
   * The highest overall percentage reached so far (`progressModel`'s weights), so
   * the bar never moves backwards when a later task announces its total.
   */
  maxPercent: number | null;
}

export const initialScanRunState: ScanRunState = {
  phaseTotal: null,
  phase: null,
  phaseTitle: null,
  phases: [],
  plannedPhases: null,
  tasks: [],
  sync: null,
  error: null,
  result: null,
  finished: null,
  summary: null,
  failure: null,
  maxPercent: null,
};

type Action =
  | { type: "event"; event: ScanEvent }
  | { type: "failure"; failure: StreamFailure }
  | { type: "reset" };

export function reduceScanRun(state: ScanRunState, action: Action): ScanRunState {
  if (action.type === "reset") return initialScanRunState;
  if (action.type === "failure") return { ...state, failure: action.failure };
  const next = foldEvent(state, action.event);
  if (next === state) return state;
  // The high-water mark: a later task's total can lower the pure ratio.
  const raw = rawPercent(next);
  const maxPercent = raw === null ? state.maxPercent : Math.max(state.maxPercent ?? 0, raw);
  return maxPercent === next.maxPercent ? next : { ...next, maxPercent };
}

function foldEvent(state: ScanRunState, e: ScanEvent): ScanRunState {
  switch (e.type) {
    case "start":
      return {
        ...state,
        phaseTotal: e.phase_total,
        plannedPhases: Array.isArray(e.phases) ? e.phases.filter((t) => typeof t === "string") : state.plannedPhases,
      };
    case "phase": {
      const phases = state.phases.filter((p) => p.phase !== e.phase);
      phases.push({ phase: e.phase, title: e.title });
      phases.sort((a, b) => a.phase - b.phase);
      return { ...state, phase: e.phase, phaseTitle: e.title, phases };
    }
    case "task": {
      const next: TaskState = {
        name: e.name,
        completed: e.completed ?? 0,
        total: e.total ?? null,
        description: e.description ?? "",
      };
      const i = state.tasks.findIndex((t) => t.name === e.name);
      const tasks = [...state.tasks];
      if (i === -1) tasks.push(next);
      else tasks[i] = next;
      return { ...state, tasks };
    }
    case "sync":
      return {
        ...state,
        sync: {
          status: e.status,
          moved: e.moved,
          error: e.error,
          // The `ok` frame of a clone carries no name: keep the `cloning` frame's.
          fullName: e.full_name ?? state.sync?.fullName,
          cloned: e.cloned ?? state.sync?.cloned,
        },
      };
    case "error":
      return { ...state, error: e.message };
    case "result":
      return { ...state, result: e };
    case "end":
      if ("reason" in e && e.reason === "access_revoked") {
        return {
          ...state,
          failure: { status: 403, message: "You no longer have access to this project.", code: "access_revoked" },
        };
      }
      return { ...state, finished: e.status, summary: e.summary };
    default:
      return state;
  }
}

const RECONNECT_MS = 1500;

/**
 * Follow a run's events stream until its terminal `end` frame. Reconnects with
 * `Last-Event-ID` when the stream drops or the portal announces a shutdown (the
 * replay resumes without duplicates). `runId` null is idle. A new `runId` resets
 * the state.
 */
export function useScanRun(slug: string, runId: number | null): ScanRunState {
  const [state, dispatch] = useReducer(reduceScanRun, initialScanRunState);

  useEffect(() => {
    dispatch({ type: "reset" });
    if (runId === null) return;
    const api = projectApi(slug);
    const controller = new AbortController();
    let done = false;

    (async () => {
      let lastId: string | null = null;
      while (!done && !controller.signal.aborted) {
        try {
          lastId = await api.streamScanEvents(
            runId,
            (event) => {
              if (event.type === "end") done = true;
              dispatch({ type: "event", event });
            },
            { signal: controller.signal, lastEventId: lastId },
          );
        } catch (err) {
          if (controller.signal.aborted) return;
          // A refused request will be refused again; only transport errors and 5xx retry.
          if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
            dispatch({
              type: "failure",
              failure: { status: err.status, message: err.message, code: err.code },
            });
            return;
          }
        }
        if (done || controller.signal.aborted) return;
        await new Promise((resolve) => setTimeout(resolve, RECONNECT_MS));
      }
    })();

    return () => controller.abort();
  }, [slug, runId]);

  return state;
}

export type PhaseStatus = "pending" | "running" | "done" | "failed" | "skipped";

export interface PhaseRow {
  phase: number;
  title: string;
  status: PhaseStatus;
  /** Seconds, known once the scan's `result` event arrived. */
  seconds: number | null;
  tasks: TaskState[];
  /** The phase's crawlers as the `result` event reported them (empty until then). */
  crawlers: CrawlerResult[];
}

export interface CrawlerResult {
  name: string;
  status: string;
  summary?: string;
  error?: string;
  warning?: string;
}

/**
 * The phase timeline of a run. Titles come from the `phase` events; a phase the
 * stream has not reached yet is listed as pending up to the announced
 * `phase_total` (or skipped, once the run has ended without reaching it). A phase
 * is done once a later one started or the run ended, and `failed` when a crawler
 * of the phase reported failure in the `result` event.
 */
export function phaseRows(state: ScanRunState): PhaseRow[] {
  const total = Math.max(state.phaseTotal ?? 0, state.phases.at(-1)?.phase ?? 0);
  const source = state.result ?? state.summary;
  const timings = (source?.phase_timings ?? {}) as Record<string, number>;
  const crawlers: CrawlerResult[] = source?.crawlers ?? [];
  const ended = state.finished !== null;
  const rows: PhaseRow[] = [];
  for (let n = 1; n <= total; n++) {
    const seen = state.phases.find((p) => p.phase === n);
    const title = seen?.title ?? `Step ${n}`;
    const names = PHASE_TASKS[title] ?? [];
    const failed = crawlers.some((c) => names.includes(c.name) && c.status === "failed");
    let status: PhaseStatus;
    if (!seen) status = ended ? "skipped" : "pending";
    else if (failed) status = "failed";
    // A run that ended without a `result` died in the phase it was in.
    else if (ended && state.finished !== "ok" && !state.result && n === state.phase) status = "failed";
    else if (ended || (state.phase ?? 0) > n) status = "done";
    else status = "running";
    rows.push({
      phase: n,
      title,
      status,
      seconds: typeof timings[title] === "number" ? timings[title] : null,
      tasks: state.tasks.filter((t) => names.includes(t.name)),
      crawlers: crawlers.filter((c) => names.includes(c.name)),
    });
  }
  return rows;
}

/** The background CodeGraph crawler's task, which belongs to no phase. */
export function codegraphRow(state: ScanRunState): { task: TaskState | null; result: CrawlerResult | null } {
  const source = state.result ?? state.summary;
  return {
    task: state.tasks.find((t) => t.name === "codegraph") ?? null,
    result: source?.crawlers?.find((c) => c.name === "codegraph") ?? null,
  };
}
