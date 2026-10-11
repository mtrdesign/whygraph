import { Fragment } from "react";
import { Link } from "@tanstack/react-router";
import { CheckIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export type WizardStep = "source" | "setup" | "configure";

/** Which wizard a project goes through: a local folder, a production import, or a platform link. */
export type WizardMode = "local" | "production" | "linked";

const LABEL: Record<WizardStep, string> = { source: "Source", setup: "Set up", configure: "Configure" };

/**
 * The steps of each wizard (M2f-3 plan section 4.9, R2): a local folder is set
 * up (Initialize, which queues the first scan) and then configured while that
 * scan runs; a production import has nothing to set up on a server copy; a
 * platform link takes its configuration from the platform.
 */
export const WIZARD_STEPS: Record<WizardMode, WizardStep[]> = {
  local: ["source", "setup", "configure"],
  production: ["source", "configure"],
  linked: ["source", "setup"],
};

/**
 * The add wizard's stepper (§4.12.1: import is a full-page flow, not a modal).
 * With a `slug`, earlier steps link back to their own page at
 * `/p/<slug>/init?step=setup|configure`. `complete` ticks the current step too
 * (Configure, once the first scan finished: BUG-14). Below `sm` it collapses to
 * one line, "Step 2 of 3 - Set up" (PH-7).
 */
export function WizardSteps({
  current,
  mode,
  slug,
  complete = false,
}: {
  current: WizardStep;
  mode: WizardMode;
  slug?: string;
  complete?: boolean;
}) {
  const steps = WIZARD_STEPS[mode];
  const at = Math.max(0, steps.indexOf(current));
  return (
    <div data-wizard-steps>
      <p className="text-[13px] text-muted-foreground sm:hidden" data-testid="wizard-step-compact">
        Step {at + 1} of {steps.length} - <span className="font-medium text-foreground">{LABEL[steps[at]]}</span>
      </p>
      <ol aria-label="Steps" className="hidden items-center gap-2 text-[13px] sm:flex">
        {steps.map((id, i) => {
          const state = i < at || (i === at && complete) ? "done" : i === at ? "current" : "todo";
          const badge = (
            <span
              className={cn(
                "flex size-[22px] shrink-0 items-center justify-center rounded-full text-xs",
                state === "current" && "bg-primary-soft text-primary-text ring-1 ring-primary/40",
                state === "done" && "bg-primary/15 text-primary-text",
                state === "todo" && "border border-border text-muted-foreground",
              )}
            >
              {state === "done" ? <CheckIcon className="size-3" /> : i + 1}
            </span>
          );
          const content = (
            <>
              {badge}
              <span className={i === at ? "font-medium text-foreground" : "text-muted-foreground"}>{LABEL[id]}</span>
            </>
          );
          const linkable = slug && i < at && id !== "source";
          return (
            <Fragment key={id}>
              {i > 0 && <li aria-hidden="true" className="h-px min-w-4 flex-1 bg-border" />}
              <li
                aria-current={i === at ? "step" : undefined}
                data-state={state}
                className="flex items-center gap-2"
              >
                {linkable ? (
                  <Link
                    to="/p/$slug/init"
                    params={{ slug }}
                    search={{ step: id as Exclude<WizardStep, "source"> }}
                    className="flex items-center gap-2 hover:underline"
                  >
                    {content}
                  </Link>
                ) : (
                  content
                )}
              </li>
            </Fragment>
          );
        })}
      </ol>
    </div>
  );
}
