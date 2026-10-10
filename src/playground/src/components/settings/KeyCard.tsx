import { useId, useState, type ReactNode } from "react";
import { useMutation } from "@tanstack/react-query";
import { CheckCircle2Icon, TriangleAlertIcon } from "lucide-react";
import type { KeyTestResponse } from "../../api";
import { errorMessage } from "../../lib/apiErrors";
import { timeAgo } from "../../lib/format";
import { keyTestText, lastUsedText } from "../../lib/settings";
import { cn } from "../../lib/utils";
import { ConfirmDialog } from "../portal/ConfirmDialog";
import { ErrorState } from "../state/ErrorState";
import { Button } from "../ui/button";
import { Input } from "../ui/input";

/**
 * One stored credential (SET-6, BUG-11, plan section 4.13): what is in effect
 * (`status`), when it was last used (configurers only - `lastUsed` absent hides
 * the line), and its actions. **Test** probes the stored key (only with
 * `onTest`, i.e. `can_test_keys`); **Replace** / **Add** opens a write-only field
 * whose save goes out at once with only this secret; **Remove** (or "Revert to the
 * organization key") confirms first. `readOnly` renders no action at all.
 */
export function KeyCard({
  label,
  testId,
  status,
  isSet,
  lastUsed,
  warning,
  note,
  readOnly,
  github = false,
  onTest,
  onSave,
  onRemove,
  removeLabel = "Remove",
  removeConfirm,
}: {
  /** "Anthropic", "GitHub token". */
  label: string;
  testId?: string;
  /** The status line: "Set ...a1b2", "Using the organization key", "No key". */
  status: string;
  /** Whether this layer holds its own key (Replace vs Add, and whether Remove shows). */
  isSet: boolean;
  /** ISO time, `null` = never; `undefined` = not for this viewer. */
  lastUsed?: string | null;
  warning?: ReactNode;
  note?: ReactNode;
  readOnly: boolean;
  github?: boolean;
  onTest?: () => Promise<KeyTestResponse>;
  onSave: (value: string) => Promise<unknown>;
  onRemove?: () => Promise<unknown>;
  removeLabel?: string;
  removeConfirm: { title: string; description: string };
}) {
  const id = useId();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const [confirmReplace, setConfirmReplace] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);

  const test = useMutation({ mutationFn: () => onTest!() });
  const save = useMutation({
    mutationFn: (v: string) => onSave(v),
    onSuccess: () => {
      setEditing(false);
      setValue("");
      setConfirmReplace(false);
      test.reset();
    },
  });
  const remove = useMutation({
    mutationFn: () => onRemove!(),
    onSuccess: () => {
      setConfirmRemove(false);
      test.reset();
    },
  });

  const submit = () => {
    if (!value.trim()) return;
    if (isSet) setConfirmReplace(true);
    else save.mutate(value.trim());
  };
  const noun = github ? "token" : "key";

  return (
    <div
      className="flex min-w-0 flex-col gap-2 rounded-lg border border-border p-3"
      data-testid={testId}
    >
      <div className="row-wrap gap-y-1.5">
        <div className="flex min-w-40 flex-1 flex-col gap-0.5">
          <span className="text-sm font-medium">{label}</span>
          <span className="text-xs text-muted-foreground" data-testid="key-status">
            {status}
            {lastUsed !== undefined && (
              <>
                {" · "}
                <span data-testid="key-last-used">{lastUsedText(lastUsed, (iso) => timeAgo(iso))}</span>
              </>
            )}
          </span>
        </div>
        {!readOnly && (
          <div className="row-wrap gap-1.5">
            {onTest && (
              <Button type="button" size="sm" variant="outline" disabled={test.isPending} onClick={() => test.mutate()}>
                {test.isPending ? "Testing…" : "Test"}
              </Button>
            )}
            {!editing && (
              <Button type="button" size="sm" variant="outline" onClick={() => setEditing(true)}>
                {isSet ? "Replace" : `Add ${noun}`}
              </Button>
            )}
            {isSet && onRemove && (
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={() => {
                  remove.reset();
                  setConfirmRemove(true);
                }}
              >
                {removeLabel}
              </Button>
            )}
          </div>
        )}
      </div>
      {warning && (
        <p className="flex items-start gap-1.5 text-xs text-warning" data-testid="key-warning">
          <TriangleAlertIcon className="mt-px size-3.5 shrink-0" />
          <span>{warning}</span>
        </p>
      )}
      {note && <p className="text-xs text-muted-foreground">{note}</p>}
      {test.isSuccess && (
        <p
          role="status"
          data-testid="key-test-result"
          className={cn("flex items-center gap-1.5 text-xs", test.data.ok ? "text-success" : "text-warning")}
        >
          {test.data.ok ? <CheckCircle2Icon className="size-3.5" /> : <TriangleAlertIcon className="size-3.5" />}
          {keyTestText(test.data.result, github)}
        </p>
      )}
      {test.isError && (
        <p role="status" data-testid="key-test-result" className="text-xs text-warning">
          {errorMessage(test.error)}
        </p>
      )}
      {editing && !readOnly && (
        <div className="row-wrap gap-2">
          <label htmlFor={`${id}-value`} className="sr-only">
            {`New ${label}${github ? "" : " key"}`}
          </label>
          <Input
            id={`${id}-value`}
            type="password"
            autoComplete="off"
            autoFocus
            className="min-w-48 flex-1"
            placeholder={isSet ? `Paste the new ${noun}` : github ? "GitHub token" : "API key"}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                submit();
              }
            }}
          />
          <Button type="button" size="sm" disabled={!value.trim() || save.isPending} onClick={submit}>
            {save.isPending ? "Saving…" : `Save ${noun}`}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => {
              setEditing(false);
              setValue("");
              save.reset();
            }}
          >
            Cancel
          </Button>
        </div>
      )}
      {save.isError && !confirmReplace && <ErrorState error={save.error} title={`Couldn't save the ${noun}`} size="inline" />}
      <ConfirmDialog
        open={confirmReplace}
        onOpenChange={setConfirmReplace}
        title={`Replace the ${label} ${noun}?`}
        description={`The ${noun} stored now stops being used at once.`}
        confirmLabel="Replace"
        pending={save.isPending}
        error={save.isError ? errorMessage(save.error) : null}
        onConfirm={() => save.mutate(value.trim())}
      />
      <ConfirmDialog
        open={confirmRemove}
        onOpenChange={setConfirmRemove}
        title={removeConfirm.title}
        description={removeConfirm.description}
        confirmLabel={removeLabel}
        pending={remove.isPending}
        error={remove.isError ? errorMessage(remove.error) : null}
        onConfirm={() => remove.mutate()}
      />
    </div>
  );
}
