import { Fragment } from "react";
import { Link } from "@tanstack/react-router";
import { CheckIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export type WizardStep = "source" | "configure" | "initialize" | "scan";

const STEPS: { id: WizardStep; label: string }[] = [
  { id: "source", label: "Source" },
  { id: "configure", label: "Configure" },
  { id: "initialize", label: "Initialize" },
  { id: "scan", label: "First scan" },
];

/**
 * The add wizard's stepper (§4.12.1: import is a full-page flow, not a modal).
 * With a `slug`, earlier steps link back to their own page - each step is also
 * reachable on its own at `/p/<slug>/init?step=...`. In production there is no
 * Initialize step: the import already did it, and there are no agent files or
 * hooks to write on a server copy.
 */
export function WizardSteps({
  current,
  slug,
  production = false,
  linked = false,
}: {
  current: WizardStep;
  slug?: string;
  production?: boolean;
  /** A linked project (M2e): its config lives on the platform, so there is no Configure step. */
  linked?: boolean;
}) {
  const steps = STEPS.filter(
    (s) => !(production && s.id === "initialize") && !(linked && s.id === "configure"),
  );
  const at = steps.findIndex((s) => s.id === current);
  return (
    <ol aria-label="Steps" className="flex items-center gap-2 text-[13px]">
      {steps.map((step, i) => {
        const state = i < at ? "done" : i === at ? "current" : "todo";
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
            <span className={state === "current" ? "font-medium text-foreground" : "text-muted-foreground"}>
              {step.label}
            </span>
          </>
        );
        const linkable = slug && state === "done" && step.id !== "source";
        return (
          <Fragment key={step.id}>
            {i > 0 && <li aria-hidden="true" className="h-px min-w-4 flex-1 bg-border" />}
            <li aria-current={state === "current" ? "step" : undefined} className="flex items-center gap-2">
              {linkable ? (
                <Link
                  to="/p/$slug/init"
                  params={{ slug }}
                  search={{ step: step.id as "configure" | "initialize" | "scan" }}
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
  );
}
