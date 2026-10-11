import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { AlertTriangleIcon, InfoIcon, RotateCwIcon } from "lucide-react";
import { errorInfo, type ErrorContext, type ErrorInfo } from "../../lib/apiErrors";
import { cn } from "../../lib/utils";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Button } from "../ui/button";
import { InPage } from "../layout/PageContainer";

const VARIANT: Record<ErrorInfo["tone"], "destructive" | "warning" | "info"> = {
  error: "destructive",
  warn: "warning",
  info: "info",
};

/** The registry's code and the server's own words, collapsed. */
function Details({ info }: { info: ErrorInfo }) {
  if (!info.detail && !info.code) return null;
  return (
    <details className="mt-2 text-xs text-muted-foreground" data-testid="error-details">
      <summary className="cursor-pointer select-none hover:text-foreground">Show details</summary>
      <div className="mt-1 flex flex-col gap-1">
        {info.code && (
          <p>
            Code: <span className="font-mono">{info.code}</span>
          </p>
        )}
        {info.detail && <p className="break-words whitespace-pre-wrap">{info.detail}</p>}
      </div>
    </details>
  );
}

/** The registry's next step (a route or an address); a retry is the Retry button's. */
function RegistryAction({ info }: { info: ErrorInfo }) {
  const action = info.action;
  if (!action || action.retry) return null;
  if (action.to) {
    return (
      <Button size="sm" variant="outline" render={<Link to={action.to as "/"} />}>
        {action.label}
      </Button>
    );
  }
  if (action.href) {
    return (
      <Button size="sm" variant="outline" render={<a href={action.href} />}>
        {action.label}
      </Button>
    );
  }
  return null;
}

/**
 * A failed load or action, worded by the error registry: its title and sentence,
 * **Retry** when `onRetry` is given, the registry's next step, and a "Show
 * details" disclosure with the code and the server's own text. `size` picks a
 * page block, a section panel or one inline line.
 */
export function ErrorState({
  error,
  title,
  onRetry,
  actions,
  size = "section",
  context,
  className,
}: {
  error: unknown;
  /** Overrides the registry's title ("Couldn't load scans"). */
  title?: string;
  onRetry?: () => void;
  /** Extra buttons beside Retry. */
  actions?: ReactNode;
  size?: "page" | "section" | "inline";
  context?: ErrorContext;
  className?: string;
}) {
  const info = errorInfo(error, context);
  const retry = onRetry && (
    <Button size="sm" variant="outline" onClick={onRetry}>
      <RotateCwIcon data-icon="inline-start" />
      Retry
    </Button>
  );

  if (size === "inline") {
    return (
      <div className={cn("flex flex-wrap items-center gap-2 text-sm", className)} role="alert" data-testid="error-state">
        <span className={info.tone === "error" ? "text-destructive" : "text-muted-foreground"}>{info.message}</span>
        {onRetry && (
          <button type="button" onClick={onRetry} className="text-primary-text underline-offset-4 hover:underline">
            Retry
          </button>
        )}
      </div>
    );
  }

  const panel = (
    <Alert variant={VARIANT[info.tone]} data-testid="error-state" data-code={info.code ?? undefined}>
      {info.tone === "info" ? <InfoIcon /> : <AlertTriangleIcon />}
      <AlertTitle>{title ?? info.title}</AlertTitle>
      <AlertDescription>
        <p>{info.message}</p>
        {(retry || actions || (info.action && !info.action.retry)) && (
          <div className="mt-2 flex flex-wrap gap-2">
            {retry}
            <RegistryAction info={info} />
            {actions}
          </div>
        )}
        <Details info={info} />
      </AlertDescription>
    </Alert>
  );

  // A page-level failure sits where the page would: inside the page's own
  // container (one is added only when nothing holds it yet), never a second box.
  if (size === "page") return <InPage>{className ? <div className={className}>{panel}</div> : panel}</InPage>;
  return className ? <div className={className}>{panel}</div> : panel;
}
