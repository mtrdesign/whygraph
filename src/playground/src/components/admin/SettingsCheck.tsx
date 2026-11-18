import { useQuery } from "@tanstack/react-query";
import { adminApi } from "../../api";
import { ErrorState } from "../state/ErrorState";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Skeleton } from "../ui/skeleton";

/** The base-URL self-check (`GET /api/admin/settings`), read-only. */
export function SettingsCheck() {
  const settings = useQuery({ queryKey: ["@admin", "settings"], queryFn: adminApi.settings });
  const checks = settings.data?.base_check ?? null;
  return (
    <section
      className="flex flex-col gap-2 rounded-xl border border-border bg-card p-5 shadow-card"
      data-testid="settings-check"
    >
      <h2 className="text-sm font-semibold">Settings check</h2>
      {settings.data && (
        <p className="break-all font-mono text-[13px] text-muted-foreground">{settings.data.base_url}</p>
      )}
      {settings.isLoading && <Skeleton className="h-10" />}
      {settings.isError && (
        <ErrorState error={settings.error} title="Couldn't load the settings check" onRetry={() => void settings.refetch()} />
      )}
      {settings.data && checks === null && (
        <p className="text-sm text-muted-foreground">The base URL check has not run yet.</p>
      )}
      {settings.data && checks !== null && checks.length === 0 && (
        <Alert variant="success" data-testid="base-check-ok">
          <AlertDescription>The base URL check found no problems.</AlertDescription>
        </Alert>
      )}
      {settings.data && checks !== null && checks.length > 0 && (
        <Alert variant="destructive" data-testid="base-check">
          <AlertTitle>The base URL check found problems</AlertTitle>
          <AlertDescription>
            <ul className="list-disc pl-4">
              {checks.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}
    </section>
  );
}
