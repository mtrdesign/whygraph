import { NetworkIcon } from "lucide-react";
import { getErrorMode } from "../../api";
import { errorInfo } from "../../lib/apiErrors";
import { hardNavigate } from "../../lib/navigation";
import { Button } from "../ui/button";

const HINT = {
  local: (
    <>
      On the machine that runs WhyGraph, <span className="font-mono">whygraph status</span> shows whether the
      portal is up and <span className="font-mono">whygraph logs</span> shows why.
    </>
  ),
  production: "Try again. If it keeps happening, tell your WhyGraph administrator.",
} as const;

/**
 * The branded page for a failure nothing below the root can render: an error the
 * router caught, or a portal whose own database did not open (degraded mode).
 * The server's text stays collapsed under "Show details".
 */
export function PortalErrorPage({ error, reset }: { error: unknown; reset?: () => void }) {
  const info = errorInfo(error);
  const detail = info.detail ?? (error instanceof Error ? error.message : typeof error === "string" ? error : null);
  const mode = getErrorMode();
  return (
    <div className="flex min-h-[70vh] items-center justify-center bg-background p-6" data-testid="portal-error">
      <div className="flex w-full max-w-md flex-col gap-4">
        <div className="flex items-center gap-2">
          <div className="flex size-[22px] items-center justify-center rounded-md bg-primary">
            <NetworkIcon className="size-3.5 text-primary-foreground" aria-hidden />
          </div>
          <span className="font-semibold tracking-tight">WhyGraph</span>
        </div>
        <h1 className="text-2xl font-semibold tracking-tight">WhyGraph hit an unexpected error</h1>
        <p className="text-sm text-muted-foreground">{HINT[mode]}</p>
        <div className="flex flex-wrap gap-2">
          {reset && <Button onClick={reset}>Try again</Button>}
          <Button variant="outline" onClick={() => void hardNavigate(window.location.href)}>
            Reload
          </Button>
        </div>
        {(detail || info.code) && (
          <details className="text-xs text-muted-foreground" data-testid="error-details">
            <summary className="cursor-pointer select-none hover:text-foreground">Show details</summary>
            <div className="mt-1 flex flex-col gap-1">
              {info.code && (
                <p>
                  Code: <span className="font-mono">{info.code}</span>
                </p>
              )}
              {detail && <pre className="overflow-auto rounded-md bg-muted p-3 whitespace-pre-wrap">{detail}</pre>}
            </div>
          </details>
        )}
      </div>
    </div>
  );
}
