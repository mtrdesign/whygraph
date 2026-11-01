import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { projectApi, projectKey, type ImportResult } from "../../api";
import { ErrorState } from "../state/ErrorState";
import { Button } from "../ui/button";
import { ImportPreviewDialog } from "./ImportPreviewDialog";

/**
 * The import flow, shared by the Settings section and the wizard card: choose a
 * file, dry run, preview dialog, Import re-sends the same `File` (nothing is read
 * in the browser). Everything the import can change is refetched when it applies.
 */
export function TransferImport({
  slug,
  disabled = false,
  label = "Choose a bundle…",
  onApplied,
}: {
  slug: string;
  disabled?: boolean;
  label?: string;
  onApplied?: (result: ImportResult) => void;
}) {
  const queryClient = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<ImportResult | null>(null);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [open, setOpen] = useState(false);

  const dryRun = useMutation({
    mutationFn: (f: File) => projectApi(slug).transfer.import(f, { dryRun: true, applyConfig: false }),
    onSuccess: (r) => {
      setPreview(r);
      setResult(null);
      setOpen(true);
    },
  });
  const apply = useMutation({
    mutationFn: (applyConfig: boolean) => projectApi(slug).transfer.import(file!, { dryRun: false, applyConfig }),
    onSuccess: (r) => {
      setResult(r);
      // Notes, the purpose card, chats, cards and the config all moved.
      void queryClient.invalidateQueries({ queryKey: [slug] });
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "transfer") });
      onApplied?.(r);
    },
  });

  const choose = (f: File | undefined) => {
    if (input.current) input.current.value = "";
    if (!f) return;
    setFile(f);
    apply.reset();
    dryRun.mutate(f);
  };

  return (
    <div className="flex flex-col gap-2">
      <input
        ref={input}
        type="file"
        accept=".zip,application/zip"
        className="hidden"
        data-testid="import-file"
        aria-label="Bundle file"
        onChange={(e) => choose(e.target.files?.[0])}
      />
      <div>
        <Button variant="outline" disabled={disabled || dryRun.isPending} onClick={() => input.current?.click()}>
          {dryRun.isPending ? "Checking the bundle…" : label}
        </Button>
      </div>
      {dryRun.isError && <ErrorState error={dryRun.error} size="inline" />}
      {preview && (
        <ImportPreviewDialog
          open={open}
          onOpenChange={setOpen}
          preview={preview}
          result={result}
          pending={apply.isPending}
          error={apply.error}
          onImport={(applyConfig) => apply.mutate(applyConfig)}
        />
      )}
    </div>
  );
}
