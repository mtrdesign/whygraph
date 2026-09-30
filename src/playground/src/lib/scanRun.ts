import { useEffect, useReducer } from "react";
import { projectApi, type ScanEvent, type ScanRunStatus } from "../api";

// Live state of one scan run, folded from the events stream. The first-scan step
// renders a simple view of it; step 13's full scan-run screen reuses the same
// hook and reducer.

export interface TaskState {
  name: string;
  completed: number;
  total: number | null;
  description: string;
}

export interface ScanRunState {
  phaseTotal: number | null;
  phase: number | null;
  phaseTitle: string | null;
  tasks: TaskState[];
  sync: { status: "fetching" | "ok" | "failed"; moved?: boolean; error?: string } | null;
  /** A runner-level failure (`{"type":"error"}`), e.g. the repo root is gone. */
  error: string | null;
  /** The child's `result` event. */
  result: Extract<ScanEvent, { type: "result" }> | null;
  /** Set by the terminal `end` frame - the run is finished. */
  finished: ScanRunStatus | null;
  summary: Record<string, unknown> | null;
}

export const initialScanRunState: ScanRunState = {
  phaseTotal: null,
  phase: null,
  phaseTitle: null,
  tasks: [],
  sync: null,
  error: null,
  result: null,
  finished: null,
  summary: null,
};

type Action = { type: "event"; event: ScanEvent } | { type: "reset" };

export function reduceScanRun(state: ScanRunState, action: Action): ScanRunState {
  if (action.type === "reset") return initialScanRunState;
  const e = action.event;
  switch (e.type) {
    case "start":
      return { ...state, phaseTotal: e.phase_total };
    case "phase":
      return { ...state, phase: e.phase, phaseTitle: e.title };
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
      return { ...state, sync: { status: e.status, moved: e.moved, error: e.error } };
    case "error":
      return { ...state, error: e.message };
    case "result":
      return { ...state, result: e };
    case "end":
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
        } catch {
          if (controller.signal.aborted) return;
        }
        if (done || controller.signal.aborted) return;
        await new Promise((resolve) => setTimeout(resolve, RECONNECT_MS));
      }
    })();

    return () => controller.abort();
  }, [slug, runId]);

  return state;
}

/** Overall progress 0-100 from the phase counter, for a coarse bar. */
export function phasePercent(state: ScanRunState): number | null {
  if (state.finished === "ok") return 100;
  if (state.phaseTotal === null || state.phase === null) return null;
  return Math.round(((state.phase - 1) / state.phaseTotal) * 100);
}
