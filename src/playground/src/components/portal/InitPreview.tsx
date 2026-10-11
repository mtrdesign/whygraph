import { useId } from "react";
import { TriangleAlertIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import type { FileOutcome, FileStatus, InitResult } from "../../api";
import { selectionNotes } from "../../lib/agents";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Badge } from "../ui/badge";
import { Checkbox } from "../ui/checkbox";
import { CopyButton } from "./CopyButton";

// Soft badges only (§4.2): "Create" in the brand tint, never the solid primary.
const STATUS: Record<FileStatus, { label: string; variant: "brand" | "secondary" | "outline" | "destructive" }> = {
  write: { label: "Create", variant: "brand" },
  overwrite: { label: "Update", variant: "secondary" },
  skip: { label: "Up to date", variant: "outline" },
  refused: { label: "Paste manually", variant: "destructive" },
  needs_confirmation: { label: "Committed file", variant: "secondary" },
};

export function StatusBadge({ status }: { status: FileStatus }) {
  const s = STATUS[status];
  return (
    <Badge variant={s.variant} data-status={status}>
      {s.label}
    </Badge>
  );
}

/** A unified diff with added / removed lines tinted. */
export function DiffView({ diff }: { diff: string }) {
  return (
    <pre
      data-testid="diff"
      className="max-h-56 overflow-auto rounded-md border border-border bg-muted/40 p-2 font-mono text-xs leading-relaxed"
    >
      {diff.split("\n").map((line, i) => (
        <div
          key={i}
          className={cn(
            "whitespace-pre-wrap break-all",
            line.startsWith("+") && !line.startsWith("+++") && "bg-success/15 text-foreground",
            line.startsWith("-") && !line.startsWith("---") && "bg-destructive/15 text-foreground",
            (line.startsWith("@@") || line.startsWith("+++") || line.startsWith("---")) &&
              "text-muted-foreground",
          )}
        >
          {line || " "}
        </div>
      ))}
    </pre>
  );
}

function AgentFileRow({
  file,
  confirmed,
  onConfirmChange,
}: {
  file: FileOutcome;
  confirmed: boolean;
  onConfirmChange: (on: boolean) => void;
}) {
  const id = useId();
  return (
    <li className="flex flex-col gap-2 py-3" data-testid={`file-${file.file}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm break-all">{file.file}</span>
        <StatusBadge status={file.status} />
        {/* "Up to date" once: the badge already says what an "already up to date" reason would (SET-8). */}
        {file.reason && !(file.status === "skip" && /up to date/i.test(file.reason)) && (
          <span className="text-xs text-muted-foreground">{file.reason}</span>
        )}
      </div>

      {file.status === "refused" && (
        <Alert>
          <TriangleAlertIcon />
          <AlertTitle>WhyGraph will not touch this file</AlertTitle>
          <AlertDescription>
            <p>
              It could not be merged safely (comments or invalid content would be lost). Add this
              entry to <span className="font-mono">{file.file}</span> yourself:
            </p>
            {file.snippet && (
              <div className="mt-2 flex flex-col gap-2">
                <pre className="max-h-48 overflow-auto rounded-md bg-muted p-2 font-mono text-xs text-foreground">
                  {file.snippet}
                </pre>
                <div>
                  <CopyButton text={file.snippet} label="Copy snippet" />
                </div>
              </div>
            )}
          </AlertDescription>
        </Alert>
      )}

      {file.status === "needs_confirmation" && (
        <div className="flex flex-col gap-2">
          {file.diff && <DiffView diff={file.diff} />}
          <label htmlFor={id} className="flex cursor-pointer items-start gap-2 text-sm">
            <Checkbox id={id} checked={confirmed} onCheckedChange={onConfirmChange} className="mt-0.5" />
            <span>
              This file is committed to git. Change it as shown above.
              <span className="block text-xs text-muted-foreground">
                The entry is commit-safe: teammates get the same one.
              </span>
            </span>
          </label>
        </div>
      )}

      {file.status === "overwrite" && file.diff && (
        <details>
          <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground">
            Show changes
          </summary>
          <div className="mt-1.5">
            <DiffView diff={file.diff} />
          </div>
        </details>
      )}
    </li>
  );
}

function AssetSummary({ files }: { files: FileOutcome[] }) {
  if (files.length === 0) return null;
  const count = (s: FileStatus) => files.filter((f) => f.status === s).length;
  const parts = [
    [count("write"), "to create"],
    [count("overwrite"), "to update"],
    [count("skip"), "already there"],
  ]
    .filter(([n]) => (n as number) > 0)
    .map(([n, l]) => `${n} ${l}`);
  return (
    <details className="py-3">
      <summary className="cursor-pointer text-sm">
        Agent assets <span className="text-muted-foreground">({parts.join(", ")})</span>
      </summary>
      <ul className="mt-2 flex flex-col gap-1">
        {files.map((f) => (
          <li key={f.file} className="flex items-center gap-2 text-xs">
            <StatusBadge status={f.status} />
            <span className="font-mono text-muted-foreground">{f.file}</span>
          </li>
        ))}
      </ul>
    </details>
  );
}

/** A repository-wide change Set up always makes, whatever the agents (BUG-16). */
function SetupRow({ id, label, children }: { id: string; label: React.ReactNode; children: React.ReactNode }) {
  return (
    <li className="flex flex-col gap-1 py-3" data-testid={`setup-${id}`}>
      <span className="text-sm">{label}</span>
      <span className="text-xs text-muted-foreground">{children}</span>
    </li>
  );
}

/**
 * Screen 6's per-file preview, from a dry-run `POST init`. Every status of §4.4.1
 * has its own rendering: `write` / `overwrite` / `skip` as a badge (an overwrite
 * expands to its diff), `refused` with the entry to paste (copyable), and
 * `needs_confirmation` (a git-tracked file) with its diff and a confirm checkbox.
 * `setupRows` lists the `.gitignore` entries and the git hooks first, which the
 * dry run does not report but every Set up writes. The notes below depend on the
 * selected agents.
 */
export function InitPreview({
  result,
  selectedAgents,
  confirmed,
  onConfirmChange,
  setupRows = true,
}: {
  result: InitResult;
  selectedAgents: readonly string[];
  confirmed: ReadonlySet<string>;
  onConfirmChange: (file: string, on: boolean) => void;
  /** Show the `.gitignore` and hooks rows (the wizard; settings leaves them out). */
  setupRows?: boolean;
}) {
  const notes = selectionNotes(selectedAgents);
  const none = result.agent_files.length === 0 && result.asset_files.length === 0;
  return (
    <div className="flex flex-col gap-3" data-testid="init-preview">
      {none && (
        <p className="text-sm text-muted-foreground">
          No agent selected: only the repository's <span className="font-mono">.gitignore</span> and git
          hooks are set up.
        </p>
      )}
      {(setupRows || !none) && (
        <ul className="flex flex-col divide-y divide-border">
          {setupRows && (
            <>
              <SetupRow id="gitignore" label={<span className="font-mono">.gitignore</span>}>
                Keeps WhyGraph's local files out of git: <span className="font-mono">whygraph.toml</span>,{" "}
                <span className="font-mono">.whygraph/</span> and <span className="font-mono">.codegraph/</span>.
              </SetupRow>
              <SetupRow id="hooks" label="Git hooks">
                After a commit, merge, rebase or checkout, the portal rescans the project in the background.
                Choose which in Settings.
              </SetupRow>
            </>
          )}
          {result.agent_files.map((f) => (
            <AgentFileRow
              key={f.file}
              file={f}
              confirmed={confirmed.has(f.file)}
              onConfirmChange={(on) => onConfirmChange(f.file, on)}
            />
          ))}
          <AssetSummary files={result.asset_files} />
        </ul>
      )}
      {notes.length > 0 && (
        <ul className="flex flex-col gap-1 rounded-md bg-muted/50 p-3 text-xs text-muted-foreground">
          {notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
