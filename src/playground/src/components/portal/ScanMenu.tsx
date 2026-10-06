import { ChevronDownIcon } from "lucide-react";
import type { ProjectSummary } from "../../api";
import { can } from "../../lib/permissions";
import type { ScanBody } from "../../lib/scanActions";
import { Button } from "../ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "../ui/dropdown-menu";

export const QUICK_SCAN: ScanBody = { trigger: "manual", analyze: false };
export const FULL_SCAN: ScanBody = { trigger: "manual", analyze: true };

/**
 * The rescan button. A project admin gets a menu (Quick rescan: git + CodeGraph, no
 * LLM; Full rescan: also describes commits); a contributor, and anyone on a linked
 * project (which never spends), gets one plain "Rescan"; a viewer gets nothing.
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
  const quick = can(project, "project.scan");
  const full = can(project, "project.scan_full") && project.source !== "platform";
  if (!quick && !full) return null;
  if (!full) {
    return (
      <Button size={size} variant={variant} disabled={disabled} onClick={() => onScan(QUICK_SCAN)}>
        Rescan
      </Button>
    );
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={<Button size={size} variant={variant} disabled={disabled} />}
      >
        Rescan
        <ChevronDownIcon data-icon="inline-end" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onClick={() => onScan(QUICK_SCAN)}>Quick rescan</DropdownMenuItem>
        <DropdownMenuItem onClick={() => onScan(FULL_SCAN)}>Full rescan</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
