import { useState } from "react";
import type { ImportResult } from "../../api";
import { formatDateTime } from "../../lib/format";
import { plural } from "../../lib/plural";
import { ErrorState } from "../state/ErrorState";
import { Alert, AlertDescription } from "../ui/alert";
import { Button } from "../ui/button";
import { Checkbox } from "../ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog";

const show = (v: unknown) => (v === null || v === undefined ? "(unset)" : typeof v === "string" ? v : JSON.stringify(v));

/** The counts the bundle adds, as short lines (zero rows are left out). */
function countLines(r: ImportResult): string[] {
  const n = r.notes.inserted;
  const lines: string[] = [];
  const add = (count: number, text: string) => count > 0 && lines.push(text);
  add(n.confirmed, `${plural(n.confirmed, "confirmed note")}`);
  add(n.proposed, `${plural(n.proposed, "proposed note")}`);
  add(n.retired, `${plural(n.retired, "retired note")}`);
  add(r.purpose_runs.inserted, `${plural(r.purpose_runs.inserted, "purpose run")}`);
  add(r.rationale.inserted, `${plural(r.rationale.inserted, "rationale card")}`);
  add(r.descriptions.filled, `${plural(r.descriptions.filled, "commit or file description")}`);
  return lines;
}

function skippedLines(r: ImportResult): string[] {
  const n = r.notes;
  const out: string[] = [];
  const add = (count: number, text: string) => count > 0 && out.push(`${count} ${text}`);
  add(n.skipped_duplicate, "notes already here");
  add(n.skipped_purpose, "purpose notes left alone");
  add(n.skipped_oversize + r.rationale.skipped_oversize + r.descriptions.skipped_oversize + r.chats.skipped_oversize, "oversize rows skipped");
  add(n.skipped_denied, "notes not allowed");
  add(n.downgraded, "notes land as proposed (you may not confirm them)");
  add(n.refs_unresolved, "note references not found here");
  add(r.rationale.existing, "rationale cards already here");
  add(r.rationale.inactive_model, "rationale cards from a model this project does not use");
  add(r.descriptions.kept_target, "descriptions kept as they are here");
  add(r.descriptions.missing_commit, "descriptions for commits not found here");
  return out;
}

/**
 * The import preview (the dry run) and, after Import, its result. Everything the
 * bundle would add is listed; every confirmed note that will reach agents broadly
 * is shown verbatim (plan 42, Q5); the settings box is off by default (Q9).
 * Import re-sends the same file, so the dialog only reports the choice.
 */
export function ImportPreviewDialog({
  open,
  onOpenChange,
  preview,
  result,
  pending,
  error,
  onImport,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  preview: ImportResult;
  /** The apply's result once it ran. */
  result: ImportResult | null;
  pending: boolean;
  error: unknown;
  onImport: (applyConfig: boolean) => void;
}) {
  const [applyConfig, setApplyConfig] = useState(false);
  const r = result ?? preview;
  const cfg = preview.config;
  const configUsable = cfg.present && cfg.invalid.length === 0 && cfg.changes.length > 0;
  const counts = countLines(r);
  const skipped = skippedLines(r);
  const done = result?.applied === true;
  const partial = [r.rationale.partial && "rationale cards", r.descriptions.partial && "descriptions"].filter(Boolean);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-xl" data-testid="import-dialog">
        <DialogHeader>
          <DialogTitle>{done ? "Bundle imported" : "Import this bundle?"}</DialogTitle>
          <DialogDescription>
            From {r.source.label} ({r.source.mode}, WhyGraph {r.source.whygraph_version}
            {r.source.exported_at ? `, exported ${formatDateTime(r.source.exported_at)}` : ""}).
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3 text-sm">
          <p className="text-muted-foreground" data-testid="import-repo">
            Stated by the bundle: same repository ({r.repo.source_origin ?? "unknown origin"}
            {r.repo.target_origin && r.repo.target_origin !== r.repo.source_origin ? `, this project: ${r.repo.target_origin}` : ""}).
          </p>

          <div>
            <p className="font-medium">{done ? "Added" : "Will add"}</p>
            {counts.length > 0 ? (
              <ul className="list-disc pl-5" data-testid="import-counts">
                {counts.map((c) => (
                  <li key={c}>{c}</li>
                ))}
              </ul>
            ) : (
              <p className="text-muted-foreground" data-testid="import-counts">
                Nothing new: this project already has everything in the bundle.
              </p>
            )}
            {skipped.length > 0 && (
              <ul className="mt-1 list-disc pl-5 text-xs text-muted-foreground" data-testid="import-skipped">
                {skipped.map((c) => (
                  <li key={c}>{c}</li>
                ))}
              </ul>
            )}
          </div>

          {partial.length > 0 && (
            <p className="text-xs text-destructive" data-testid="import-partial">
              Partially imported: not all {partial.join(" and ")} landed. Import the bundle again to add the rest.
            </p>
          )}

          {r.wide_confirmed.length > 0 && (
            <div className="flex flex-col gap-2" data-testid="import-wide">
              <p className="font-medium">These confirmed notes will reach every agent session:</p>
              {r.wide_confirmed.map((w, i) => (
                <div key={i} className="rounded-lg border border-border bg-muted/40 p-2" data-testid="import-wide-note">
                  <p className="text-xs text-muted-foreground">
                    {w.kind} - {w.target ?? w.anchor}
                    {w.unresolved_area ? " (area not defined here)" : ""}
                  </p>
                  <p className="whitespace-pre-wrap break-words">{w.body}</p>
                </div>
              ))}
            </div>
          )}

          {r.chats.sessions > 0 && (
            <p data-testid="import-chats">
              {plural(r.chats.sessions, "chat")} ({plural(r.chats.messages, "message")}) will be added to <b>your</b> chats
              {r.chats.providers.length > 0 ? `, written with ${r.chats.providers.join(", ")}` : ""}.
            </p>
          )}

          {cfg.present && (
            <div className="flex flex-col gap-1.5" data-testid="import-config">
              <p className="font-medium">Settings in the bundle</p>
              {cfg.changes.length === 0 ? (
                <p className="text-muted-foreground">Same as this project's settings.</p>
              ) : (
                <ul className="flex flex-col gap-0.5 text-xs">
                  {cfg.changes.map((c) => (
                    <li key={c.key} className="break-all">
                      <code>{c.key}</code>: {show(c.from)} {"->"} {show(c.to)}
                    </li>
                  ))}
                </ul>
              )}
              {cfg.invalid.length > 0 && (
                <Alert variant="destructive" data-testid="import-config-invalid">
                  <AlertDescription>
                    These settings cannot be applied here: {cfg.invalid.join(", ")}.
                  </AlertDescription>
                </Alert>
              )}
              {!done && (
                <label className="flex cursor-pointer items-start gap-2">
                  <Checkbox
                    checked={applyConfig && configUsable}
                    onCheckedChange={setApplyConfig}
                    disabled={!configUsable || pending}
                    className="mt-0.5"
                  />
                  <span>
                    Apply these settings
                    <span className="block text-xs text-muted-foreground">
                      The bundle wins for every setting it carries. Off by default.
                    </span>
                  </span>
                </label>
              )}
              {done && (
                <p className="text-xs text-muted-foreground" data-testid="import-config-result">
                  {result.config.applied
                    ? "The settings were applied."
                    : result.config.error
                      ? `The settings were not applied: ${result.config.error}`
                      : "The settings were left as they were."}
                </p>
              )}
            </div>
          )}

          {r.rationale.inactive_model > 0 && (
            <p className="text-xs text-muted-foreground" data-testid="import-inactive">
              {plural(r.rationale.inactive_model, "rationale card")} came from a model this project does not use; they stay
              until that model is chosen.
            </p>
          )}

          {done && r.backup.written && (
            <p className="text-xs text-muted-foreground" data-testid="import-backup">
              A backup of the project's database was written{r.backup.path ? ` to ${r.backup.path}` : ""}.
            </p>
          )}
          {done && r.backup.skipped_low_disk && (
            <p className="text-xs text-muted-foreground">No backup was written: the disk is nearly full.</p>
          )}

          {!done && (
            <p className="text-xs text-muted-foreground">Chat and agents on this project may pause for a moment.</p>
          )}
        </div>

        {error != null && <ErrorState error={error} size="inline" />}

        <DialogFooter>
          {done ? (
            <Button onClick={() => onOpenChange(false)}>Done</Button>
          ) : (
            <>
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button disabled={pending} onClick={() => onImport(applyConfig && configUsable)}>
                {pending ? "Importing…" : "Import"}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
