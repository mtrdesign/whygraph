import { useState, type ReactNode } from "react";
import { Alert, AlertDescription } from "../ui/alert";
import { Button } from "../ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../ui/dialog";
import { Input } from "../ui/input";

/**
 * A destructive confirmation that needs `expected` typed exactly (an org's slug).
 * The typed text clears when the dialog closes; the error stays in the dialog.
 */
export function TypedConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  expected,
  confirmLabel,
  pendingLabel,
  pending,
  error,
  onConfirm,
  testId,
  errorTestId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: ReactNode;
  expected: string;
  confirmLabel: string;
  pendingLabel: string;
  pending: boolean;
  error: string | null;
  onConfirm: () => void;
  testId?: string;
  errorTestId?: string;
}) {
  const [typed, setTyped] = useState("");
  const change = (o: boolean) => {
    if (!o) setTyped("");
    onOpenChange(o);
  };
  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogContent data-testid={testId}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-1.5">
          <label htmlFor="confirm-typed" className="text-sm">
            Type <span className="font-mono font-medium">{expected}</span> to confirm
          </label>
          <Input id="confirm-typed" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
        </div>
        {error && (
          <Alert variant="destructive" data-testid={errorTestId}>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => change(false)}>
            Cancel
          </Button>
          <Button variant="destructive" disabled={typed !== expected || pending} onClick={onConfirm}>
            {pending ? pendingLabel : confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
