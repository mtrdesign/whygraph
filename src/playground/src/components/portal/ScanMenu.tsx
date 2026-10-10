import { useId, type ReactElement } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDownIcon } from "lucide-react";
import { projectApi, projectKey, type ProjectSummary, type ScanEstimate } from "../../api";
import { formatUsd } from "../../lib/format";
import { can } from "../../lib/permissions";
import type { ScanBody } from "../../lib/scanActions";
import { scanAvailability, type Avail } from "../../lib/scanAvailability";
import { DisabledReason } from "../state/DisabledReason";
import { Button } from "../ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "../ui/dropdown-menu";

export const QUICK_SCAN: ScanBody = { trigger: "manual", analyze: false };
export const FULL_SCAN: ScanBody = { trigger: "manual", analyze: true };

/** Which scan items the caller's role offers at all (a viewer gets none). */
function offered(project: ProjectSummary) {
  const retryImport = !!project.importing && !project.running_scan;
  return {
    quick: can(project, "project.scan"),
    // A linked project never spends, an import is retried as a quick scan.
    full: can(project, "project.scan_full") && project.source !== "platform" && !retryImport,
  };
}

/** The describe estimate, fetched while a menu that offers Full rescan is open (admins only). */
function useMenuEstimate(project: ProjectSummary, wanted: boolean): ScanEstimate | undefined {
  const usable = project.initialized && project.root_status === "ok" && !project.importing;
  return useQuery({
    queryKey: projectKey(project.slug, "scan-estimate"),
    queryFn: () => projectApi(project.slug).scanEstimate(),
    enabled: wanted && usable && !!project.last_scan_at,
    retry: false,
  }).data;
}

function fullDescription(estimate: ScanEstimate | undefined): string {
  const base = "Also describes new commits";
  if (!estimate) return `${base}, at a cost`;
  if (estimate.commits === 0) return `${base} (none waiting now)`;
  if (estimate.cost) return `${base} (~${formatUsd(estimate.cost.usd)})`;
  return `${base} (${estimate.commits.toLocaleString("en-US")} waiting)`;
}

/** One rescan item: its label, a one-line description, and why it is disabled. */
function ScanItem({
  label,
  description,
  avail,
  pending,
  onClick,
}: {
  label: string;
  description: string;
  avail: Avail;
  pending?: boolean;
  onClick: () => void;
}) {
  const id = useId();
  return (
    <DropdownMenuItem
      disabled={!avail.allowed || pending}
      onClick={onClick}
      aria-labelledby={`${id}-label`}
      aria-describedby={`${id}-desc`}
      // The item stays opaque so the reason line keeps its contrast; the label carries
      // the disabled look (muted and faded) with the menu's not-allowed cursor.
      className="flex-col items-start gap-0.5 py-1.5 data-disabled:opacity-100"
    >
      <span id={`${id}-label`} className={avail.allowed ? "font-medium" : "font-medium text-muted-foreground opacity-60"}>
        {label}
      </span>
      <span id={`${id}-desc`} className="text-xs text-muted-foreground">
        {avail.allowed ? description : avail.reason}
      </span>
    </DropdownMenuItem>
  );
}

/**
 * The rescan items for an open `DropdownMenuContent` (the Projects card's "..."
 * menu holds them beside Settings): Quick rescan and, for a project admin, Full
 * rescan, each with what it does and, when it cannot run, why
 * (`scanAvailability`). Mounted only while the menu is open, so the estimate is
 * fetched then.
 */
export function ScanMenuItems({
  project,
  onScan,
  pending,
}: {
  project: ProjectSummary;
  onScan: (body: ScanBody) => void;
  pending?: boolean;
}) {
  const show = offered(project);
  const estimate = useMenuEstimate(project, show.full);
  const avail = scanAvailability(project, { analyzeMissingKey: estimate?.missing_key ?? null });
  return (
    <>
      {show.quick && (
        <ScanItem
          label={avail.retryImport ? "Retry import" : "Quick rescan"}
          description={
            avail.retryImport ? "Clone the repository again and run its first scan" : "Git history and the code index, no LLM cost"
          }
          avail={avail.quick}
          pending={pending}
          onClick={() => onScan(QUICK_SCAN)}
        />
      )}
      {show.full && (
        <ScanItem
          label="Full rescan"
          description={fullDescription(estimate)}
          avail={avail.full}
          pending={pending}
          onClick={() => onScan(FULL_SCAN)}
        />
      )}
    </>
  );
}

/**
 * The rescan button. A project admin gets a menu (`ScanMenuItems`: Quick and Full
 * rescan, with descriptions and disabled reasons); a contributor, and anyone on
 * a linked project (which never spends), gets one plain "Rescan"; an import that
 * did not finish gets "Retry import"; a viewer gets nothing. A control that
 * cannot run says why (`DisabledReason`); `disabled` is for a request in flight.
 */
export function ScanMenu({
  project,
  onScan,
  disabled,
  size,
  variant,
}: {
  project: ProjectSummary;
  onScan: (body: ScanBody) => void;
  disabled?: boolean;
  size?: "sm";
  variant?: "default" | "outline";
}) {
  const show = offered(project);
  const avail = scanAvailability(project);
  if (!show.quick && !show.full) return null;

  const reasoned = (node: ReactElement, a: Avail) =>
    a.allowed ? node : <DisabledReason reason={a.reason}>{node}</DisabledReason>;

  if (avail.retryImport || !show.full) {
    const label = avail.retryImport ? "Retry import" : "Rescan";
    return reasoned(
      <Button size={size} variant={variant} disabled={disabled || !avail.quick.allowed} onClick={() => onScan(QUICK_SCAN)}>
        {label}
      </Button>,
      avail.quick,
    );
  }
  if (!avail.quick.allowed && !avail.full.allowed) {
    // Nothing in the menu could run: one disabled button that says why.
    return reasoned(
      <Button size={size} variant={variant} disabled>
        Rescan
        <ChevronDownIcon data-icon="inline-end" />
      </Button>,
      avail.quick,
    );
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button size={size} variant={variant} disabled={disabled} />}>
        Rescan
        <ChevronDownIcon data-icon="inline-end" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-72 max-w-[calc(100vw-2rem)]">
        <ScanMenuItems project={project} onScan={onScan} pending={disabled} />
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
