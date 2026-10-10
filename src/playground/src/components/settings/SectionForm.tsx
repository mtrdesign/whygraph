import { useEffect, useState, type ReactNode } from "react";
import { CheckIcon } from "lucide-react";
import { cn } from "../../lib/utils";
import { ErrorState } from "../state/ErrorState";
import { Button } from "../ui/button";
import { useSettingsDirty } from "./SettingsLayout";

/**
 * A settings section's staged fields (SET-2, plan section 0.3 #21, R3): edits
 * wait for the footer's **Save** (or **Discard**), both disabled until something
 * changed; a successful save says "Saved" in place, a failure is worded by the
 * error registry. While dirty, leaving the page asks first (`SettingsLayout`).
 *
 * `onSave` resolves when the change is stored; resolving `false` means the
 * fields did not validate (nothing was sent, no "Saved"). `readOnly` disables
 * every field (`<fieldset disabled>`) and drops the footer.
 */
export function SectionForm({
  name,
  dirty,
  onSave,
  onDiscard,
  readOnly = false,
  saveDisabled = false,
  saveLabel = "Save",
  errorTitle = "Couldn't save",
  testId,
  className,
  children,
}: {
  /** The section's name, for the leave guard ("Your changes to General ..."). */
  name: string;
  dirty: boolean;
  onSave: () => Promise<unknown> | unknown;
  onDiscard: () => void;
  readOnly?: boolean;
  /** Keep Save off even while dirty (a preview still loading, a confirm box unticked). */
  saveDisabled?: boolean;
  saveLabel?: string;
  errorTitle?: string;
  testId?: string;
  className?: string;
  children: ReactNode;
}) {
  const [pending, setPending] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useSettingsDirty(`form:${name}`, name, dirty && !readOnly);

  // A new edit retires the last "Saved".
  useEffect(() => {
    if (dirty) setSaved(false);
  }, [dirty]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (readOnly || !dirty || pending || saveDisabled) return;
    setPending(true);
    setError(null);
    try {
      const result = await onSave();
      if (result !== false) setSaved(true);
    } catch (err) {
      setError(err);
    } finally {
      setPending(false);
    }
  };

  return (
    <form onSubmit={submit} noValidate className={cn("flex min-w-0 flex-col gap-4", className)} data-testid={testId}>
      <fieldset disabled={readOnly} className="flex min-w-0 flex-col gap-4">
        {children}
      </fieldset>
      {error !== null && <ErrorState error={error} title={errorTitle} />}
      {!readOnly && (
        <div className="row-wrap justify-end gap-2 border-t border-border pt-3">
          {saved && !dirty && (
            <span role="status" className="mr-auto flex items-center gap-1 text-xs text-success" data-testid="section-saved">
              <CheckIcon className="size-3.5" />
              Saved
            </span>
          )}
          <Button
            type="button"
            variant="outline"
            disabled={!dirty || pending}
            onClick={() => {
              setError(null);
              onDiscard();
            }}
          >
            Discard
          </Button>
          <Button type="submit" disabled={!dirty || pending || saveDisabled}>
            {pending ? "Saving…" : saveLabel}
          </Button>
        </div>
      )}
    </form>
  );
}
