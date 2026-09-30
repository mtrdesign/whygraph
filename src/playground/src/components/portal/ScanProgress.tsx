import { phasePercent, type ScanRunState } from "../../lib/scanRun";
import { Progress } from "../ui/progress";

/**
 * A plain live view of one scan run (phase counter + per-crawler task bars), used
 * by the wizard's first-scan step. Step 13's scan-run screen replaces it with the
 * full phase timeline; both read the same `useScanRun` state.
 */
export function ScanProgress({ state }: { state: ScanRunState }) {
  const percent = phasePercent(state);
  const waiting = state.phaseTotal === null;
  return (
    <div className="flex flex-col gap-4" data-testid="scan-progress" aria-live="polite">
      <div className="flex flex-col gap-1.5">
        <div className="flex items-baseline justify-between text-sm">
          <span className="font-medium">
            {state.sync?.status === "fetching"
              ? "Fetching from GitHub…"
              : waiting
                ? "Waiting for the scan to start…"
                : (state.phaseTitle ?? "Starting…")}
          </span>
          {!waiting && state.phase !== null && (
            <span className="text-xs text-muted-foreground tabular-nums">
              Step {state.phase} of {state.phaseTotal}
            </span>
          )}
        </div>
        <Progress value={percent} aria-label="Scan progress" />
      </div>
      {state.tasks.length > 0 && (
        <ul className="flex flex-col gap-2.5">
          {state.tasks.map((task) => (
            <li key={task.name} className="flex flex-col gap-1">
              <div className="flex items-baseline justify-between gap-3 text-xs">
                <span className="font-medium">{task.name}</span>
                <span className="truncate text-muted-foreground">{task.description}</span>
              </div>
              {task.total ? (
                <Progress value={Math.round((task.completed / task.total) * 100)} aria-label={task.name} />
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
