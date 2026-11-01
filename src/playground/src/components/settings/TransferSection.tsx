import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { projectApi, projectKey, type TransferImportRow } from "../../api";
import { saveBlob } from "../../lib/download";
import { formatDateTime } from "../../lib/format";
import { errorMessage } from "../../lib/apiErrors";
import { plural } from "../../lib/plural";
import { ConfirmDialog } from "../portal/ConfirmDialog";
import { ErrorState } from "../state/ErrorState";
import { Button } from "../ui/button";
import { Checkbox } from "../ui/checkbox";
import { SettingsSection } from "./SettingsLayout";
import { TransferImport } from "./TransferImport";

/** One line for an import's stored counts; tolerant of missing blocks (older rows). */
export function importSummary(c: TransferImportRow["counts"]): string {
  const inserted = c.notes?.inserted;
  const notes = (inserted?.proposed ?? 0) + (inserted?.confirmed ?? 0) + (inserted?.retired ?? 0);
  const parts = [
    plural(notes, "note"),
    plural(c.rationale?.inserted ?? 0, "rationale card"),
    plural(c.descriptions?.filled ?? 0, "description"),
    plural(c.chats?.sessions ?? 0, "chat"),
  ];
  const partial = c.rationale?.partial || c.descriptions?.partial;
  return parts.join(", ") + (partial ? " (partial)" : "");
}

/**
 * "Export and import" (plan 42): a bundle of this project's notes, purpose card,
 * descriptions, rationale cards, settings and (opt-in) your own chats. The caller
 * mounts it only for a project admin on a local or GitHub project.
 */
export function TransferSection({ slug, importing = false }: { slug: string; importing?: boolean }) {
  const queryClient = useQueryClient();
  const status = useQuery({
    queryKey: projectKey(slug, "transfer"),
    queryFn: () => projectApi(slug).transfer.status(),
  });
  const [chats, setChats] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);

  const exportBundle = useMutation({
    mutationFn: () => projectApi(slug).transfer.export(chats),
    onSuccess: ({ blob, filename }) => saveBlob(blob, filename),
  });
  const remove = useMutation({
    mutationFn: () => projectApi(slug).transfer.removeImportedRationale(),
    onSuccess: ({ removed }) => {
      setConfirming(false);
      setRemoveError(null);
      toast.success(`Removed ${plural(removed, "imported rationale card")}`);
      void queryClient.invalidateQueries({ queryKey: [slug] });
    },
    onError: (err) => setRemoveError(errorMessage(err)),
  });

  const data = status.data;
  const needsScan = data ? !data.scanned : false;
  const blocked = importing || needsScan || !data;
  const reason = importing ? "The project is still being imported." : needsScan ? "Run the first scan first." : null;

  return (
    <SettingsSection
      id="transfer"
      title="Export and import"
      testId="transfer-section"
      description="Move this project's notes, purpose, descriptions, rationale cards and settings to another portal, or back. The bundle is a .zip file; it never holds git history, code or keys."
    >
      {status.isError && <ErrorState error={status.error} onRetry={() => void status.refetch()} />}
      {reason && (
        <p className="text-sm text-muted-foreground" data-testid="transfer-blocked">
          {reason}
        </p>
      )}

      <div className="flex flex-col gap-2" data-testid="transfer-export">
        <h3 className="text-sm font-medium">Export</h3>
        <label className="flex cursor-pointer items-start gap-2 text-sm">
          <Checkbox checked={chats} onCheckedChange={setChats} className="mt-0.5" />
          <span>
            Include my chats
            <span className="block text-xs text-muted-foreground">Only your own conversations. Off by default.</span>
          </span>
        </label>
        <div>
          <Button variant="outline" disabled={blocked || exportBundle.isPending} onClick={() => exportBundle.mutate()}>
            {exportBundle.isPending ? "Preparing…" : "Export bundle"}
          </Button>
        </div>
        {exportBundle.isError && <ErrorState error={exportBundle.error} size="inline" />}
      </div>

      <div className="flex flex-col gap-2" data-testid="transfer-import">
        <h3 className="text-sm font-medium">Import</h3>
        <p className="text-xs text-muted-foreground">
          You see what the bundle would add before anything is written. Bundles must come from this same repository.
        </p>
        <TransferImport slug={slug} disabled={blocked} />
      </div>

      {data && data.imported_rationale > 0 && (
        <div className="flex flex-col gap-2" data-testid="transfer-remove">
          <h3 className="text-sm font-medium">Imported rationale cards</h3>
          <div>
            <Button variant="outline" className="text-destructive" onClick={() => setConfirming(true)}>
              Remove imported rationale cards ({data.imported_rationale})
            </Button>
          </div>
        </div>
      )}

      {data && data.imports.length > 0 && (
        <div className="flex flex-col gap-1" data-testid="transfer-history">
          <h3 className="text-sm font-medium">Recent imports</h3>
          <ul className="flex flex-col gap-1 text-xs text-muted-foreground">
            {data.imports.map((i) => (
              <li key={i.id}>
                {formatDateTime(i.imported_at)} - {i.source_label} ({i.source_mode}, WhyGraph {i.whygraph_version})
                {i.imported_by_name ? ` by ${i.imported_by_name}` : ""}: {importSummary(i.counts)}
              </li>
            ))}
          </ul>
        </div>
      )}

      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title="Remove imported rationale cards?"
        description="Rationale cards that came from a bundle are deleted. They are written again the next time something asks for them, using this project's model."
        confirmLabel="Remove"
        pending={remove.isPending}
        error={removeError}
        onConfirm={() => remove.mutate()}
      />
    </SettingsSection>
  );
}
