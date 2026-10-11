import { useEffect, useRef } from "react";
import { Link } from "@tanstack/react-router";
import { CheckIcon, CircleIcon, LoaderCircleIcon, MinusIcon, XIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { progressModel, type ProgressStep } from "../../lib/scanProgress";
import type { ScanRunState } from "../../lib/scanRun";
import { announce } from "../shell/LiveRegion";
import { Progress as ProgressPrimitive } from "@base-ui/react/progress";
import { ProgressIndicator, ProgressTrack } from "../ui/progress";

/** At most one status announcement per this many milliseconds. */
const ANNOUNCE_MS = 5000;

const ICON: Record<ProgressStep["state"], React.ReactNode> = {
  done: <CheckIcon className="size-3.5 text-success" />,
  running: <LoaderCircleIcon className="size-3.5 animate-spin text-primary-text" />,
  pending: <CircleIcon className="size-3.5 text-muted-foreground" />,
  failed: <XIcon className="size-3.5 text-destructive" />,
  skipped: <MinusIcon className="size-3.5 text-muted-foreground" />,
};

const STATE_TEXT: Record<ProgressStep["state"], string> = {
  done: "done",
  running: "in progress",
  pending: "waiting",
  failed: "failed",
  skipped: "skipped",
};

/** Say the status line through the live region, at most every five seconds (the latest wins). */
function useThrottledAnnounce(text: string) {
  const last = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    const wait = last.current + ANNOUNCE_MS - Date.now();
    if (timer.current) clearTimeout(timer.current);
    const say = () => {
      last.current = Date.now();
      timer.current = null;
      announce(text);
    };
    if (wait <= 0) say();
    else timer.current = setTimeout(say, wait);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [text]);
}

/**
 * One scan run as the wizard shows it (M2f-3 plan section 4.9): one overall bar
 * (indeterminate until a total is known), one status line, a checklist of the
 * run's steps with counts as text, and **Show details** to the run page, where
 * the per-task bars stay. No raw crawler names.
 */
export function ScanProgress({
  slug,
  runId,
  state,
  fullName,
}: {
  slug: string;
  runId: number;
  state: ScanRunState;
  /** The repository a production import clones, until the stream names it. */
  fullName?: string;
}) {
  const model = progressModel(state, { fullName });
  useThrottledAnnounce(model.status);
  return (
    <div className="flex flex-col gap-3" data-testid="scan-progress">
      <div className="flex flex-col gap-1.5">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 text-sm">
          <span className="min-w-0 font-medium break-words" data-testid="scan-status">
            {model.status}
          </span>
          {model.percent !== null && (
            <span className="text-xs text-muted-foreground tabular-nums">{model.percent}%</span>
          )}
        </div>
        <ProgressPrimitive.Root value={model.percent} aria-label="Scan progress" data-testid="scan-bar">
          <ProgressTrack>
            {/* No total yet: a sliding third of the track says "working" without a number. */}
            <ProgressIndicator className="data-[indeterminate]:w-1/3 data-[indeterminate]:animate-pulse" />
          </ProgressTrack>
        </ProgressPrimitive.Root>
      </div>
      {model.steps.length > 0 && (
        <ul className="flex flex-col gap-1.5" aria-label="Scan steps" data-testid="scan-steps">
          {model.steps.map((step) => (
            <li key={step.label} className="row-wrap items-center gap-x-2 text-[13px]" data-state={step.state}>
              <span className="flex size-4 shrink-0 items-center justify-center" aria-hidden>
                {ICON[step.state]}
              </span>
              <span className={cn(step.state === "pending" || step.state === "skipped" ? "text-muted-foreground" : "")}>
                {step.label}
                <span className="sr-only">, {STATE_TEXT[step.state]}</span>
              </span>
              {step.detail && <span className="text-xs text-muted-foreground tabular-nums">{step.detail}</span>}
            </li>
          ))}
        </ul>
      )}
      <div>
        <Link
          to="/p/$slug/scans/{-$runId}"
          params={{ slug, runId: String(runId) }}
          className="text-[13px] text-primary-text hover:underline"
        >
          Show details
        </Link>
      </div>
    </div>
  );
}
