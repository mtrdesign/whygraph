import { TriangleAlertIcon } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Button } from "../ui/button";
import { CommandBlock } from "../layout/CommandBlock";

/**
 * Before Connect: where the checkout will come from, or why linking cannot finish yet.
 * A link in progress lives in memory, and sharing a folder restarts the portal, so the
 * folder must be shared before the platform mints a token (plan rev 4, I13).
 */
export function CheckoutHint({
  shared,
  onCheckAgain,
  checking,
}: {
  shared: string[] | undefined;
  onCheckAgain: () => void;
  checking: boolean;
}) {
  if (shared === undefined) return null; // portal state still loading
  if (shared.length === 0) {
    return (
      <Alert variant="warning" data-testid="no-shared-folders" id="no-shared-folders">
        <TriangleAlertIcon />
        <AlertTitle>Share a folder first</AlertTitle>
        <AlertDescription>
          <p>
            The portal links a checkout inside a folder it can see, and no folder is shared yet. Share the
            folder that holds your repositories (replace ~/Work), then check again. The portal restarts in a
            few seconds.
          </p>
          <CommandBlock command="whygraph up --add-folder ~/Work" className="mt-2 text-foreground" />
          <div className="mt-2">
            <Button type="button" size="sm" variant="outline" onClick={onCheckAgain} disabled={checking}>
              {checking ? "Checking…" : "Check again"}
            </Button>
          </div>
        </AlertDescription>
      </Alert>
    );
  }
  return (
    <div className="flex flex-col gap-1 text-xs text-muted-foreground" data-testid="checkout-hint">
      <p>
        Next you pick a clone of the project's repository in{" "}
        {shared.length === 1 ? <span className="break-all font-mono">{shared[0]}</span> : "one of your shared folders"}.
        Clone it there first if you have not.
      </p>
      <p>
        Is your clone somewhere else? Share that folder first with{" "}
        <span className="font-mono">whygraph up --add-folder &lt;folder&gt;</span>; the portal restarts, so do it
        before you connect.
      </p>
    </div>
  );
}
