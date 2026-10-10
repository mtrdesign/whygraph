import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { AlertTriangleIcon, CheckCircle2Icon, CircleAlertIcon, InfoIcon } from "lucide-react";
import { githubApi, type ProjectDetails } from "../../api";
import { authMessage } from "../../lib/authErrors";
import type { ProjectProblem } from "../../lib/errors";
import { canAdmin, useRole } from "../../lib/identity";
import { hardNavigate } from "../../lib/navigation";
import { reconnectSearch, safeHref } from "../../lib/platformLink";
import type { HealthAction, HealthItem, HealthTone, ProjectHealth } from "../../lib/projectHealth";
import type { ScanBody } from "../../lib/scanActions";
import { cn } from "../../lib/utils";
import { Button } from "../ui/button";
import { ProblemAlert, ProjectUnavailable } from "./EdgeStates";
import { QUICK_SCAN, ScanMenu } from "./ScanMenu";

const ICON: Record<HealthTone, ReactNode> = {
  ok: <CheckCircle2Icon className="size-4 text-success" aria-hidden />,
  info: <InfoIcon className="size-4 text-info" aria-hidden />,
  warn: <AlertTriangleIcon className="size-4 text-warning" aria-hidden />,
  error: <CircleAlertIcon className="size-4 text-destructive" aria-hidden />,
};

/** Rows that keep the test ids the older notices had, so links and checks still find them. */
const TEST_ID: Partial<Record<HealthItem["id"], string>> = {
  importing: "importing-notice",
  setup: "not-initialized",
  access: "access-lost",
  behind: "stale-banner",
};

function GitHubFix({ label }: { label: string }) {
  const reconnect = useMutation({
    mutationFn: () => githubApi.authorize(true),
    onSuccess: ({ url }) => void hardNavigate(url),
    onError: (err) => toast.error(authMessage(err)),
  });
  return (
    <Button size="sm" variant="outline" disabled={reconnect.isPending} onClick={() => reconnect.mutate()}>
      {label}
    </Button>
  );
}

/**
 * The Overview's health panel (M2f-3 plan section 4.10, OVW-1 / OVW-3): every
 * item of `projectHealth` in precedence order, each with what fixes it - or one
 * "Healthy" line. The folder and symbolic-link items render their full fix
 * (`ProjectUnavailable`, `ProblemAlert`); the behind item carries the rescan menu.
 */
export function HealthPanel({
  project,
  health,
  onScan,
  scanPending,
  unsafeProblem,
  hideDescribe = false,
}: {
  project: ProjectDetails;
  health: ProjectHealth;
  onScan: (body: ScanBody) => void;
  scanPending?: boolean;
  /** The `unsafe_path` refusal, rendered as its instruction. */
  unsafeProblem?: ProjectProblem | null;
  /** The estimate card below already offers Describe now. */
  hideDescribe?: boolean;
}) {
  const role = useRole();
  const slug = project.slug;
  const link = project.link ?? null;

  const action = (a: HealthAction): ReactNode => {
    switch (a.kind) {
      case "reconnect":
        return link ? (
          <Button size="sm" render={<Link to="/link" search={reconnectSearch(link)} />}>
            {a.label}
          </Button>
        ) : null;
      case "remove":
        return (
          <Button size="sm" variant="outline" render={<Link to="/p/$slug/settings" params={{ slug }} search={{ section: "danger" }} />}>
            {a.label}
          </Button>
        );
      case "manage": {
        const href = safeHref(link?.manage_url);
        return href ? (
          <Button size="sm" variant="outline" render={<a href={href} target="_blank" rel="noreferrer" />}>
            {a.label}
          </Button>
        ) : null;
      }
      case "github":
        return canAdmin(role) ? (
          <GitHubFix label={a.label} />
        ) : (
          <span className="text-xs text-muted-foreground">An organization owner or admin can fix this.</span>
        );
      case "follow_run":
      case "open_log":
        return (
          <Button
            size="sm"
            variant={a.kind === "follow_run" ? "default" : "outline"}
            render={
              <Link
                to="/p/$slug/scans/{-$runId}"
                params={{ slug, runId: a.runId === undefined ? undefined : String(a.runId) }}
              />
            }
          >
            {a.label}
          </Button>
        );
      case "finish_setup":
        return (
          <Button size="sm" render={<Link to="/p/$slug/init" params={{ slug }} />}>
            {a.label}
          </Button>
        );
      case "usage":
        return (
          <Button size="sm" variant="outline" render={<Link to="/p/$slug/settings" params={{ slug }} search={{ section: "budgets" }} />}>
            {a.label}
          </Button>
        );
      case "add_key":
        return (
          <Button size="sm" variant="outline" render={<Link to="/p/$slug/settings" params={{ slug }} search={{ section: "models" }} />}>
            {a.label}
          </Button>
        );
      case "rescan":
        // The behind item offers the same menu as the header; "Scan now" is the first scan.
        return a.label === "Rescan" ? (
          <ScanMenu project={project} size="sm" variant="outline" onScan={onScan} disabled={scanPending} />
        ) : (
          <Button size="sm" variant="outline" disabled={scanPending} onClick={() => onScan(QUICK_SCAN)}>
            {a.label}
          </Button>
        );
      case "retry_import":
      case "retry_scan":
        return (
          <Button size="sm" disabled={scanPending} onClick={() => onScan(QUICK_SCAN)}>
            {a.label}
          </Button>
        );
      case "describe":
        return hideDescribe ? null : (
          <Button size="sm" variant="outline" disabled={scanPending} onClick={() => onScan({ trigger: "describe" })}>
            {a.label}
          </Button>
        );
    }
  };

  const row = (item: HealthItem): ReactNode => {
    if (item.id === "folder") return <ProjectUnavailable project={project} />;
    if (item.id === "unsafe_path") return unsafeProblem ? <ProblemAlert problem={unsafeProblem} /> : null;
    const actions = item.actions.map((a) => ({ a, node: action(a) })).filter((x) => x.node);
    return (
      <div
        className="flex items-start gap-3"
        data-testid={TEST_ID[item.id] ?? `health-${item.id}`}
        data-tone={item.tone}
        data-reason={item.id === "access" ? (project.access_lost_reason ?? "") : undefined}
      >
        <span className="mt-0.5 shrink-0">{ICON[item.tone]}</span>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <p className={cn("text-sm font-medium", item.tone === "error" && "text-destructive")}>{item.title}</p>
          {item.detail && <p className="text-xs break-words text-muted-foreground">{item.detail}</p>}
          {actions.length > 0 && (
            <div className="mt-1 flex flex-wrap items-center gap-2">
              {actions.map(({ a, node }) => (
                <span key={a.kind} className="contents">
                  {node}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  };

  return (
    <section
      aria-labelledby="health-title"
      data-testid="health-panel"
      data-status={health.status.key}
      className="flex flex-col gap-3 rounded-xl bg-card p-5 shadow-card"
    >
      <h2 id="health-title" className="text-sm font-semibold">
        Health
      </h2>
      {health.items.length === 0 ? (
        <div className="flex items-center gap-2 text-sm" data-testid="health-ok">
          {ICON.ok}
          <span>Healthy: nothing needs your attention.</span>
        </div>
      ) : (
        <ul className="flex flex-col gap-4">
          {health.items.map((item) => (
            <li key={item.id}>{row(item)}</li>
          ))}
        </ul>
      )}
    </section>
  );
}
