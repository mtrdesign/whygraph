import { TriangleAlertIcon } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Button } from "../ui/button";
import { CommandBlock } from "../layout/CommandBlock";

/**
 * Screen 3's alert for a folder the portal cannot see. The command comes from the
 * backend (`check-path`'s `command`, already shell-quoted), so the UI never builds
 * one. The portal runs in Docker and only sees folders shared when it started;
 * after the command runs the portal restarts, and "Check again" re-runs the check.
 */
export function NotSharedAlert({
  command,
  onCheckAgain,
  checking = false,
}: {
  command: string | null;
  onCheckAgain: () => void;
  checking?: boolean;
}) {
  return (
    <Alert variant="warning" data-testid="not-shared-alert">
      <TriangleAlertIcon />
      <AlertTitle>This folder isn't shared with the portal</AlertTitle>
      <AlertDescription>
        <p>
          The portal runs in Docker and can only see folders that were shared when it started.
          Share the folder, then check again. The portal restarts in a few seconds and keeps your
          projects.
        </p>
        {/* The command wraps instead of widening the page, and what shows is what Copy copies (PH-4). */}
        {command && <CommandBlock command={command} className="mt-2 text-foreground" />}
        <div className="mt-2">
          <Button type="button" size="sm" variant="outline" onClick={onCheckAgain} disabled={checking}>
            {checking ? "Checking…" : "Check again"}
          </Button>
        </div>
      </AlertDescription>
    </Alert>
  );
}
